"""Tests unitarios de lógica core (sin requerir dependencias externas en el host)."""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from app.config import SettingsStore, normalize_remote_path, resolve_safe_path
from app.db import QueueDB
from app.mover_worker import file_signature, process_mover_item
from app.switch_utils import detect_switch_type, scan_switch_ftp, switch_sort_key
from app.worker_manager import WorkerManager


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="ga_test_")
        self.cfg_dir = os.path.join(self.tmp, "config")
        self.data_root = os.path.join(self.tmp, "host")
        os.makedirs(self.cfg_dir, exist_ok=True)
        os.makedirs(self.data_root, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_safe_paths_and_remote_normalization(self) -> None:
        sub = os.path.join(self.data_root, "descargas")
        os.makedirs(sub, exist_ok=True)
        self.assertEqual(resolve_safe_path(sub, self.data_root), os.path.normpath(sub))
        with self.assertRaises(ValueError):
            resolve_safe_path("/etc/passwd", self.data_root)
        with self.assertRaises(ValueError):
            resolve_safe_path(os.path.join(self.data_root, "..", "escape"), self.data_root)

        self.assertEqual(normalize_remote_path("install//juegos/../nsp"), "/install/nsp")
        self.assertEqual(normalize_remote_path(""), "/")

    def test_settings_hides_password_and_persists(self) -> None:
        store = SettingsStore(config_dir=self.cfg_dir, data_root=self.data_root)
        store.update({"ftp_host": "192.168.1.50", "ftp_password": "secret", "mover_limit_kbps": 1200})
        pub = store.get_public()
        self.assertEqual(pub["ftp_password"], "")
        self.assertTrue(pub["ftp_has_password"])
        self.assertEqual(store.get()["ftp_password"], "secret")
        self.assertEqual(store.get()["mover_limit_kbps"], 1200)

    def test_switch_badges_and_sorting(self) -> None:
        self.assertEqual(detect_switch_type("Zelda [BASE].nsp"), "BASE")
        self.assertEqual(detect_switch_type("Zelda [UPD][v65536].nsp"), "UPD")
        self.assertEqual(detect_switch_type("Zelda [DLC].nsp"), "DLC")
        self.assertEqual(detect_switch_type("dbi.nro"), "NRO")
        self.assertEqual(detect_switch_type("readme.txt"), "")

        items = [
            {"id": 1, "status": "PEND", "source_path": "/a/Zelda [DLC].nsp", "switch_type": "DLC"},
            {"id": 2, "status": "PEND", "source_path": "/a/Zelda [UPD].nsp", "switch_type": "UPD"},
            {"id": 3, "status": "PEND", "source_path": "/a/Zelda.nsp", "switch_type": "BASE"},
        ]
        ordered = [it["id"] for it in sorted(items, key=switch_sort_key)]
        self.assertEqual(ordered, [3, 2, 1])

    def test_db_queue_operations_and_recovery(self) -> None:
        db_path = os.path.join(self.cfg_dir, "queue.db")
        db = QueueDB(db_path=db_path)
        added = db.add_items(
            [
                {
                    "kind": "mover",
                    "operation": "copiar",
                    "source_path": "/host/a.bin",
                    "dest_path": "/host/dst/a.bin",
                    "size_bytes": 10,
                },
                {
                    "kind": "mover",
                    "operation": "copiar",
                    "source_path": "/host/b.bin",
                    "dest_path": "/host/dst/b.bin",
                    "size_bytes": 20,
                },
            ]
        )
        self.assertEqual(len(added), 2)
        # Duplicado de destino activo se omite
        dup = db.add_items(
            [
                {
                    "kind": "mover",
                    "operation": "copiar",
                    "source_path": "/host/a2.bin",
                    "dest_path": "/host/dst/a.bin",
                    "size_bytes": 10,
                }
            ]
        )
        self.assertEqual(len(dup), 0)

        db.move_position(added[1]["id"], "top")
        items = db.list_items("mover")
        self.assertEqual(items[0]["id"], added[1]["id"])

        # Simular ítem que quedó en PROC tras corte -> al reabrir pasa a REAN
        db.update_item(added[0]["id"], status="PROC")
        db.close()
        db2 = QueueDB(db_path=db_path)
        recovered = db2.get_item(added[0]["id"])
        self.assertEqual(recovered["status"], "REAN")
        self.assertEqual(recovered["resume"], 1)
        db2.close()

    def test_mover_copy_with_rsync_and_move_cleanup(self) -> None:
        src_dir = os.path.join(self.data_root, "origen", "serie")
        dst_dir = os.path.join(self.data_root, "destino")
        os.makedirs(src_dir, exist_ok=True)
        os.makedirs(dst_dir, exist_ok=True)

        src_file = os.path.join(src_dir, "cap1.mkv")
        with open(src_file, "wb") as fh:
            fh.write(b"contenido-de-prueba-" * 1024)

        dst_file = os.path.join(dst_dir, "cap1.mkv")
        db = QueueDB(os.path.join(self.cfg_dir, "queue.db"))
        store = SettingsStore(config_dir=self.cfg_dir, data_root=self.data_root)
        store.update({"mover_verify": "checksum", "mover_conflict": "renombrar"})

        # Forzar ruta rsync para probar progreso, verificación SHA-256 y limpieza
        os.environ["MOVER_FORZAR_RSYNC"] = "1"
        try:
            item = db.add_items(
                [
                    {
                        "kind": "mover",
                        "operation": "mover",
                        "source_path": src_file,
                        "dest_path": dst_file,
                        "size_bytes": os.path.getsize(src_file),
                        "source_sig": file_signature(src_file),
                        "cleanup_dir": os.path.join(self.data_root, "origen"),
                    }
                ]
            )[0]
            res = process_mover_item(
                item=item,
                settings=store.get(),
                progress_cb=lambda _p: None,
                stop_event=threading.Event(),
                log_cb=db.add_log,
                has_pending_cleanup_cb=lambda _d: False,
            )
            self.assertEqual(res["status"], "OK")
            self.assertTrue(os.path.isfile(dst_file))
            self.assertFalse(os.path.exists(src_file))
            # La carpeta vacía 'origen/serie' y 'origen' deben haberse limpiado
            self.assertFalse(os.path.exists(os.path.join(self.data_root, "origen")))
        finally:
            os.environ.pop("MOVER_FORZAR_RSYNC", None)
            db.close()

    def test_worker_manager_executes_mover_queue(self) -> None:
        src = os.path.join(self.data_root, "foto.jpg")
        dst = os.path.join(self.data_root, "copias", "foto.jpg")
        Path(src).write_bytes(b"jpegdata")

        db = QueueDB(os.path.join(self.cfg_dir, "queue.db"))
        store = SettingsStore(config_dir=self.cfg_dir, data_root=self.data_root)
        mgr = WorkerManager(db, store)
        db.add_items(
            [
                {
                    "kind": "mover",
                    "operation": "copiar",
                    "source_path": src,
                    "dest_path": dst,
                    "size_bytes": 8,
                    "source_sig": file_signature(src),
                }
            ]
        )
        self.assertTrue(mgr.start("mover"))
        # Esperar brevemente a que el hilo termine
        runner = mgr.runners["mover"]
        if runner._thread:
            runner._thread.join(timeout=5)
        self.assertTrue(os.path.isfile(dst))
        self.assertEqual(db.list_items("mover")[0]["status"], "OK")
        db.close()

    def test_scan_switch_ftp_reads_banner(self) -> None:
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(2)
        stop = False

        def accept_loop() -> None:
            srv.settimeout(0.2)
            while not stop:
                try:
                    c, _ = srv.accept()
                    c.sendall(b"220 DBI FTP Ready\r\n")
                    c.close()
                except OSError:
                    pass

        t = threading.Thread(target=accept_loop, daemon=True)
        t.start()
        try:
            # Probar directamente el probe TCP/FTP sobre el socket mock
            from app.switch_utils import _probe_tcp_ftp

            hit = _probe_tcp_ftp("127.0.0.1", port, timeout_s=1.5)
            self.assertIsNotNone(hit)
            self.assertTrue(hit["is_ftp"])
            self.assertIn("DBI FTP Ready", hit["banner"])
        finally:
            stop = True
            srv.close()
            t.join(timeout=1)


if __name__ == "__main__":
    unittest.main()

