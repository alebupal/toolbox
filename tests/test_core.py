"""Tests unitarios de lógica core (sin requerir dependencias externas en el host)."""

from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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

    def test_worker_exception_marks_err_and_queue_continues(self) -> None:
        """Una excepción inesperada en un elemento lo marca ERR y la cola sigue con el siguiente."""
        for kind, target in (("mover", "process_mover_item"), ("ftp", "process_ftp_item")):
            with self.subTest(kind=kind):
                db = QueueDB(os.path.join(self.cfg_dir, f"queue_{kind}.db"))
                store = SettingsStore(config_dir=self.cfg_dir, data_root=self.data_root)
                mgr = WorkerManager(db, store)
                db.add_items(
                    [
                        {
                            "kind": kind,
                            "operation": "copiar",
                            "source_path": os.path.join(self.data_root, f"{kind}_{n}.bin"),
                            "dest_path": f"/destino/{kind}_{n}.bin",
                            "size_bytes": 1,
                        }
                        for n in (1, 2)
                    ]
                )
                calls: list[str] = []

                def fake_process(item, **_kwargs):
                    calls.append(item["source_path"])
                    if len(calls) == 1:
                        raise PermissionError("sin permiso")
                    return {"status": "OK"}

                with mock.patch(f"app.worker_manager.{target}", side_effect=fake_process):
                    self.assertTrue(mgr.start(kind))
                    runner = mgr.runners[kind]
                    if runner._thread:
                        runner._thread.join(timeout=5)
                    self.assertFalse(runner.is_running())

                first, second = db.list_items(kind)
                self.assertEqual(len(calls), 2)
                self.assertEqual(first["status"], "ERR")
                self.assertIn("PermissionError: sin permiso", first["error"])
                self.assertEqual(second["status"], "OK")
                logs = [entry["message"] for entry in db.get_logs(kind)]
                self.assertTrue(any("Error inesperado procesando" in m for m in logs))
                # El elemento fallido se puede reintentar (ya no está bloqueado en PROC)
                self.assertEqual(db.retry_errors(kind), 1)
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

    def test_cli_shares_web_queue_and_shell_wrappers(self) -> None:
        import io
        import subprocess
        from contextlib import redirect_stdout
        from unittest import mock
        from app.cli import ToolboxClient, run_cli

        src_dir = os.path.join(self.data_root, "juegos")
        dst_dir = os.path.join(self.data_root, "destino_cli")
        os.makedirs(src_dir, exist_ok=True)
        os.makedirs(dst_dir, exist_ok=True)
        video = os.path.join(self.data_root, "peli.mkv")
        nsp = os.path.join(src_dir, "Zelda [0100F2C0115B6000][v0].nsp")
        txt = os.path.join(src_dir, "leeme.txt")
        Path(video).write_bytes(b"video123")
        Path(nsp).write_bytes(b"nspdata")
        Path(txt).write_bytes(b"notas")

        client = ToolboxClient(base_url="http://127.0.0.1:9")
        client._web_ok = False  # Forzar modo DB compartida sin depender de uvicorn

        with mock.patch.dict(os.environ, {"CONFIG_DIR": self.cfg_dir, "DATA_ROOT": self.data_root}), mock.patch(
            "sys.stdin.isatty", return_value=False
        ):
            rc_mover = run_cli(["mover", "-m", "-d", dst_dir, video], client=client)
            self.assertEqual(rc_mover, 0)

            rc_ftp = run_cli(["ftp", "-j", "-d", "/install", src_dir], client=client)
            self.assertEqual(rc_ftp, 0)

            db = QueueDB(os.path.join(self.cfg_dir, "queue.db"))
            mover_items = db.list_items("mover")
            ftp_items = db.list_items("ftp")
            db.close()

            self.assertEqual(len(mover_items), 1)
            self.assertEqual(mover_items[0]["operation"], "mover")
            self.assertEqual(mover_items[0]["dest_path"], os.path.join(dst_dir, "peli.mkv"))

            # Con -j/--solo-switch solo debe haberse encolado el .nsp y no leeme.txt
            self.assertEqual(len(ftp_items), 1)
            self.assertEqual(ftp_items[0]["source_path"], nsp)
            self.assertTrue(ftp_items[0]["dest_path"].startswith("/install/juegos/"))

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc_st = run_cli(["mover", "--estado"], client=client)
            self.assertEqual(rc_st, 0)
            self.assertIn("peli.mkv", buf.getvalue())

        # Verificar que scripts/mover.sh y scripts/subeftp.sh delegan en app.cli con TOOLBOX_UNIFIED=1
        repo_root = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ, TOOLBOX_UNIFIED="1", PORT="9", CONFIG_DIR=self.cfg_dir, DATA_ROOT=self.data_root)
        proc = subprocess.run(
            [os.path.join(repo_root, "scripts", "mover.sh"), "--estado"],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Cola compartida Web + SSH", proc.stdout)
        self.assertIn("peli.mkv", proc.stdout)

    def test_explorer_actions_change_dest_and_space_check(self) -> None:
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        from app.cli import ToolboxClient, run_cli
        from app.mover_worker import (
            calculate_dir_size,
            delete_entries,
            evaluate_mover_space,
            rename_entry,
        )

        # 1.1 Acciones rápidas: calcular tamaño, renombrar y eliminar (con protección de sistema)
        folder = os.path.join(self.data_root, "album")
        sub = os.path.join(folder, "cd1")
        os.makedirs(sub, exist_ok=True)
        f1 = os.path.join(sub, "pista1.mp3")
        f2 = os.path.join(sub, "pista2.mp3")
        Path(f1).write_bytes(b"a" * 1024)
        Path(f2).write_bytes(b"b" * 2048)

        sz = calculate_dir_size(folder, self.data_root)
        self.assertEqual(sz["size_bytes"], 3072)
        self.assertEqual(sz["files_count"], 2)

        renamed_path = rename_entry(f2, "pista2_renombrada.mp3", self.data_root)
        self.assertTrue(os.path.exists(renamed_path))
        self.assertFalse(os.path.exists(f2))

        # No permitir borrar carpetas críticas del sistema ni data_root
        with self.assertRaises(ValueError):
            delete_entries([self.data_root], self.data_root)
        sys_etc = os.path.join(self.data_root, "etc")
        os.makedirs(sys_etc, exist_ok=True)
        with self.assertRaises(ValueError):
            delete_entries([sys_etc], self.data_root)

        # 1.2 Cambiar destino preservando subcarpetas cuando cleanup_dir está definido
        dst_a = os.path.join(self.data_root, "dest_a")
        dst_b = os.path.join(self.data_root, "dest_b")
        os.makedirs(dst_a, exist_ok=True)
        os.makedirs(dst_b, exist_ok=True)

        db = QueueDB(os.path.join(self.cfg_dir, "queue.db"))
        added = db.add_items(
            [
                {
                    "kind": "mover",
                    "operation": "copiar",
                    "source_path": f1,
                    "dest_path": os.path.join(dst_a, "album", "cd1", "pista1.mp3"),
                    "size_bytes": 1024,
                    "cleanup_dir": folder,
                }
            ]
        )
        item_id = added[0]["id"]
        changed = db.change_dest_dir([item_id], dst_b, data_root=self.data_root)
        self.assertEqual(changed, 1)
        updated_item = db.get_item(item_id)
        self.assertEqual(
            updated_item["dest_path"],
            os.path.join(dst_b, "album", "cd1", "pista1.mp3"),
        )

        # 1.3 Comprobación de espacio libre total antes de empezar
        items = db.list_items("mover")
        db.close()
        sp_ok = evaluate_mover_space(items, dst_b)
        self.assertFalse(sp_ok["space_warning"])
        self.assertEqual(sp_ok["needed_bytes"], 1024)

        # Simular falta de espacio en destino
        fake_statvfs = mock.Mock(f_bavail=10, f_blocks=100, f_frsize=10)
        with mock.patch("os.statvfs", return_value=fake_statvfs):
            sp_warn = evaluate_mover_space(items, dst_b)
            self.assertTrue(sp_warn["space_warning"])
            self.assertEqual(sp_warn["shortfall_bytes"], 924)

        # Probar equivalentes por CLI (--tamano, --renombrar, --cambiar-destino, --borrar -y)
        client = ToolboxClient(base_url="http://127.0.0.1:9")
        client._web_ok = False
        dst_c = os.path.join(self.data_root, "dest_c")
        os.makedirs(dst_c, exist_ok=True)

        with mock.patch.dict(os.environ, {"CONFIG_DIR": self.cfg_dir, "DATA_ROOT": self.data_root}), mock.patch(
            "sys.stdin.isatty", return_value=False
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(run_cli(["mover", "--tamano", folder], client=client), 0)
            self.assertIn("2 archivo(s)", buf.getvalue())

            self.assertEqual(
                run_cli(["mover", "--renombrar", renamed_path, "pista2_final.mp3"], client=client),
                0,
            )
            final_f2 = os.path.join(sub, "pista2_final.mp3")
            self.assertTrue(os.path.exists(final_f2))

            self.assertEqual(
                run_cli(["mover", "--cambiar-destino", str(item_id), dst_c], client=client),
                0,
            )
            cli_db = QueueDB(os.path.join(self.cfg_dir, "queue.db"))
            self.assertEqual(
                cli_db.get_item(item_id)["dest_path"],
                os.path.join(dst_c, "album", "cd1", "pista1.mp3"),
            )
            cli_db.close()

            self.assertEqual(
                run_cli(["mover", "--borrar", "-y", final_f2], client=client),
                0,
            )
            self.assertFalse(os.path.exists(final_f2))


if __name__ == "__main__":
    unittest.main()

