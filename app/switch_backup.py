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
import shutil
import tarfile
import threading
import time
from datetime import datetime
from typing import Any, Callable

from .config import DEFAULT_BACKUP_EXCLUDE, DEFAULT_BACKUP_EXCLUDE_EXT, normalize_remote_path, parse_extensions, parse_patterns, resolve_safe_path

BACKUP_PREFIX = "switch-sd"
# Backups con fecha (carpeta o .tar.gz): los únicos que se borran al aplicar «conservar los últimos N»
DATED_BACKUP_RE = re.compile(rf"^{BACKUP_PREFIX}-\d{{8}}-\d{{6}}(\.tar\.gz)?$")
BACKUP_NAME_RE = re.compile(rf"^{BACKUP_PREFIX}(-\d{{8}}-\d{{6}})?(\.tar\.gz)?$")
TAR_SUFFIX = ".tar.gz"
# Carpeta de trabajo de un backup en curso (oculta: no aparece en la lista ni cuenta para «conservar N»)
WORKDIR_SUFFIX = ".tmp"
WORKDIR_RE = re.compile(rf"^\.{BACKUP_PREFIX}-\d{{8}}-\d{{6}}{re.escape(WORKDIR_SUFFIX)}$")
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
            if entries or state.get("mlsd") is True:
                state["mlsd"] = True
                return entries
            # Primera carpeta y MLSD vacío: algunos servidores (el FTP de DBI) anuncian MLST y
            # responden 226 a MLSD sin enviar nada. Se confirma con LIST antes de fiarse.
            listed = _list_with_list(ftp, path)
            state["mlsd"] = not listed
            return listed
        except ftplib.error_perm:
            if state.get("mlsd") is True:
                raise  # El servidor soporta MLSD: el error es de esta carpeta (permisos, no existe…)
            state["mlsd"] = False  # Primera carpeta: el servidor no soporta MLSD, usar LIST
    return _list_with_list(ftp, path)


def _list_with_list(ftp: ftplib.FTP, path: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
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
    measure_excluded: bool = False,
) -> dict[str, Any]:
    """Recorre el árbol remoto y devuelve los ficheros a copiar y lo excluido.

    Con `measure_excluded` también recorre las carpetas excluidas para saber cuánto ocupan
    (lo usa la simulación; en un backup normal no se entra en ellas)."""
    root = normalize_remote_path(root)
    files: list[dict[str, Any]] = []
    excluded_dirs: list[str] = []
    excluded_files = 0
    excluded_bytes = 0
    unknown_sizes: list[str] = []
    errors: list[str] = []
    state: dict[str, Any] = {}
    excluded_detail: list[dict[str, Any]] = []
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
                    if measure_excluded:
                        excluded_detail.append(
                            {"path": "/" + rel, **_measure_remote_dir(ftp, posixpath.join(root, rel), state, stop_event)}
                        )
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
        "excluded_detail": excluded_detail,
        "errors": errors,
        "mlsd": bool(state.get("mlsd")),
    }


def _measure_remote_dir(
    ftp: ftplib.FTP, remote_dir: str, state: dict[str, Any], stop_event: threading.Event | None
) -> dict[str, Any]:
    """Ficheros y bytes de una carpeta remota (recursivo). Los errores de listado se ignoran."""
    files = 0
    total = 0
    pending = [remote_dir]
    while pending:
        if stop_event is not None and stop_event.is_set():
            raise BackupStopped()
        cur = pending.pop()
        try:
            entries = list_remote(ftp, cur, state)
        except ftplib.error_perm:
            continue
        for e in entries:
            if e["is_dir"]:
                pending.append(posixpath.join(cur, e["name"]))
            else:
                files += 1
                total += int(e["size"] or 0)
    return {"files": files, "bytes": total}


def summarize_top_dirs(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Agrupa los ficheros a copiar por carpeta de primer nivel (mayor primero)."""
    groups: dict[str, dict[str, Any]] = {}
    for f in files:
        top = f["rel"].split("/", 1)[0] if "/" in f["rel"] else "(raíz)"
        g = groups.setdefault(top, {"path": "/" + top if top != "(raíz)" else "/", "files": 0, "bytes": 0})
        g["files"] += 1
        g["bytes"] += int(f["size"] or 0)
    return sorted(groups.values(), key=lambda g: (-g["bytes"], g["path"].lower()))


def backup_names(base_dir: str, now: datetime | None = None) -> tuple[str, str]:
    """(fichero .tar.gz final, carpeta de trabajo oculta donde se descarga antes de comprimir)."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    name = f"{BACKUP_PREFIX}-{stamp}"
    return os.path.join(base_dir, name + TAR_SUFFIX), os.path.join(base_dir, f".{name}{WORKDIR_SUFFIX}")


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
        extra_exclude: list[str] | None = None,
        stop_event: threading.Event | None = None,
        log_cb: LogCallback | None = None,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
        simulate: bool = False,
    ) -> None:
        self.settings = settings
        self.simulate = bool(simulate)
        self.data_root = data_root
        base = (dest_dir or settings.get("ftp_backup_dir") or "").strip()
        if not base:
            raise BackupError("Indica la carpeta local donde guardar el backup (ajuste «Carpeta de backups»)")
        try:
            self.base_dir = resolve_safe_path(base, data_root)
        except ValueError as exc:
            raise BackupError(str(exc)) from exc
        self.remote_root = normalize_remote_path(settings.get("ftp_backup_remote_root") or "/")
        self.exclude_dirs = parse_patterns(settings.get("ftp_backup_exclude", DEFAULT_BACKUP_EXCLUDE))
        self.exclude_dirs += [p for p in parse_patterns(extra_exclude or []) if p not in self.exclude_dirs]
        self.exclude_ext = parse_extensions(settings.get("ftp_backup_exclude_ext", DEFAULT_BACKUP_EXCLUDE_EXT))
        self.keep = max(0, int(settings.get("ftp_backup_keep") or 0))
        self.retries = max(0, int(settings.get("ftp_retries", 3)))
        self.retry_wait = max(1, int(settings.get("ftp_retry_wait", 10)))
        self.stop_event = stop_event or threading.Event()
        self.log_cb = log_cb or (lambda *_a: None)
        self.progress_cb = progress_cb
        # Siempre se guarda comprimido: se descarga en `workdir` y al terminar se empaqueta en `target`
        self.target, self.workdir = backup_names(self.base_dir)
        host = (settings.get("ftp_host") or "").strip()
        # Sin '/' en la etiqueta: el historial muestra el nombre base del origen
        self.source_label = f"microSD Switch ({host}:{int(settings.get('ftp_port') or 5000)})"
        self.progress: dict[str, Any] = {
            "phase": "scanning",
            "simulate": self.simulate,
            "target": self.target,
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
            "pruned": [],
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
        res = scan_remote_tree(
            ftp, self.remote_root, self.exclude_dirs, self.exclude_ext, self.stop_event, on_dir,
            measure_excluded=self.simulate,
        )
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
        local = _local_path(self.workdir, f["rel"])
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

    def _compress(self) -> None:
        """Empaqueta la carpeta de trabajo en el .tar.gz final y la borra."""
        self.progress.update({"phase": "compressing", "current": os.path.basename(self.target)})
        self._emit(force=True)
        part = self.target + _PART_SUFFIX
        try:
            with tarfile.open(part, "w:gz", compresslevel=6) as tar:
                for dirpath, dirnames, filenames in os.walk(self.workdir):
                    dirnames.sort()
                    for name in sorted(filenames):
                        if name.endswith(_PART_SUFFIX):
                            continue
                        self._check_stop()
                        full = os.path.join(dirpath, name)
                        self.progress["current"] = os.path.relpath(full, self.workdir)
                        self._emit()
                        tar.add(full, arcname=os.path.relpath(full, self.workdir), recursive=False)
            os.replace(part, self.target)
        except BaseException:
            try:
                os.unlink(part)
            except OSError:
                pass
            raise
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.progress["archive_bytes"] = os.path.getsize(self.target)
        self.log_cb("ftp", "INFO", f"Backup comprimido en {self.target} ({_fmt(self.progress['archive_bytes'])})")

    def _clean_workdirs(self, all_stale: bool = False) -> None:
        """Borra la carpeta de trabajo de esta ejecución (o, al empezar, las que dejó un corte)."""
        targets = [self.workdir]
        if all_stale:
            try:
                targets = [os.path.join(self.base_dir, n) for n in os.listdir(self.base_dir) if WORKDIR_RE.match(n)]
            except OSError:
                targets = []
        for d in targets:
            shutil.rmtree(d, ignore_errors=True)

    def _prune(self) -> None:
        """Conserva solo los `keep` backups con fecha más recientes (carpetas o .tar.gz)."""
        try:
            names = sorted((n for n in os.listdir(self.base_dir) if DATED_BACKUP_RE.match(n)), reverse=True)
        except OSError:
            return
        for name in names[self.keep:]:
            full = os.path.join(self.base_dir, name)
            try:
                if os.path.isdir(full) and not os.path.islink(full):
                    shutil.rmtree(full)
                else:
                    os.unlink(full)
            except OSError as exc:
                self.log_cb("ftp", "WARN", f"No se pudo borrar el backup antiguo {full}: {exc}")
                continue
            self.progress["pruned"].append(name)
        if self.progress["pruned"]:
            self.log_cb(
                "ftp", "INFO", f"Backups antiguos borrados (se conservan {self.keep}): {', '.join(self.progress['pruned'])}"
            )

    def run(self) -> dict[str, Any]:
        """Ejecuta el backup completo. Devuelve el resumen final (status OK / ERR / STOP)."""
        status = "OK"
        error = ""
        try:
            if self.simulate:
                self.log_cb("ftp", "INFO", "Simulación del backup de la microSD iniciada")
            else:
                self.log_cb("ftp", "INFO", f"Backup de la microSD iniciado -> {self.target}")
            res = self.scan()
            self._check_stop()
            files = res["files"]
            if self.simulate:
                self.progress["top_dirs"] = summarize_top_dirs(files)
                self.progress["excluded_detail"] = res["excluded_detail"]
                return self._finish("SIM", "")
            os.makedirs(self.base_dir, exist_ok=True)
            self._clean_workdirs(all_stale=True)  # restos de un backup que se cortó (reinicio, etc.)
            # Hace falta sitio para lo descargado y, mientras se comprime, también para el .tar.gz
            needed = self.progress["bytes_total"] * 2
            st = os.statvfs(self.base_dir)
            free = st.f_bavail * st.f_frsize
            if needed > free:
                raise BackupError(
                    f"Espacio insuficiente en {self.base_dir}: hacen falta {_fmt(needed)} "
                    f"(lo descargado y el .tar.gz mientras se comprime) y quedan {_fmt(free)}"
                )
            os.makedirs(self.workdir, exist_ok=True)
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
            # Se comprime aunque algún fichero haya fallado: el backup parcial sigue siendo útil
            self._compress()
            if self.progress["files_failed"]:
                status = "ERR"
                error = f"{self.progress['files_failed']} fichero(s) con error (el resto está en el .tar.gz)"
            elif self.keep:
                self._prune()
        except BackupStopped:
            status = "STOP"
            error = "detenido por el usuario"
        except BackupError as exc:
            status = "ERR"
            error = str(exc)
        except (OSError, EOFError, ftplib.Error) as exc:
            status = "ERR"
            error = f"Error FTP: {exc}" if isinstance(exc, (EOFError, ftplib.Error)) else f"Error: {exc}"
        finally:
            self._drop_ftp()
            if not self.simulate:
                self._clean_workdirs()  # detenido o con error antes de comprimir: no dejar la carpeta a medias
        return self._finish(status, error)

    def _finish(self, status: str, error: str) -> dict[str, Any]:
        self._drop_ftp()
        p = self.progress
        p.update({"phase": "done", "status": status, "error": error, "finished_at": time.time(), "current": ""})
        if status == "SIM":
            self.log_cb(
                "ftp",
                "INFO",
                f"Simulación del backup: se copiarían {p['files_total']} fichero(s), {_fmt(p['bytes_total'])}"
                f" (excluidas {len(p['excluded_dirs'])} carpeta(s))",
            )
        elif status == "OK":
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

    def start(
        self,
        dest_dir: str | None = None,
        extra_exclude: list[str] | None = None,
        simulate: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise BackupError("Ya hay un backup en marcha")
            self._stop_event = threading.Event()
            run = BackupRun(
                self.settings_store.get(),
                self.data_root,
                dest_dir=dest_dir,
                extra_exclude=extra_exclude,
                stop_event=self._stop_event,
                log_cb=self.db.add_log,
                progress_cb=self._set_progress,
                simulate=simulate,
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


# ---------------------------------------------------------------------------
# Backups guardados y restauración
# ---------------------------------------------------------------------------
def _dir_size(path: str) -> tuple[int, int]:
    files = 0
    total = 0
    for dirpath, _dirs, filenames in os.walk(path):
        for name in filenames:
            if name.endswith(_PART_SUFFIX):
                continue
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                files += 1
            except OSError:
                pass
    return files, total


def list_backups(base_dir: str) -> list[dict[str, Any]]:
    """Backups de `base_dir` (carpetas `switch-sd*` y `.tar.gz`), del más reciente al más antiguo."""
    out: list[dict[str, Any]] = []
    try:
        names = os.listdir(base_dir)
    except OSError:
        return out
    for name in names:
        if not BACKUP_NAME_RE.match(name):
            continue
        full = os.path.join(base_dir, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        if name.endswith(TAR_SUFFIX):
            if not os.path.isfile(full):
                continue
            item = {"name": name, "path": full, "kind": "tar", "size": st.st_size, "files": None}
        else:
            if not os.path.isdir(full):
                continue
            files, size = _dir_size(full)
            item = {"name": name, "path": full, "kind": "dir", "size": size, "files": files}
        item["mtime"] = st.st_mtime
        item["incremental"] = name.startswith(BACKUP_PREFIX) and name.removesuffix(TAR_SUFFIX) == BACKUP_PREFIX
        out.append(item)
    out.sort(key=lambda b: b["mtime"], reverse=True)
    return out


def _backup_root(path: str) -> str | None:
    """Carpeta `switch-sd*` que contiene `path` (o el propio `path`)."""
    cur = os.path.normpath(path)
    while True:
        if BACKUP_NAME_RE.match(os.path.basename(cur)) and os.path.isdir(cur):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def extract_backup(tar_path: str) -> str:
    """Descomprime un backup .tar.gz junto a él (si no estaba ya) y devuelve la carpeta."""
    folder = tar_path[: -len(TAR_SUFFIX)]
    if os.path.isdir(folder):
        return folder
    tmp = folder + ".extrayendo"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(tmp, filter="data")  # sin rutas absolutas, '..' ni enlaces peligrosos
        os.replace(tmp, folder)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return folder


def plan_restore(path: str, data_root: str, remote_root: str) -> dict[str, Any]:
    """Prepara la subida a la SD de un backup (o de una carpeta o fichero dentro de él).

    Devuelve {root, entries} con los elementos para la cola FTP: cada fichero va a
    `remote_root/<ruta dentro del backup>`, es decir, a su sitio original en la SD.
    """
    from .mover_worker import file_signature  # import diferido: evita ciclos al cargar la app

    try:
        safe = resolve_safe_path(path, data_root)
    except ValueError as exc:
        raise BackupError(str(exc)) from exc
    if safe.endswith(TAR_SUFFIX) and os.path.isfile(safe):
        safe = extract_backup(safe)
    root = _backup_root(safe)
    if root is None:
        raise BackupError("La ruta no es un backup de la microSD (carpeta switch-sd… o .tar.gz)")
    if os.path.isfile(safe):
        candidates = [safe]
    elif os.path.isdir(safe):
        candidates = []
        for dirpath, dirnames, filenames in os.walk(safe):
            dirnames.sort()
            candidates.extend(os.path.join(dirpath, n) for n in sorted(filenames) if not n.endswith(_PART_SUFFIX))
    else:
        raise BackupError(f"No existe: {safe}")
    remote_root = normalize_remote_path(remote_root or "/")
    entries: list[dict[str, Any]] = []
    for full in candidates:
        rel = os.path.relpath(full, root).replace(os.sep, "/")
        try:
            size = os.path.getsize(full)
            sig = file_signature(full)
        except OSError:
            continue
        entries.append(
            {
                "kind": "ftp",
                "operation": "subir",
                "source_path": full,
                "dest_path": normalize_remote_path(f"{remote_root}/{rel}"),
                "size_bytes": size,
                "source_sig": sig,
                "cleanup_dir": "",
                "switch_type": "",
            }
        )
    return {"root": root, "entries": entries}
