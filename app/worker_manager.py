"""Gestor de workers en segundo plano para las colas de Mover y FTP."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .config import SettingsStore, get_data_root
from .db import QueueDB
from .ftp_worker import get_remote_free_space, process_ftp_item
from .mover_worker import evaluate_mover_space, process_mover_item
from .services import short_rel

# Suavizado de la velocidad instantánea (media móvil exponencial)
_SPEED_ALPHA = 0.3


class FtpSpaceCache:
    """Último espacio libre conocido del servidor FTP (la SD de la Switch)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"supported": None, "free_bytes": None, "method": "", "checked_at": None}

    def get(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)

    def refresh(self, settings: dict[str, Any], query: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
        res = (query or get_remote_free_space)(settings)
        data = {
            "supported": bool(res.get("supported")),
            "free_bytes": res.get("free_bytes"),
            "method": res.get("method", ""),
            "checked_at": time.time(),
        }
        if res.get("error"):
            data["error"] = res["error"]
        with self._lock:
            self._data = data
        return dict(data)


class _QueueRunner:
    """Ejecuta elementos de una cola ('mover' o 'ftp') de uno en uno en un hilo."""

    def __init__(
        self,
        kind: str,
        db: QueueDB,
        settings: SettingsStore,
        free_space_cb: Callable[[], int | None] | None = None,
    ) -> None:
        self.kind = kind
        self.db = db
        self.settings = settings
        self.free_space_cb = free_space_cb
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._progress: dict[str, Any] = {}
        self._run: dict[str, Any] = {}
        self._speed_sample: tuple[float, int] | None = None
        self._space_unsupported = False

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            self._progress = {}
            self._run = {"started_at": time.time(), "done_items": 0, "done_bytes": 0, "moved_bytes": 0}
            self._space_unsupported = False
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
            run = dict(self._run) if running else {}
        if not running:
            phase = "idle"
        elif progress.get("phase") == "transferring":
            phase = "transferring"
        else:
            phase = "preparing"
        current = None
        if running and progress.get("item_id"):
            total = int(progress.get("total_bytes") or 0)
            done = int(progress.get("transferred_bytes") or 0)
            speed = progress.get("speed_bps")
            eta = (total - done) / speed if speed and speed > 0 and total > done else None
            current = {
                "id": progress.get("item_id"),
                "name": progress.get("filename", ""),
                "rel_src": progress.get("rel_src", ""),
                "bytes_done": done,
                "total_bytes": total,
                "speed_bps": speed,
                "eta_s": eta,
                "percent": float(progress.get("percent") or 0.0),
            }
        return {
            "running": running,
            "stopping": running and self._stop_event.is_set(),
            "progress": {k: v for k, v in progress.items() if k not in ("phase", "speed_bps", "rel_src")},
            "phase": phase,
            "current": current,
            "_run": run,
        }

    def _on_progress(self, data: dict[str, Any]) -> None:
        now = time.monotonic()
        transferred = int(data.get("transferred_bytes") or 0)
        with self._lock:
            prev_speed = self._progress.get("speed_bps") if self._progress.get("item_id") == data.get("item_id") else None
            speed = prev_speed
            if self._speed_sample is not None:
                t0, b0 = self._speed_sample
                dt = now - t0
                if dt >= 0.5 and transferred >= b0:
                    inst = (transferred - b0) / dt
                    speed = inst if prev_speed is None else (_SPEED_ALPHA * inst + (1 - _SPEED_ALPHA) * prev_speed)
                    self._speed_sample = (now, transferred)
            else:
                self._speed_sample = (now, transferred)
            merged = dict(data)
            merged["phase"] = "transferring"
            merged["speed_bps"] = speed
            merged["rel_src"] = self._progress.get("rel_src", "")
            self._progress = merged

    def _free_space(self) -> int | None:
        """Espacio libre del servidor FTP antes de cada subida (None si no se sabe)."""
        if self.free_space_cb is None or self._space_unsupported:
            return None
        free = self.free_space_cb()
        if free is None:
            # El servidor no lo soporta: no volver a preguntar en esta ejecución
            self._space_unsupported = True
        return free

    def _process_item(self, item: dict[str, Any]) -> dict[str, Any]:
        """Procesa un elemento con el motor de su cola y devuelve su resultado."""
        cfg = self.settings.get()
        if self.kind == "mover":
            return process_mover_item(
                item=item,
                settings=cfg,
                progress_cb=self._on_progress,
                stop_event=self._stop_event,
                log_cb=self.db.add_log,
                has_pending_cleanup_cb=lambda d: self.db.has_pending_cleanup("mover", d),
            )
        else:
            return process_ftp_item(
                item=item,
                settings=cfg,
                progress_cb=self._on_progress,
                stop_event=self._stop_event,
                log_cb=self.db.add_log,
                free_space_cb=self._free_space if self.free_space_cb else None,
            )

    def _rel_src(self, item: dict[str, Any]) -> str:
        cfg = self.settings.get()
        base = cfg.get("ftp_local_root" if self.kind == "ftp" else "mover_origin", "")
        return short_rel(item.get("source_path", ""), [base, get_data_root()])

    def _run_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                item = self.db.next_pending_item(self.kind)
                if not item:
                    break

                started_at = time.time()
                self.db.update_item(item["id"], status="PROC", error="", note="")
                with self._lock:
                    self._speed_sample = None
                    self._progress = {
                        "item_id": item["id"],
                        "filename": item["source_path"].rsplit("/", 1)[-1],
                        "rel_src": self._rel_src(item),
                        "percent": 0.0,
                        "transferred_bytes": 0,
                        "total_bytes": item.get("size_bytes", 0),
                        "speed": "--",
                        "eta": "--",
                        "phase": "preparing",
                        "speed_bps": None,
                    }

                try:
                    result = self._process_item(item)
                except Exception as exc:
                    # Un fallo inesperado en un elemento no debe dejarlo en PROC ni parar la cola:
                    # se marca ERR (reintentable con 'e') y se continúa con el siguiente.
                    msg = f"{type(exc).__name__}: {exc}"
                    name = item["source_path"].rsplit("/", 1)[-1]
                    self.db.add_log(self.kind, "ERROR", f"Error inesperado procesando {name}: {msg}")
                    result = {"status": "ERR", "error": f"Error inesperado: {msg}", "resume": item.get("resume", 0)}

                update_fields: dict[str, Any] = {
                    "status": result.get("status", "ERR"),
                    "note": result.get("note", ""),
                    "error": result.get("error", ""),
                    "resume": int(result.get("resume", 0)),
                }
                if "retries_done" in result:
                    update_fields["retries_done"] = int(result["retries_done"])
                finished_at = time.time()
                if update_fields["status"] == "OK":
                    update_fields["finished_at"] = finished_at

                self.db.update_item(item["id"], **update_fields)
                self._record_history(item, update_fields, started_at, finished_at)
                with self._lock:
                    self._progress = {}
        except Exception as exc:  # pragma: no cover - fallos de la propia cola (p. ej. SQLite)
            self.db.add_log(self.kind, "ERROR", f"Excepción inesperada en worker {self.kind}: {exc}")
        finally:
            with self._lock:
                self._progress = {}
            self.db.add_log(self.kind, "INFO", f"Worker {self.kind.upper()} detenido")

    def _record_history(
        self, item: dict[str, Any], fields: dict[str, Any], started_at: float, finished_at: float
    ) -> None:
        status = fields["status"]
        if status not in ("OK", "ERR"):
            return  # Detenido o pendiente de reanudar: aún no ha terminado
        size = int(item.get("size_bytes") or 0)
        with self._lock:
            if self._run:
                self._run["done_items"] = int(self._run.get("done_items", 0)) + 1
                if status == "OK":
                    self._run["done_bytes"] = int(self._run.get("done_bytes", 0)) + size
        try:
            self.db.add_history(
                {
                    "item_id": item["id"],
                    "kind": self.kind,
                    "op": item.get("operation", ""),
                    "src": item.get("source_path", ""),
                    "dest": item.get("dest_path", ""),
                    "size": size,
                    "status": status,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "error": fields.get("error", ""),
                    "note": fields.get("note", ""),
                }
            )
        except Exception as exc:  # pragma: no cover - el historial nunca debe parar la cola
            self.db.add_log(self.kind, "WARN", f"No se pudo registrar en el historial: {exc}")


class WorkerManager:
    """Coordina los runners de Mover y FTP."""

    def __init__(self, db: QueueDB, settings: SettingsStore) -> None:
        self.db = db
        self.settings = settings
        self.ftp_space = FtpSpaceCache()
        self.runners = {
            "mover": _QueueRunner("mover", db, settings),
            "ftp": _QueueRunner("ftp", db, settings, free_space_cb=self._ftp_free_space),
        }

    def _ftp_free_space(self) -> int | None:
        data = self.ftp_space.refresh(self.settings.get())
        return data["free_bytes"] if data.get("supported") else None

    def refresh_ftp_space(self) -> dict[str, Any]:
        return self.ftp_space.refresh(self.settings.get())

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
            pending_items = 0
            for it in items:
                st = it["status"]
                counts[st] = counts.get(st, 0) + 1
                if st in ("PEND", "REAN", "PROC"):
                    pending_bytes += int(it.get("size_bytes") or 0)
                    pending_items += 1
            snap = runner.snapshot()
            run = snap.pop("_run", {})
            snap["counts"] = counts
            snap["total_items"] = len(items)
            snap["pending_bytes"] = pending_bytes
            snap["aggregate"] = _aggregate(run, snap.get("current"), pending_items, pending_bytes)
            if kind == "mover":
                default_dest = self.settings.get().get("mover_dest") or ""
                snap["space_check"] = evaluate_mover_space(items, default_dest)
            else:
                space = self.ftp_space.get()
                snap["remote_free_bytes"] = space.get("free_bytes") if space.get("supported") else None
                snap["remote_space"] = space
            res[kind] = snap
        return res


_MIN_BYTES_FOR_AVG_ETA = 16 * 1024 * 1024
_MAX_ETA_S = 30 * 24 * 3600


def _aggregate(
    run: dict[str, Any], current: dict[str, Any] | None, pending_items: int, pending_bytes: int
) -> dict[str, Any]:
    """Progreso global de la ejecución en curso (o de lo pendiente si no hay ninguna)."""
    done_items = int(run.get("done_items", 0)) if run else 0
    done_bytes = int(run.get("done_bytes", 0)) if run else 0
    cur_done = int(current.get("bytes_done", 0)) if current else 0
    total_items = done_items + pending_items
    total_bytes = done_bytes + pending_bytes
    done_total = done_bytes + cur_done
    eta = None
    remaining = total_bytes - done_total
    if run and run.get("started_at") and remaining > 0:
        # Preferimos la velocidad actual (suavizada) de la transferencia en curso; la media de la
        # ejecución solo es fiable cuando ya se ha movido un volumen razonable (con unos pocos bytes
        # copiados en segundos la media sale absurdamente baja y la ETA se dispara).
        speed = current.get("speed_bps") if current else None
        if speed and speed > 0:
            eta = remaining / speed
        else:
            elapsed = time.time() - float(run["started_at"])
            if elapsed > 1 and done_total >= _MIN_BYTES_FOR_AVG_ETA:
                eta = remaining / (done_total / elapsed)
        if eta is not None and eta > _MAX_ETA_S:
            eta = None
    return {
        "total_items": total_items,
        "done_items": done_items,
        "total_bytes": total_bytes,
        "done_bytes": done_total,
        "eta_s": eta,
    }
