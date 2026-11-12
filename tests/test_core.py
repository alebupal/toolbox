"""Tests unitarios de lógica core (sin requerir dependencias externas en el host)."""

from __future__ import annotations

import json
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



class FeatureTests(unittest.TestCase):
    """Orden, búsqueda, juego completo, historial, papelera, espacio SD y CLI de las nuevas opciones."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.mkdtemp(prefix="tb_feat_")
        cls.cfg_dir = os.path.join(cls.tmp, "config")
        cls.data_root = os.path.join(cls.tmp, "host")
        os.makedirs(cls.cfg_dir, exist_ok=True)
        os.makedirs(cls.data_root, exist_ok=True)
        cls.env = mock.patch.dict(os.environ, {"CONFIG_DIR": cls.cfg_dir, "DATA_ROOT": cls.data_root})
        cls.env.start()
        import importlib

        import app.main as main_mod

        cls.main = importlib.reload(main_mod)
        from fastapi.testclient import TestClient

        cls.api = TestClient(cls.main.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self) -> None:
        self.case_dir = tempfile.mkdtemp(prefix="case_", dir=self.data_root)

    def _write(self, rel: str, data: bytes = b"x", mtime: float | None = None) -> str:
        path = os.path.join(self.case_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_bytes(data)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def _cli(self, argv: list[str]) -> tuple[int, str]:
        import io
        from contextlib import redirect_stdout

        from app.cli import ToolboxClient, run_cli

        client = ToolboxClient(base_url="http://127.0.0.1:9")
        client._web_ok = False
        buf = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(buf):
            rc = run_cli(argv, client=client)
        return rc, buf.getvalue()

    # 1. Orden del listado ------------------------------------------------
    def test_list_sort_by_mtime_and_size(self) -> None:
        self._write("a_vieja.mkv", b"1" * 300, mtime=1_000_000)
        self._write("b_nueva.mkv", b"1" * 100, mtime=3_000_000)
        self._write("c_media.mkv", b"1" * 200, mtime=2_000_000)
        os.makedirs(os.path.join(self.case_dir, "zz_carpeta"))
        r = self.api.get("/api/fs/list", params={"path": self.case_dir, "sort": "mtime", "order": "desc"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual([f["name"] for f in body["files"]], ["b_nueva.mkv", "c_media.mkv", "a_vieja.mkv"])
        self.assertEqual(body["dirs"][0]["name"], "zz_carpeta")  # carpetas primero
        self.assertIn("mtime", body["dirs"][0])
        self.assertIn("size", body["files"][0])
        r = self.api.get("/api/fs/list", params={"path": self.case_dir, "sort": "size"})
        self.assertEqual([f["name"] for f in r.json()["files"]], ["b_nueva.mkv", "c_media.mkv", "a_vieja.mkv"])
        r = self.api.get("/api/fs/list", params={"path": self.case_dir})
        self.assertEqual([f["name"] for f in r.json()["files"]], ["a_vieja.mkv", "b_nueva.mkv", "c_media.mkv"])

        rc, out = self._cli(["mover", "--listar", self.case_dir, "--orden", "fecha", "--desc"])
        self.assertEqual(rc, 0)
        self.assertLess(out.index("b_nueva.mkv"), out.index("a_vieja.mkv"))
        self.assertIn("1970-01", out)  # columna de fecha

    # 2. Búsqueda -----------------------------------------------------------
    def test_search_accents_limit_and_no_symlinks(self) -> None:
        self._write("Series/La Película Ácida (2024).mkv")
        self._write("Otra/pelicula acida extra.mkv")
        self._write("Otra/nada.txt")
        os.symlink(self.case_dir, os.path.join(self.case_dir, "Otra", "bucle"))
        r = self.api.get("/api/fs/search", params={"path": self.case_dir, "q": "PELICULA ácida"})
        self.assertEqual(r.status_code, 200)
        names = sorted(x["name"] for x in r.json()["results"])
        self.assertEqual(names, ["La Película Ácida (2024).mkv", "pelicula acida extra.mkv"])
        self.assertFalse(r.json()["truncated"])
        self.assertEqual(r.json()["results"][0]["rel"].split("/")[0], "Otra")

        r = self.api.get("/api/fs/search", params={"path": self.case_dir, "q": "a", "limit": 2})
        self.assertEqual(len(r.json()["results"]), 2)
        self.assertTrue(r.json()["truncated"])

        # Todas las palabras, en cualquier orden
        r = self.api.get("/api/fs/search", params={"path": self.case_dir, "q": "acida película"})
        self.assertEqual(len(r.json()["results"]), 2)
        rc, out = self._cli(["mover", "--buscar", "acida", self.case_dir])
        self.assertEqual(rc, 0)
        self.assertIn("2 resultado(s)", out)
        rc, _ = self._cli(["mover", "--buscar", "noexiste-xyz", self.case_dir])
        self.assertEqual(rc, 1)

    # 3. Juego completo -----------------------------------------------------
    def test_add_game_orders_base_update_dlc_and_warns(self) -> None:
        self._write("Zelda/Zelda [0100F2C0115B7001] [DLC].nsp", b"d")
        self._write("Zelda/Zelda [0100F2C0115B6800][v65536].nsp", b"u")
        self._write("Zelda/Zelda [0100F2C0115B6000][v0].nsp", b"b")
        self._write("Zelda/leeme.txt", b"t")
        self._write("Mario/Mario [0100AAAA00000800][v131072].nsp", b"u")
        r = self.api.post("/api/ftp/add-game", json={"paths": [self.case_dir], "dest_dir": "/install"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["added"], 4)
        zelda = [it for it in body["items"] if "Zelda" in it["source_path"]]
        self.assertEqual([it["switch_type"] for it in zelda], ["BASE", "UPD", "DLC"])
        self.assertTrue(all(it["dest_path"].startswith("/install/") for it in body["items"]))
        self.assertTrue(any("sin el juego base" in w and "Mario" in w for w in body["warnings"]))
        groups = {g["name"]: g for g in body["groups"]}
        self.assertTrue(groups["Zelda"]["base"])
        self.assertEqual((groups["Zelda"]["upd"], groups["Zelda"]["dlc"]), (1, 1))
        # Segunda vez: no duplica
        r2 = self.api.post("/api/ftp/add-game", json={"paths": [self.case_dir]})
        self.assertEqual(r2.json()["added"], 0)
        self.assertTrue(any("Ya estaba en la cola" in w for w in r2.json()["warnings"]))
        self.api.post("/api/queue/ftp/clear", params={"scope": "pending"})

        rc, out = self._cli(["ftp", "--juego", os.path.join(self.case_dir, "Zelda"), "-d", "/nsp"])
        self.assertEqual(rc, 0)
        self.assertIn("Añadidos a la cola FTP: 3", out)
        self.assertIn("Base ✓", out)
        self.api.post("/api/queue/ftp/clear", params={"scope": "pending"})

    # 4. Historial ----------------------------------------------------------
    def test_history_records_ok_and_err_and_survives_queue_clear(self) -> None:
        db = QueueDB(os.path.join(self.cfg_dir, "hist.db"))
        store = SettingsStore(config_dir=self.cfg_dir, data_root=self.data_root)
        mgr = WorkerManager(db, store)
        db.add_items(
            [
                {"kind": "mover", "operation": "copiar", "source_path": f"/x/{n}.bin", "dest_path": f"/y/{n}.bin", "size_bytes": 1000}
                for n in (1, 2)
            ]
        )
        results = iter([{"status": "OK"}, {"status": "ERR", "error": "disco lleno"}])
        with mock.patch("app.worker_manager.process_mover_item", side_effect=lambda **_k: next(results)):
            mgr.start("mover")
            mgr.runners["mover"]._thread.join(timeout=5)
        items, total = db.list_history("mover")
        self.assertEqual(total, 2)
        self.assertEqual({h["status"] for h in items}, {"OK", "ERR"})
        err = next(h for h in items if h["status"] == "ERR")
        self.assertEqual(err["error"], "disco lleno")
        ok = next(h for h in items if h["status"] == "OK")
        self.assertIsNotNone(ok["duration_s"])
        self.assertEqual(db.clear_by_status("mover", ("OK",)), 1)
        self.assertEqual(db.list_history("mover")[1], 2)
        self.assertEqual(db.clear_history("mover"), 2)
        db.close()

        # API + CLI sobre la base de datos de la app
        self.main.queue_db.add_history({"kind": "ftp", "op": "subir", "src": "/a/juego.nsp", "size": 5, "status": "OK", "started_at": 1, "finished_at": 3})
        r = self.api.get("/api/history", params={"kind": "ftp"})
        self.assertEqual(r.json()["total"], 1)
        self.assertEqual(r.json()["items"][0]["avg_bps"], 2.5)
        rc, out = self._cli(["ftp", "--historial", "5"])
        self.assertEqual(rc, 0)
        self.assertIn("juego.nsp", out)
        rc, out = self._cli(["ftp", "--limpiar-historial"])
        self.assertIn("eliminados: 1", out)
        self.assertEqual(self.api.get("/api/history").json()["total"], 0)

    # 5. Papelera -----------------------------------------------------------
    def test_trash_move_restore_collision_empty_and_expiry(self) -> None:
        f = self._write("Descargas/peli.mkv", b"datos")
        r = self.api.post("/api/fs/delete", json={"paths": [f]})
        self.assertEqual(r.status_code, 200, r.text)
        trashed = r.json()["trashed"]
        self.assertEqual(len(trashed), 1)
        self.assertFalse(os.path.exists(f))
        tid = trashed[0]["id"]
        listing = self.api.get("/api/fs/list", params={"path": self.data_root, "show_hidden": True}).json()
        self.assertNotIn(".toolbox-trash", [d["name"] for d in listing["dirs"]])
        items = self.api.get("/api/trash").json()["items"]
        entry = next(i for i in items if i["id"] == tid)
        self.assertEqual(entry["original_path"], f)
        self.assertEqual(entry["size"], 5)
        self.assertGreater(entry["expires_at"], entry["trashed_at"])

        # Colisión: ya existe otro fichero con ese nombre -> se restaura con sufijo
        Path(f).write_bytes(b"nuevo")
        r = self.api.post("/api/trash/restore", json={"ids": [tid]})
        restored = r.json()["restored"][0]
        self.assertTrue(restored["renamed"])
        self.assertTrue(restored["path"].endswith("peli (restaurado).mkv"))
        self.assertEqual(Path(restored["path"]).read_bytes(), b"datos")
        self.assertEqual(Path(f).read_bytes(), b"nuevo")

        # Vaciar
        tid2 = self.api.post("/api/fs/delete", json={"paths": [f]}).json()["trashed"][0]["id"]
        self.assertEqual(self.api.post("/api/trash/empty", json={"ids": [tid2]}).json()["removed"], 1)
        self.assertFalse(any(i["id"] == tid2 for i in self.api.get("/api/trash").json()["items"]))

        # Caducidad
        g = self._write("viejo.txt")
        tid3 = self.api.post("/api/fs/delete", json={"paths": [g]}).json()["trashed"][0]["id"]
        import time as _time

        self.assertEqual(self.main.trash_manager.purge_expired(now=_time.time() + 8 * 86400), 1)
        self.assertIsNone(self.main.queue_db.get_trash_entry(tid3))

        # Protecciones y borrado definitivo
        self.assertEqual(self.api.post("/api/fs/delete", json={"paths": [self.data_root]}).status_code, 400)
        h = self._write("perm.txt")
        r = self.api.post("/api/fs/delete", json={"paths": [h], "permanent": True})
        self.assertEqual(r.json(), {"trashed": [], "deleted": 1})

    def test_trash_cross_device_requires_permanent(self) -> None:
        import errno

        f = self._write("otro_disco.bin")
        real_rename = os.rename

        def fake_rename(src, dst):
            if src == f:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_rename(src, dst)

        with mock.patch("app.trash.os.rename", side_effect=fake_rename):
            r = self.api.post("/api/fs/delete", json={"paths": [f]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("otro sistema de ficheros", r.json()["detail"])
        self.assertTrue(os.path.exists(f))
        self.assertEqual(self.main.queue_db.list_trash(), [])

    def test_trash_toggle_web_and_cli(self) -> None:
        from app.config import effective_trash_days, sanitize_settings

        self.assertTrue(sanitize_settings({})["trash_enabled"])
        self.assertFalse(sanitize_settings({"trash_enabled": "false"})["trash_enabled"])
        self.assertEqual(effective_trash_days({"trash_enabled": False, "trash_days": 7}), 0)
        self.assertEqual(effective_trash_days({"trash_enabled": True, "trash_days": 7}), 7)
        try:
            # Desactivada desde la web: el borrado es definitivo y lo ya enviado se conserva
            keep = self._write("antes.txt")
            kept_id = self.api.post("/api/fs/delete", json={"paths": [keep]}).json()["trashed"][0]["id"]
            r = self.api.put("/api/settings", json={"trash_enabled": False})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertFalse(r.json()["trash_enabled"])
            info = self.api.get("/api/trash").json()
            self.assertFalse(info["trash_enabled"])
            self.assertTrue(any(i["id"] == kept_id for i in info["items"]))
            f = self._write("sin_papelera.txt")
            res = self.api.post("/api/fs/delete", json={"paths": [f]}).json()
            self.assertEqual(res["trashed"], [])
            self.assertEqual(res["deleted"], 1)
            self.assertFalse(os.path.exists(f))

            # La CLI sin web lee el mismo settings.json que guardó la web
            rc, out = self._cli(["mover", "--papelera"])
            self.assertIn("Papelera DESACTIVADA", out)

            # Reactivada desde la CLI (modo sin web: escribe settings.json)
            rc, out = self._cli(["mover", "--papelera-activar"])
            self.assertEqual(rc, 0, out)
            self.assertIn("Papelera activada", out)
            rc, out = self._cli(["mover", "--papelera"])
            self.assertIn("Papelera ACTIVADA", out)
            g = self._write("con_papelera.txt")
            rc, out = self._cli(["mover", "--borrar", "-y", g])
            self.assertEqual(rc, 0, out)
            self.assertIn("Enviados a la papelera", out)

            # Desactivada desde la CLI: --borrar avisa y borra definitivamente
            rc, out = self._cli(["subeftp", "--papelera-desactivar"])
            self.assertEqual(rc, 0, out)
            rc, out = self._cli(["mover", "--papelera"])
            self.assertIn("Papelera DESACTIVADA", out)
            h = self._write("directo.txt")
            rc, out = self._cli(["mover", "--borrar", "-y", h])
            self.assertEqual(rc, 0, out)
            self.assertIn("Eliminados definitivamente: 1", out)
            self.assertFalse(os.path.exists(h))
        finally:
            self.api.put("/api/settings", json={"trash_enabled": True})
            self._cli(["mover", "--papelera-activar"])

    def test_trash_cli_and_days_zero(self) -> None:
        f = self._write("cli/borrame.txt", b"abc")
        rc, out = self._cli(["mover", "--borrar", "-y", f])
        self.assertEqual(rc, 0)
        self.assertIn("Enviados a la papelera: 1", out)
        rc, out = self._cli(["mover", "--papelera"])
        self.assertIn(f, out)
        tid = self.main.queue_db.list_trash()[0]["id"]
        rc, out = self._cli(["mover", "--restaurar", str(tid)])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(f))
        rc, _ = self._cli(["mover", "--restaurar", "999999"])
        self.assertEqual(rc, 1)
        self._cli(["mover", "--borrar", "-y", f])
        rc, out = self._cli(["mover", "--vaciar-papelera", "-y"])
        self.assertIn("Eliminados definitivamente: 1", out)
        # trash_days = 0 -> borrado directo
        self.main.settings_store.update({"trash_days": 0})
        try:
            g = self._write("cli/directo.txt")
            self.assertEqual(self.api.post("/api/fs/delete", json={"paths": [g]}).json()["deleted"], 1)
        finally:
            self.main.settings_store.update({"trash_days": 7})

    # 6. Espacio en la SD ---------------------------------------------------
    def test_ftp_insufficient_space_marks_err_and_queue_continues(self) -> None:
        big = self._write("sd/grande.nsp", b"g" * 1000)
        small = self._write("sd/peque.nsp", b"p" * 10)
        db = QueueDB(os.path.join(self.cfg_dir, "space.db"))
        store = SettingsStore(config_dir=self.cfg_dir, data_root=self.data_root)
        store.update({"ftp_host": "127.0.0.1", "ftp_port": 9})
        mgr = WorkerManager(db, store)
        db.add_items(
            [
                {"kind": "ftp", "operation": "subir", "source_path": p, "dest_path": "/install/" + os.path.basename(p), "size_bytes": os.path.getsize(p)}
                for p in (big, small)
            ]
        )
        space = mock.Mock(return_value={"supported": True, "free_bytes": 100, "method": "AVBL"})
        # El pequeño ya está en el servidor con el mismo tamaño -> OK sin lanzar curl
        remote = mock.Mock(side_effect=lambda _s, path: 10 if path.endswith("peque.nsp") else None)
        with mock.patch("app.worker_manager.get_remote_free_space", space), mock.patch(
            "app.ftp_worker.get_remote_file_size", remote
        ):
            mgr.start("ftp")
            mgr.runners["ftp"]._thread.join(timeout=10)
        first, second = db.list_items("ftp")
        self.assertEqual(first["status"], "ERR")
        self.assertIn("Espacio insuficiente en la SD", first["error"])
        self.assertEqual(second["status"], "OK")
        self.assertEqual(mgr.status()["ftp"]["remote_free_bytes"], 100)
        db.close()

        # Servidor sin soporte: tras la primera respuesta "no soportado" no se vuelve a preguntar
        from app.worker_manager import _QueueRunner

        cb = mock.Mock(return_value=None)
        runner = _QueueRunner("ftp", db, store, free_space_cb=cb)
        self.assertIsNone(runner._free_space())
        self.assertIsNone(runner._free_space())
        self.assertEqual(cb.call_count, 1)

    def test_ftp_space_endpoint_and_cli(self) -> None:
        with mock.patch(
            "app.worker_manager.get_remote_free_space",
            return_value={"supported": True, "free_bytes": 5 * 1024**3, "method": "AVBL"},
        ):
            r = self.api.get("/api/ftp/space")
        body = r.json()
        self.assertTrue(body["supported"])
        self.assertEqual(body["free_bytes"], 5 * 1024**3)
        self.assertTrue(body["enough"])
        st = self.api.get("/api/status").json()["workers"]["ftp"]
        self.assertEqual(st["remote_free_bytes"], 5 * 1024**3)
        self.assertEqual(st["phase"], "idle")
        self.assertIn("aggregate", st)
        with mock.patch(
            "app.ftp_worker.get_remote_free_space",
            return_value={"supported": False, "free_bytes": None, "method": ""},
        ):
            rc, out = self._cli(["ftp", "--espacio"])
        self.assertEqual(rc, 0)
        self.assertIn("no informa del espacio libre", out)

    def test_free_space_parser_with_fake_ftp_server(self) -> None:
        from app.ftp_worker import get_remote_free_space

        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(1)

        def serve() -> None:
            conn, _ = srv.accept()
            f = conn.makefile("rwb")
            f.write(b"220 ftpd\r\n")
            f.flush()
            for raw in f:
                cmd = raw.decode().strip().upper()
                if cmd.startswith("USER"):
                    f.write(b"331 ok\r\n")
                elif cmd.startswith("PASS"):
                    f.write(b"230 ok\r\n")
                elif cmd.startswith("AVBL"):
                    f.write(b"213 123456789\r\n")
                elif cmd.startswith("QUIT"):
                    f.write(b"221 bye\r\n")
                    f.flush()
                    break
                else:
                    f.write(b"502 no\r\n")
                f.flush()
            conn.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        res = get_remote_free_space({"ftp_host": "127.0.0.1", "ftp_port": port, "ftp_remote_dir": "/install"})
        t.join(timeout=3)
        srv.close()
        self.assertEqual(res, {"supported": True, "free_bytes": 123456789, "method": "AVBL"})

    # 7. Estado y rutas relativas -------------------------------------------
    def test_queue_rel_paths_and_status_shape(self) -> None:
        f = self._write("Descargas/Peliculas/Otra (2023).mkv", b"v")
        self.main.settings_store.update({"mover_origin": os.path.join(self.case_dir, "Descargas")})
        r = self.api.post("/api/queue/add", json={"kind": "mover", "paths": [f], "dest_dir": os.path.join(self.case_dir, "Multimedia")})
        self.assertEqual(r.json()["added"], 1)
        items = self.api.get("/api/queue", params={"kind": "mover"}).json()["items"]
        it = next(i for i in items if i["source_path"] == f)
        self.assertEqual(it["rel_src"], "Peliculas/Otra (2023).mkv")
        self.assertTrue(it["rel_dest"].endswith("Multimedia/Otra (2023).mkv"))
        st = self.api.get("/api/status").json()["workers"]["mover"]
        self.assertEqual(st["phase"], "idle")
        self.assertIsNone(st["current"])
        self.assertGreaterEqual(st["aggregate"]["total_items"], 1)
        rc, out = self._cli(["mover", "--estado"])
        self.assertIn("Peliculas/Otra (2023).mkv", out)
        self.api.post("/api/queue/mover/clear", params={"scope": "pending"})

    def test_progress_phase_current_and_aggregate_while_running(self) -> None:
        db = QueueDB(os.path.join(self.cfg_dir, "prog.db"))
        store = SettingsStore(config_dir=self.cfg_dir, data_root=self.data_root)
        mgr = WorkerManager(db, store)
        db.add_items(
            [
                {"kind": "mover", "operation": "copiar", "source_path": f"/x/p{n}.bin", "dest_path": f"/y/p{n}.bin", "size_bytes": 1000}
                for n in (1, 2)
            ]
        )
        gate = threading.Event()
        seen: list[dict] = []

        def fake(item, progress_cb, **_k):
            if item["source_path"].endswith("p1.bin"):
                seen.append(mgr.status()["mover"])  # antes de progreso: preparando
                progress_cb({"item_id": item["id"], "filename": "p1.bin", "percent": 50.0, "transferred_bytes": 500, "total_bytes": 1000})
                seen.append(mgr.status()["mover"])
                gate.wait(2)
            return {"status": "OK"}

        with mock.patch("app.worker_manager.process_mover_item", side_effect=fake):
            mgr.start("mover")
            import time as _time

            for _ in range(50):
                if len(seen) >= 2:
                    break
                _time.sleep(0.05)
            gate.set()
            mgr.runners["mover"]._thread.join(timeout=5)
        self.assertEqual(seen[0]["phase"], "preparing")
        self.assertEqual(seen[1]["phase"], "transferring")
        self.assertEqual(seen[1]["current"]["bytes_done"], 500)
        self.assertEqual(seen[1]["aggregate"]["total_items"], 2)
        self.assertEqual(seen[1]["aggregate"]["total_bytes"], 2000)
        self.assertEqual(seen[1]["aggregate"]["done_bytes"], 500)
        self.assertEqual(mgr.status()["mover"]["phase"], "idle")
        db.close()

    # 8. Ajustes ------------------------------------------------------------
    def test_favorites_accept_json_and_lines(self) -> None:
        a = os.path.join(self.case_dir, "fa")
        b = os.path.join(self.case_dir, "fb")
        os.makedirs(a)
        os.makedirs(b)
        r = self.api.put("/api/settings", json={"mover_favorites": json.dumps([a, b])})
        self.assertEqual(r.json()["mover_favorites"], [a, b])
        r = self.api.put("/api/settings", json={"mover_favorites": f"{b}\n{a}"})
        self.assertEqual(r.json()["mover_favorites"], [b, a])
        r = self.api.put("/api/settings", json={"mover_favorites": [a]})
        self.assertEqual(r.json()["mover_favorites"], [a])
        self.assertEqual(r.json()["trash_days"], 7)
