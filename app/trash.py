"""Papelera del explorador: los borrados se mueven a `<punto de montaje>/.toolbox-trash/<id>/`.

Mover dentro del mismo sistema de ficheros es un `rename` instantáneo, así que borrar y restaurar no
copian datos. Los elementos caducan tras `trash_days` días y se purgan en segundo plano.
"""

from __future__ import annotations

import errno
import os
import shutil
import time
from typing import Any

from .config import resolve_safe_path
from .db import QueueDB
from .mover_worker import PROTECTED_TOP_DIRS
from .services import TRASH_DIR_NAME


class TrashError(ValueError):
    """Error de papelera con mensaje apto para mostrar al usuario."""


def _entry_size(path: str) -> int:
    if os.path.islink(path) or os.path.isfile(path):
        try:
            return os.lstat(path).st_size
        except OSError:
            return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                if not os.path.islink(full):
                    total += os.path.getsize(full)
            except OSError:
                continue
    return total


def find_trash_root(path: str, data_root: str) -> str:
    """Punto de montaje que contiene `path` (sin salir de DATA_ROOT) + `.toolbox-trash`."""
    norm_root = os.path.normpath(data_root)
    cur = os.path.dirname(os.path.normpath(path))
    while True:
        if cur == norm_root or os.path.ismount(cur):
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        try:
            if os.path.commonpath([norm_root, parent]) != norm_root:
                break
        except ValueError:
            break
        cur = parent
    return os.path.join(cur, TRASH_DIR_NAME)


def check_deletable(safe_p: str, data_root: str) -> None:
    """Mismas protecciones que el borrado directo: ni la raíz de datos ni carpetas del sistema."""
    norm_root = os.path.realpath(data_root)
    if safe_p == norm_root or safe_p == os.path.normpath(data_root):
        raise TrashError("No se permite borrar la raíz de datos")
    if os.path.dirname(safe_p) in (norm_root, os.path.normpath(data_root)) and os.path.basename(safe_p) in PROTECTED_TOP_DIRS:
        raise TrashError(f"Directorio del sistema protegido: {os.path.basename(safe_p)}")
    if os.path.basename(safe_p) == TRASH_DIR_NAME:
        raise TrashError("La papelera se vacía desde la sección Papelera")


def _is_inside_trash(path: str) -> bool:
    return f"/{TRASH_DIR_NAME}/" in os.path.normpath(path) + "/"


def _remove_path(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


def _unique_restore_path(path: str) -> str:
    if not os.path.lexists(path):
        return path
    parent, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    if os.path.isdir(path):
        stem, ext = name, ""
    n = 1
    while True:
        candidate = os.path.join(parent, f"{stem} (restaurado {n}){ext}" if n > 1 else f"{stem} (restaurado){ext}")
        if not os.path.lexists(candidate):
            return candidate
        n += 1


class TrashManager:
    def __init__(self, db: QueueDB, data_root: str) -> None:
        self.db = db
        self.data_root = os.path.normpath(data_root)

    # ------------------------------------------------------------------
    def delete(self, paths: list[str], trash_days: int, permanent: bool = False) -> dict[str, Any]:
        """Envía a la papelera (o borra si `permanent` o `trash_days == 0`). Devuelve {trashed, deleted}."""
        trashed: list[dict[str, Any]] = []
        deleted = 0
        direct = permanent or trash_days <= 0
        # Validar todo antes de tocar nada
        targets: list[str] = []
        for raw in paths:
            try:
                safe_p = resolve_safe_path(raw, self.data_root)
            except ValueError as exc:
                raise TrashError(str(exc)) from exc
            check_deletable(safe_p, self.data_root)
            if os.path.lexists(safe_p):
                targets.append(safe_p)

        for safe_p in targets:
            if direct or _is_inside_trash(safe_p):
                _remove_path(safe_p)
                deleted += 1
                continue
            trash_root = find_trash_root(safe_p, self.data_root)
            is_dir = os.path.isdir(safe_p) and not os.path.islink(safe_p)
            size = _entry_size(safe_p)
            expires = time.time() + trash_days * 86400
            trash_id = self.db.add_trash_entry(safe_p, os.path.basename(safe_p), is_dir, size, expires)
            dest_dir = os.path.join(trash_root, str(trash_id))
            dest = os.path.join(dest_dir, os.path.basename(safe_p))
            try:
                os.makedirs(dest_dir, exist_ok=True)
                os.rename(safe_p, dest)
            except OSError as exc:
                self.db.remove_trash_entry(trash_id)
                try:
                    os.rmdir(dest_dir)
                except OSError:
                    pass
                if exc.errno == errno.EXDEV:
                    raise TrashError(
                        f"No se puede enviar '{os.path.basename(safe_p)}' a la papelera: está en otro sistema de "
                        "ficheros que la papelera. Bórralo definitivamente (permanent)."
                    ) from exc
                if exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM):
                    raise TrashError(
                        f"No se puede enviar '{os.path.basename(safe_p)}' a la papelera ({exc.strerror}). "
                        "Bórralo definitivamente (permanent)."
                    ) from exc
                raise
            self.db.set_trash_path(trash_id, dest)
            trashed.append({"id": trash_id, "original_path": safe_p})
        return {"trashed": trashed, "deleted": deleted}

    # ------------------------------------------------------------------
    def list(self) -> list[dict[str, Any]]:
        self.purge_expired()
        items = []
        for row in self.db.list_trash():
            items.append(
                {
                    "id": row["id"],
                    "original_path": row["original_path"],
                    "name": row["name"],
                    "trashed_at": row["trashed_at"],
                    "expires_at": row["expires_at"],
                    "size": row["size"],
                    "is_dir": bool(row["is_dir"]),
                }
            )
        return items

    def restore(self, ids: list[int]) -> list[dict[str, Any]]:
        restored: list[dict[str, Any]] = []
        for trash_id in ids:
            row = self.db.get_trash_entry(int(trash_id))
            if not row:
                continue
            src = row["trash_path"]
            if not src or not os.path.lexists(src):
                self.db.remove_trash_entry(row["id"])
                continue
            target = _unique_restore_path(row["original_path"])
            try:
                resolve_safe_path(target, self.data_root)
            except ValueError as exc:
                raise TrashError(str(exc)) from exc
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.rename(src, target)
            self._cleanup_slot(src)
            self.db.remove_trash_entry(row["id"])
            restored.append({"id": row["id"], "path": target, "renamed": target != row["original_path"]})
        return restored

    def empty(self, ids: list[int] | None = None) -> int:
        rows = self.db.list_trash()
        if ids is not None:
            wanted = {int(i) for i in ids}
            rows = [r for r in rows if r["id"] in wanted]
        removed = 0
        for row in rows:
            src = row["trash_path"]
            if src and os.path.lexists(src):
                _remove_path(src)
                self._cleanup_slot(src)
            self.db.remove_trash_entry(row["id"])
            removed += 1
        return removed

    def purge_expired(self, now: float | None = None) -> int:
        now = now or time.time()
        expired = [r["id"] for r in self.db.list_trash() if r["expires_at"] and r["expires_at"] <= now]
        return self.empty(expired) if expired else 0

    @staticmethod
    def _cleanup_slot(trash_path: str) -> None:
        """Borra la carpeta <id>/ vacía y la propia .toolbox-trash si quedó vacía."""
        slot = os.path.dirname(trash_path)
        for d in (slot, os.path.dirname(slot)):
            try:
                os.rmdir(d)
            except OSError:
                break
