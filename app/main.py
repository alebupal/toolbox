"""Aplicación FastAPI para Toolbox (Utilidades del servidor: Mover/Copiar + FTP/Switch)."""

from __future__ import annotations

import hashlib
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import (
    SettingsStore,
    effective_trash_days,
    get_data_root,
    normalize_remote_path,
    resolve_safe_path,
)
from .db import QueueDB
from .ftp_worker import list_remote_dirs, test_ftp_connection
from .models import (
    AddGameRequest,
    AddQueueRequest,
    BackupRestoreRequest,
    BackupStartRequest,
    ChangeDestRequest,
    DeletePathsRequest,
    MkdirRequest,
    MoveItemRequest,
    RenameRequest,
    ScanSwitchRequest,
    SettingsUpdateRequest,
    TrashIdsRequest,
)
from .mover_worker import calculate_dir_size, file_signature, rename_entry
from .services import VIRTUAL_ROOT_DIRS, decorate_queue_items, plan_game_upload, search_files
from .services import list_directory as list_directory_entries
from .switch_backup import BackupError, BackupJob, list_backups, plan_restore
from .switch_utils import SWITCH_EXTENSIONS, detect_switch_type, scan_switch_ftp, switch_sort_key
from .trash import TrashError, TrashManager
from .worker_manager import WorkerManager

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
README_PATH = Path(__file__).resolve().parent.parent / "README.md"
TRASH_PURGE_INTERVAL_S = 3600

settings_store = SettingsStore()
queue_db = QueueDB()
worker_manager = WorkerManager(queue_db, settings_store)
trash_manager = TrashManager(queue_db, get_data_root())
backup_job = BackupJob(queue_db, settings_store, get_data_root())
_purge_stop = threading.Event()


def _trash_purge_loop() -> None:
    """Purga periódica de los elementos caducados de la papelera."""
    while not _purge_stop.is_set():
        try:
            purged = trash_manager.purge_expired()
            if purged:
                queue_db.add_log("sistema", "INFO", f"Papelera: eliminados {purged} elemento(s) caducados")
        except Exception as exc:  # pragma: no cover - la purga nunca debe tumbar la app
            queue_db.add_log("sistema", "WARN", f"Error purgando la papelera: {exc}")
        _purge_stop.wait(TRASH_PURGE_INTERVAL_S)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    queue_db.add_log("sistema", "INFO", f"Toolbox iniciado (DATA_ROOT={get_data_root()})")
    _purge_stop.clear()
    purge_thread = threading.Thread(target=_trash_purge_loop, name="trash-purge", daemon=True)
    purge_thread.start()
    try:
        yield
    finally:
        _purge_stop.set()
        worker_manager.stop_all()
        backup_job.stop()
        queue_db.close()


app = FastAPI(title="Toolbox", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


def _disk_free_bytes(path: str) -> dict[str, int] | None:
    cur = path
    while cur and not os.path.exists(cur):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    try:
        st = os.statvfs(cur or "/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return {"total_bytes": total, "free_bytes": free}
    except OSError:
        return None


def _quick_bookmarks(data_root: str) -> list[dict[str, str]]:
    candidates = [
        ("Raíz servidor", data_root),
        ("Home", os.path.join(data_root, "home")),
        ("Montajes (/mnt)", os.path.join(data_root, "mnt")),
        ("Medios (/media)", os.path.join(data_root, "media")),
    ]
    bookmarks: list[dict[str, str]] = []
    seen: set[str] = set()
    for label, path in candidates:
        norm = os.path.normpath(path)
        if norm not in seen and os.path.isdir(norm):
            seen.add(norm)
            bookmarks.append({"label": label, "path": norm})
    return bookmarks


def _collect_files_recursive(folder: str, include_hidden: bool = False) -> list[str]:
    files: list[str] = []
    data_root = get_data_root()
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        # Evitar pseudo-sistemas del kernel cuando se navega desde la raíz del host
        dirnames[:] = sorted(
            d
            for d in dirnames
            if (include_hidden or not d.startswith("."))
            and not (os.path.normpath(dirpath) == data_root and d in VIRTUAL_ROOT_DIRS)
        )
        for fn in sorted(filenames):
            if not include_hidden and fn.startswith("."):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.isfile(full):
                files.append(full)
    return files


_STATIC_REF_RE = re.compile(r'(/static/([\w.-]+\.(?:js|css|svg|png)))(\?v=[^"\']*)?')
_index_cache: dict[str, Any] = {}


def _static_version(name: str) -> str:
    try:
        return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


def render_index() -> str:
    """index.html con ?v=<hash del contenido> en cada /static/…: tras actualizar, el navegador
    (o un proxy con caché de estáticos, como NPM «Cache Assets») nunca mezcla JS/CSS antiguos
    con el HTML nuevo. Se recalcula solo si cambia algún fichero estático."""
    files = sorted(STATIC_DIR.glob("*"))
    sig = tuple((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in files if f.is_file())
    if _index_cache.get("sig") != sig:
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        _index_cache["html"] = _STATIC_REF_RE.sub(lambda m: f"{m.group(1)}?v={_static_version(m.group(2))}", html)
        _index_cache["sig"] = sig
    return _index_cache["html"]


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse(render_index(), headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    # Los navegadores piden /favicon.ico aunque la página enlace el SVG
    return FileResponse(str(STATIC_DIR / "favicon.svg"), media_type="image/svg+xml")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/readme")
def get_readme() -> dict[str, str]:
    if not README_PATH.is_file():
        raise HTTPException(status_code=404, detail="README.md no encontrado")
    return {"content": README_PATH.read_text(encoding="utf-8")}


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    cfg = settings_store.get()
    return {
        "workers": worker_manager.status(),
        "backup": backup_job.status(),
        "disk_dest": _disk_free_bytes(cfg.get("mover_dest", get_data_root())),
        "data_root": get_data_root(),
    }


@app.get("/api/fs/list")
def list_directory(
    path: str | None = None,
    kind: Literal["mover", "ftp"] = "mover",
    show_hidden: bool = False,
    sort: Literal["name", "mtime", "size"] = "name",
    order: Literal["asc", "desc"] = "asc",
) -> dict[str, Any]:
    data_root = get_data_root()
    cfg = settings_store.get()
    default_start = cfg["ftp_local_root"] if kind == "ftp" else cfg["mover_origin"]
    requested = path if path is not None and path.strip() != "" else default_start

    try:
        safe_path = resolve_safe_path(requested, data_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not os.path.isdir(safe_path):
        safe_path = data_root

    statuses = queue_db.get_source_statuses(kind)
    try:
        dirs, files = list_directory_entries(safe_path, data_root, statuses, show_hidden, sort, order)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Sin permisos para leer {safe_path}") from exc

    parent = os.path.dirname(safe_path) if safe_path != data_root else None
    return {
        "path": safe_path,
        "parent": parent,
        "data_root": data_root,
        "bookmarks": _quick_bookmarks(data_root),
        "disk": _disk_free_bytes(safe_path),
        "sort": sort,
        "order": order,
        "dirs": dirs,
        "files": files,
    }


@app.get("/api/fs/search")
def api_search(
    q: str = Query(..., min_length=1, max_length=200),
    path: str | None = None,
    kind: Literal["mover", "ftp"] = "mover",
    limit: int = Query(300, ge=1, le=1000),
) -> dict[str, Any]:
    data_root = get_data_root()
    cfg = settings_store.get()
    default_start = cfg["ftp_local_root"] if kind == "ftp" else cfg["mover_origin"]
    try:
        safe_path = resolve_safe_path(path if path and path.strip() else default_start, data_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not os.path.isdir(safe_path):
        raise HTTPException(status_code=400, detail=f"No es una carpeta: {safe_path}")
    return search_files(safe_path, q, data_root, limit=limit)


@app.post("/api/fs/mkdir")
def make_directory(req: MkdirRequest) -> dict[str, str]:
    data_root = get_data_root()
    clean_name = req.name.strip().replace("/", "").replace("\\", "")
    if not clean_name or clean_name in (".", ".."):
        raise HTTPException(status_code=400, detail="Nombre de carpeta inválido")
    try:
        parent = resolve_safe_path(req.parent, data_root)
        target = resolve_safe_path(os.path.join(parent, clean_name), data_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo crear la carpeta: {exc}") from exc
    return {"path": target}


@app.post("/api/fs/rename")
def api_rename_entry(req: RenameRequest) -> dict[str, str]:
    try:
        new_path = rename_entry(req.path, req.new_name, get_data_root())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo renombrar: {exc}") from exc
    queue_db.add_log("sistema", "INFO", f"Renombrado: {req.path} -> {new_path}")
    return {"path": new_path}


@app.post("/api/fs/delete")
def api_delete_entries(req: DeletePathsRequest) -> dict[str, Any]:
    trash_days = effective_trash_days(settings_store.get())
    try:
        res = trash_manager.delete(req.paths, trash_days, permanent=req.permanent)
    except TrashError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo eliminar: {exc}") from exc
    if res["trashed"]:
        queue_db.add_log("sistema", "INFO", f"Enviados {len(res['trashed'])} elemento(s) a la papelera")
    if res["deleted"]:
        queue_db.add_log("sistema", "INFO", f"Eliminados definitivamente {res['deleted']} elemento(s)")
    return res


@app.get("/api/trash")
def api_list_trash() -> dict[str, Any]:
    cfg = settings_store.get()
    return {
        "items": trash_manager.list(),
        "trash_days": int(cfg.get("trash_days", 7)),
        "trash_enabled": effective_trash_days(cfg) > 0,
    }


@app.post("/api/trash/restore")
def api_restore_trash(req: TrashIdsRequest) -> dict[str, Any]:
    if not req.ids:
        raise HTTPException(status_code=400, detail="Indica los elementos a restaurar")
    try:
        restored = trash_manager.restore(req.ids)
    except TrashError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo restaurar: {exc}") from exc
    if restored:
        queue_db.add_log("sistema", "INFO", f"Restaurados {len(restored)} elemento(s) de la papelera")
    return {"restored": restored}


@app.post("/api/trash/empty")
def api_empty_trash(req: TrashIdsRequest | None = None) -> dict[str, int]:
    try:
        removed = trash_manager.empty(req.ids if req else None)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo vaciar la papelera: {exc}") from exc
    if removed:
        queue_db.add_log("sistema", "INFO", f"Papelera: eliminados definitivamente {removed} elemento(s)")
    return {"removed": removed}


@app.get("/api/fs/dir-size")
def api_dir_size(path: str) -> dict[str, Any]:
    try:
        return calculate_dir_size(path, get_data_root())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Error calculando tamaño: {exc}") from exc


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return settings_store.get_public()


@app.put("/api/settings")
def update_settings(req: SettingsUpdateRequest) -> dict[str, Any]:
    patch = req.model_dump(exclude_none=True)
    settings_store.update(patch)
    queue_db.add_log("sistema", "INFO", "Configuración actualizada desde la interfaz web")
    return settings_store.get_public()


@app.get("/api/queue")
def list_queue(
    kind: Literal["mover", "ftp"] | None = None,
    only_pending: bool = False,
) -> dict[str, Any]:
    items = queue_db.list_items(kind=kind, only_pending=only_pending)
    return {"items": decorate_queue_items(items, settings_store.get(), get_data_root())}


@app.post("/api/queue/add")
def add_to_queue(req: AddQueueRequest) -> dict[str, Any]:
    data_root = get_data_root()
    cfg = settings_store.get()
    entries: list[dict[str, Any]] = []

    if req.kind == "mover":
        op = req.operation or cfg["mover_operation"]
        base_origin = cfg["mover_origin"]
        if req.base_browse_dir:
            try:
                base_origin = resolve_safe_path(req.base_browse_dir, data_root)
            except ValueError:
                pass
        chosen_dest = None
        if req.dest_dir and req.dest_dir.strip():
            try:
                chosen_dest = resolve_safe_path(req.dest_dir, data_root)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        auto_dest = cfg["mover_dest"]
        keep_struct = bool(cfg["mover_keep_structure"])
    else:
        op = "subir"
        base_origin = cfg["ftp_local_root"]
        chosen_dest = normalize_remote_path(req.dest_dir) if (req.dest_dir and req.dest_dir.strip()) else None
        auto_dest = normalize_remote_path(cfg["ftp_remote_dir"])
        keep_struct = bool(cfg["ftp_keep_structure"])

    for raw_p in req.paths:
        try:
            safe_p = resolve_safe_path(raw_p, data_root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if os.path.isdir(safe_p):
            if not req.recursive:
                continue
            folder_name = os.path.basename(safe_p)
            sub_files = _collect_files_recursive(safe_p)
            for fpath in sub_files:
                if req.only_switch and not fpath.lower().endswith(SWITCH_EXTENSIONS):
                    continue
                rel_inside = os.path.relpath(fpath, safe_p)
                if req.kind == "mover":
                    target_base = chosen_dest if chosen_dest else auto_dest
                    dest_p = (
                        os.path.join(target_base, folder_name, rel_inside)
                        if (chosen_dest or keep_struct)
                        else os.path.join(target_base, os.path.basename(fpath))
                    )
                else:
                    target_base = chosen_dest if chosen_dest else auto_dest
                    rel_posix = rel_inside.replace(os.sep, "/")
                    dest_p = normalize_remote_path(f"{target_base}/{folder_name}/{rel_posix}")

                try:
                    sig = file_signature(fpath)
                    size_b = os.path.getsize(fpath)
                except OSError:
                    continue
                entries.append(
                    {
                        "kind": req.kind,
                        "operation": op,
                        "source_path": fpath,
                        "dest_path": dest_p,
                        "size_bytes": size_b,
                        "source_sig": sig,
                        "cleanup_dir": safe_p if (req.kind == "mover" and op == "mover") else "",
                        "switch_type": detect_switch_type(fpath),
                    }
                )
        elif os.path.isfile(safe_p):
            if req.only_switch and not safe_p.lower().endswith(SWITCH_EXTENSIONS):
                continue
            filename = os.path.basename(safe_p)
            if req.kind == "mover":
                if chosen_dest:
                    dest_p = os.path.join(chosen_dest, filename)
                elif keep_struct:
                    try:
                        rel = os.path.relpath(safe_p, base_origin)
                    except ValueError:
                        rel = filename
                    if rel.startswith(".."):
                        rel = filename
                    dest_p = os.path.join(auto_dest, rel)
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
                    if rel.startswith(".."):
                        rel = filename
                    dest_p = normalize_remote_path(f"{auto_dest}/{rel.replace(os.sep, '/')}")
                else:
                    dest_p = normalize_remote_path(f"{auto_dest}/{filename}")

            try:
                sig = file_signature(safe_p)
                size_b = os.path.getsize(safe_p)
            except OSError:
                continue
            entries.append(
                {
                    "kind": req.kind,
                    "operation": op,
                    "source_path": safe_p,
                    "dest_path": dest_p,
                    "size_bytes": size_b,
                    "source_sig": sig,
                    "cleanup_dir": "",
                    "switch_type": detect_switch_type(safe_p),
                }
            )

    inserted = queue_db.add_items(entries)
    if inserted:
        queue_db.add_log(req.kind, "INFO", f"Añadidos {len(inserted)} archivo(s) a la cola de {req.kind}")
        if req.kind == "ftp":
            all_ftp = queue_db.list_items(kind="ftp")
            sorted_ids = [it["id"] for it in sorted(all_ftp, key=switch_sort_key)]
            queue_db.reorder_items("ftp", sorted_ids)

    return {"added": len(inserted), "skipped": len(entries) - len(inserted), "items": inserted}


@app.post("/api/queue/{kind}/start")
def start_queue(kind: Literal["mover", "ftp"]) -> dict[str, Any]:
    if kind == "ftp" and backup_job.is_running():
        # Los servidores FTP de la Switch suelen admitir una sola sesión: no mezclar subidas y backup
        raise HTTPException(status_code=409, detail="Hay un backup de la microSD en marcha; espera a que termine o detenlo")
    started = worker_manager.start(kind)
    return {"started": started, "status": worker_manager.status()[kind]}


@app.post("/api/queue/{kind}/stop")
def stop_queue(kind: Literal["mover", "ftp"]) -> dict[str, Any]:
    stopped = worker_manager.stop(kind)
    return {"stopped": stopped, "status": worker_manager.status()[kind]}


@app.post("/api/queue/{kind}/auto-order")
def auto_order_queue(kind: Literal["mover", "ftp"]) -> dict[str, Any]:
    items = queue_db.list_items(kind=kind)
    if kind == "ftp":
        ordered = sorted(items, key=switch_sort_key)
    else:
        ordered = sorted(
            items,
            key=lambda it: (
                0 if it["status"] in ("PROC", "PEND", "REAN", "PAUS") else (1 if it["status"] == "ERR" else 2),
                it["source_path"].lower(),
            ),
        )
    queue_db.reorder_items(kind, [it["id"] for it in ordered])
    queue_db.add_log(kind, "INFO", f"Cola {kind} reordenada automáticamente")
    return {"ok": True}


@app.post("/api/queue/{kind}/retry-errors")
def retry_queue_errors(kind: Literal["mover", "ftp"]) -> dict[str, int]:
    count = queue_db.retry_errors(kind)
    if count:
        queue_db.add_log(kind, "INFO", f"Reencolados {count} elemento(s) con error")
    return {"retried": count}


@app.post("/api/queue/{kind}/clear")
def clear_queue(
    kind: Literal["mover", "ftp"],
    scope: Literal["completed", "errors", "pending"] = Query("completed"),
) -> dict[str, int]:
    mapping = {
        "completed": ("OK",),
        "errors": ("ERR",),
        "pending": ("PEND", "REAN", "PAUS"),
    }
    removed = queue_db.clear_by_status(kind, mapping[scope])
    if removed:
        queue_db.add_log(kind, "INFO", f"Limpiados {removed} elemento(s) ({scope})")
    return {"removed": removed}


@app.post("/api/queue/item/{item_id}/pause")
def pause_queue_item(item_id: int) -> dict[str, Any]:
    updated = queue_db.toggle_pause(item_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Elemento no encontrado")
    return {"item": updated}


@app.post("/api/queue/item/{item_id}/toggle-op")
def toggle_queue_item_op(item_id: int) -> dict[str, Any]:
    updated = queue_db.toggle_operation(item_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Elemento no encontrado")
    return {"item": updated}


@app.post("/api/queue/item/{item_id}/move")
def move_queue_item(item_id: int, req: MoveItemRequest) -> dict[str, bool]:
    ok = queue_db.move_position(item_id, req.direction)
    return {"ok": ok}


@app.delete("/api/queue/item/{item_id}")
def delete_queue_item(item_id: int) -> dict[str, bool]:
    ok = queue_db.remove_item(item_id)
    if not ok:
        raise HTTPException(status_code=400, detail="No se puede eliminar (puede estar en ejecución)")
    return {"ok": True}


@app.post("/api/queue/change-dest")
def change_queue_dest(req: ChangeDestRequest) -> dict[str, int]:
    try:
        updated = queue_db.change_dest_dir(req.item_ids, req.dest_dir, get_data_root())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated:
        queue_db.add_log("sistema", "INFO", f"Destino actualizado a '{req.dest_dir}' en {updated} elemento(s)")
    return {"updated": updated}


@app.post("/api/ftp/test")
def api_ftp_test() -> dict[str, Any]:
    cfg = settings_store.get()
    res = test_ftp_connection(cfg)
    queue_db.add_log("ftp", "INFO" if res["ok"] else "ERROR", res["message"])
    return res


@app.get("/api/ftp/dirs")
def api_ftp_dirs(path: str = "/") -> dict[str, Any]:
    cfg = settings_store.get()
    return list_remote_dirs(cfg, path)


@app.post("/api/ftp/scan-switch")
def api_scan_switch(req: ScanSwitchRequest | None = None) -> dict[str, Any]:
    cfg = settings_store.get()
    ports = req.ports if req and req.ports else None
    if not ports:
        pto_conf = int(cfg.get("ftp_port") or 5000)
        ports = [pto_conf, 5000] if pto_conf not in (21, 5000) else [5000, 2121, 21]
    extra_subnets = req.extra_subnets if req else None
    res = scan_switch_ftp(ports=ports, extra_subnets=extra_subnets)
    queue_db.add_log(
        "ftp",
        "INFO",
        f"Escaneo Switch LAN en subredes {res['prefixes']} puertos {res['ports']}: {len(res['found'])} encontrado(s)",
    )
    return res


@app.get("/api/ftp/space")
def api_ftp_space() -> dict[str, Any]:
    space = worker_manager.refresh_ftp_space()
    pending = worker_manager.status()["ftp"]["pending_bytes"]
    free = space.get("free_bytes") if space.get("supported") else None
    res: dict[str, Any] = {
        "supported": bool(space.get("supported")),
        "free_bytes": free,
        "pending_bytes": pending,
        "enough": (free >= pending) if free is not None else None,
        "method": space.get("method", ""),
    }
    if space.get("error"):
        res["error"] = space["error"]
    return res


@app.get("/api/ftp/backup")
def api_backup_status() -> dict[str, Any]:
    return backup_job.status()


@app.post("/api/ftp/backup/start")
def api_backup_start(req: BackupStartRequest | None = None) -> dict[str, Any]:
    if worker_manager.runners["ftp"].is_running():
        raise HTTPException(status_code=409, detail="La cola FTP está subiendo; detenla antes de hacer el backup")
    req = req or BackupStartRequest()
    try:
        return backup_job.start(
            dest_dir=req.dest_dir if (req.dest_dir and req.dest_dir.strip()) else None,
            incremental=req.incremental,
            extra_exclude=req.extra_exclude,
            simulate=req.simulate,
        )
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/ftp/backup/list")
def api_backup_list(path: str | None = None) -> dict[str, Any]:
    base = path if (path and path.strip()) else settings_store.get().get("ftp_backup_dir", "")
    if not base:
        return {"path": "", "items": []}
    try:
        safe = resolve_safe_path(base, get_data_root())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"path": safe, "items": list_backups(safe)}


@app.post("/api/ftp/backup/restore")
def api_backup_restore(req: BackupRestoreRequest) -> dict[str, Any]:
    if backup_job.is_running():
        raise HTTPException(status_code=409, detail="Hay un backup de la microSD en marcha; espera a que termine")
    cfg = settings_store.get()
    try:
        plan = plan_restore(req.path, get_data_root(), cfg.get("ftp_backup_remote_root") or "/")
    except BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo preparar la restauración: {exc}") from exc
    inserted = queue_db.add_items(plan["entries"])
    if inserted:
        queue_db.add_log("ftp", "INFO", f"Restauración a la SD encolada desde {req.path}: {len(inserted)} archivo(s)")
    started = worker_manager.start("ftp") if (req.start and inserted) else False
    return {
        "root": plan["root"],
        "added": len(inserted),
        "skipped": len(plan["entries"]) - len(inserted),
        "bytes": sum(int(e["size_bytes"]) for e in plan["entries"]),
        "started": started,
    }


@app.post("/api/ftp/backup/stop")
def api_backup_stop() -> dict[str, Any]:
    return {"stopped": backup_job.stop(), **backup_job.status()}


@app.post("/api/ftp/add-game")
def api_add_game(req: AddGameRequest) -> dict[str, Any]:
    data_root = get_data_root()
    cfg = settings_store.get()
    dest = req.dest_dir if (req.dest_dir and req.dest_dir.strip()) else cfg["ftp_remote_dir"]
    active = {
        it["source_path"] for it in queue_db.list_items(kind="ftp") if it["status"] in ("PEND", "REAN", "PAUS", "PROC")
    }
    try:
        plan = plan_game_upload(req.paths, data_root, dest, active)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    inserted = queue_db.add_items(plan["entries"])
    if inserted:
        names = ", ".join(g["name"] for g in plan["groups"]) or "juego"
        queue_db.add_log("ftp", "INFO", f"Juego completo añadido a la cola FTP ({len(inserted)} archivo(s)): {names}")
    return {
        "added": len(inserted),
        "items": decorate_queue_items(inserted, cfg, data_root),
        "warnings": plan["warnings"],
        "groups": plan["groups"],
    }


@app.get("/api/history")
def api_history(
    kind: Literal["todos", "mover", "ftp"] = "todos",
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    items, total = queue_db.list_history(kind=kind, limit=limit, offset=offset)
    return {"items": items, "total": total}


@app.delete("/api/history")
def api_clear_history(kind: Literal["todos", "mover", "ftp"] = "todos") -> dict[str, int]:
    removed = queue_db.clear_history(kind)
    if removed:
        queue_db.add_log("sistema", "INFO", f"Historial ({kind}) vaciado: {removed} registro(s)")
    return {"removed": removed}


@app.get("/api/logs")
def api_logs(
    kind: Literal["todos", "mover", "ftp", "sistema"] = "todos",
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    return {"logs": queue_db.get_logs(kind=kind, limit=limit)}


@app.delete("/api/logs")
def api_clear_logs() -> dict[str, bool]:
    queue_db.clear_logs()
    return {"ok": True}

