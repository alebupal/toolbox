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

from .config import SettingsStore, get_data_root, normalize_remote_path, resolve_safe_path
from .db import QueueDB
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
                snap: dict[str, Any] = {
                    "running": False,
                    "stopping": False,
                    "progress": {},
                    "counts": counts,
                    "pending_bytes": pending_bytes,
                }
                if kind == "mover":
                    snap["space_check"] = evaluate_mover_space(items, cfg.get("mover_dest") or "")
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

    def list_dir(self, path: str, kind: str, show_hidden: bool = False) -> dict[str, Any]:
        if self.is_web_running():
            qs = urllib.parse.urlencode(
                {
                    "kind": kind,
                    "path": path,
                    "show_hidden": "true" if show_hidden else "false",
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
        dirs, files = [], []
        with os.scandir(safe) as entries:
            for entry in entries:
                if not show_hidden and entry.name.startswith("."):
                    continue
                if safe == data_root and entry.name in {"proc", "sys", "dev", "run"}:
                    continue
                full = os.path.join(safe, entry.name)
                try:
                    if entry.is_dir(follow_symlinks=True):
                        dirs.append({"name": entry.name, "path": full})
                    elif entry.is_file(follow_symlinks=True):
                        files.append(
                            {
                                "name": entry.name,
                                "path": full,
                                "size_bytes": entry.stat(follow_symlinks=True).st_size,
                                "switch_type": detect_switch_type(entry.name),
                                "queue_status": statuses.get(full, ""),
                            }
                        )
                except OSError:
                    continue
        dirs.sort(key=lambda d: d["name"].lower())
        files.sort(key=lambda f: f["name"].lower())
        parent = os.path.dirname(safe) if safe != data_root else None
        return {"path": safe, "parent": parent, "data_root": data_root, "dirs": dirs, "files": files}

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
            return db.list_items(kind=kind, only_pending=only_pending)
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
                else sorted(items, key=lambda it: it["source_path"].lower())
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

    def delete_entries(self, paths: list[str]) -> int:
        if self.is_web_running():
            return int(self._http("POST", "/api/fs/delete", {"paths": paths}).get("deleted", 0))
        from .mover_worker import delete_entries

        deleted = delete_entries(paths, get_data_root())
        if deleted:
            db = self._local_db()
            try:
                db.add_log("sistema", "INFO", f"[CLI] Eliminados {deleted} elemento(s)")
            finally:
                db.close()
        return deleted

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
    prog = st.get("progress") or {}
    if st.get("running") and prog.get("filename"):
        print(
            f"  Progreso: {prog['filename']} -> {float(prog.get('percent', 0)):.1f}% "
            f"| {prog.get('speed', '--')} | ETA {prog.get('eta', '--')}"
        )


def _print_queue_table(items: list[dict[str, Any]]) -> None:
    if not items:
        print("  (cola vacía)")
        return
    for idx, it in enumerate(items, 1):
        sw = f"[{it['switch_type']}] " if it.get("switch_type") else ""
        err = f" | ERR: {it['error']}" if it.get("error") else ""
        print(
            f"  {idx:3d}. [#{it['id']}] [{it['status']:4s}] [{it['operation'].upper():5s}] "
            f"{sw}{it['source_path']} -> {it['dest_path']} ({_fmt_bytes(it.get('size_bytes'))}){err}"
        )


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
  mover --estado | --pendientes       Muestra estado, espacio libre y cola compartida
  mover --cambiar-destino IDs RUTA    Cambia destino de ítems en cola (ej: 1,2 /host/mnt/pelis)
  mover --tamano RUTA                 Calcula tamaño total y nº de archivos de una carpeta
  mover --renombrar RUTA NUEVO_NOMBRE Renombra un archivo o carpeta del servidor
  mover --borrar [-y] RUTA...         Elimina archivos o carpetas del servidor
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
  subeftp --estado | --pendientes     Muestra estado y cola FTP compartida
  subeftp --cambiar-destino IDs RUTA  Cambia carpeta remota en cola (ej: 1,2 /install)
  subeftp --tamano RUTA               Calcula tamaño total de una carpeta local
  subeftp --renombrar RUTA NUEVO_NOM  Renombra un archivo o carpeta local
  subeftp --borrar [-y] RUTA...       Elimina archivos o carpetas locales
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

    while True:
        listing = client.list_dir(cur_path, kind)
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
        if text_filter:
            print(f"Filtro activo: '{text_filter}' (escribe * para quitar)")
        print("-" * 76)
        if not entries:
            print("  (carpeta vacía o sin coincidencias)")
        for idx, (etype, obj) in enumerate(entries, 1):
            if etype == "dir":
                print(f"  {idx:3d}) [DIR ] {obj['name']}/")
            else:
                sw = f"[{obj['switch_type']}] " if obj.get("switch_type") else ""
                st = f" ({obj['queue_status']})" if obj.get("queue_status") else ""
                print(f"  {idx:3d})       {sw}{obj['name']} [{_fmt_bytes(obj.get('size_bytes'))}]{st}")
        print("-" * 76)
        if kind == "mover":
            print("  N / 1,3-5: abrir/añadir | t: todo | r N: carpeta recursiva | o: copiar/mover")
            print("  tam N: tamaño carpeta | ren N: renombrar | del N[,M-K]: borrar del servidor")
            print("  d: elegir destino local | a: destino automático | /TXT | * | .. | v: volver")
        else:
            print("  N / 1,3-5: abrir/añadir | j: solo juegos Switch | t: todo | r N: carpeta recursiva")
            print("  tam N: tamaño carpeta | ren N: renombrar | del N[,M-K]: borrar del servidor")
            print("  d: escribir ruta destino FTP | a: destino automático | /TXT | * | .. | v: volver")

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
            if targets and _ask_yes_no(f"  ¿Eliminar permanentemente {len(targets)} elemento(s)?", False):
                deleted = client.delete_entries(targets)
                print(f"  -> Eliminados: {deleted}")
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
        ]
    )
    for key, label in fields:
        cur = cfg.get(key, "")
        val = input(f"  {label} [{cur}]: ").strip()
        if val != "":
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
            print("  5) Iniciar / Detener worker       0) Salir")
        else:
            print("  1) Explorar y añadir a la cola    6) Probar conexión FTP")
            print("  2) Ver / gestionar toda la cola   7) Buscar IP de Switch en red")
            print("  3) Ver solo pendientes            8) Editar configuración FTP")
            print("  4) Ver progreso en vivo           9) Ver registro (log)")
            print("  5) Iniciar / Detener subida       h) Ayuda   0) Salir")
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
        except Exception as exc:
            print(f"  Error: {exc}")
        input("\n  Pulsa Enter para continuar...")


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
        rest = args[1:]
        force = False
        if rest and rest[0] in ("-y", "--yes"):
            force = True
            rest = rest[1:]
        targets = _resolve_cli_paths(rest)
        if not targets:
            print("No se indicaron rutas válidas para borrar.", file=sys.stderr)
            return 1
        if not force and sys.stdin.isatty() and not _ask_yes_no(f"¿Eliminar {len(targets)} elemento(s)?", False):
            return 0
        deleted = cli.delete_entries(targets)
        print(f"Eliminados: {deleted}")
        return 0
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
