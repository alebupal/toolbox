"""Interfaz CLI y TUI unificada que comparte colas, ajustes y logs con la Web."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import SettingsStore, effective_trash_days, get_data_root, normalize_remote_path, resolve_safe_path
from .db import QueueDB
from .services import decorate_queue_items, plan_game_upload, search_files
from .services import list_directory as list_directory_entries
from .switch_utils import SWITCH_EXTENSIONS, detect_switch_type, scan_switch_ftp, switch_sort_key


def _fmt_bytes(num: Any) -> str:
    try:
        val = float(num)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


def _fmt_date(ts: Any) -> str:
    import datetime

    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def _fmt_duration(seconds: Any) -> str:
    try:
        s = int(float(seconds))
    except (TypeError, ValueError):
        return "--"
    if s < 0:
        return "--"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


SORT_ALIASES = {"nombre": "name", "name": "name", "fecha": "mtime", "mtime": "mtime", "tamano": "size", "tamaño": "size", "size": "size"}


def _parse_selection(expr: str, max_idx: int) -> list[int]:
    """Convierte expresiones tipo '1,3,5-7' en índices 1-based válidos sin duplicados."""
    result: list[int] = []
    seen: set[int] = set()
    for part in expr.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            bounds = part.split("-", 1)
            if len(bounds) == 2 and bounds[0].isdigit() and bounds[1].isdigit():
                a, b = int(bounds[0]), int(bounds[1])
                step = 1 if a <= b else -1
                for i in range(a, b + step, step):
                    if 1 <= i <= max_idx and i not in seen:
                        seen.add(i)
                        result.append(i)
        elif part.isdigit():
            i = int(part)
            if 1 <= i <= max_idx and i not in seen:
                seen.add(i)
                result.append(i)
    return result


class ToolboxClient:
    """Cliente que habla con la API Web local y hace fallback a SQLite si no está activa."""

    def __init__(self, base_url: str | None = None) -> None:
        port = os.environ.get("PORT", "8090")
        self.base_url = (base_url or f"http://127.0.0.1:{port}").rstrip("/")
        self._web_ok: bool | None = None

    def is_web_running(self) -> bool:
        if self._web_ok is None:
            try:
                self._http("GET", "/api/health", timeout=1.5)
                self._web_ok = True
            except Exception:
                self._web_ok = False
        return self._web_ok

    def _http(self, method: str, path: str, payload: Any = None, timeout: float = 15.0) -> Any:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("detail", body)
            except Exception:
                detail = body
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    def _local_db(self) -> QueueDB:
        return QueueDB()

    def _local_settings(self) -> SettingsStore:
        return SettingsStore()

    def get_status(self) -> dict[str, Any]:
        if self.is_web_running():
            return self._http("GET", "/api/status")
        db = self._local_db()
        try:
            from .mover_worker import evaluate_mover_space

            cfg = self._local_settings().get()
            workers = {}
            for kind in ("mover", "ftp"):
                items = db.list_items(kind=kind)
                counts = {"PEND": 0, "REAN": 0, "PAUS": 0, "PROC": 0, "OK": 0, "ERR": 0}
                pending_bytes = 0
                for it in items:
                    st = it["status"]
                    counts[st] = counts.get(st, 0) + 1
                    if st in ("PEND", "REAN", "PROC"):
                        pending_bytes += int(it.get("size_bytes") or 0)
                pending_items = counts["PEND"] + counts["REAN"] + counts["PROC"]
                snap: dict[str, Any] = {
                    "running": False,
                    "stopping": False,
                    "progress": {},
                    "phase": "idle",
                    "current": None,
                    "aggregate": {
                        "total_items": pending_items,
                        "done_items": 0,
                        "total_bytes": pending_bytes,
                        "done_bytes": 0,
                        "eta_s": None,
                    },
                    "counts": counts,
                    "total_items": len(items),
                    "pending_bytes": pending_bytes,
                }
                if kind == "mover":
                    snap["space_check"] = evaluate_mover_space(items, cfg.get("mover_dest") or "")
                else:
                    snap["remote_free_bytes"] = None
                workers[kind] = snap
            return {"workers": workers, "data_root": get_data_root()}
        finally:
            db.close()

    def get_settings(self) -> dict[str, Any]:
        if self.is_web_running():
            return self._http("GET", "/api/settings")
        return self._local_settings().get_public()

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        if self.is_web_running():
            return self._http("PUT", "/api/settings", patch)
        return self._local_settings().update(patch)

    def list_dir(
        self, path: str, kind: str, show_hidden: bool = False, sort: str = "name", order: str = "asc"
    ) -> dict[str, Any]:
        if self.is_web_running():
            qs = urllib.parse.urlencode(
                {
                    "kind": kind,
                    "path": path,
                    "show_hidden": "true" if show_hidden else "false",
                    "sort": sort,
                    "order": order,
                }
            )
            return self._http("GET", f"/api/fs/list?{qs}")
        data_root = get_data_root()
        cfg = self._local_settings().get()
        req_path = path or (cfg["ftp_local_root"] if kind == "ftp" else cfg["mover_origin"])
        safe = resolve_safe_path(req_path, data_root)
        if not os.path.isdir(safe):
            safe = data_root
        db = self._local_db()
        try:
            statuses = db.get_source_statuses(kind)
        finally:
            db.close()
        dirs, files = list_directory_entries(safe, data_root, statuses, show_hidden, sort, order)
        parent = os.path.dirname(safe) if safe != data_root else None
        return {
            "path": safe,
            "parent": parent,
            "data_root": data_root,
            "sort": sort,
            "order": order,
            "dirs": dirs,
            "files": files,
        }

    def search(self, query: str, path: str, kind: str, limit: int = 300) -> dict[str, Any]:
        if self.is_web_running():
            qs = urllib.parse.urlencode({"q": query, "path": path, "kind": kind, "limit": limit})
            return self._http("GET", f"/api/fs/search?{qs}", timeout=20.0)
        data_root = get_data_root()
        cfg = self._local_settings().get()
        start = path or (cfg["ftp_local_root"] if kind == "ftp" else cfg["mover_origin"])
        safe = resolve_safe_path(start, data_root)
        if not os.path.isdir(safe):
            raise ValueError(f"No es una carpeta: {safe}")
        return search_files(safe, query, data_root, limit=limit)

    def add_game(self, paths: list[str], dest_dir: str = "") -> dict[str, Any]:
        if self.is_web_running():
            return self._http(
                "POST", "/api/ftp/add-game", {"paths": paths, "dest_dir": dest_dir or None}, timeout=60.0
            )
        data_root = get_data_root()
        cfg = self._local_settings().get()
        db = self._local_db()
        try:
            active = {
                it["source_path"]
                for it in db.list_items(kind="ftp")
                if it["status"] in ("PEND", "REAN", "PAUS", "PROC")
            }
            plan = plan_game_upload(paths, data_root, dest_dir or cfg["ftp_remote_dir"], active)
            inserted = db.add_items(plan["entries"])
            if inserted:
                db.add_log("ftp", "INFO", f"[CLI] Juego completo añadido a la cola FTP ({len(inserted)} archivo(s))")
            return {
                "added": len(inserted),
                "items": decorate_queue_items(inserted, cfg, data_root),
                "warnings": plan["warnings"],
                "groups": plan["groups"],
            }
        finally:
            db.close()

    def get_history(self, kind: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        if self.is_web_running():
            qs = urllib.parse.urlencode({"kind": kind, "limit": limit, "offset": offset})
            return self._http("GET", f"/api/history?{qs}")
        db = self._local_db()
        try:
            items, total = db.list_history(kind=kind, limit=limit, offset=offset)
            return {"items": items, "total": total}
        finally:
            db.close()

    def clear_history(self, kind: str) -> int:
        if self.is_web_running():
            return int(self._http("DELETE", f"/api/history?kind={kind}").get("removed", 0))
        db = self._local_db()
        try:
            return db.clear_history(kind)
        finally:
            db.close()

    def ftp_space(self) -> dict[str, Any]:
        if self.is_web_running():
            return self._http("GET", "/api/ftp/space", timeout=20.0)
        from .ftp_worker import get_remote_free_space

        res = get_remote_free_space(self._local_settings().get())
        pending = sum(int(it.get("size_bytes") or 0) for it in self.get_queue("ftp", only_pending=True) if it["status"] != "PAUS")
        free = res.get("free_bytes") if res.get("supported") else None
        out = {
            "supported": bool(res.get("supported")),
            "free_bytes": free,
            "pending_bytes": pending,
            "enough": (free >= pending) if free is not None else None,
            "method": res.get("method", ""),
        }
        if res.get("error"):
            out["error"] = res["error"]
        return out

    # --- Backup de la microSD ------------------------------------------------
    def backup_status(self) -> dict[str, Any] | None:
        """Estado del backup de la web, o None si la web no responde."""
        if not self.is_web_running():
            return None
        return self._http("GET", "/api/ftp/backup")

    def backup_start(self, dest_dir: str = "", incremental: bool | None = None, extra_exclude: list[str] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"dest_dir": dest_dir or None, "incremental": incremental, "extra_exclude": extra_exclude or None}
        return self._http("POST", "/api/ftp/backup/start", payload, timeout=20.0)

    def backup_stop(self) -> dict[str, Any]:
        if not self.is_web_running():
            raise RuntimeError("El servidor web de Toolbox no está respondiendo.")
        return self._http("POST", "/api/ftp/backup/stop")

    def backup_foreground(
        self, dest_dir: str = "", incremental: bool | None = None, extra_exclude: list[str] | None = None
    ) -> dict[str, Any]:
        """Backup en primer plano (sin web): Ctrl+C lo detiene. Guarda log e historial en SQLite."""
        import threading

        from .switch_backup import BackupRun, record_history

        stop = threading.Event()
        db = self._local_db()
        try:
            run = BackupRun(
                self._local_settings().get(),
                get_data_root(),
                dest_dir=dest_dir or None,
                incremental=incremental,
                extra_exclude=extra_exclude,
                stop_event=stop,
                log_cb=db.add_log,
                progress_cb=lambda p: _print_backup_line(p, live=True),
            )
            print(f"Backup de la microSD -> {run.target}")
            import signal

            # Ctrl+C pide una parada limpia (el fichero a medias queda como .part y se reanuda la próxima vez)
            prev = signal.signal(signal.SIGINT, lambda *_a: stop.set())
            try:
                result = run.run()
            finally:
                signal.signal(signal.SIGINT, prev)
            record_history(db, run, result)
            return result
        finally:
            db.close()

    def _trash(self, db: QueueDB):
        from .trash import TrashManager

        return TrashManager(db, get_data_root())

    def list_trash(self) -> dict[str, Any]:
        if self.is_web_running():
            return self._http("GET", "/api/trash")
        db = self._local_db()
        try:
            cfg = self._local_settings().get()
            return {
                "items": self._trash(db).list(),
                "trash_days": int(cfg.get("trash_days", 7)),
                "trash_enabled": effective_trash_days(cfg) > 0,
            }
        finally:
            db.close()

    def restore_trash(self, ids: list[int]) -> list[dict[str, Any]]:
        if self.is_web_running():
            return self._http("POST", "/api/trash/restore", {"ids": ids}).get("restored", [])
        db = self._local_db()
        try:
            restored = self._trash(db).restore(ids)
            if restored:
                db.add_log("sistema", "INFO", f"[CLI] Restaurados {len(restored)} elemento(s) de la papelera")
            return restored
        finally:
            db.close()

    def empty_trash(self, ids: list[int] | None = None) -> int:
        if self.is_web_running():
            return int(self._http("POST", "/api/trash/empty", {"ids": ids}).get("removed", 0))
        db = self._local_db()
        try:
            removed = self._trash(db).empty(ids)
            if removed:
                db.add_log("sistema", "INFO", f"[CLI] Papelera: eliminados definitivamente {removed} elemento(s)")
            return removed
        finally:
            db.close()

    def list_ftp_dirs(self, remote_path: str) -> dict[str, Any]:
        if self.is_web_running():
            qs = urllib.parse.urlencode({"path": remote_path})
            return self._http("GET", f"/api/ftp/dirs?{qs}", timeout=20.0)
        from .ftp_worker import list_remote_dirs

        return list_remote_dirs(self._local_settings().get(), remote_path)

    def get_queue(self, kind: str, only_pending: bool = False) -> list[dict[str, Any]]:
        if self.is_web_running():
            qs = urllib.parse.urlencode(
                {"kind": kind, "only_pending": "true" if only_pending else "false"}
            )
            return self._http("GET", f"/api/queue?{qs}").get("items", [])
        db = self._local_db()
        try:
            items = db.list_items(kind=kind, only_pending=only_pending)
            return decorate_queue_items(items, self._local_settings().get(), get_data_root())
        finally:
            db.close()

    def add_paths(
        self,
        kind: str,
        paths: list[str],
        recursive: bool = True,
        only_switch: bool = False,
        base_browse_dir: str = "",
        operation: str | None = None,
        dest_dir: str = "",
    ) -> dict[str, Any]:
        if self.is_web_running():
            return self._http(
                "POST",
                "/api/queue/add",
                {
                    "kind": kind,
                    "paths": paths,
                    "recursive": recursive,
                    "only_switch": only_switch,
                    "base_browse_dir": base_browse_dir,
                    "operation": operation,
                    "dest_dir": dest_dir,
                },
                timeout=30.0,
            )
        # Fallback local sin FastAPI: encola directamente en SQLite con la misma lógica
        from .mover_worker import file_signature

        data_root = get_data_root()
        cfg = self._local_settings().get()
        db = self._local_db()
        try:
            entries: list[dict[str, Any]] = []
            if kind == "mover":
                op = operation or cfg["mover_operation"]
                base_origin = resolve_safe_path(base_browse_dir or cfg["mover_origin"], data_root)
                chosen_dest = resolve_safe_path(dest_dir, data_root) if dest_dir.strip() else None
                auto_dest = cfg["mover_dest"]
                keep_struct = bool(cfg["mover_keep_structure"])
            else:
                op = "subir"
                base_origin = cfg["ftp_local_root"]
                chosen_dest = normalize_remote_path(dest_dir) if dest_dir.strip() else None
                auto_dest = normalize_remote_path(cfg["ftp_remote_dir"])
                keep_struct = bool(cfg["ftp_keep_structure"])

            for raw_p in paths:
                safe_p = resolve_safe_path(raw_p, data_root)
                if os.path.isdir(safe_p) and recursive:
                    folder_name = os.path.basename(safe_p)
                    for dirpath, dirnames, filenames in os.walk(safe_p, followlinks=False):
                        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                        for fn in sorted(filenames):
                            if fn.startswith("."):
                                continue
                            fpath = os.path.join(dirpath, fn)
                            if not os.path.isfile(fpath):
                                continue
                            if only_switch and not fpath.lower().endswith(SWITCH_EXTENSIONS):
                                continue
                            rel_inside = os.path.relpath(fpath, safe_p)
                            target_base = chosen_dest if chosen_dest else auto_dest
                            if kind == "mover":
                                dest_p = (
                                    os.path.join(target_base, folder_name, rel_inside)
                                    if (chosen_dest or keep_struct)
                                    else os.path.join(target_base, fn)
                                )
                            else:
                                dest_p = normalize_remote_path(
                                    f"{target_base}/{folder_name}/{rel_inside.replace(os.sep, '/')}"
                                )
                            entries.append(
                                {
                                    "kind": kind,
                                    "operation": op,
                                    "source_path": fpath,
                                    "dest_path": dest_p,
                                    "size_bytes": os.path.getsize(fpath),
                                    "source_sig": file_signature(fpath),
                                    "cleanup_dir": safe_p if (kind == "mover" and op == "mover") else "",
                                    "switch_type": detect_switch_type(fpath),
                                }
                            )
                elif os.path.isfile(safe_p):
                    if only_switch and not safe_p.lower().endswith(SWITCH_EXTENSIONS):
                        continue
                    filename = os.path.basename(safe_p)
                    if kind == "mover":
                        if chosen_dest:
                            dest_p = os.path.join(chosen_dest, filename)
                        elif keep_struct:
                            try:
                                rel = os.path.relpath(safe_p, base_origin)
                            except ValueError:
                                rel = filename
                            dest_p = os.path.join(auto_dest, filename if rel.startswith("..") else rel)
                        else:
                            dest_p = os.path.join(auto_dest, filename)
                    else:
                        if chosen_dest:
                            dest_p = normalize_remote_path(f"{chosen_dest}/{filename}")
                        elif keep_struct:
                            try:
                                rel = os.path.relpath(safe_p, base_origin)
                            except ValueError:
                                rel = filename
                            dest_p = normalize_remote_path(
                                f"{auto_dest}/{filename if rel.startswith('..') else rel.replace(os.sep, '/')}"
                            )
                        else:
                            dest_p = normalize_remote_path(f"{auto_dest}/{filename}")
                    entries.append(
                        {
                            "kind": kind,
                            "operation": op,
                            "source_path": safe_p,
                            "dest_path": dest_p,
                            "size_bytes": os.path.getsize(safe_p),
                            "source_sig": file_signature(safe_p),
                            "cleanup_dir": "",
                            "switch_type": detect_switch_type(safe_p),
                        }
                    )

            inserted = db.add_items(entries)
            if inserted:
                db.add_log(kind, "INFO", f"[CLI] Añadidos {len(inserted)} archivo(s) a la cola de {kind}")
                if kind == "ftp":
                    all_ftp = db.list_items(kind="ftp")
                    db.reorder_items("ftp", [it["id"] for it in sorted(all_ftp, key=switch_sort_key)])
            return {"added": len(inserted), "skipped": len(entries) - len(inserted), "items": inserted}
        finally:
            db.close()

    def start_queue(self, kind: str) -> dict[str, Any]:
        if not self.is_web_running():
            raise RuntimeError(
                "El servidor web de Toolbox no está respondiendo; inicia el contenedor o usa --legacy."
            )
        return self._http("POST", f"/api/queue/{kind}/start")

    def stop_queue(self, kind: str) -> dict[str, Any]:
        if not self.is_web_running():
            raise RuntimeError("El servidor web de Toolbox no está respondiendo.")
        return self._http("POST", f"/api/queue/{kind}/stop")

    def auto_order(self, kind: str) -> None:
        if self.is_web_running():
            self._http("POST", f"/api/queue/{kind}/auto-order")
            return
        db = self._local_db()
        try:
            items = db.list_items(kind=kind)
            ordered = (
                sorted(items, key=switch_sort_key)
                if kind == "ftp"
                else sorted(
                    items,
                    key=lambda it: (
                        0 if it["status"] in ("PROC", "PEND", "REAN", "PAUS") else (1 if it["status"] == "ERR" else 2),
                        it["source_path"].lower(),
                    ),
                )
            )
            db.reorder_items(kind, [it["id"] for it in ordered])
        finally:
            db.close()

    def retry_errors(self, kind: str) -> int:
        if self.is_web_running():
            return int(self._http("POST", f"/api/queue/{kind}/retry-errors").get("retried", 0))
        db = self._local_db()
        try:
            return db.retry_errors(kind)
        finally:
            db.close()

    def clear_queue(self, kind: str, scope: str) -> int:
        if self.is_web_running():
            return int(self._http("POST", f"/api/queue/{kind}/clear?scope={scope}").get("removed", 0))
        mapping = {"completed": ("OK",), "errors": ("ERR",), "pending": ("PEND", "REAN", "PAUS")}
        db = self._local_db()
        try:
            return db.clear_by_status(kind, mapping[scope])
        finally:
            db.close()

    def item_action(self, item_id: int, action: str, direction: str | None = None) -> None:
        if self.is_web_running():
            if action == "pause":
                self._http("POST", f"/api/queue/item/{item_id}/pause")
            elif action == "toggle-op":
                self._http("POST", f"/api/queue/item/{item_id}/toggle-op")
            elif action == "move" and direction:
                self._http("POST", f"/api/queue/item/{item_id}/move", {"direction": direction})
            elif action == "delete":
                self._http("DELETE", f"/api/queue/item/{item_id}")
            return
        db = self._local_db()
        try:
            if action == "pause":
                db.toggle_pause(item_id)
            elif action == "toggle-op":
                db.toggle_operation(item_id)
            elif action == "move" and direction:
                db.move_position(item_id, direction)
            elif action == "delete":
                db.remove_item(item_id)
        finally:
            db.close()

    def get_logs(self, kind: str = "todos", limit: int = 40) -> list[dict[str, Any]]:
        if self.is_web_running():
            qs = urllib.parse.urlencode({"kind": kind, "limit": limit})
            return self._http("GET", f"/api/logs?{qs}").get("logs", [])
        db = self._local_db()
        try:
            return db.get_logs(kind=kind, limit=limit)
        finally:
            db.close()

    def test_ftp(self) -> dict[str, Any]:
        if self.is_web_running():
            return self._http("POST", "/api/ftp/test", timeout=25.0)
        from .ftp_worker import test_ftp_connection

        return test_ftp_connection(self._local_settings().get())

    def scan_switch(self) -> dict[str, Any]:
        if self.is_web_running():
            return self._http("POST", "/api/ftp/scan-switch", {}, timeout=35.0)
        cfg = self._local_settings().get()
        pto = int(cfg.get("ftp_port") or 5000)
        ports = [pto, 5000] if pto not in (21, 5000) else [5000, 2121, 21]
        return scan_switch_ftp(ports=ports)

    def dir_size(self, path: str) -> dict[str, Any]:
        if self.is_web_running():
            qs = urllib.parse.urlencode({"path": path})
            return self._http("GET", f"/api/fs/dir-size?{qs}", timeout=60.0)
        from .mover_worker import calculate_dir_size

        return calculate_dir_size(path, get_data_root())

    def rename_entry(self, path: str, new_name: str) -> str:
        if self.is_web_running():
            return self._http("POST", "/api/fs/rename", {"path": path, "new_name": new_name}).get("path", "")
        from .mover_worker import rename_entry

        new_path = rename_entry(path, new_name, get_data_root())
        db = self._local_db()
        try:
            db.add_log("sistema", "INFO", f"[CLI] Renombrado: {path} -> {new_path}")
        finally:
            db.close()
        return new_path

    def trash_active(self) -> bool:
        """True si los borrados van a la papelera (activada y con días > 0)."""
        return effective_trash_days(self.get_settings()) > 0

    def set_trash_enabled(self, enabled: bool) -> bool:
        self.update_settings({"trash_enabled": bool(enabled)})
        return self.trash_active()

    def delete_entries(self, paths: list[str], permanent: bool = False) -> dict[str, Any]:
        """Envía a la papelera (o borra definitivamente). Devuelve {trashed:[...], deleted:N}."""
        if self.is_web_running():
            return self._http("POST", "/api/fs/delete", {"paths": paths, "permanent": permanent})
        db = self._local_db()
        try:
            days = effective_trash_days(self._local_settings().get())
            res = self._trash(db).delete(paths, days, permanent=permanent)
            if res["trashed"]:
                db.add_log("sistema", "INFO", f"[CLI] Enviados {len(res['trashed'])} elemento(s) a la papelera")
            if res["deleted"]:
                db.add_log("sistema", "INFO", f"[CLI] Eliminados definitivamente {res['deleted']} elemento(s)")
            return res
        finally:
            db.close()

    def change_dest_dir(self, item_ids: list[int], dest_dir: str) -> int:
        if self.is_web_running():
            return int(
                self._http("POST", "/api/queue/change-dest", {"item_ids": item_ids, "dest_dir": dest_dir}).get(
                    "updated", 0
                )
            )
        db = self._local_db()
        try:
            updated = db.change_dest_dir(item_ids, dest_dir, get_data_root())
            if updated:
                db.add_log("sistema", "INFO", f"[CLI] Destino actualizado a '{dest_dir}' en {updated} elemento(s)")
            return updated
        finally:
            db.close()


def _print_status_summary(client: ToolboxClient, kind: str) -> None:
    st = client.get_status()["workers"][kind]
    c = st.get("counts", {})
    pend = c.get("PEND", 0) + c.get("REAN", 0) + c.get("PROC", 0)
    state = "DETENIENDO" if st.get("stopping") else ("EN EJECUCIÓN" if st.get("running") else "DETENIDO")
    title = "MOVER / COPIAR" if kind == "mover" else "FTP / SWITCH"
    sc = st.get("space_check") or {}
    free_str = (
        f" | Libre dest: {_fmt_bytes(sc['free_bytes'])}"
        if (kind == "mover" and sc.get("free_bytes") is not None)
        else ""
    )
    print(f"=== {title} (Cola compartida Web + SSH) ===")
    print(
        f"  Estado: {state} | Pendientes: {pend} ({_fmt_bytes(st.get('pending_bytes', 0))}) "
        f"| Pausados: {c.get('PAUS', 0)} | OK: {c.get('OK', 0)} | ERR: {c.get('ERR', 0)}{free_str}"
    )
    if kind == "mover" and sc.get("space_warning"):
        print(
            f"  ⚠ AVISO ESPACIO: Se requieren {_fmt_bytes(sc.get('needed_bytes', 0))} "
            f"pero solo quedan {_fmt_bytes(sc.get('free_bytes', 0))} libres en destino "
            f"(faltan {_fmt_bytes(sc.get('shortfall_bytes', 0))})"
        )
    if kind == "ftp":
        free_sd = st.get("remote_free_bytes")
        if free_sd is not None:
            warn = "  ⚠ NO CABE lo pendiente" if free_sd < int(st.get("pending_bytes") or 0) else ""
            print(f"  Espacio libre en la SD: {_fmt_bytes(free_sd)}{warn}")
        else:
            print("  Espacio libre en la SD: desconocido (consulta con: subeftp --espacio)")
    phase = st.get("phase") or ("transferring" if st.get("running") else "idle")
    cur = st.get("current") or {}
    if st.get("running"):
        if phase == "preparing" or not cur:
            name = cur.get("name") or (st.get("progress") or {}).get("filename", "")
            print(f"  Fase: PREPARANDO {name}".rstrip())
        else:
            speed = f"{_fmt_bytes(cur['speed_bps'])}/s" if cur.get("speed_bps") else "--"
            print(
                f"  Fase: TRANSFIRIENDO {cur.get('rel_src') or cur.get('name', '')} -> "
                f"{float(cur.get('percent', 0)):.1f}% ({_fmt_bytes(cur.get('bytes_done'))}/"
                f"{_fmt_bytes(cur.get('total_bytes'))}) | {speed} | ETA {_fmt_duration(cur.get('eta_s'))}"
            )
        agg = st.get("aggregate") or {}
        if agg.get("total_items"):
            print(
                f"  Global: {agg.get('done_items', 0)}/{agg.get('total_items', 0)} archivo(s) · "
                f"{_fmt_bytes(agg.get('done_bytes', 0))}/{_fmt_bytes(agg.get('total_bytes', 0))} · "
                f"ETA total {_fmt_duration(agg.get('eta_s'))}"
            )


def _print_queue_table(items: list[dict[str, Any]]) -> None:
    if not items:
        print("  (cola vacía)")
        return
    for idx, it in enumerate(items, 1):
        sw = f"[{it['switch_type']}] " if it.get("switch_type") else ""
        err = f" | ERR: {it['error']}" if it.get("error") else ""
        src = it.get("rel_src") or it["source_path"]
        dst = it.get("rel_dest") or it["dest_path"]
        print(
            f"  {idx:3d}. [#{it['id']}] [{it['status']:4s}] [{it['operation'].upper():5s}] "
            f"{sw}{src} -> {dst} ({_fmt_bytes(it.get('size_bytes'))}){err}"
        )


def _print_listing(listing: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Imprime un listado de carpeta con fecha de modificación y devuelve las entradas numeradas."""
    entries: list[tuple[str, dict[str, Any]]] = [("dir", d) for d in listing["dirs"]] + [
        ("file", f) for f in listing["files"]
    ]
    if not entries:
        print("  (carpeta vacía)")
    for idx, (etype, obj) in enumerate(entries, 1):
        date = _fmt_date(obj.get("mtime"))
        if etype == "dir":
            print(f"  {idx:3d}) {date}  [DIR ]           {obj['name']}/")
        else:
            sw = f"[{obj['switch_type']}] " if obj.get("switch_type") else ""
            print(f"  {idx:3d}) {date}  {_fmt_bytes(obj.get('size_bytes')):>10s}  {sw}{obj['name']}")
    return entries


def _print_search(res: dict[str, Any]) -> None:
    results = res.get("results", [])
    print(f"Búsqueda de '{res.get('query', '')}' en {res.get('path', '')}: {len(results)} resultado(s)")
    for idx, r in enumerate(results, 1):
        kind_lbl = "[DIR ]" if r.get("is_dir") else f"{_fmt_bytes(r.get('size')):>10s}"
        sw = f"[{r['switch_type']}] " if r.get("switch_type") else ""
        print(f"  {idx:3d}) {_fmt_date(r.get('mtime'))}  {kind_lbl}  {sw}{r['rel']}")
    if res.get("truncated"):
        print("  (resultados truncados: afina la búsqueda o elige una carpeta más concreta)")


def _print_game_result(res: dict[str, Any]) -> None:
    for g in res.get("groups", []):
        tid = f" [{g['title_id']}]" if g.get("title_id") else ""
        base = "Base ✓" if g.get("base") else "SIN BASE"
        print(f"  • {g['name']}{tid}: {base} · {g.get('upd', 0)} update(s) · {g.get('dlc', 0)} DLC")
    for w in res.get("warnings", []):
        print(f"  ⚠ {w}")
    print(f"Añadidos a la cola FTP: {res.get('added', 0)} archivo(s) en orden Base → Update → DLC")


def _print_history(data: dict[str, Any]) -> None:
    items = data.get("items", [])
    if not items:
        print("  (historial vacío)")
        return
    for h in items:
        speed = f"{_fmt_bytes(h['avg_bps'])}/s" if h.get("avg_bps") else "--"
        err = f" | {h['error']}" if h.get("error") else ""
        name = os.path.basename(h.get("src") or "")
        print(
            f"  {_fmt_date(h.get('finished_at'))}  [{h['kind'].upper():5s}] [{h['status']:3s}] "
            f"{name} ({_fmt_bytes(h.get('size'))}) · {_fmt_duration(h.get('duration_s'))} · {speed}{err}"
        )
    print(f"  Mostrando {len(items)} de {data.get('total', len(items))} registro(s)")


def _print_trash(data: dict[str, Any]) -> None:
    items = data.get("items", [])
    days = data.get("trash_days")
    if data.get("trash_enabled", True):
        print(f"Papelera ACTIVADA ({len(items)} elemento(s); se conservan {days} día(s))")
    else:
        print(
            f"Papelera DESACTIVADA: los borrados son definitivos ({len(items)} elemento(s) antiguos; "
            "activar con --papelera-activar)"
        )
    if not items:
        print("  (papelera vacía)")
    for t in items:
        kind_lbl = "[DIR]" if t.get("is_dir") else "     "
        print(
            f"  #{t['id']:<5d} {kind_lbl} {t['original_path']} ({_fmt_bytes(t.get('size'))}) · "
            f"borrado {_fmt_date(t.get('trashed_at'))} · caduca {_fmt_date(t.get('expires_at'))}"
        )


def _print_ftp_space(res: dict[str, Any]) -> int:
    pending = _fmt_bytes(res.get("pending_bytes", 0))
    if not res.get("supported"):
        extra = f" ({res['error']})" if res.get("error") else ""
        print(f"El servidor FTP no informa del espacio libre{extra}. Pendiente de subir: {pending}")
        return 0 if not res.get("error") else 1
    free = _fmt_bytes(res.get("free_bytes"))
    if res.get("enough"):
        print(f"Espacio libre en la SD: {free} (método {res.get('method')}). Pendiente: {pending} → cabe.")
    else:
        print(f"Espacio libre en la SD: {free} (método {res.get('method')}). Pendiente: {pending} → ⚠ NO CABE.")
    return 0


_PHASES = {"scanning": "Analizando la SD", "downloading": "Copiando", "done": "Terminado"}


def _print_backup_line(p: dict[str, Any], live: bool = False) -> None:
    phase = _PHASES.get(p.get("phase", ""), p.get("phase", ""))
    total_b = int(p.get("bytes_total") or 0)
    done_b = int(p.get("bytes_done") or 0)
    if p.get("phase") == "scanning":
        text = f"{phase}… {p.get('files_total', 0)} fichero(s), {_fmt_bytes(total_b)} · {p.get('current', '')}"
    else:
        pct = (done_b * 100.0 / total_b) if total_b else 100.0
        text = (
            f"{phase}: {pct:5.1f}% · {p.get('files_done', 0)}/{p.get('files_total', 0)} fichero(s) · "
            f"{_fmt_bytes(done_b)}/{_fmt_bytes(total_b)} · {p.get('current', '')}"
        )
    if live and sys.stdout.isatty():
        width = 120
        try:
            width = os.get_terminal_size().columns - 1
        except OSError:
            pass
        print("\r" + text[:width].ljust(width), end="", flush=True)
    elif not live:
        print("  " + text)


def _print_backup_result(p: dict[str, Any]) -> int:
    if sys.stdout.isatty():
        print()
    st = p.get("status", "")
    label = {"OK": "✓ Backup completado", "ERR": "⚠ Backup con errores", "STOP": "⏹ Backup detenido"}.get(st, st)
    print(f"{label}: {p.get('files_done', 0)}/{p.get('files_total', 0)} fichero(s), {_fmt_bytes(p.get('bytes_done', 0))}")
    print(f"  Carpeta: {p.get('target', '')}")
    if p.get("files_skipped"):
        print(f"  Sin cambios (ya estaban): {p['files_skipped']} fichero(s)")
    exc_dirs = p.get("excluded_dirs") or []
    if exc_dirs:
        print(f"  Carpetas de juegos excluidas ({len(exc_dirs)}): {', '.join(exc_dirs[:8])}{' …' if len(exc_dirs) > 8 else ''}")
    if p.get("excluded_files"):
        print(f"  Ficheros de juego excluidos: {p['excluded_files']} ({_fmt_bytes(p.get('excluded_bytes', 0))})")
    if p.get("error"):
        print(f"  Motivo: {p['error']}")
    for err in (p.get("errors") or [])[:10]:
        print(f"    - {err}")
    return 0 if st == "OK" else 1


def _print_backup_status(client: ToolboxClient) -> int:
    st = client.backup_status()
    if st is None:
        print("La web no responde: no hay backup en segundo plano. Lánzalo con: subeftp --backup")
        return 0
    if st.get("running") and st.get("progress"):
        print("Backup de la microSD EN MARCHA" + (" (deteniéndose)" if st.get("stopping") else ""))
        _print_backup_line(st["progress"])
        print(f"  Carpeta: {st['progress'].get('target', '')}")
        return 0
    if st.get("last"):
        print("Último backup de la microSD:")
        return _print_backup_result_quiet(st["last"])
    print("No se ha hecho ningún backup desde que arrancó la web (ver: subeftp --historial).")
    return 0


def _print_backup_result_quiet(p: dict[str, Any]) -> int:
    print(f"  {_fmt_date(p.get('finished_at'))}  [{p.get('status', '')}]  {p.get('files_done', 0)}/{p.get('files_total', 0)} fichero(s), "
          f"{_fmt_bytes(p.get('bytes_done', 0))} -> {p.get('target', '')}")
    if p.get("error"):
        print(f"  Motivo: {p['error']}")
    return 0


def _follow_backup(client: ToolboxClient) -> int:
    """Sigue el backup de la web hasta que termine. Ctrl+C deja de mirar (el backup continúa)."""
    try:
        while True:
            st = client.backup_status() or {}
            if st.get("running") and st.get("progress"):
                _print_backup_line(st["progress"], live=True)
                time.sleep(1.0)
                continue
            if st.get("last"):
                return _print_backup_result(st["last"])
            return 0
    except KeyboardInterrupt:
        print("\nSigue en segundo plano (consulta: subeftp --backup-estado · detener: subeftp --backup-parar).")
        return 0


def _run_backup_cli(client: ToolboxClient, args: list[str]) -> int:
    dest = ""
    incremental: bool | None = None
    follow = False
    extra: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-d", "--destino") and i + 1 < len(args):
            dest = args[i + 1]
            i += 2
            continue
        if a in ("-x", "--excluir") and i + 1 < len(args):
            extra.append(args[i + 1])
            i += 2
            continue
        if a.startswith("--destino="):
            dest = a.split("=", 1)[1]
        elif a.startswith("--excluir="):
            extra.append(a.split("=", 1)[1])
        elif a in ("-n", "--incremental"):
            incremental = True
        elif a == "--completo":
            incremental = False
        elif a in ("-f", "--seguir", "--esperar"):
            follow = True
        else:
            print(f"Opción no reconocida en --backup: {a}", file=sys.stderr)
            return 1
        i += 1
    if dest:
        dest = str(Path(dest).expanduser().resolve())
    if client.is_web_running():
        st = client.backup_start(dest, incremental, extra)
        target = (st.get("progress") or {}).get("target", "")
        print(f"Backup de la microSD iniciado en segundo plano -> {target}")
        if follow or sys.stdout.isatty():
            return _follow_backup(client)
        print("Consulta el progreso con: subeftp --backup-estado")
        return 0
    print("(La web no responde: el backup se hace en primer plano; Ctrl+C lo detiene.)")
    return _print_backup_result(client.backup_foreground(dest, incremental, extra))


def _report_delete(res: dict[str, Any]) -> None:
    trashed = res.get("trashed", [])
    if trashed:
        ids = ",".join(str(t["id"]) for t in trashed)
        print(f"Enviados a la papelera: {len(trashed)} (restaurar con --restaurar {ids})")
    if res.get("deleted"):
        print(f"Eliminados definitivamente: {res['deleted']}")


def _ask_yes_no(prompt: str, default_yes: bool = True) -> bool:
    suffix = "[S/n]" if default_yes else "[s/N]"
    try:
        ans = input(f"{prompt} {suffix}: ").strip().lower()
    except EOFError:
        return default_yes
    if not ans:
        return default_yes
    return ans in ("s", "si", "sí", "y", "yes")


def _print_help(kind: str) -> None:
    if kind == "mover":
        print(
            """mover (modo unificado Web + SSH)

Uso:
  mover                               Abre el menú interactivo TUI compartido con la Web
  mover [OPCIONES] [RUTAS...]         Añade rutas a la cola compartida
  mover --iniciar | --parar           Arranca o detiene el worker en segundo plano
  mover --estado | --pendientes       Estado, fase, progreso global, espacio libre y cola
  mover --listar [RUTA] [--orden nombre|fecha|tamano] [--desc]
                                      Lista una carpeta con fecha de modificación
  mover --buscar TEXTO [RUTA]         Busca recursivamente (sin distinguir mayúsculas ni acentos)
  mover --cambiar-destino IDs RUTA    Cambia destino de ítems en cola (ej: 1,2 /host/mnt/pelis)
  mover --tamano RUTA                 Calcula tamaño total y nº de archivos de una carpeta
  mover --renombrar RUTA NUEVO_NOMBRE Renombra un archivo o carpeta del servidor
  mover --borrar [-y] [--permanente] RUTA...
                                      Envía a la papelera (o borra definitivamente)
  mover --papelera                    Lista la papelera con sus IDs
  mover --restaurar ID[,ID...]        Restaura elementos de la papelera
  mover --vaciar-papelera [-y] [ID,...]
                                      Borra definitivamente toda la papelera (o esos IDs)
  mover --papelera-activar | --papelera-desactivar
                                      Activa/desactiva la papelera (desactivada = borrado definitivo)
  mover --historial [N]               Últimas N transferencias terminadas (def. 30)
  mover --limpiar-historial           Vacía el historial de mover
  mover --log [N]                     Últimas N líneas del registro compartido (def. 40)
  mover --legacy [...]                Usa el script Bash clásico aislado (/config/cli/mover)

Flags al añadir:
  -c, --copiar                        Operación copiar (mantiene origen)
  -m, --mover                         Operación mover (borra origen tras verificar)
  -d, --destino RUTA                  Carpeta destino explícita
  -i, --iniciar (-s, --ahora)         Arranca en segundo plano de inmediato sin preguntar"""
        )
    else:
        print(
            """subeftp (modo unificado Web + SSH)

Uso:
  subeftp                             Abre el menú interactivo TUI compartido con la Web
  subeftp [OPCIONES] [RUTAS...]       Añade rutas a la cola FTP compartida con la Web
  subeftp --subir | --parar           Arranca o detiene las subidas en segundo plano
  subeftp --estado | --pendientes     Estado, fase, progreso global, espacio SD y cola FTP
  subeftp --juego RUTA... [-d DEST] [-s]
                                      Encola juego(s) completo(s): Base -> Update -> DLC
  subeftp --espacio                   Consulta el espacio libre en la SD (si el servidor lo soporta)
  subeftp --backup [-d CARPETA] [-n|--completo] [-x PATRON]... [-f]
                                      Copia la microSD al servidor SIN las carpetas de juegos
                                      (-n incremental en CARPETA/switch-sd, -f espera a que termine)
  subeftp --backup-estado | --backup-parar
                                      Progreso / detención del backup de la microSD
  subeftp --listar [RUTA] [--orden nombre|fecha|tamano] [--desc]
                                      Lista una carpeta local con fecha de modificación
  subeftp --buscar TEXTO [RUTA]       Busca recursivamente en las carpetas locales
  subeftp --cambiar-destino IDs RUTA  Cambia carpeta remota en cola (ej: 1,2 /install)
  subeftp --tamano RUTA               Calcula tamaño total de una carpeta local
  subeftp --renombrar RUTA NUEVO_NOM  Renombra un archivo o carpeta local
  subeftp --borrar [-y] [--permanente] RUTA...
                                      Envía a la papelera (o borra definitivamente)
  subeftp --papelera | --restaurar ID[,ID...] | --vaciar-papelera [-y] [ID,...]
  subeftp --papelera-activar | --papelera-desactivar
                                      Activa/desactiva la papelera (desactivada = borrado definitivo)
  subeftp --historial [N]             Últimas N subidas terminadas (def. 30)
  subeftp --limpiar-historial         Vacía el historial FTP
  subeftp --probar                    Prueba la conexión FTP configurada
  subeftp --buscar-switch             Escanea la red LAN buscando Nintendo Switch
  subeftp --log [N]                   Últimas N líneas del registro compartido (def. 40)
  subeftp --legacy [...]              Usa el script Bash clásico aislado (/config/cli/ftp)

Flags al añadir:
  -d, --destino RUTA                  Carpeta remota destino (ej: -d /install)
  -j, --solo-switch                   Solo .nsp, .nsz, .xci, .xcz, .nro
  -s, --subir (-i, --ahora)           Arranca la subida de inmediato sin preguntar"""
        )


def _interactive_explore(client: ToolboxClient, kind: str) -> None:
    cfg = client.get_settings()
    cur_path = cfg.get("ftp_local_root" if kind == "ftp" else "mover_origin") or ""
    op = cfg.get("mover_operation", "copiar")
    dest_dir = ""
    text_filter = ""
    sort, order = "name", "asc"

    while True:
        listing = client.list_dir(cur_path, kind, sort=sort, order=order)
        cur_path = listing["path"]
        dirs = [d for d in listing["dirs"] if not text_filter or text_filter in d["name"].lower()]
        files = [f for f in listing["files"] if not text_filter or text_filter in f["name"].lower()]
        entries: list[tuple[str, dict[str, Any]]] = [("dir", d) for d in dirs] + [("file", f) for f in files]

        print("\n" + "=" * 76)
        print(f"EXPLORADOR ({kind.upper()} · Cola Web+SSH) | Origen: {cur_path}")
        if kind == "mover":
            print(f"Operación: {op.upper()} | Destino: {dest_dir or '(automático de Configuración)'}")
        else:
            print(f"Destino FTP: {dest_dir or cfg.get('ftp_remote_dir', '/')} (automático si vacío)")
        sort_lbl = {"name": "nombre", "mtime": "fecha", "size": "tamaño"}[sort]
        print(f"Orden: {sort_lbl} {'descendente' if order == 'desc' else 'ascendente'}")
        if text_filter:
            print(f"Filtro activo: '{text_filter}' (escribe * para quitar)")
        print("-" * 76)
        if not entries:
            print("  (carpeta vacía o sin coincidencias)")
        for idx, (etype, obj) in enumerate(entries, 1):
            date = _fmt_date(obj.get("mtime"))
            if etype == "dir":
                print(f"  {idx:3d}) {date}  [DIR ]      {obj['name']}/")
            else:
                sw = f"[{obj['switch_type']}] " if obj.get("switch_type") else ""
                st = f" ({obj['queue_status']})" if obj.get("queue_status") else ""
                print(f"  {idx:3d}) {date}  {_fmt_bytes(obj.get('size_bytes')):>9s}  {sw}{obj['name']}{st}")
        print("-" * 76)
        if kind == "mover":
            print("  N / 1,3-5: abrir/añadir | t: todo | r N: carpeta recursiva | o: copiar/mover")
            print("  tam N: tamaño | ren N: renombrar | del N[,M-K]: a la papelera | ?TXT: buscar")
            print("  orden nombre|fecha|tamano [desc] | d: destino local | a: destino auto")
            print("  /TXT: filtrar | *: quitar filtro | ..: subir | v: volver")
        else:
            print("  N / 1,3-5: abrir/añadir | j: solo juegos Switch | jc N: juego completo")
            print("  t: todo | r N: carpeta recursiva | tam N | ren N | del N[,M-K]: a la papelera")
            print("  ?TXT: buscar | orden nombre|fecha|tamano [desc] | d: destino FTP | a: auto")
            print("  /TXT: filtrar | *: quitar filtro | ..: subir | v: volver")

        cmd = input("  Opción: ").strip()
        if not cmd or cmd.lower() == "v":
            return
        if cmd == "..":
            if listing.get("parent"):
                cur_path = listing["parent"]
            continue
        if cmd == "*":
            text_filter = ""
            continue
        if cmd.startswith("/"):
            text_filter = cmd[1:].strip().lower()
            continue
        if cmd.lower().startswith("orden"):
            parts = cmd.lower().split()
            if len(parts) >= 2 and parts[1] in SORT_ALIASES:
                sort = SORT_ALIASES[parts[1]]
                order = "desc" if (len(parts) >= 3 and parts[2] in ("desc", "d", "-")) else "asc"
            else:
                print("  Uso: orden nombre|fecha|tamano [desc]")
            continue
        if cmd.startswith("?") and len(cmd) > 1:
            res = client.search(cmd[1:].strip(), cur_path, kind)
            print()
            _print_search(res)
            hits = res.get("results", [])
            if hits:
                sel = input("  Números a añadir a la cola (ej: 1,3-5; Enter = ninguno): ").strip()
                picked = [hits[i - 1]["path"] for i in _parse_selection(sel, len(hits))] if sel else []
                if picked:
                    added = client.add_paths(
                        kind, picked, recursive=True, base_browse_dir=cur_path, operation=op, dest_dir=dest_dir
                    )
                    print(f"  -> Añadidos: {added['added']} (omitidos: {added['skipped']})")
            continue
        if kind == "ftp" and cmd.lower().startswith("jc "):
            idxs = _parse_selection(cmd[3:].strip(), len(entries))
            picked = [entries[i - 1][1]["path"] for i in idxs]
            if picked:
                _print_game_result(client.add_game(picked, dest_dir))
            continue
        if cmd.lower() == "a":
            dest_dir = ""
            continue
        if cmd.lower() == "o" and kind == "mover":
            op = "mover" if op == "copiar" else "copiar"
            continue
        if cmd.lower().startswith(("tam ", "s ")):
            num = cmd.split(None, 1)[1].strip()
            if num.isdigit() and 1 <= int(num) <= len(entries):
                _etype, obj = entries[int(num) - 1]
                info = client.dir_size(obj["path"])
                print(
                    f"  -> Tamaño de '{obj['name']}': {_fmt_bytes(info['size_bytes'])} "
                    f"({info['files_count']} archivo(s))"
                )
            continue
        if cmd.lower().startswith(("ren ", "rn ")):
            num = cmd.split(None, 1)[1].strip()
            if num.isdigit() and 1 <= int(num) <= len(entries):
                _etype, obj = entries[int(num) - 1]
                new_n = input(f"  Nuevo nombre para '{obj['name']}': ").strip()
                if new_n and new_n != obj["name"]:
                    client.rename_entry(obj["path"], new_n)
                    print(f"  -> Renombrado a '{new_n}'")
            continue
        if cmd.lower().startswith(("del ", "rm ")):
            sel_str = cmd.split(None, 1)[1].strip()
            idxs = _parse_selection(sel_str, len(entries))
            targets = [entries[i - 1][1]["path"] for i in idxs]
            if targets:
                what = (
                    "¿Enviar {n} elemento(s) a la papelera?"
                    if client.trash_active()
                    else "¿Eliminar DEFINITIVAMENTE {n} elemento(s)? (papelera desactivada)"
                )
                if _ask_yes_no("  " + what.format(n=len(targets)), False):
                    _report_delete(client.delete_entries(targets))
            continue
        if cmd.lower() == "d":
            if kind == "mover":
                new_d = input(f"  Carpeta destino local [{dest_dir or cur_path}]: ").strip()
                if new_d:
                    dest_dir = new_d
            else:
                new_d = input(f"  Carpeta remota FTP [{dest_dir or '/'}]: ").strip()
                if new_d:
                    dest_dir = normalize_remote_path(new_d)
            continue
        if cmd.lower() == "t":
            paths = [f["path"] for f in files]
            res = client.add_paths(kind, paths, recursive=False, base_browse_dir=cur_path, operation=op, dest_dir=dest_dir)
            print(f"  -> Añadidos: {res['added']} (omitidos: {res['skipped']})")
            continue
        if cmd.lower() == "j" and kind == "ftp":
            paths = [f["path"] for f in files if f.get("switch_type")]
            res = client.add_paths(kind, paths, recursive=False, only_switch=True, base_browse_dir=cur_path, dest_dir=dest_dir)
            print(f"  -> Juegos Switch añadidos: {res['added']} (omitidos: {res['skipped']})")
            continue
        if cmd.lower().startswith("r "):
            num = cmd[2:].strip()
            if num.isdigit() and 1 <= int(num) <= len(entries):
                etype, obj = entries[int(num) - 1]
                if etype == "dir":
                    res = client.add_paths(kind, [obj["path"]], recursive=True, base_browse_dir=cur_path, operation=op, dest_dir=dest_dir)
                    print(f"  -> Carpeta añadida ({res['added']} archivos)")
            continue
        if cmd.isdigit() and 1 <= int(cmd) <= len(entries):
            etype, obj = entries[int(cmd) - 1]
            if etype == "dir":
                cur_path = obj["path"]
            else:
                res = client.add_paths(kind, [obj["path"]], recursive=False, base_browse_dir=cur_path, operation=op, dest_dir=dest_dir)
                print(f"  -> Añadido: {res['added']} (omitido: {res['skipped']})")
            continue
        indices = _parse_selection(cmd, len(entries))
        if indices:
            sel = [entries[i - 1][1]["path"] for i in indices if entries[i - 1][0] == "file"]
            if sel:
                res = client.add_paths(kind, sel, recursive=False, base_browse_dir=cur_path, operation=op, dest_dir=dest_dir)
                print(f"  -> Añadidos: {res['added']} (omitidos: {res['skipped']})")


def _interactive_queue(client: ToolboxClient, kind: str, only_pending: bool) -> None:
    text_filter = ""
    while True:
        items = client.get_queue(kind, only_pending=only_pending)
        shown = [
            it
            for it in items
            if not text_filter
            or text_filter in it["source_path"].lower()
            or text_filter in it["status"].lower()
            or text_filter in str(it["id"])
        ]
        print("\n" + "=" * 76)
        print(f"COLA {kind.upper()} ({'SOLO PENDIENTES' if only_pending else 'TODOS'})")
        if text_filter:
            print(f"Filtro: '{text_filter}' (* para limpiar)")
        print("-" * 76)
        _print_queue_table(shown)
        print("-" * 76)
        print("  t: todos/pendientes | ord: auto-ordenar | /TXT | *: quitar filtro")
        print("  b N[,M-K]: quitar | p N[,M-K]: pausar/reanudar | d N[,M-K]: cambiar destino")
        print("  sub N | baj N | top N" + (" | o N[,M-K]: alternar copiar/mover" if kind == "mover" else ""))
        print("  c: limpiar OK | f: limpiar ERR | e: reintentar ERR | x: vaciar pend | v: volver")

        cmd = input("  Acción: ").strip()
        if not cmd or cmd.lower() == "v":
            return
        low = cmd.lower()
        if low == "t":
            only_pending = not only_pending
        elif low == "ord":
            client.auto_order(kind)
        elif low in ("*", "/"):
            text_filter = ""
        elif cmd.startswith("/"):
            text_filter = cmd[1:].strip().lower()
        elif low == "c":
            print(f"  Limpiados OK: {client.clear_queue(kind, 'completed')}")
        elif low == "f":
            print(f"  Limpiados ERR: {client.clear_queue(kind, 'errors')}")
        elif low == "e":
            print(f"  Reintentando ERR: {client.retry_errors(kind)}")
        elif low == "x":
            if _ask_yes_no("  ¿Vaciar todos los pendientes/pausados?", False):
                print(f"  Eliminados: {client.clear_queue(kind, 'pending')}")
        elif low.startswith("b "):
            for i in _parse_selection(cmd[2:], len(shown)):
                client.item_action(shown[i - 1]["id"], "delete")
        elif low.startswith("p "):
            for i in _parse_selection(cmd[2:], len(shown)):
                client.item_action(shown[i - 1]["id"], "pause")
        elif low.startswith("o ") and kind == "mover":
            for i in _parse_selection(cmd[2:], len(shown)):
                client.item_action(shown[i - 1]["id"], "toggle-op")
        elif low.startswith("d "):
            idxs = _parse_selection(cmd[2:], len(shown))
            ids = [shown[i - 1]["id"] for i in idxs]
            if ids:
                prompt_lbl = "Nueva carpeta destino local" if kind == "mover" else "Nueva carpeta remota FTP"
                new_d = input(f"  {prompt_lbl}: ").strip()
                if new_d:
                    upd = client.change_dest_dir(ids, new_d)
                    print(f"  -> Destino actualizado en {upd} elemento(s)")
        elif low.startswith(("sub ", "baj ", "top ")):
            verb, rest = low.split(None, 1)
            dir_map = {"sub": "up", "baj": "down", "top": "top"}
            for i in _parse_selection(rest, len(shown)):
                client.item_action(shown[i - 1]["id"], "move", direction=dir_map[verb])


def _watch_progress(client: ToolboxClient, kind: str) -> None:
    print("  Pulsa Ctrl+C para volver al menú.")
    try:
        while True:
            print("\033[2J\033[H", end="")
            _print_status_summary(client, kind)
            time.sleep(1.5)
    except KeyboardInterrupt:
        print()


def _edit_settings_cli(client: ToolboxClient, kind: str) -> None:
    cfg = client.get_settings()
    patch: dict[str, Any] = {}
    print(f"\n=== Editar configuración {kind.upper()} (Enter mantiene valor actual) ===")
    fields = (
        [
            ("mover_origin", "Carpeta origen inicial"),
            ("mover_dest", "Carpeta destino por defecto"),
            ("mover_operation", "Operación por defecto (copiar/mover)"),
            ("mover_limit_kbps", "Límite velocidad KB/s (0=sin límite)"),
        ]
        if kind == "mover"
        else [
            ("ftp_host", "IP / Host FTP"),
            ("ftp_port", "Puerto FTP"),
            ("ftp_user", "Usuario (vacío=anónimo)"),
            ("ftp_local_root", "Carpeta local inicial"),
            ("ftp_remote_dir", "Carpeta remota por defecto"),
            ("ftp_limit_kbps", "Límite subida KB/s"),
            ("ftp_backup_dir", "Carpeta local de backups de la SD"),
            ("ftp_backup_exclude", "Carpetas excluidas del backup (comas; 'defecto' = restaurar)"),
            ("ftp_backup_exclude_ext", "Extensiones excluidas del backup (comas; 'defecto' = restaurar)"),
        ]
    )
    for key, label in fields:
        cur = cfg.get(key, "")
        if isinstance(cur, list):
            cur = ", ".join(cur)
        val = input(f"  {label} [{cur}]: ").strip()
        if val.lower() == "defecto" and key.startswith("ftp_backup_exclude"):
            patch[key] = cfg.get(f"{key}_default") or []
        elif val != "":
            patch[key] = int(val) if key.endswith(("kbps", "port")) and val.isdigit() else val
    if kind == "ftp":
        pw = input("  Nueva contraseña FTP (Enter para mantener): ").strip()
        if pw:
            patch["ftp_password"] = pw
    if patch:
        client.update_settings(patch)
        print("  ✓ Configuración compartida actualizada.")


def _interactive_menu(client: ToolboxClient, kind: str) -> int:
    while True:
        print("\n" + "=" * 76)
        _print_status_summary(client, kind)
        print("-" * 76)
        if kind == "mover":
            print("  1) Explorar y añadir a la cola    6) Editar configuración")
            print("  2) Ver / gestionar toda la cola   7) Ver configuración actual")
            print("  3) Ver solo pendientes            8) Ver registro (log)")
            print("  4) Ver progreso en vivo           9) Ayuda")
            print("  5) Iniciar / Detener worker       i) Historial   t) Papelera   0) Salir")
        else:
            print("  1) Explorar y añadir a la cola    6) Probar conexión FTP")
            print("  2) Ver / gestionar toda la cola   7) Buscar IP de Switch en red")
            print("  3) Ver solo pendientes            8) Editar configuración FTP")
            print("  4) Ver progreso en vivo           9) Ver registro (log)")
            print("  5) Iniciar / Detener subida       e) Espacio libre en la SD")
            print("  b) Backup de la microSD (sin juegos)")
            print("  i) Historial   t) Papelera        h) Ayuda   0) Salir")
        print("=" * 76)

        op = input("  Opción: ").strip().lower()
        if op in ("0", "q"):
            return 0
        try:
            if op == "1":
                _interactive_explore(client, kind)
            elif op == "2":
                _interactive_queue(client, kind, False)
            elif op in ("3", "p"):
                _interactive_queue(client, kind, True)
            elif op == "4":
                _watch_progress(client, kind)
            elif op in ("5", "s"):
                st = client.get_status()["workers"][kind]
                if st.get("running"):
                    client.stop_queue(kind)
                    print("  Solicitada detención limpia del worker.")
                else:
                    client.start_queue(kind)
                    print("  Worker iniciado en segundo plano.")
            elif op == "6":
                if kind == "mover":
                    _edit_settings_cli(client, "mover")
                else:
                    res = client.test_ftp()
                    print("  ", res.get("message"))
            elif op == "7":
                if kind == "mover":
                    print(json.dumps(client.get_settings(), indent=2, ensure_ascii=False))
                else:
                    _run_scan_switch(client)
            elif op == "8":
                if kind == "mover":
                    _print_logs(client, "mover", 40)
                else:
                    _edit_settings_cli(client, "ftp")
            elif op == "9":
                if kind == "mover":
                    _print_help("mover")
                else:
                    _print_logs(client, "ftp", 40)
            elif op in ("h", "?"):
                _print_help(kind)
            elif op == "i":
                _print_history(client.get_history(kind, 30))
                if _ask_yes_no("  ¿Vaciar el historial de esta cola?", False):
                    print(f"  Eliminados: {client.clear_history(kind)}")
            elif op == "t":
                _interactive_trash(client)
            elif op == "b" and kind == "ftp":
                _interactive_backup(client)
            elif op == "e" and kind == "ftp":
                print("  Consultando el espacio libre en el servidor FTP...")
                _print_ftp_space(client.ftp_space())
        except Exception as exc:
            print(f"  Error: {exc}")
        input("\n  Pulsa Enter para continuar...")


def _interactive_backup(client: ToolboxClient) -> None:
    st = client.backup_status()
    if st and st.get("running"):
        _print_backup_status(client)
        if _ask_yes_no("  ¿Detener el backup?", False):
            client.backup_stop()
            print("  Detención solicitada.")
        elif _ask_yes_no("  ¿Ver el progreso en vivo? (Ctrl+C vuelve)", True):
            _follow_backup(client)
        return
    cfg = client.get_settings()
    print("\n=== Backup de la microSD (sin carpetas de juegos) ===")
    print(f"  Carpeta remota (raíz de la SD): {cfg.get('ftp_backup_remote_root') or '/'}")
    print(f"  Excluye carpetas: {', '.join(cfg.get('ftp_backup_exclude') or []) or '(ninguna)'}")
    print(f"  Excluye ficheros: {', '.join('.' + e for e in cfg.get('ftp_backup_exclude_ext') or []) or '(ninguno)'}")
    base = cfg.get("ftp_backup_dir") or ""
    val = input(f"  Carpeta local de backups [{base or 'sin configurar'}]: ").strip()
    dest = str(Path(val).expanduser().resolve()) if val else base
    if not dest:
        print("  Indica una carpeta de destino (o configúrala con 8).")
        return
    if val and _ask_yes_no("  ¿Guardar esa carpeta como carpeta de backups por defecto?", True):
        client.update_settings({"ftp_backup_dir": dest})
    incremental = _ask_yes_no(
        "  ¿Incremental? (S = actualizar switch-sd/ y copiar solo lo cambiado · N = carpeta nueva con fecha)",
        bool(cfg.get("ftp_backup_incremental")),
    )
    if client.is_web_running():
        client.backup_start(dest, incremental)
        print("  Backup iniciado en segundo plano (visible en la web). Ctrl+C deja de mirar.")
        _follow_backup(client)
    else:
        _print_backup_result(client.backup_foreground(dest, incremental))


def _interactive_trash(client: ToolboxClient) -> None:
    while True:
        data = client.list_trash()
        print()
        _print_trash(data)
        items = data.get("items", [])
        toggle_lbl = "desactivar" if data.get("trash_enabled", True) else "activar"
        if items:
            print("  r ID[,ID]: restaurar | b ID[,ID]: borrar definitivamente | x: vaciar todo")
        print(f"  a: {toggle_lbl} papelera | v: volver")
        cmd = input("  Acción: ").strip().lower()
        if not cmd or cmd == "v":
            return
        if cmd == "a":
            if data.get("trash_enabled", True):
                if _ask_yes_no("  ¿Desactivar la papelera? Los próximos borrados serán definitivos", False):
                    client.set_trash_enabled(False)
                    print("  -> Papelera desactivada (lo que ya contiene se conserva hasta caducar)")
            else:
                active = client.set_trash_enabled(True)
                print("  -> Papelera activada" + ("" if active else " (pero trash_days=0: sube los días en ajustes)"))
            continue
        ids = [int(x.lstrip("#")) for x in cmd[2:].replace(" ", "").split(",") if x.lstrip("#").isdigit()]
        if cmd.startswith("r ") and ids:
            for r in client.restore_trash(ids):
                extra = " (renombrado: ya existía)" if r.get("renamed") else ""
                print(f"  -> Restaurado #{r['id']} en {r['path']}{extra}")
        elif cmd.startswith("b ") and ids:
            if _ask_yes_no(f"  ¿Borrar definitivamente {len(ids)} elemento(s)?", False):
                print(f"  -> Eliminados: {client.empty_trash(ids)}")
        elif cmd == "x":
            if _ask_yes_no("  ¿Vaciar toda la papelera? No se puede deshacer", False):
                print(f"  -> Eliminados: {client.empty_trash(None)}")


def _print_logs(client: ToolboxClient, kind: str, limit: int) -> None:
    import datetime

    logs = client.get_logs(kind=kind, limit=limit)
    if not logs:
        print("(sin registros todavía)")
        return
    for entry in reversed(logs):
        ts = datetime.datetime.fromtimestamp(entry["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [{entry['kind'].upper()}] [{entry['level']}] {entry['message']}")


def _run_scan_switch(client: ToolboxClient) -> int:
    print("Escaneando subredes LAN en busca de Switch FTP...")
    res = client.scan_switch()
    found = res.get("found", [])
    if not found:
        print("No se encontró ninguna Switch FTP en:", ", ".join(res.get("prefixes", [])))
        return 1
    for idx, hit in enumerate(found, 1):
        print(f"  {idx}) {hit['ip']}:{hit['port']} ({hit.get('banner') or 'puerto abierto'})")
    if sys.stdin.isatty() and _ask_yes_no("¿Guardar la primera IP/puerto encontrada en Configuración?", True):
        first = found[0]
        client.update_settings({"ftp_host": first["ip"], "ftp_port": int(first["port"])})
        print(f"Guardado {first['ip']}:{first['port']} en Configuración compartida.")
    return 0


def _parse_ids(expr: str) -> list[int]:
    return [int(x.lstrip("#")) for x in expr.replace(" ", "").split(",") if x.lstrip("#").isdigit()]


def _resolve_cli_paths(raw_items: list[str]) -> list[str]:
    resolved: list[str] = []
    for item in raw_items:
        p = Path(item).expanduser()
        if p.exists():
            resolved.append(str(p.resolve()))
        else:
            print(f"Aviso: no existe o no es accesible: {item}", file=sys.stderr)
    return resolved


def run_cli(argv: list[str], client: ToolboxClient | None = None) -> int:
    if not argv or argv[0] not in ("mover", "ftp", "subeftp"):
        print("Uso: python3 -m app.cli <mover|ftp> [OPCIONES...]", file=sys.stderr)
        return 1

    kind = "mover" if argv[0] == "mover" else "ftp"
    args = argv[1:]
    cli = client or ToolboxClient()

    if not args:
        return _interactive_menu(cli, kind)

    first = args[0]
    if first in ("-h", "--help", "help"):
        _print_help(kind)
        return 0
    if first == "--estado":
        _print_status_summary(cli, kind)
        _print_queue_table(cli.get_queue(kind, only_pending=False))
        return 0
    if first == "--pendientes":
        _print_status_summary(cli, kind)
        _print_queue_table(cli.get_queue(kind, only_pending=True))
        return 0
    if first == "--parar":
        cli.stop_queue(kind)
        print(f"Detención solicitada para {kind}.")
        return 0
    if first == "--log":
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 40
        _print_logs(cli, kind, limit)
        return 0
    if first == "--tamano":
        if len(args) < 2:
            print("Uso: --tamano RUTA", file=sys.stderr)
            return 1
        target_p = str(Path(args[1]).expanduser().resolve())
        info = cli.dir_size(target_p)
        print(f"{info['path']}: {_fmt_bytes(info['size_bytes'])} ({info['files_count']} archivo(s))")
        return 0
    if first == "--renombrar":
        if len(args) < 3:
            print("Uso: --renombrar RUTA NUEVO_NOMBRE", file=sys.stderr)
            return 1
        target_p = str(Path(args[1]).expanduser().resolve())
        new_p = cli.rename_entry(target_p, args[2])
        print(f"Renombrado a: {new_p}")
        return 0
    if first == "--borrar":
        force = False
        permanent = False
        rest: list[str] = []
        for a in args[1:]:
            if a in ("-y", "--yes"):
                force = True
            elif a == "--permanente":
                permanent = True
            else:
                rest.append(a)
        targets = _resolve_cli_paths(rest)
        if not targets:
            print("No se indicaron rutas válidas para borrar.", file=sys.stderr)
            return 1
        if not permanent and not cli.trash_active():
            permanent = True
            print("Papelera desactivada: el borrado será definitivo.", file=sys.stderr)
        what = "Eliminar DEFINITIVAMENTE" if permanent else "Enviar a la papelera"
        if not force and sys.stdin.isatty() and not _ask_yes_no(f"¿{what} {len(targets)} elemento(s)?", False):
            return 0
        _report_delete(cli.delete_entries(targets, permanent=permanent))
        return 0
    if first == "--papelera":
        _print_trash(cli.list_trash())
        return 0
    if first in ("--papelera-activar", "--papelera-desactivar"):
        enable = first == "--papelera-activar"
        active = cli.set_trash_enabled(enable)
        if enable:
            print("Papelera activada: los borrados se podrán restaurar." if active
                  else "Papelera activada, pero trash_days=0: súbelo en los ajustes para que tenga efecto.")
        else:
            print("Papelera desactivada: los próximos borrados serán definitivos "
                  "(lo que ya contiene se conserva hasta caducar).")
        return 0
    if first == "--restaurar":
        ids = _parse_ids(args[1] if len(args) > 1 else "")
        if not ids:
            print("Uso: --restaurar ID[,ID...]  (los IDs salen en --papelera)", file=sys.stderr)
            return 1
        restored = cli.restore_trash(ids)
        for r in restored:
            extra = " (renombrado: ya existía)" if r.get("renamed") else ""
            print(f"Restaurado #{r['id']} en {r['path']}{extra}")
        missing = set(ids) - {r["id"] for r in restored}
        if missing:
            print(f"No encontrados en la papelera: {', '.join(str(i) for i in sorted(missing))}", file=sys.stderr)
            return 1
        return 0
    if first == "--vaciar-papelera":
        rest = [a for a in args[1:] if a not in ("-y", "--yes")]
        force = len(rest) != len(args) - 1
        ids = _parse_ids(rest[0]) if rest else None
        what = f"{len(ids)} elemento(s) de la papelera" if ids else "TODA la papelera"
        if not force and sys.stdin.isatty() and not _ask_yes_no(f"¿Borrar definitivamente {what}?", False):
            return 0
        print(f"Eliminados definitivamente: {cli.empty_trash(ids)}")
        return 0
    if first == "--listar":
        path_arg = ""
        sort, order = "name", "asc"
        i = 1
        while i < len(args):
            a = args[i]
            if a == "--orden" and i + 1 < len(args):
                key = args[i + 1].lower()
                if key not in SORT_ALIASES:
                    print("Uso: --orden nombre|fecha|tamano", file=sys.stderr)
                    return 1
                sort = SORT_ALIASES[key]
                i += 2
                continue
            if a.startswith("--orden="):
                key = a.split("=", 1)[1].lower()
                if key not in SORT_ALIASES:
                    print("Uso: --orden nombre|fecha|tamano", file=sys.stderr)
                    return 1
                sort = SORT_ALIASES[key]
            elif a == "--desc":
                order = "desc"
            elif a.startswith("-"):
                print(f"Opción no reconocida en --listar: {a}", file=sys.stderr)
                return 1
            else:
                path_arg = str(Path(a).expanduser().resolve())
            i += 1
        listing = cli.list_dir(path_arg, kind, sort=sort, order=order)
        print(f"{listing['path']}  (orden: {sort} {order})")
        _print_listing(listing)
        return 0
    if first == "--buscar":
        if len(args) < 2 or not args[1].strip():
            print("Uso: --buscar TEXTO [RUTA]", file=sys.stderr)
            return 1
        start = str(Path(args[2]).expanduser().resolve()) if len(args) > 2 else ""
        res = cli.search(args[1], start, kind)
        _print_search(res)
        return 0 if res.get("results") else 1
    if first == "--historial":
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        _print_history(cli.get_history(kind, limit))
        return 0
    if first == "--limpiar-historial":
        print(f"Registros de historial eliminados: {cli.clear_history(kind)}")
        return 0
    if kind == "ftp" and first == "--espacio":
        return _print_ftp_space(cli.ftp_space())
    if kind == "ftp" and first == "--backup":
        return _run_backup_cli(cli, args[1:])
    if kind == "ftp" and first == "--backup-estado":
        return _print_backup_status(cli)
    if kind == "ftp" and first == "--backup-parar":
        res = cli.backup_stop()
        print("Detención del backup solicitada." if res.get("stopped") else "No hay ningún backup en marcha.")
        return 0
    if kind == "ftp" and first == "--juego":
        dest = ""
        start_now = False
        raw: list[str] = []
        i = 1
        while i < len(args):
            a = args[i]
            if a in ("-d", "--destino") and i + 1 < len(args):
                dest = args[i + 1]
                i += 2
                continue
            if a.startswith("--destino="):
                dest = a.split("=", 1)[1]
            elif a in ("-s", "--subir", "--ahora", "-i", "--iniciar"):
                start_now = True
            elif a.startswith("-"):
                print(f"Opción no reconocida en --juego: {a}", file=sys.stderr)
                return 1
            else:
                raw.append(a)
            i += 1
        paths = _resolve_cli_paths(raw)
        if not paths:
            print("Uso: subeftp --juego RUTA... [-d DESTINO] [-s]", file=sys.stderr)
            return 1
        res = cli.add_game(paths, normalize_remote_path(dest) if dest else "")
        _print_game_result(res)
        if res.get("added", 0) > 0 and (
            start_now or (sys.stdin.isatty() and _ask_yes_no("¿Iniciar la subida ahora?", True))
        ):
            cli.start_queue("ftp")
            print("Subida FTP iniciada en segundo plano (visible en Web y SSH).")
        return 0 if res.get("added", 0) > 0 else 1
    if first == "--cambiar-destino":
        if len(args) < 3:
            print("Uso: --cambiar-destino ID[,ID...] RUTA_DESTINO", file=sys.stderr)
            return 1
        ids = [int(x.lstrip("#")) for x in args[1].split(",") if x.lstrip("#").isdigit()]
        dest_arg = str(Path(args[2]).expanduser().resolve()) if kind == "mover" else args[2]
        updated = cli.change_dest_dir(ids, dest_arg)
        print(f"Destino actualizado en {updated} elemento(s).")
        return 0
    if kind == "mover" and first in ("--iniciar", "-i") and len(args) == 1:
        cli.start_queue("mover")
        print("Worker mover iniciado en segundo plano.")
        return 0
    if kind == "ftp" and first == "--subir" and len(args) == 1:
        cli.start_queue("ftp")
        print("Worker ftp iniciado en segundo plano.")
        return 0
    if kind == "ftp" and first == "--probar":
        res = cli.test_ftp()
        print(res.get("message"))
        return 0 if res.get("ok") else 1
    if kind == "ftp" and first == "--buscar-switch":
        return _run_scan_switch(cli)

    # Añadir archivos / carpetas desde CLI
    auto_start = False
    only_switch = False
    op: str | None = None
    dest_dir = ""
    raw_items: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--anadir":
            i += 1
        elif arg in ("-i", "--iniciar", "-s", "--subir", "--start", "--now", "--ahora"):
            auto_start = True
            i += 1
        elif kind == "mover" and arg in ("-c", "--copiar"):
            op = "copiar"
            i += 1
        elif kind == "mover" and arg in ("-m", "--mover"):
            op = "mover"
            i += 1
        elif kind == "ftp" and arg in ("-j", "--solo-switch"):
            only_switch = True
            i += 1
        elif arg in ("-d", "--destino"):
            if i + 1 >= len(args):
                print(f"Falta la ruta tras {arg}", file=sys.stderr)
                return 1
            dest_dir = args[i + 1]
            i += 2
        elif arg.startswith("--destino="):
            dest_dir = arg.split("=", 1)[1]
            i += 1
        elif arg == "--":
            raw_items.extend(args[i + 1 :])
            break
        elif arg.startswith("-"):
            print(f"Opción no reconocida: {arg}", file=sys.stderr)
            return 1
        else:
            raw_items.append(arg)
            i += 1

    paths = _resolve_cli_paths(raw_items)
    if not paths:
        print("No se encontraron archivos válidos para añadir a la cola.", file=sys.stderr)
        return 1

    if dest_dir and kind == "mover":
        dest_dir = str(Path(dest_dir).expanduser().resolve())

    res = cli.add_paths(
        kind=kind,
        paths=paths,
        recursive=True,
        only_switch=only_switch,
        base_browse_dir=os.path.dirname(paths[0]),
        operation=op,
        dest_dir=dest_dir,
    )
    print(f"Añadidos a la cola compartida ({kind}): {res['added']} (omitidos: {res['skipped']})")
    if res["added"] > 0 and (auto_start or (sys.stdin.isatty() and _ask_yes_no("¿Iniciar en segundo plano ahora?", True))):
        cli.start_queue(kind)
        print(f"Worker {kind} iniciado en segundo plano (visible en Web y SSH).")
    return 0


def main() -> None:
    try:
        sys.exit(run_cli(sys.argv[1:]))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
