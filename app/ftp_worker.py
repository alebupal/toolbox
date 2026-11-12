"""Motor de transferencia FTP/FTPS basado en curl con reanudación, límite KB/s y verificación."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
from typing import Any, Callable

from .config import normalize_remote_path

ProgressCallback = Callable[[dict[str, Any]], None]

# Códigos de curl que no se arreglan reintentando (login fallido, acceso denegado, etc.)
NON_RETRYABLE_CURL_CODES = {9, 67, 69, 78}


def _fmt_size(num: float) -> str:
    val = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


def build_ftp_url(host: str, port: int, remote_path: str, tls: bool = False) -> str:
    clean_rel = remote_path.lstrip("/")
    encoded = "/".join(urllib.parse.quote(seg, safe="") for seg in clean_rel.split("/") if seg)
    scheme = "ftp"
    # curl usa ftp:// + --ssl-reqd para FTPS explícito
    return f"{scheme}://{host}:{port}/{encoded}"


def _write_curl_auth_file(settings: dict[str, Any]) -> str:
    user = settings.get("ftp_user") or ""
    password = settings.get("ftp_password") or ""
    fd, cfg_path = tempfile.mkstemp(prefix="curl_ftp_", suffix=".cfg")
    try:
        os.chmod(cfg_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if user:
                safe_user = user.replace("\\", "\\\\").replace('"', '\\"')
                safe_pw = password.replace("\\", "\\\\").replace('"', '\\"')
                fh.write(f'user = "{safe_user}:{safe_pw}"\n')
            else:
                fh.write('user = "anonymous:anonymous@local"\n')
    except Exception:
        try:
            os.unlink(cfg_path)
        except OSError:
            pass
        raise
    return cfg_path


def _base_curl_opts(settings: dict[str, Any], cfg_path: str) -> list[str]:
    opts = [
        "--globoff",
        "--connect-timeout",
        "30",
        "--speed-time",
        "30",
        "--speed-limit",
        "100",
        "--tcp-nodelay",
        "--keepalive-time",
        "20",
        "-K",
        cfg_path,
    ]
    if settings.get("ftp_tls"):
        opts.append("--ssl-reqd")
    if settings.get("ftp_passive", True):
        opts.append("--disable-epsv")
    else:
        opts.extend(["--ftp-port", "-"])
    return opts


def test_ftp_connection(settings: dict[str, Any]) -> dict[str, Any]:
    host = (settings.get("ftp_host") or "").strip()
    port = int(settings.get("ftp_port") or 5000)
    if not host:
        return {"ok": False, "message": "No se ha configurado la IP o servidor FTP."}

    cfg_path = _write_curl_auth_file(settings)
    try:
        url = build_ftp_url(host, port, "/")
        cmd = ["curl", "-sS", *_base_curl_opts(settings, cfg_path), "--list-only", url]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or f"curl código {proc.returncode}").strip()
            return {"ok": False, "message": f"Error conectando a {host}:{port}: {err}"}

        remote_dir = (settings.get("ftp_remote_dir") or "/").strip()
        if remote_dir and remote_dir != "/":
            dir_url = build_ftp_url(host, port, remote_dir).rstrip("/") + "/"
            proc_dir = subprocess.run(
                ["curl", "-sS", *_base_curl_opts(settings, cfg_path), "--list-only", dir_url],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc_dir.returncode != 0:
                return {
                    "ok": True,
                    "message": f"Conexión OK con {host}:{port}. Aviso: {remote_dir} aún no existe (se creará al subir).",
                }
        return {"ok": True, "message": f"Conexión correcta con {host}:{port}."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"Tiempo de espera agotado conectando a {host}:{port}."}
    finally:
        try:
            os.unlink(cfg_path)
        except OSError:
            pass


def list_remote_dirs(settings: dict[str, Any], remote_path: str = "/") -> dict[str, Any]:
    host = (settings.get("ftp_host") or "").strip()
    port = int(settings.get("ftp_port") or 5000)
    if not host:
        return {"ok": False, "error": "Servidor FTP no configurado", "dirs": []}

    cfg_path = _write_curl_auth_file(settings)
    try:
        url = build_ftp_url(host, port, remote_path).rstrip("/") + "/"
        cmd = ["curl", "-sS", *_base_curl_opts(settings, cfg_path), url]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or f"curl código {proc.returncode}").strip()
            return {"ok": False, "error": err, "dirs": []}

        dirs: list[str] = []
        for raw_line in proc.stdout.replace("\r", "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            m_unix = re.match(r"^d(?:\S+\s+){8}(.+)$", line)
            m_dos = re.match(r"^(?:\S+\s+){2}<DIR>\s+(.+)$", line, re.IGNORECASE)
            name = m_unix.group(1) if m_unix else (m_dos.group(1) if m_dos else "")
            if name and name not in (".", "..") and name not in dirs:
                dirs.append(name)
        dirs.sort(key=str.lower)
        return {"ok": True, "path": "/" + remote_path.strip("/"), "dirs": dirs}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timeout listando carpetas remotas", "dirs": []}
    finally:
        try:
            os.unlink(cfg_path)
        except OSError:
            pass


def get_remote_file_size(settings: dict[str, Any], remote_path: str) -> int | None:
    host = (settings.get("ftp_host") or "").strip()
    port = int(settings.get("ftp_port") or 5000)
    if not host:
        return None
    cfg_path = _write_curl_auth_file(settings)
    try:
        url = build_ftp_url(host, port, remote_path)
        cmd = ["curl", "-sS", "-I", *_base_curl_opts(settings, cfg_path), url]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if proc.returncode != 0:
            return None
        for line in proc.stdout.splitlines():
            if line.lower().startswith("content-length:"):
                val = line.split(":", 1)[1].strip()
                if val.isdigit():
                    return int(val)
        return None
    except subprocess.TimeoutExpired:
        return None
    finally:
        try:
            os.unlink(cfg_path)
        except OSError:
            pass


# Comandos que algunos servidores FTP usan para informar del espacio libre, por orden de preferencia.
# La mayoría de servidores de la Switch (ftpd, sys-ftpd, DBI) no implementan ninguno: en ese caso
# get_remote_free_space devuelve supported=False sin error.
FREE_SPACE_COMMANDS = ("AVBL {dir}", "AVBL", "SITE FREE", "SITE DF")
_FREE_BYTES_RE = re.compile(r"\b(\d{4,})\b")


def _parse_free_bytes(reply: str) -> int | None:
    """Extrae el primer número grande (bytes) de una respuesta 2xx tipo '213 123456789'."""
    body = reply.split(" ", 1)[1] if " " in reply else reply
    m = _FREE_BYTES_RE.search(body)
    return int(m.group(1)) if m else None


def get_remote_free_space(settings: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
    """Consulta el espacio libre del servidor FTP (p. ej. la SD de la Switch).

    Devuelve {supported, free_bytes, method, error?}. Nunca lanza excepción.
    """
    import ftplib

    host = (settings.get("ftp_host") or "").strip()
    port = int(settings.get("ftp_port") or 5000)
    if not host:
        return {"supported": False, "free_bytes": None, "method": "", "error": "Servidor FTP no configurado"}
    user = settings.get("ftp_user") or "anonymous"
    password = settings.get("ftp_password") or ("anonymous@local" if user == "anonymous" else "")
    remote_dir = normalize_remote_path(settings.get("ftp_remote_dir") or "/")
    ftp: ftplib.FTP | None = None
    try:
        ftp = ftplib.FTP_TLS(timeout=timeout) if settings.get("ftp_tls") else ftplib.FTP(timeout=timeout)
        ftp.connect(host, port)
        ftp.login(user, password)
        if isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        for template in FREE_SPACE_COMMANDS:
            cmd = template.format(dir=remote_dir)
            try:
                reply = ftp.sendcmd(cmd)
            except ftplib.error_perm:
                continue
            except ftplib.Error:
                continue
            if reply[:1] == "2":
                free = _parse_free_bytes(reply)
                if free is not None:
                    return {"supported": True, "free_bytes": free, "method": cmd.split()[0] if cmd.startswith("AVBL") else cmd}
        return {"supported": False, "free_bytes": None, "method": ""}
    except (OSError, EOFError, ftplib.Error) as exc:
        return {"supported": False, "free_bytes": None, "method": "", "error": f"No se pudo consultar el espacio: {exc}"}
    finally:
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass


def _parse_curl_progress_line(line: str) -> dict[str, Any] | None:
    # Columnas estándar de curl:
    # % Total  % Received  % Xferd  Average Dload  Upload  Total  Spent  Left  Speed
    parts = line.split()
    if len(parts) < 12:
        return None
    if not re.fullmatch(r"\d+(?:\.\d+)?", parts[0]):
        return None
    return {
        "percent": float(parts[0]),
        "speed": parts[11],
        "eta": parts[10],
    }


def _sleep_interruptible(seconds: float, stop_event: threading.Event) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stop_event.is_set():
            return False
        time.sleep(min(0.4, max(0.05, end - time.monotonic())))
    return not stop_event.is_set()


def process_ftp_item(
    item: dict[str, Any],
    settings: dict[str, Any],
    progress_cb: ProgressCallback,
    stop_event: threading.Event,
    log_cb: Callable[[str, str, str], None],
    free_space_cb: Callable[[], int | None] | None = None,
) -> dict[str, Any]:
    """Sube un fichero por FTP/FTPS usando curl.

    `free_space_cb` (opcional) devuelve los bytes libres en el servidor o None si no se sabe; si el
    fichero no cabe, el elemento termina en ERR sin intentar la subida.
    """
    src = item["source_path"]
    remote_dest = item["dest_path"]
    host = (settings.get("ftp_host") or "").strip()
    port = int(settings.get("ftp_port") or 5000)
    limit_kbps = int(settings.get("ftp_limit_kbps") or 0)
    # `or` convertiría un 0 explícito (sin reintentos / sin espera) en el valor por defecto
    max_retries = int(settings.get("ftp_retries", 3))
    base_wait = int(settings.get("ftp_retry_wait", 10))
    verify_size = bool(settings.get("ftp_verify_size", False))

    if not host:
        return {"status": "ERR", "error": "Servidor FTP no configurado", "note": "", "resume": 0}
    if not os.path.isfile(src):
        return {"status": "ERR", "error": "El fichero local ya no existe", "note": "", "resume": 0}

    st = os.stat(src)
    local_size = st.st_size
    current_sig = f"{local_size}:{st.st_mtime_ns}"
    can_resume = bool(item.get("resume")) and (not item.get("source_sig") or item["source_sig"] == current_sig)

    url = build_ftp_url(host, port, remote_dest)
    attempt = 0

    while True:
        if stop_event.is_set():
            return {"status": "REAN", "note": "detenido por usuario", "error": "", "resume": 1}

        remote_size = get_remote_file_size(settings, remote_dest)
        if remote_size is not None and remote_size == local_size and local_size > 0:
            log_cb("ftp", "INFO", f"Omitido (ya existe con mismo tamaño en FTP): {remote_dest}")
            return {"status": "OK", "note": "omitido (ya existía en servidor)", "error": "", "resume": 0}

        use_append = can_resume and remote_size is not None and 0 < remote_size < local_size

        if free_space_cb is not None and attempt == 0:
            free_bytes = free_space_cb()
            needed = local_size - (remote_size if use_append and remote_size else 0)
            if free_bytes is not None and free_bytes < needed:
                msg = (
                    f"Espacio insuficiente en la SD (libre {_fmt_size(free_bytes)}, necesita {_fmt_size(needed)})"
                )
                log_cb("ftp", "ERROR", f"{msg}: {os.path.basename(src)}")
                return {"status": "ERR", "error": msg, "note": "", "resume": 1 if use_append else 0}

        cfg_path = _write_curl_auth_file(settings)
        cmd = ["curl", "-S", *_base_curl_opts(settings, cfg_path), "--ftp-create-dirs"]
        if limit_kbps > 0:
            cmd.extend(["--limit-rate", f"{limit_kbps}k"])
        if use_append:
            cmd.extend(["-C", "-"])
        cmd.extend(["-T", src, url])

        env = os.environ.copy()
        env["LC_ALL"] = "C"

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
            text=False,
        )

        interrupted = False
        err_lines: list[str] = []
        buf = b""
        assert proc.stderr is not None
        try:
            while True:
                if stop_event.is_set():
                    interrupted = True
                    proc.terminate()
                    break
                chunk = proc.stderr.read(256)
                if not chunk:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                buf += chunk
                while b"\r" in buf or b"\n" in buf:
                    idx_r = buf.find(b"\r")
                    idx_n = buf.find(b"\n")
                    candidates = [i for i in (idx_r, idx_n) if i != -1]
                    cut = min(candidates)
                    line = buf[:cut].decode("utf-8", errors="replace").strip()
                    buf = buf[cut + 1 :]
                    if not line:
                        continue
                    parsed = _parse_curl_progress_line(line)
                    if parsed:
                        pct = parsed["percent"]
                        progress_cb(
                            {
                                "item_id": item["id"],
                                "filename": os.path.basename(src),
                                "percent": pct,
                                "transferred_bytes": int(local_size * pct / 100.0),
                                "total_bytes": local_size,
                                "speed": parsed["speed"],
                                "eta": parsed["eta"],
                            }
                        )
                    elif not line.startswith("% Total") and not line.startswith("Dload"):
                        err_lines.append(line)
                        if len(err_lines) > 8:
                            err_lines.pop(0)
        finally:
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            try:
                proc.stderr.close()
            except OSError:
                pass
            try:
                os.unlink(cfg_path)
            except OSError:
                pass

        if interrupted or stop_event.is_set():
            return {"status": "REAN", "note": "detenido, listo para reanudar", "error": "", "resume": 1}

        rc = proc.returncode
        if rc == 0:
            if verify_size:
                final_remote_size = get_remote_file_size(settings, remote_dest)
                if final_remote_size is not None and final_remote_size != local_size:
                    rc = -1
                    err_lines.append(
                        f"Tamaño en servidor ({final_remote_size} B) no coincide con local ({local_size} B)"
                    )
                else:
                    note = "subido (servidor no informó tamaño)" if final_remote_size is None else ""
                    log_cb("ftp", "INFO", f"Subida FTP completada: {src} -> {remote_dest}")
                    return {"status": "OK", "note": note, "error": "", "resume": 0, "retries_done": attempt}
            else:
                log_cb("ftp", "INFO", f"Subida FTP completada: {src} -> {remote_dest}")
                return {"status": "OK", "note": "", "error": "", "resume": 0, "retries_done": attempt}
        else:
            # Si curl falló pero el servidor ya tiene el tamaño completo (si verify_size activo)
            if verify_size:
                remote_after = get_remote_file_size(settings, remote_dest)
                if remote_after is not None and remote_after == local_size and local_size > 0:
                    log_cb("ftp", "INFO", f"Subida FTP verificada tras cierre: {remote_dest}")
                    return {"status": "OK", "note": "verificado tras corte", "error": "", "resume": 0}

        err_msg = err_lines[-1] if err_lines else f"curl finalizó con código {rc}"
        if rc in NON_RETRYABLE_CURL_CODES:
            log_cb("ftp", "ERROR", f"Error no reintentable en FTP ({src}): {err_msg}")
            return {"status": "ERR", "error": err_msg, "note": "", "resume": 0}

        attempt += 1
        can_resume = True
        if attempt > max_retries:
            log_cb("ftp", "ERROR", f"Fallo FTP definitivo tras {max_retries} reintentos ({src}): {err_msg}")
            return {
                "status": "ERR",
                "error": err_msg,
                "note": "",
                "resume": 1,
                "retries_done": attempt - 1,
            }

        wait_s = min(max(base_wait, 600), base_wait * (3 ** (attempt - 1))) if base_wait > 600 else min(600, base_wait * (3 ** (attempt - 1)))
        log_cb("ftp", "WARN", f"Reintento FTP {attempt}/{max_retries} en {wait_s}s ({os.path.basename(src)}): {err_msg}")
        if not _sleep_interruptible(wait_s, stop_event):
            return {
                "status": "REAN",
                "note": "detenido durante espera de reintento",
                "error": "",
                "resume": 1,
                "retries_done": attempt,
            }
