"""Gestor de workers en segundo plano para las colas de Mover y FTP."""

from __future__ import annotations

import threading
import time
from typing import Any

from .config import SettingsStore
from .db import QueueDB
from .ftp_worker import process_ftp_item
from .mover_worker import process_mover_item


class _QueueRunner:
    """Ejecuta elementos de una cola ('mover' o 'ftp') de uno en uno en un hilo."""

    def __init__(self, kind: str, db: QueueDB, settings: SettingsStore) -> None:
        self.kind = kind
        self.db = db
        self.settings = settings
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._progress: dict[str, Any] = {}

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._progress = {}
            self._thread = threading.Thread(
                target=self._run_loop,
                name=f"worker-{self.kind}",
                daemon=True,
            )
            self._thread.start()
        self.db.add_log(self.kind, "INFO", f"Worker {self.kind.upper()} iniciado")
        return True

    def stop(self) -> bool:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False
            self._stop_event.set()
        self.db.add_log(self.kind, "INFO", f"Solicitada detención del worker {self.kind.upper()}")
        return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            progress = dict(self._progress) if running else {}
        return {
            "running": running,
            "stopping": running and self._stop_event.is_set(),
            "progress": progress,
        }

    def _on_progress(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._progress = data

    def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                item = self.db.next_pending_item(self.kind)
                if not item:
                    break

                self.db.update_item(item["id"], status="PROC", error="", note="")
                with self._lock:
                    self._progress = {
                        "item_id": item["id"],
                        "filename": item["source_path"].rsplit("/", 1)[-1],
                        "percent": 0.0,
                        "transferred_bytes": 0,
                        "total_bytes": item.get("size_bytes", 0),
                        "speed": "--",
                        "eta": "--",
                    }

                cfg = self.settings.get()
                if self.kind == "mover":
                    result = process_mover_item(
                        item=item,
                        settings=cfg,
                        progress_cb=self._on_progress,
                        stop_event=self._stop_event,
                        log_cb=self.db.add_log,
                        has_pending_cleanup_cb=lambda d: self.db.has_pending_cleanup("mover", d),
                    )
                else:
                    result = process_ftp_item(
                        item=item,
                        settings=cfg,
                        progress_cb=self._on_progress,
                        stop_event=self._stop_event,
                        log_cb=self.db.add_log,
                    )

                update_fields: dict[str, Any] = {
                    "status": result.get("status", "ERR"),
                    "note": result.get("note", ""),
                    "error": result.get("error", ""),
                    "resume": int(result.get("resume", 0)),
                }
                if "retries_done" in result:
                    update_fields["retries_done"] = int(result["retries_done"])
                if update_fields["status"] == "OK":
                    update_fields["finished_at"] = time.time()

                self.db.update_item(item["id"], **update_fields)
                with self._lock:
                    self._progress = {}
        except Exception as exc:  # pragma: no cover
            self.db.add_log(self.kind, "ERROR", f"Excepción inesperada en worker {self.kind}: {exc}")
        finally:
            with self._lock:
                self._progress = {}
            self.db.add_log(self.kind, "INFO", f"Worker {self.kind.upper()} detenido")


class WorkerManager:
    """Coordina los runners de Mover y FTP."""

    def __init__(self, db: QueueDB, settings: SettingsStore) -> None:
        self.db = db
        self.settings = settings
        self.runners = {
            "mover": _QueueRunner("mover", db, settings),
            "ftp": _QueueRunner("ftp", db, settings),
        }

    def start(self, kind: str) -> bool:
        return self.runners[kind].start()

    def stop(self, kind: str) -> bool:
        return self.runners[kind].stop()

    def stop_all(self) -> None:
        for r in self.runners.values():
            r.stop()

    def status(self) -> dict[str, Any]:
        res: dict[str, Any] = {}
        for kind, runner in self.runners.items():
            items = self.db.list_items(kind=kind)
            counts = {"PEND": 0, "REAN": 0, "PAUS": 0, "PROC": 0, "OK": 0, "ERR": 0}
            pending_bytes = 0
            for it in items:
                st = it["status"]
                counts[st] = counts.get(st, 0) + 1
                if st in ("PEND", "REAN", "PROC"):
                    pending_bytes += int(it.get("size_bytes") or 0)
            snap = runner.snapshot()
            snap["counts"] = counts
            snap["total_items"] = len(items)
            snap["pending_bytes"] = pending_bytes
            res[kind] = snap
        return res

