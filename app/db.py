"""Persistencia SQLite para la cola unificada (mover + ftp) y registro de actividad."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import get_config_dir, normalize_remote_path, resolve_safe_path

ACTIVE_STATUSES = ("PEND", "REAN", "PAUS", "PROC")
PENDING_STATUSES = ("PEND", "REAN")


class QueueDB:
    """Capa de acceso SQLite thread-safe para cola y logs."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            db_path = str(Path(get_config_dir()) / "queue.db")
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS queue_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('mover', 'ftp')),
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('PEND', 'REAN', 'PAUS', 'PROC', 'OK', 'ERR')),
                    source_path TEXT NOT NULL,
                    dest_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    source_sig TEXT NOT NULL DEFAULT '',
                    resume INTEGER NOT NULL DEFAULT 0,
                    retries_done INTEGER NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    cleanup_dir TEXT NOT NULL DEFAULT '',
                    switch_type TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    finished_at REAL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_kind_pos ON queue_items (kind, position, id)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                )
                """
            )
            # Historial de transferencias terminadas (independiente de la cola: "Limpiar OK" no lo borra)
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    kind TEXT NOT NULL,
                    op TEXT NOT NULL DEFAULT '',
                    src TEXT NOT NULL DEFAULT '',
                    dest TEXT NOT NULL DEFAULT '',
                    size INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    started_at REAL,
                    finished_at REAL NOT NULL,
                    duration_s REAL,
                    avg_bps REAL,
                    error TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_history_kind ON history (kind, id)")
            # Papelera: elementos borrados desde el explorador que aún se pueden restaurar
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trash (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_path TEXT NOT NULL,
                    trash_path TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    is_dir INTEGER NOT NULL DEFAULT 0,
                    size INTEGER NOT NULL DEFAULT 0,
                    trashed_at REAL NOT NULL,
                    expires_at REAL
                )
                """
            )
            # Recuperar elementos que quedaron en PROC tras un apagado brusco
            now = time.time()
            cur.execute(
                """
                UPDATE queue_items
                SET status = 'REAN', resume = 1, updated_at = ?, note = 'recuperado tras reinicio'
                WHERE status = 'PROC'
                """,
                (now,),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add_log(self, kind: str, level: str, message: str) -> None:
        now = time.time()
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO logs (ts, kind, level, message) VALUES (?, ?, ?, ?)",
                (now, kind, level, message),
            )
            # Mantener acotado el historial de logs en SQLite
            cur.execute(
                "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 5000)"
            )
            self._conn.commit()

    def get_logs(self, kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 1000))
        with self._lock:
            cur = self._conn.cursor()
            if kind and kind != "todos":
                rows = cur.execute(
                    "SELECT id, ts, kind, level, message FROM logs WHERE kind = ? ORDER BY id DESC LIMIT ?",
                    (kind, limit),
                ).fetchall()
            else:
                rows = cur.execute(
                    "SELECT id, ts, kind, level, message FROM logs ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def clear_logs(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM logs")
            self._conn.commit()

    def list_items(self, kind: str | None = None, only_pending: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            clauses: list[str] = []
            params: list[Any] = []
            if kind:
                clauses.append("kind = ?")
                params.append(kind)
            if only_pending:
                clauses.append("status IN ('PEND', 'REAN', 'PAUS', 'PROC')")
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = self._conn.execute(
                f"SELECT * FROM queue_items {where} ORDER BY position ASC, id ASC",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_item(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def get_active_dest_paths(self, kind: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT dest_path FROM queue_items WHERE kind = ? AND status IN ('PEND', 'REAN', 'PAUS', 'PROC')",
                (kind,),
            ).fetchall()
        return {r["dest_path"] for r in rows}

    def get_source_statuses(self, kind: str) -> dict[str, str]:
        """Devuelve un mapa source_path -> estado prioritario para marcar el explorador."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_path, status FROM queue_items WHERE kind = ? ORDER BY id ASC",
                (kind,),
            ).fetchall()
        priority = {"PROC": 4, "PEND": 3, "REAN": 3, "PAUS": 2, "OK": 1, "ERR": 0}
        result: dict[str, str] = {}
        for r in rows:
            sp = r["source_path"]
            st = r["status"]
            if sp not in result or priority.get(st, 0) >= priority.get(result[sp], 0):
                result[sp] = st
        return result

    def add_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Inserta elementos nuevos en la cola evitando colisiones de ruta destino activa."""
        if not items:
            return []
        now = time.time()
        inserted: list[dict[str, Any]] = []
        with self._lock:
            cur = self._conn.cursor()
            row = cur.execute("SELECT COALESCE(MAX(position), 0) AS m FROM queue_items").fetchone()
            pos = int(row["m"])
            for entry in items:
                kind = entry["kind"]
                dest_path = entry["dest_path"]
                dup = cur.execute(
                    """
                    SELECT 1 FROM queue_items
                    WHERE kind = ? AND dest_path = ? AND status IN ('PEND', 'REAN', 'PAUS', 'PROC')
                    LIMIT 1
                    """,
                    (kind, dest_path),
                ).fetchone()
                if dup:
                    continue
                pos += 1
                cur.execute(
                    """
                    INSERT INTO queue_items (
                        position, kind, operation, status, source_path, dest_path,
                        size_bytes, source_sig, resume, retries_done, note, error,
                        cleanup_dir, switch_type, created_at, updated_at
                    ) VALUES (?, ?, ?, 'PEND', ?, ?, ?, ?, 0, 0, '', '', ?, ?, ?, ?)
                    """,
                    (
                        pos,
                        kind,
                        entry["operation"],
                        entry["source_path"],
                        dest_path,
                        int(entry.get("size_bytes", 0)),
                        entry.get("source_sig", ""),
                        entry.get("cleanup_dir", ""),
                        entry.get("switch_type", ""),
                        now,
                        now,
                    ),
                )
                new_id = cur.lastrowid
                new_row = cur.execute("SELECT * FROM queue_items WHERE id = ?", (new_id,)).fetchone()
                if new_row:
                    inserted.append(dict(new_row))
            self._conn.commit()
        return inserted

    def next_pending_item(self, kind: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM queue_items
                WHERE kind = ? AND status IN ('PEND', 'REAN')
                ORDER BY position ASC, id ASC
                LIMIT 1
                """,
                (kind,),
            ).fetchone()
        return dict(row) if row else None

    def update_item(self, item_id: int, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_item(item_id)
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [item_id]
        with self._lock:
            self._conn.execute(f"UPDATE queue_items SET {cols} WHERE id = ?", vals)
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def remove_item(self, item_id: int) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT status FROM queue_items WHERE id = ?", (item_id,)).fetchone()
            if not row or row["status"] == "PROC":
                return False
            self._conn.execute("DELETE FROM queue_items WHERE id = ?", (item_id,))
            self._conn.commit()
            return True

    def toggle_pause(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
            if not row:
                return None
            st = row["status"]
            if st == "PAUS":
                new_st = "REAN" if row["resume"] else "PEND"
            elif st in ("PEND", "REAN"):
                new_st = "PAUS"
            else:
                return dict(row)
            self._conn.execute(
                "UPDATE queue_items SET status = ?, updated_at = ? WHERE id = ?",
                (new_st, time.time(), item_id),
            )
            self._conn.commit()
            updated = self._conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
        return dict(updated) if updated else None

    def toggle_operation(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
            if not row or row["kind"] != "mover" or row["status"] == "PROC":
                return dict(row) if row else None
            new_op = "mover" if row["operation"] == "copiar" else "copiar"
            self._conn.execute(
                "UPDATE queue_items SET operation = ?, updated_at = ? WHERE id = ?",
                (new_op, time.time(), item_id),
            )
            self._conn.commit()
            updated = self._conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
        return dict(updated) if updated else None

    def move_position(self, item_id: int, direction: str) -> bool:
        """Mueve un ítem dentro de la cola de su mismo tipo (up, down, top)."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
            if not row or row["status"] == "PROC":
                return False
            kind = row["kind"]
            rows = self._conn.execute(
                "SELECT id, position, status FROM queue_items WHERE kind = ? ORDER BY position ASC, id ASC",
                (kind,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if item_id not in ids:
                return False
            idx = ids.index(item_id)

            if direction == "up" and idx > 0:
                if rows[idx - 1]["status"] == "PROC":
                    return False
                ids[idx - 1], ids[idx] = ids[idx], ids[idx - 1]
            elif direction == "down" and idx < len(ids) - 1:
                ids[idx + 1], ids[idx] = ids[idx], ids[idx + 1]
            elif direction == "top":
                # Colocar después de cualquier PROC en ejecución
                target = 0
                while target < len(rows) and rows[target]["status"] == "PROC":
                    target += 1
                ids.pop(idx)
                ids.insert(target, item_id)
            else:
                return False

            base_pos = [r["position"] for r in rows]
            for pos_val, qid in zip(sorted(base_pos), ids):
                self._conn.execute("UPDATE queue_items SET position = ? WHERE id = ?", (pos_val, qid))
            self._conn.commit()
            return True

    def reorder_items(self, kind: str, ordered_ids: list[int]) -> None:
        """Asigna posiciones según la lista ordered_ids manteniendo elementos PROC arriba."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, position, status FROM queue_items WHERE kind = ? ORDER BY position ASC, id ASC",
                (kind,),
            ).fetchall()
            if not rows:
                return
            positions = sorted(r["position"] for r in rows)
            proc_ids = [r["id"] for r in rows if r["status"] == "PROC"]
            rest = [qid for qid in ordered_ids if qid not in proc_ids]
            existing_ids = {r["id"] for r in rows}
            final_ids = proc_ids + [qid for qid in rest if qid in existing_ids]
            for r in rows:
                if r["id"] not in final_ids:
                    final_ids.append(r["id"])
            for pos_val, qid in zip(positions, final_ids):
                self._conn.execute("UPDATE queue_items SET position = ? WHERE id = ?", (pos_val, qid))
            self._conn.commit()

    def retry_errors(self, kind: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE queue_items
                SET status = CASE WHEN resume = 1 THEN 'REAN' ELSE 'PEND' END,
                    error = '',
                    retries_done = 0,
                    updated_at = ?
                WHERE kind = ? AND status = 'ERR'
                """,
                (time.time(), kind),
            )
            self._conn.commit()
            return cur.rowcount

    def clear_by_status(self, kind: str, statuses: tuple[str, ...]) -> int:
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM queue_items WHERE kind = ? AND status IN ({placeholders})",
                (kind, *statuses),
            )
            self._conn.commit()
            return cur.rowcount

    def has_pending_cleanup(self, kind: str, cleanup_dir: str) -> bool:
        if not cleanup_dir:
            return True
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM queue_items
                WHERE kind = ? AND cleanup_dir = ? AND status IN ('PEND', 'REAN', 'PAUS', 'PROC')
                LIMIT 1
                """,
                (kind, cleanup_dir),
            ).fetchone()
        return row is not None

    def change_dest_dir(self, item_ids: list[int], new_dest_dir: str, data_root: str) -> int:
        """Cambia la carpeta destino de los elementos indicados que no estén en PROC ni OK."""
        if not item_ids:
            return 0
        updated = 0
        now = time.time()
        with self._lock:
            for item_id in item_ids:
                row = self._conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
                if not row or row["status"] in ("PROC", "OK"):
                    continue
                src = row["source_path"]
                cleanup_dir = row["cleanup_dir"] or ""
                if row["kind"] == "mover":
                    target_base = resolve_safe_path(new_dest_dir, data_root)
                    if cleanup_dir and src.startswith(cleanup_dir.rstrip("/") + "/"):
                        folder_name = os.path.basename(cleanup_dir.rstrip("/"))
                        rel_inside = os.path.relpath(src, cleanup_dir)
                        new_dest = os.path.join(target_base, folder_name, rel_inside)
                    else:
                        new_dest = os.path.join(target_base, os.path.basename(src))
                else:
                    target_base = normalize_remote_path(new_dest_dir)
                    new_dest = normalize_remote_path(f"{target_base}/{os.path.basename(src)}")

                self._conn.execute(
                    "UPDATE queue_items SET dest_path = ?, updated_at = ? WHERE id = ?",
                    (new_dest, now, item_id),
                )
                updated += 1
            self._conn.commit()
        return updated


    # ------------------------------------------------------------------
    # Historial de transferencias
    # ------------------------------------------------------------------
    HISTORY_MAX_ROWS = 20000

    def add_history(self, entry: dict[str, Any]) -> int:
        """Registra una transferencia terminada (OK o ERR)."""
        finished = float(entry.get("finished_at") or time.time())
        started = entry.get("started_at")
        duration = entry.get("duration_s")
        if duration is None and started:
            duration = max(0.0, finished - float(started))
        size = int(entry.get("size") or 0)
        avg_bps = entry.get("avg_bps")
        # Velocidad media solo con duraciones medibles (un `mv` en el mismo disco es instantáneo)
        if avg_bps is None and duration and duration >= 1 and entry.get("status") == "OK":
            avg_bps = size / duration
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT INTO history (
                    item_id, kind, op, src, dest, size, status, started_at,
                    finished_at, duration_s, avg_bps, error, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.get("item_id"),
                    entry["kind"],
                    entry.get("op") or "",
                    entry.get("src") or "",
                    entry.get("dest") or "",
                    size,
                    entry.get("status") or "ERR",
                    started,
                    finished,
                    duration,
                    avg_bps,
                    entry.get("error") or "",
                    entry.get("note") or "",
                ),
            )
            new_id = int(cur.lastrowid or 0)
            cur.execute(
                "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                (self.HISTORY_MAX_ROWS,),
            )
            self._conn.commit()
        return new_id

    def list_history(self, kind: str | None = None, limit: int = 100, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        where = ""
        params: list[Any] = []
        if kind and kind != "todos":
            where = "WHERE kind = ?"
            params.append(kind)
        with self._lock:
            total = int(self._conn.execute(f"SELECT COUNT(*) FROM history {where}", params).fetchone()[0])
            rows = self._conn.execute(
                f"""
                SELECT id, item_id, kind, op, src, dest, size, status, started_at, finished_at,
                       duration_s, avg_bps, error, note
                FROM history {where} ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows], total

    def clear_history(self, kind: str | None = None) -> int:
        with self._lock:
            if kind and kind != "todos":
                cur = self._conn.execute("DELETE FROM history WHERE kind = ?", (kind,))
            else:
                cur = self._conn.execute("DELETE FROM history")
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Papelera
    # ------------------------------------------------------------------
    def add_trash_entry(self, original_path: str, name: str, is_dir: bool, size: int, expires_at: float | None) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO trash (original_path, trash_path, name, is_dir, size, trashed_at, expires_at)
                VALUES (?, '', ?, ?, ?, ?, ?)
                """,
                (original_path, name, 1 if is_dir else 0, int(size), time.time(), expires_at),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def set_trash_path(self, trash_id: int, trash_path: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE trash SET trash_path = ? WHERE id = ?", (trash_path, trash_id))
            self._conn.commit()

    def get_trash_entry(self, trash_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM trash WHERE id = ?", (trash_id,)).fetchone()
        return dict(row) if row else None

    def list_trash(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM trash ORDER BY trashed_at DESC, id DESC").fetchall()
        return [dict(r) for r in rows]

    def remove_trash_entry(self, trash_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM trash WHERE id = ?", (trash_id,))
            self._conn.commit()
