"""Aplicación FastAPI para Toolbox (Utilidades del servidor: Mover/Copiar + FTP/Switch)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import (
    SettingsStore,
    get_data_root,
    normalize_remote_path,
    resolve_safe_path,
)
from .db import QueueDB
from .ftp_worker import list_remote_dirs, test_ftp_connection
from .models import (
    AddQueueRequest,
    MkdirRequest,
    MoveItemRequest,
    ScanSwitchRequest,
    SettingsUpdateRequest,
)
from .mover_worker import file_signature
from .switch_utils import SWITCH_EXTENSIONS, detect_switch_type, scan_switch_ftp, switch_sort_key
from .worker_manager import WorkerManager

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
VIRTUAL_ROOT_DIRS = {"proc", "sys", "dev", "run"}

settings_store = SettingsStore()
queue_db = QueueDB()
worker_manager = WorkerManager(queue_db, settings_store)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    queue_db.add_log("sistema", "INFO", f"Toolbox iniciado (DATA_ROOT={get_data_root()})")
    try:
        yield
    finally:
        worker_manager.stop_all()
        queue_db.close()


app = FastAPI(title="Toolbox", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
def get_status() -> dict[str, Any]:
    cfg = settings_store.get()
    return {
        "workers": worker_manager.status(),
        "disk_dest": _disk_free_bytes(cfg.get("mover_dest", get_data_root())),
        "data_root": get_data_root(),
    }


@app.get("/api/fs/list")
def list_directory(
    path: str | None = None,
    kind: Literal["mover", "ftp"] = "mover",
    show_hidden: bool = False,
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
    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []

    try:
        with os.scandir(safe_path) as entries:
            for entry in entries:
                name = entry.name
                if not show_hidden and name.startswith("."):
                    continue
                if safe_path == data_root and name in VIRTUAL_ROOT_DIRS:
                    continue
                full = os.path.join(safe_path, name)
                try:
                    is_dir = entry.is_dir(follow_symlinks=True)
                    if is_dir:
                        dirs.append({"name": name, "path": full})
                    elif entry.is_file(follow_symlinks=True):
                        st = entry.stat(follow_symlinks=True)
                        files.append(
                            {
                                "name": name,
                                "path": full,
                                "size_bytes": st.st_size,
                                "mtime": st.st_mtime,
                                "switch_type": detect_switch_type(name),
                                "queue_status": statuses.get(full, ""),
                            }
                        )
                except OSError:
                    continue
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"Sin permisos para leer {safe_path}") from exc

    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())

    parent = os.path.dirname(safe_path) if safe_path != data_root else None
    return {
        "path": safe_path,
        "parent": parent,
        "data_root": data_root,
        "bookmarks": _quick_bookmarks(data_root),
        "disk": _disk_free_bytes(safe_path),
        "dirs": dirs,
        "files": files,
    }


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
    return {"items": items}


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

