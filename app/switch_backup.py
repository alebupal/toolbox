"""Copia de seguridad de la microSD de la Switch por FTP (descarga al servidor), sin las carpetas de juegos.

Recorre el árbol remoto (MLSD y, si el servidor no lo soporta, LIST), descarta las carpetas excluidas
(`Nintendo/Contents`, `/install`, `/nsp`…) y los ficheros de juego (`.nsp`, `.xci`…), y descarga el resto
a una carpeta local con `ftplib`, escribiendo cada fichero como `.part` y renombrándolo al terminar.

El mismo motor se usa desde la web (hilo en segundo plano, `BackupJob`) y desde `subeftp --backup`
(en primer plano cuando la web no responde).
"""

from __future__ import annotations

import fnmatch
import ftplib
import os
import posixpath
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable

from .config import DEFAULT_BACKUP_EXCLUDE, DEFAULT_BACKUP_EXCLUDE_EXT, normalize_remote_path, parse_extensions, parse_patterns, resolve_safe_path

BACKUP_PREFIX = "switch-sd"
_PART_SUFFIX = ".part"
_MLSD_MODIFY_RE = re.compile(r"^(\d{14})")

LogCallback = Callable[[str, str, str], None]


class BackupStopped(Exception):
    """El usuario ha pedido detener el backup."""


class BackupError(Exception):
    """Error que impide empezar o continuar el backup."""


# ---------------------------------------------------------------------------
# Exclusiones
# ---------------------------------------------------------------------------
def is_excluded_dir(rel_path: str, patterns: list[str]) -> bool:
    """`rel_path` es la ruta de la carpeta relativa a la raíz del backup (sin '/' inicial)."""
    segs = [s.lower() for s in rel_path.strip("/").split("/") if s]
    if not segs:
        return False
    for pat in patterns:
        anchored = pat.startswith("/")
        psegs = [s.lower() for s in pat.strip("/").split("/") if s]
        if not psegs or len(psegs) > len(segs):
            continue
        if anchored and len(psegs) != len(segs):
            continue
        tail = segs[-len(psegs):]
        if all(fnmatch.fnmatchcase(s, p) for s, p in zip(tail, psegs)):
            return True
    return False


def is_excluded_file(name: str, extensions: list[str]) -> bool:
    low = name.lower()
    return any(low.endswith("." + ext) for ext in extensions)


# ---------------------------------------------------------------------------
# FTP
# ---------------------------------------------------------------------------
def _connect(settings: dict[str, Any], timeout: float = 30.0) -> ftplib.FTP:
    host = (settings.get("ftp_host") or "").strip()
    if not host:
        raise BackupError("Servidor FTP no configurado")
    port = int(settings.get("ftp_port") or 5000)
    user = settings.get("ftp_user") or "anonymous"
    password = settings.get("ftp_password") or ("anonymous@local" if user == "anonymous" else "")
    ftp: ftplib.FTP = ftplib.FTP_TLS(timeout=timeout) if settings.get("ftp_tls") else ftplib.FTP(timeout=timeout)
    ftp.encoding = "utf-8"
    ftp.connect(host, port)
    ftp.login(user, password)
    if isinstance(ftp, ftplib.FTP_TLS):
        ftp.prot_p()
    ftp.set_pasv(bool(settings.get("ftp_passive", True)))
    try:
        ftp.voidcmd("TYPE I")
    except ftplib.Error:
        pass
    return ftp


def _close(ftp: ftplib.FTP | None) -> None:
    if ftp is None:
        return
    try:
        ftp.quit()
    except Exception:
        try:
            ftp.close()
        except Exception:
            pass


def _parse_list_line(line: str) -> tuple[str, bool, int | None] | None:
    """Interpreta una línea de LIST estilo Unix o DOS -> (nombre, es_carpeta, tamaño)."""
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    parts = line.split(None, 8)
    if len(parts) == 9 and parts[0][:1] in ("d", "-", "l"):
        kind = parts[0][0]
        if kind == "l":
            return None  # enlaces simbólicos: no se siguen
        name = parts[8]
        size = int(parts[4]) if (kind != "d" and parts[4].isdigit()) else None
        return name, kind == "d", size
    m = re.match(r"^\S+\s+\S+\s+(<DIR>|\d+)\s+(.+)$", line, re.IGNORECASE)
    if m:
        is_dir = m.group(1).upper() == "<DIR>"
        return m.group(2), is_dir, None if is_dir else int(m.group(1))
    return None


def _mlsd_mtime(facts: dict[str, str]) -> float | None:
    m = _MLSD_MODIFY_RE.match(facts.get("modify", ""))
    if not m:
        return None
    try:
        import calendar

        return float(calendar.timegm(time.strptime(m.group(1), "%Y%m%d%H%M%S")))
    except (ValueError, OverflowError):
        return None


def list_remote(ftp: ftplib.FTP, path: str, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Lista una carpeta remota. `state['mlsd']` recuerda si el servidor soporta MLSD."""
    entries: list[dict[str, Any]] = []
    if state.get("mlsd") is not False:
        try:
            for name, facts in ftp.mlsd(path, facts=["type", "size", "modify"]):
                typ = (facts.get("type") or "").lower()
                if name in (".", "..") or typ in ("cdir", "pdir"):
                    continue
                if typ == "dir":
                    entries.append({"name": name, "is_dir": True, "size": None, "mtime": None})
                elif typ == "file" or not typ:
                    size = facts.get("size")
                    entries.append(
                        {
                            "name": name,
                            "is_dir": False,
                            "size": int(size) if size and size.isdigit() else None,
                            "mtime": _mlsd_mtime(facts),
                        }
                    )
            state["mlsd"] = True
            return entries
        except ftplib.error_perm:
            if state.get("mlsd") is True:
                raise  # El servidor soporta MLSD: el error es de esta carpeta (permisos, no existe…)
            state["mlsd"] = False  # Primera carpeta: el servidor no soporta MLSD, usar LIST
    lines: list[str] = []
    ftp.cwd(path)
    ftp.retrlines("LIST", lines.append)
    for line in lines:
        parsed = _parse_list_line(line)
        if not parsed:
            continue
        name, is_dir, size = parsed
        if name in (".", ".."):
            continue
        entries.append({"name": name, "is_dir": is_dir, "size": size, "mtime": None})
    return entries


def scan_remote_tree(
    ftp: ftplib.FTP,
    root: str,
    exclude_dirs: list[str],
    exclude_ext: list[str],
    stop_event: threading.Event | None = None,
    on_dir: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """Recorre el árbol remoto y devuelve los ficheros a copiar y lo excluido."""
    root = normalize_remote_path(root)
    files: list[dict[str, Any]] = []
    excluded_dirs: list[str] = []
    excluded_files = 0
    excluded_bytes = 0
    unknown_sizes: list[str] = []
    errors: list[str] = []
    state: dict[str, Any] = {}
    pending = [""]
    while pending:
        if stop_event is not None and stop_event.is_set():
            raise BackupStopped()
        rel_dir = pending.pop()
        remote_dir = posixpath.join(root, rel_dir) if rel_dir else root
        try:
            entries = list_remote(ftp, remote_dir, state)
        except ftplib.error_perm as exc:
            errors.append(f"{remote_dir}: {exc}")
            continue
        subdirs: list[str] = []
        for e in sorted(entries, key=lambda x: x["name"].lower()):
            rel = posixpath.join(rel_dir, e["name"]) if rel_dir else e["name"]
            if e["is_dir"]:
                if is_excluded_dir(rel, exclude_dirs):
                    excluded_dirs.append("/" + rel)
                else:
                    subdirs.append(rel)
                continue
            if is_excluded_file(e["name"], exclude_ext):
                excluded_files += 1
                excluded_bytes += int(e["size"] or 0)
                continue
            if e["size"] is None:
                unknown_sizes.append(rel)
            files.append({"rel": rel, "remote": posixpath.join(root, rel), "size": e["size"], "mtime": e["mtime"]})
        pending.extend(reversed(subdirs))
        if on_dir is not None:
            on_dir(remote_dir, len(files), sum(int(f["size"] or 0) for f in files))
    # Tamaños que LIST no dio: preguntar con SIZE
    for f in files:
        if f["size"] is None:
            try:
                f["size"] = ftp.size(f["remote"])
            except ftplib.Error:
                f["size"] = None
    files.sort(key=lambda f: f["rel"].lower())
    return {
        "files": files,
        "excluded_dirs": excluded_dirs,
        "excluded_files": excluded_files,
        "excluded_bytes": excluded_bytes,
        "errors": errors,
        "mlsd": bool(state.get("mlsd")),
    }


def backup_target_dir(base_dir: str, incremental: bool, now: datetime | None = None) -> str:
    if incremental:
        return os.path.join(base_dir, BACKUP_PREFIX)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return os.path.join(base_dir, f"{BACKUP_PREFIX}-{stamp}")


def _local_path(target: str, rel: str) -> str:
    """Ruta local de un fichero remoto, sin permitir salir de `target`."""
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    full = os.path.normpath(os.path.join(target, *parts))
    if os.path.commonpath([os.path.normpath(target), full]) != os.path.normpath(target):
        raise BackupError(f"Ruta remota no válida: {rel}")
    return full


# ---------------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------------
class BackupRun:
    """Una ejecución del backup. `progress` se actualiza en vivo (lo leen la web y el CLI)."""

    def __init__(
        self,
        settings: dict[str, Any],
        data_root: str,
        dest_dir: str | None = None,
        incremental: bool | None = None,
        extra_exclude: list[str] | None = None,
        stop_event: threading.Event | None = None,
        log_cb: LogCallback | None = None,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.data_root = data_root
        base = (dest_dir or settings.get("ftp_backup_dir") or "").strip()
        if not base:
            raise BackupError("Indica la carpeta local donde guardar el backup (ajuste «Carpeta de backups»)")
        try:
            self.base_dir = resolve_safe_path(base, data_root)
        except ValueError as exc:
            raise BackupError(str(exc)) from exc
        self.incremental = bool(settings.get("ftp_backup_incremental")) if incremental is None else bool(incremental)
        self.remote_root = normalize_remote_path(settings.get("ftp_backup_remote_root") or "/")
        self.exclude_dirs = parse_patterns(settings.get("ftp_backup_exclude", DEFAULT_BACKUP_EXCLUDE))
        self.exclude_dirs += [p for p in parse_patterns(extra_exclude or []) if p not in self.exclude_dirs]
        self.exclude_ext = parse_extensions(settings.get("ftp_backup_exclude_ext", DEFAULT_BACKUP_EXCLUDE_EXT))
        self.retries = max(0, int(settings.get("ftp_retries", 3)))
        self.retry_wait = max(1, int(settings.get("ftp_retry_wait", 10)))
        self.stop_event = stop_event or threading.Event()
        self.log_cb = log_cb or (lambda *_a: None)
        self.progress_cb = progress_cb
        self.target = backup_target_dir(self.base_dir, self.incremental)
        host = (settings.get("ftp_host") or "").strip()
        # Sin '/' en la etiqueta: el historial muestra el nombre base del origen
        self.source_label = f"microSD Switch ({host}:{int(settings.get('ftp_port') or 5000)})"
        self.progress: dict[str, Any] = {
            "phase": "scanning",
            "target": self.target,
            "incremental": self.incremental,
            "remote_root": self.remote_root,
            "exclude_dirs": list(self.exclude_dirs),
            "exclude_ext": list(self.exclude_ext),
            "files_total": 0,
            "files_done": 0,
            "files_skipped": 0,
            "files_failed": 0,
            "bytes_skipped": 0,
            "bytes_total": 0,
            "bytes_done": 0,
            "current": "",
            "current_bytes": 0,
            "current_total": 0,
            "excluded_dirs": [],
            "excluded_files": 0,
            "excluded_bytes": 0,
            "errors": [],
            "started_at": time.time(),
        }
        self._ftp: ftplib.FTP | None = None
        self._last_emit = 0.0

    # -- utilidades --------------------------------------------------------
    def _emit(self, force: bool = False) -> None:
        if self.progress_cb is None:
            return
        now = time.monotonic()
        if force or now - self._last_emit >= 0.25:
            self._last_emit = now
            self.progress_cb(dict(self.progress))

    def _check_stop(self) -> None:
        if self.stop_event.is_set():
            raise BackupStopped()

    def _ensure_ftp(self) -> ftplib.FTP:
        if self._ftp is None:
            self._ftp = _connect(self.settings)
        return self._ftp

    def _drop_ftp(self) -> None:
        _close(self._ftp)
        self._ftp = None

    # -- fases -------------------------------------------------------------
    def scan(self) -> dict[str, Any]:
        def on_dir(remote_dir: str, nfiles: int, nbytes: int) -> None:
            self.progress.update({"current": remote_dir, "files_total": nfiles, "bytes_total": nbytes})
            self._emit()

        self._emit(force=True)
        ftp = self._ensure_ftp()
        res = scan_remote_tree(ftp, self.remote_root, self.exclude_dirs, self.exclude_ext, self.stop_event, on_dir)
        self.progress.update(
            {
                "files_total": len(res["files"]),
                "bytes_total": sum(int(f["size"] or 0) for f in res["files"]),
                "excluded_dirs": res["excluded_dirs"],
                "excluded_files": res["excluded_files"],
                "excluded_bytes": res["excluded_bytes"],
                "errors": list(res["errors"]),
                "current": "",
            }
        )
        return res

    def _download(self, f: dict[str, Any]) -> str:
        """Descarga un fichero. Devuelve 'ok' u 'omitido'."""
        local = _local_path(self.target, f["rel"])
        size = f["size"]
        if os.path.isfile(local) and size is not None and os.path.getsize(local) == size:
            return "omitido"
        os.makedirs(os.path.dirname(local), exist_ok=True)
        part = local + _PART_SUFFIX
        attempt = 0
        while True:
            self._check_stop()
            offset = os.path.getsize(part) if os.path.isfile(part) else 0
            if size is not None and offset > size:
                os.unlink(part)
                offset = 0
            self.progress.update({"current": f["rel"], "current_bytes": offset, "current_total": int(size or 0)})
            base_done = self.progress["bytes_done"]
            try:
                ftp = self._ensure_ftp()
                with open(part, "ab" if offset else "wb") as fh:
                    received = [offset]

                    def on_chunk(chunk: bytes) -> None:
                        if self.stop_event.is_set():
                            raise BackupStopped()
                        fh.write(chunk)
                        received[0] += len(chunk)
                        self.progress["current_bytes"] = received[0]
                        self.progress["bytes_done"] = base_done + received[0] - offset
                        self._emit()

                    if size == 0:
                        pass
                    elif offset and size is not None and offset == size:
                        pass
                    else:
                        ftp.retrbinary(f"RETR {f['remote']}", on_chunk, blocksize=64 * 1024, rest=offset or None)
                got = os.path.getsize(part)
                if size is not None and got != size:
                    raise OSError(f"tamaño descargado {got} B distinto del remoto {size} B")
                os.replace(part, local)
                if f.get("mtime"):
                    try:
                        os.utime(local, (f["mtime"], f["mtime"]))
                    except OSError:
                        pass
                self.progress["bytes_done"] = base_done + (got - offset)
                return "ok"
            except BackupStopped:
                self._drop_ftp()
                raise
            except ftplib.error_perm:
                self.progress["bytes_done"] = base_done
                if offset:
                    # El servidor no admite REST (reanudar): empezar el fichero desde cero
                    self._drop_ftp()
                    try:
                        os.unlink(part)
                    except OSError:
                        pass
                    continue
                # Permiso denegado o fichero inexistente: reintentar no lo arregla
                raise
            except (OSError, EOFError, ftplib.Error) as exc:
                self._drop_ftp()
                self.progress["bytes_done"] = base_done + max(0, (os.path.getsize(part) if os.path.isfile(part) else 0) - offset)
                attempt += 1
                if attempt > self.retries:
                    raise
                wait = min(600, self.retry_wait * (3 ** (attempt - 1)))
                self.log_cb("ftp", "WARN", f"Backup: reintento {attempt}/{self.retries} en {wait}s ({f['rel']}): {exc}")
                end = time.monotonic() + wait
                while time.monotonic() < end:
                    self._check_stop()
                    time.sleep(0.3)

    def run(self) -> dict[str, Any]:
        """Ejecuta el backup completo. Devuelve el resumen final (status OK / ERR / STOP)."""
        status = "OK"
        error = ""
        try:
            self.log_cb("ftp", "INFO", f"Backup de la microSD iniciado -> {self.target}")
            res = self.scan()
            self._check_stop()
            files = res["files"]
            needed = self.progress["bytes_total"]
            os.makedirs(self.base_dir, exist_ok=True)
            st = os.statvfs(self.base_dir)
            free = st.f_bavail * st.f_frsize
            if needed > free:
                raise BackupError(
                    f"Espacio insuficiente en {self.base_dir}: hacen falta {_fmt(needed)} y quedan {_fmt(free)}"
                )
            os.makedirs(self.target, exist_ok=True)
            self.progress["phase"] = "downloading"
            self._emit(force=True)
            for f in files:
                self._check_stop()
                try:
                    result = self._download(f)
                except BackupStopped:
                    raise
                except (OSError, EOFError, ftplib.Error, BackupError) as exc:
                    self.progress["files_failed"] += 1
                    msg = f"{f['rel']}: {exc}"
                    self.progress["errors"].append(msg)
                    self.log_cb("ftp", "ERROR", f"Backup: no se pudo copiar {msg}")
                    continue
                if result == "omitido":
                    self.progress["files_skipped"] += 1
                    self.progress["bytes_skipped"] += int(f["size"] or 0)
                    self.progress["bytes_done"] += int(f["size"] or 0)
                self.progress["files_done"] += 1
                self._emit()
            if self.progress["files_failed"]:
                status = "ERR"
                error = f"{self.progress['files_failed']} fichero(s) con error"
        except BackupStopped:
            status = "STOP"
            error = "detenido por el usuario"
        except BackupError as exc:
            status = "ERR"
            error = str(exc)
        except (OSError, EOFError, ftplib.Error) as exc:
            status = "ERR"
            error = f"Error FTP: {exc}"
        finally:
            self._drop_ftp()
        p = self.progress
        p.update({"phase": "done", "status": status, "error": error, "finished_at": time.time(), "current": ""})
        if status == "OK":
            self.log_cb(
                "ftp",
                "INFO",
                f"Backup de la microSD completado: {p['files_done']} fichero(s), {_fmt(p['bytes_done'])} en {self.target}"
                f" (omitidos sin cambios: {p['files_skipped']}; excluidas {len(p['excluded_dirs'])} carpeta(s) de juegos)",
            )
        elif status == "STOP":
            self.log_cb("ftp", "INFO", f"Backup de la microSD detenido ({p['files_done']}/{p['files_total']} ficheros)")
        else:
            self.log_cb("ftp", "ERROR", f"Backup de la microSD con errores: {error}")
        self._emit(force=True)
        return dict(p)


def _fmt(num: float) -> str:
    val = float(num or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


class BackupJob:
    """Backup en segundo plano para la web (un único backup a la vez)."""

    def __init__(self, db: Any, settings_store: Any, data_root: str) -> None:
        self.db = db
        self.settings_store = settings_store
        self.data_root = data_root
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._progress: dict[str, Any] | None = None
        self._last: dict[str, Any] | None = None

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self, dest_dir: str | None = None, incremental: bool | None = None, extra_exclude: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise BackupError("Ya hay un backup en marcha")
            self._stop_event = threading.Event()
            run = BackupRun(
                self.settings_store.get(),
                self.data_root,
                dest_dir=dest_dir,
                incremental=incremental,
                extra_exclude=extra_exclude,
                stop_event=self._stop_event,
                log_cb=self.db.add_log,
                progress_cb=self._set_progress,
            )
            self._progress = dict(run.progress)
            self._thread = threading.Thread(target=self._run, args=(run,), name="switch-backup", daemon=True)
            self._thread.start()
        return self.status()

    def _set_progress(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._progress = data

    def _run(self, run: BackupRun) -> None:
        try:
            result = run.run()
        except Exception as exc:  # pragma: no cover - nunca dejar el hilo sin resultado
            result = dict(run.progress)
            result.update({"phase": "done", "status": "ERR", "error": f"Error inesperado: {exc}", "finished_at": time.time()})
            self.db.add_log("ftp", "ERROR", f"Backup de la microSD: error inesperado: {exc}")
        record_history(self.db, run, result)
        with self._lock:
            self._last = result
            self._progress = None

    def stop(self) -> bool:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False
            self._stop_event.set()
        self.db.add_log("ftp", "INFO", "Solicitada detención del backup de la microSD")
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            current = dict(self._progress) if (running and self._progress) else None
            last = dict(self._last) if self._last else None
            stopping = running and self._stop_event.is_set()
        return {"running": running, "stopping": stopping, "progress": current, "last": last}


def record_history(db: Any, run: BackupRun, result: dict[str, Any]) -> None:
    status = result.get("status")
    if status not in ("OK", "ERR"):
        return
    copied = max(0, int(result.get("bytes_done") or 0) - int(result.get("bytes_skipped") or 0))
    started = result.get("started_at")
    finished = result.get("finished_at") or time.time()
    duration = max(0.0, finished - float(started)) if started else None
    try:
        db.add_history(
            {
                "item_id": None,
                "kind": "ftp",
                "op": "backup",
                "src": run.source_label,
                "dest": run.target,
                "size": int(result.get("bytes_done") or 0),
                "status": status,
                "started_at": started,
                "finished_at": finished,
                "duration_s": duration,
                "avg_bps": (copied / duration) if (duration and duration >= 1 and copied) else None,
                "error": result.get("error", ""),
                "note": f"{result.get('files_done', 0)} fichero(s), {result.get('files_skipped', 0)} sin cambios",
            }
        )
    except Exception as exc:  # pragma: no cover
        db.add_log("ftp", "WARN", f"No se pudo registrar el backup en el historial: {exc}")
