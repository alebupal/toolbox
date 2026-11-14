"""Tests del backup de la microSD (exclusiones, parseo LIST y backup real contra pyftpdlib si está instalado)."""

from __future__ import annotations

import ftplib
import os
import shutil
import socket
import tempfile
import threading
import unittest
from unittest import mock

import json

from app.config import DEFAULT_BACKUP_EXCLUDE, SettingsStore, parse_patterns
from app.db import QueueDB
from app.switch_backup import (
    BackupRun,
    _parse_list_line,
    is_excluded_dir,
    is_excluded_file,
    record_history,
)

try:
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    HAS_FTPD = True
except ImportError:  # pragma: no cover - dependencia opcional
    HAS_FTPD = False

DEFAULT_EXCL = DEFAULT_BACKUP_EXCLUDE


class ExclusionTests(unittest.TestCase):
    def test_dir_patterns(self) -> None:
        self.assertTrue(is_excluded_dir("Nintendo/Contents", DEFAULT_EXCL))
        self.assertTrue(is_excluded_dir("emuMMC/RAW1/Nintendo/Contents", DEFAULT_EXCL))
        self.assertTrue(is_excluded_dir("NINTENDO/contents", DEFAULT_EXCL))
        self.assertTrue(is_excluded_dir("install", DEFAULT_EXCL))
        self.assertFalse(is_excluded_dir("switch/install", DEFAULT_EXCL))  # anclada a la raíz
        self.assertFalse(is_excluded_dir("Nintendo", DEFAULT_EXCL))
        self.assertTrue(is_excluded_dir("roms/snes", ["roms/*"]))
        self.assertTrue(is_excluded_dir("emummc", DEFAULT_EXCL))
        self.assertTrue(is_excluded_dir("Nintendo/Album", DEFAULT_EXCL))
        self.assertTrue(is_excluded_dir("backup", DEFAULT_EXCL))
        self.assertFalse(is_excluded_dir("atmosphere", DEFAULT_EXCL))
        self.assertFalse(is_excluded_dir("JKSV", DEFAULT_EXCL))

    def test_old_defaults_are_migrated(self) -> None:
        tmp = tempfile.mkdtemp(prefix="bk_cfg_")
        try:
            with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as fh:
                json.dump({"ftp_backup_exclude": ["Nintendo/Contents", "/install", "/nsp", "/xci", "/games", "/juegos"],
                           "ftp_backup_exclude_ext": ["nsp", "nsz", "xci", "xcz"]}, fh)
            cfg = SettingsStore(config_dir=tmp, data_root=tmp).get()
            self.assertIn("/emuMMC", cfg["ftp_backup_exclude"])
            self.assertIn("nca", cfg["ftp_backup_exclude_ext"])
            # Una lista personalizada se respeta
            store = SettingsStore(config_dir=tmp, data_root=tmp)
            store.update({"ftp_backup_exclude": "/solo_esta"})
            self.assertEqual(SettingsStore(config_dir=tmp, data_root=tmp).get()["ftp_backup_exclude"], ["/solo_esta"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_file_ext_and_parse(self) -> None:
        self.assertTrue(is_excluded_file("Zelda [0100][v0].NSP", ["nsp"]))
        self.assertFalse(is_excluded_file("hbmenu.nro", ["nsp", "xci"]))
        self.assertEqual(parse_patterns("Nintendo/Contents\n/install, /nsp/\n\n"), ["Nintendo/Contents", "/install", "/nsp"])

    def test_list_line(self) -> None:
        self.assertEqual(_parse_list_line("drwxr-xr-x 1 root root 0 Jan 01 00:00 Nintendo"), ("Nintendo", True, None))
        self.assertEqual(
            _parse_list_line("-rw-r--r-- 1 root root 1234 Jan 01 2024 mi save.bin"), ("mi save.bin", False, 1234)
        )
        self.assertIsNone(_parse_list_line("lrwxrwxrwx 1 root root 4 Jan 01 00:00 x -> y"))


@unittest.skipUnless(HAS_FTPD, "pyftpdlib no instalado")
class BackupFtpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="bk_test_")
        self.sd = os.path.join(self.tmp, "sd")
        self.data_root = os.path.join(self.tmp, "host")
        os.makedirs(self.data_root)
        files = {
            "atmosphere/config/system_settings.ini": b"x" * 100,
            "switch/hbmenu.nro": b"n" * 50,
            "Nintendo/Album/2026/shot.jpg": b"j" * 300,
            "Nintendo/Contents/registered/big.nca": b"g" * 5000,
            "emuMMC/RAW1/Nintendo/Contents/registered/x.nca": b"g" * 500,
            "emuMMC/RAW1/eMMC/00": b"e" * 900,
            "backup/A1B2/rawnand.bin": b"r" * 800,
            "install/juego.nsp": b"g" * 4000,
            "otros/suelto.xci": b"g" * 700,
            "JKSV/Zelda/save.bin": b"s" * 2000,
        }
        for rel, data in files.items():
            full = os.path.join(self.sd, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(data)

        auth = DummyAuthorizer()
        auth.add_user("u", "p", self.sd, perm="elr")
        handler = type("H", (FTPHandler,), {})
        handler.authorizer = auth
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.server = FTPServer(("127.0.0.1", self.port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"timeout": 0.2}, daemon=True)
        self.thread.start()
        store = SettingsStore(config_dir=os.path.join(self.tmp, "cfg"), data_root=self.data_root)
        self.settings = store.update(
            {"ftp_host": "127.0.0.1", "ftp_port": self.port, "ftp_user": "u", "ftp_password": "p", "ftp_backup_dir": os.path.join(self.data_root, "bk")}
        )

    def tearDown(self) -> None:
        self.server.close_all()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, **kw):
        return BackupRun(self.settings, self.data_root, **kw).run()

    def _check_target(self, target: str) -> None:
        got = sorted(
            os.path.relpath(os.path.join(d, f), target).replace(os.sep, "/") for d, _, fs in os.walk(target) for f in fs
        )
        self.assertEqual(
            got,
            ["JKSV/Zelda/save.bin", "atmosphere/config/system_settings.ini", "switch/hbmenu.nro"],
        )

    def test_backup_excludes_games(self) -> None:
        res = self._run()
        self.assertEqual(res["status"], "OK", res.get("error"))
        self.assertEqual(res["files_total"], 3)
        self.assertEqual(res["bytes_done"], 2150)
        self.assertIn("/Nintendo/Contents", res["excluded_dirs"])
        self.assertIn("/Nintendo/Album", res["excluded_dirs"])
        self.assertIn("/emuMMC", res["excluded_dirs"])
        self.assertIn("/backup", res["excluded_dirs"])
        self.assertIn("/install", res["excluded_dirs"])
        self.assertEqual(res["excluded_files"], 1)  # otros/suelto.xci
        self.assertTrue(os.path.basename(res["target"]).startswith("switch-sd-"))
        self._check_target(res["target"])

        db = QueueDB(os.path.join(self.tmp, "q.db"))
        run = BackupRun(self.settings, self.data_root)
        record_history(db, run, res)
        items, total = db.list_history("ftp")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["op"], "backup")
        db.close()

    def test_incremental_and_list_fallback(self) -> None:
        # Servidor sin MLSD (como algunos de la Switch): se usa LIST
        orig = ftplib.FTP.mlsd

        def no_mlsd(self, *a, **k):
            raise ftplib.error_perm("500 Unknown command")

        with mock.patch.object(ftplib.FTP, "mlsd", no_mlsd):
            res = self._run(incremental=True)
        self.assertEqual(res["status"], "OK", res.get("error"))
        self.assertTrue(res["target"].endswith("switch-sd"))
        self._check_target(res["target"])
        self.assertIs(ftplib.FTP.mlsd, orig)

        res2 = self._run(incremental=True)
        self.assertEqual(res2["files_skipped"], 3)

    def test_stop_and_missing_dest(self) -> None:
        stop = threading.Event()
        stop.set()
        res = BackupRun(self.settings, self.data_root, stop_event=stop).run()
        self.assertEqual(res["status"], "STOP")
        from app.switch_backup import BackupError

        with self.assertRaises(BackupError):
            BackupRun(dict(self.settings, ftp_backup_dir=""), self.data_root)


if __name__ == "__main__":
    unittest.main()
