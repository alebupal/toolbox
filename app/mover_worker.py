"""Motor de copia y movimiento local con reanudación (.mover-partial) y verificación."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

ProgressCallback = Callable[[dict[str, Any]], None]

RSYNC_PROGRESS_RE = re.compile(
    r"^\s*([\d,\.]+)\s+(\d+)%\s+(\S+)\s+(\d+:\d+:\d+)"
)


def file_signature(path: str) -> str:
    st = os.stat(path)
    return f"{st.st_size}:{st.st_mtime_ns}"


def _nearest_existing_dir(path: str) -> str:
    cur = os.path.abspath(path)
    while cur and not os.path.exists(cur):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return cur or "/"


def same_filesystem(src_path: str, dest_dir: str) -> bool:
    try:
        src_dev = os.stat(src_path).st_dev
        dst_dev = os.stat(_nearest_existing_dir(dest_dir)).st_dev
        return src_dev == dst_dev
    except OSError:
        return False


def files_identical(path_a: str, path_b: str, chunk_size: int = 1024 * 1024) -> bool:
    if os.path.getsize(path_a) != os.path.getsize(path_b):
        return False
    with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
        while True:
            ba = fa.read(chunk_size)
            bb = fb.read(chunk_size)
            if ba != bb:
                return False
            if not ba:
                return True


def sha256_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def next_renamed_path(dest_path: str) -> str:
    p = Path(dest_path)
    stem = p.stem
    suffix = p.suffix
    parent = p.parent
    counter = 1
    while True:
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return str(candidate)
        counter += 1


def cleanup_empty_parents(src_file: str, cleanup_root: str) -> None:
    """Elimina subcarpetas vacías tras mover una carpeta completa, sin salir de cleanup_root."""
    if not cleanup_root:
        return
    root_real = os.path.realpath(cleanup_root)
    cur = os.path.dirname(os.path.realpath(src_file))
    while cur and cur != root_real:
        try:
            common = os.path.commonpath([root_real, cur])
        except ValueError:
            break
        if common != root_real:
            break
        try:
            os.rmdir(cur)
        except OSError:
            break
        cur = os.path.dirname(cur)
    # Intentar borrar también la propia carpeta añadida si quedó vacía
    if cur == root_real:
        try:
            os.rmdir(root_real)
        except OSError:
            pass


def _parse_rsync_bytes(raw: str) -> int:
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else 0


def _sleep_interruptible(seconds: float, stop_event: threading.Event) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if stop_event.is_set():
            return False
        time.sleep(min(0.4, max(0.05, end - time.monotonic())))
    return not stop_event.is_set()


def process_mover_item(
    item: dict[str, Any],
    settings: dict[str, Any],
    progress_cb: ProgressCallback,
    stop_event: threading.Event,
    log_cb: Callable[[str, str, str], None],
    has_pending_cleanup_cb: Callable[[str], bool],
) -> dict[str, Any]:
    """
    Procesa un elemento de la cola de Mover/Copiar.
    Devuelve diccionario con campos actualizados: status, note, error, resume, retries_done.
    """
    src = item["source_path"]
    dest = item["dest_path"]
    operation = item.get("operation", "copiar")
    cleanup_dir = item.get("cleanup_dir", "")
    conflict = settings.get("mover_conflict", "omitir")
    verify_mode = settings.get("mover_verify", "tamano")
    limit_kbps = int(settings.get("mover_limit_kbps", 0))
    max_retries = int(settings.get("mover_retries", 3))
    base_wait = int(settings.get("mover_retry_wait", 10))

    if not os.path.isfile(src):
        return {
            "status": "ERR",
            "error": "El archivo de origen ya no existe o no es legible",
            "note": "",
            "resume": 0,
        }

    src_size = os.path.getsize(src)
    current_sig = file_signature(src)
    dest_dir = os.path.dirname(dest)
    partial_dir = os.path.join(dest_dir, ".mover-partial")
    partial_path = os.path.join(partial_dir, os.path.basename(dest))

    # Si el origen cambió desde que se encoló o empezó, descartar parcial previo
    if item.get("source_sig") and item["source_sig"] != current_sig and os.path.exists(partial_path):
        try:
            os.remove(partial_path)
        except OSError:
            pass

    final_dest = dest
    if os.path.lexists(final_dest):
        if os.path.isdir(final_dest) and conflict != "renombrar":
            return {
                "status": "ERR",
                "error": "En el destino ya existe una carpeta con ese nombre",
                "note": "",
                "resume": 0,
            }
        if conflict == "omitir":
            dst_size = os.path.getsize(final_dest)
            if dst_size != src_size:
                msg = (
                    "ya existe en destino con otro tamaño; origen conservado"
                    if operation == "mover"
                    else "ya existe en destino con otro tamaño"
                )
                return {"status": "ERR", "error": msg, "note": "", "resume": 0}
            if operation == "mover":
                if files_identical(src, final_dest):
                    os.unlink(src)
                    if cleanup_dir and not has_pending_cleanup_cb(cleanup_dir):
                        cleanup_empty_parents(src, cleanup_dir)
                    return {
                        "status": "OK",
                        "note": "ya existía idéntico, origen eliminado",
                        "error": "",
                        "resume": 0,
                    }
                return {
                    "status": "ERR",
                    "error": "ya existe con mismo tamaño pero distinto contenido; origen conservado",
                    "note": "",
                    "resume": 0,
                }
            return {"status": "OK", "note": "ya existía (omitido)", "error": "", "resume": 0}

        if conflict == "renombrar":
            final_dest = next_renamed_path(final_dest)
            partial_path = os.path.join(partial_dir, os.path.basename(final_dest))

    # Mover instantáneo en el mismo disco (salvo si se fuerza rsync)
    force_rsync = os.environ.get("MOVER_FORZAR_RSYNC") == "1"
    if operation == "mover" and not force_rsync and same_filesystem(src, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        try:
            os.replace(src, final_dest)
        except OSError:
            shutil.move(src, final_dest)
        if cleanup_dir and not has_pending_cleanup_cb(cleanup_dir):
            cleanup_empty_parents(src, cleanup_dir)
        log_cb("mover", "INFO", f"Movido instantáneo (mismo disco): {src} -> {final_dest}")
        return {"status": "OK", "note": "movido (mismo disco)", "error": "", "resume": 0}

    # Copia o movimiento entre discos mediante rsync a .mover-partial
    attempt = 0
    while True:
        if stop_event.is_set():
            return {"status": "REAN", "note": "pausado por usuario", "error": "", "resume": 1}

        if not os.path.isfile(src):
            return {"status": "ERR", "error": "El archivo de origen desapareció", "note": "", "resume": 0}

        partial_existing = os.path.getsize(partial_path) if os.path.isfile(partial_path) else 0
        needed_bytes = max(0, src_size - partial_existing)
        existing_parent = _nearest_existing_dir(dest_dir)
        fs_stat = os.statvfs(existing_parent)
        avail_bytes = fs_stat.f_bavail * fs_stat.f_frsize
        if avail_bytes < needed_bytes:
            return {
                "status": "ERR",
                "error": f"Sin espacio libre suficiente en destino (necesarios {needed_bytes} B, libres {avail_bytes} B)",
                "note": "",
                "resume": 1 if partial_existing > 0 else 0,
            }

        os.makedirs(partial_dir, exist_ok=True)
        cmd = ["rsync", "-a", "--partial", "--append-verify", "--info=progress2"]
        if limit_kbps > 0:
            cmd.append(f"--bwlimit={limit_kbps}")
        cmd.extend([src, partial_path])

        env = os.environ.copy()
        env["LC_ALL"] = "C"

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=False,
        )

        interrupted = False
        last_output_lines: list[str] = []
        buf = b""
        assert proc.stdout is not None
        try:
            while True:
                if stop_event.is_set():
                    interrupted = True
                    proc.terminate()
                    break
                chunk = proc.stdout.read(256)
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
                    m = RSYNC_PROGRESS_RE.match(line)
                    if m:
                        transferred = _parse_rsync_bytes(m.group(1))
                        pct = min(100, max(0, int(m.group(2))))
                        speed = m.group(3)
                        eta = m.group(4)
                        progress_cb(
                            {
                                "item_id": item["id"],
                                "filename": os.path.basename(src),
                                "percent": float(pct),
                                "transferred_bytes": transferred,
                                "total_bytes": src_size,
                                "speed": speed,
                                "eta": eta,
                            }
                        )
                    else:
                        last_output_lines.append(line)
                        if len(last_output_lines) > 8:
                            last_output_lines.pop(0)
        finally:
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            try:
                proc.stdout.close()
            except OSError:
                pass

        if interrupted or stop_event.is_set():
            return {"status": "REAN", "note": "detenido, listo para reanudar", "error": "", "resume": 1}

        if proc.returncode == 0 and os.path.isfile(partial_path):
            # Verificar que el origen no cambió durante la copia
            if not os.path.isfile(src) or file_signature(src) != current_sig:
                try:
                    os.remove(partial_path)
                except OSError:
                    pass
                return {
                    "status": "ERR",
                    "error": "El archivo de origen cambió durante la copia",
                    "note": "",
                    "resume": 0,
                }

            effective_verify = "tamano" if (operation == "mover" and verify_mode == "no") else verify_mode
            verified = True
            verify_err = ""

            if effective_verify == "tamano":
                if os.path.getsize(partial_path) != src_size:
                    verified = False
                    verify_err = "Tamaño verificado incorrecto tras la copia"
            elif effective_verify == "checksum":
                if sha256_file(partial_path) != sha256_file(src):
                    verified = False
                    verify_err = "Checksum SHA-256 distinto tras la copia"

            if verified:
                os.replace(partial_path, final_dest)
                try:
                    if not os.listdir(partial_dir):
                        os.rmdir(partial_dir)
                except OSError:
                    pass

                note = f"guardado como {os.path.basename(final_dest)}" if final_dest != dest else ""
                if operation == "mover":
                    os.unlink(src)
                    if cleanup_dir and not has_pending_cleanup_cb(cleanup_dir):
                        cleanup_empty_parents(src, cleanup_dir)
                log_cb("mover", "INFO", f"{operation.capitalize()} completado: {src} -> {final_dest}")
                return {"status": "OK", "note": note, "error": "", "resume": 0, "retries_done": attempt}
            err_msg = verify_err
        else:
            err_msg = last_output_lines[-1] if last_output_lines else f"rsync salió con código {proc.returncode}"

        attempt += 1
        if attempt > max_retries:
            log_cb("mover", "ERROR", f"Fallo definitivo tras {max_retries} reintentos ({src}): {err_msg}")
            return {
                "status": "ERR",
                "error": err_msg,
                "note": "",
                "resume": 1 if os.path.isfile(partial_path) else 0,
                "retries_done": attempt - 1,
            }

        wait_s = min(max(base_wait, 600), base_wait * (3 ** (attempt - 1))) if base_wait > 600 else min(600, base_wait * (3 ** (attempt - 1)))
        log_cb("mover", "WARN", f"Reintento {attempt}/{max_retries} en {wait_s}s para {os.path.basename(src)}: {err_msg}")
        if not _sleep_interruptible(wait_s, stop_event):
            return {
                "status": "REAN",
                "note": "detenido durante espera de reintento",
                "error": "",
                "resume": 1 if os.path.isfile(partial_path) else 0,
                "retries_done": attempt,
            }
