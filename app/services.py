"""Lógica compartida entre la API web y la CLI (listado, búsqueda, rutas relativas y juegos Switch)."""

from __future__ import annotations

import os
import re
import time
import unicodedata
from typing import Any

from .config import normalize_remote_path, resolve_safe_path
from .switch_utils import SWITCH_EXTENSIONS, detect_switch_type

VIRTUAL_ROOT_DIRS = {"proc", "sys", "dev", "run"}
TRASH_DIR_NAME = ".toolbox-trash"
SORT_KEYS = ("name", "mtime", "size")
SEARCH_MAX_LIMIT = 1000
SEARCH_TIME_BUDGET_S = 5.0


# ----------------------------------------------------------------------------
# Listado de carpetas
# ----------------------------------------------------------------------------
def list_directory(
    safe_path: str,
    data_root: str,
    statuses: dict[str, str] | None = None,
    show_hidden: bool = False,
    sort: str = "name",
    order: str = "asc",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lista una carpeta ya validada. Devuelve (dirs, files) ordenados; las carpetas siempre van primero."""
    statuses = statuses or {}
    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    with os.scandir(safe_path) as entries:
        for entry in entries:
            name = entry.name
            if name == TRASH_DIR_NAME:
                continue
            if not show_hidden and name.startswith("."):
                continue
            if safe_path == data_root and name in VIRTUAL_ROOT_DIRS:
                continue
            full = os.path.join(safe_path, name)
            try:
                if entry.is_dir(follow_symlinks=True):
                    try:
                        mtime = entry.stat(follow_symlinks=True).st_mtime
                    except OSError:
                        mtime = None
                    dirs.append({"name": name, "path": full, "is_dir": True, "mtime": mtime, "size": None})
                elif entry.is_file(follow_symlinks=True):
                    st = entry.stat(follow_symlinks=True)
                    files.append(
                        {
                            "name": name,
                            "path": full,
                            "is_dir": False,
                            "size": st.st_size,
                            "size_bytes": st.st_size,
                            "mtime": st.st_mtime,
                            "switch_type": detect_switch_type(name),
                            "queue_status": statuses.get(full, ""),
                        }
                    )
            except OSError:
                continue

    sort = sort if sort in SORT_KEYS else "name"
    reverse = order == "desc"

    def name_key(e: dict[str, Any]) -> str:
        return e["name"].lower()

    if sort == "mtime":
        dirs.sort(key=lambda e: (e.get("mtime") or 0, name_key(e)), reverse=reverse)
        files.sort(key=lambda e: (e.get("mtime") or 0, name_key(e)), reverse=reverse)
    elif sort == "size":
        # Las carpetas no tienen tamaño calculado: se ordenan por nombre
        dirs.sort(key=name_key, reverse=reverse)
        files.sort(key=lambda e: (e.get("size") or 0, name_key(e)), reverse=reverse)
    else:
        dirs.sort(key=name_key, reverse=reverse)
        files.sort(key=name_key, reverse=reverse)
    return dirs, files


# ----------------------------------------------------------------------------
# Búsqueda recursiva
# ----------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    """Minúsculas y sin acentos, para comparar nombres de forma tolerante."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def search_files(
    root_path: str,
    query: str,
    data_root: str,
    limit: int = 300,
    time_budget_s: float = SEARCH_TIME_BUDGET_S,
    show_hidden: bool = False,
) -> dict[str, Any]:
    """Busca recursivamente nombres que contengan todas las palabras de `query`, en cualquier orden
    (sin distinguir mayúsculas ni acentos).

    No sigue enlaces simbólicos, salta los pseudo-sistemas del kernel en la raíz y la papelera,
    y se detiene al llegar a `limit` resultados o al agotar el tiempo.
    """
    words = normalize_text(query.strip()).split()
    limit = max(1, min(int(limit), SEARCH_MAX_LIMIT))
    results: list[dict[str, Any]] = []
    truncated = False
    if not words:
        return {"path": root_path, "query": query, "results": results, "truncated": False}

    deadline = time.monotonic() + max(0.1, time_budget_s)
    norm_root = os.path.normpath(data_root)
    stack = [root_path]
    while stack:
        if time.monotonic() > deadline:
            truncated = True
            break
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            continue
        subdirs: list[str] = []
        for entry in entries:
            name = entry.name
            if name == TRASH_DIR_NAME:
                continue
            if not show_hidden and name.startswith("."):
                continue
            if os.path.normpath(current) == norm_root and name in VIRTUAL_ROOT_DIRS:
                continue
            try:
                if entry.is_symlink():
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if not is_dir and not entry.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if is_dir:
                subdirs.append(entry.path)
            norm_name = normalize_text(name)
            if all(w in norm_name for w in words):
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                results.append(
                    {
                        "name": name,
                        "path": entry.path,
                        "rel": os.path.relpath(entry.path, root_path),
                        "is_dir": is_dir,
                        "size": None if is_dir else st.st_size,
                        "mtime": st.st_mtime,
                        "switch_type": "" if is_dir else detect_switch_type(name),
                    }
                )
                if len(results) >= limit:
                    truncated = True
                    stack.clear()
                    break
        # Recorrido en profundidad respetando orden alfabético
        stack.extend(reversed(subdirs))
    return {"path": root_path, "query": query, "results": results, "truncated": truncated}


# ----------------------------------------------------------------------------
# Rutas relativas para mostrar en la interfaz
# ----------------------------------------------------------------------------
def short_rel(path: str, bases: list[str], remote: bool = False) -> str:
    """Devuelve la ruta relativa más corta de `path` respecto a alguna de las `bases` que la contengan."""
    if not path:
        return ""
    best = path
    for base in bases:
        if not base:
            continue
        if remote:
            b = normalize_remote_path(base).rstrip("/")
            p = normalize_remote_path(path)
            if b == "":
                cand = p  # Raíz remota: la ruta absoluta ya es la más clara
            elif p == b:
                cand = os.path.basename(p)
            elif p.startswith(b + "/"):
                cand = p[len(b) + 1 :]
            else:
                continue
        else:
            b = os.path.normpath(base)
            p = os.path.normpath(path)
            try:
                if os.path.commonpath([b, p]) != b:
                    continue
            except ValueError:
                continue
            cand = os.path.relpath(p, b) if p != b else os.path.basename(p)
        if cand and len(cand) < len(best):
            best = cand
    return best


def decorate_queue_items(items: list[dict[str, Any]], settings: dict[str, Any], data_root: str) -> list[dict[str, Any]]:
    """Añade rel_src / rel_dest a los elementos de la cola."""
    out = []
    for it in items:
        it = dict(it)
        if it.get("kind") == "ftp":
            it["rel_src"] = short_rel(it.get("source_path", ""), [settings.get("ftp_local_root", ""), data_root])
            it["rel_dest"] = short_rel(it.get("dest_path", ""), [settings.get("ftp_remote_dir", "/")], remote=True)
        else:
            it["rel_src"] = short_rel(it.get("source_path", ""), [settings.get("mover_origin", ""), data_root])
            it["rel_dest"] = short_rel(it.get("dest_path", ""), [settings.get("mover_dest", ""), data_root])
        out.append(it)
    return out


# ----------------------------------------------------------------------------
# Juego Switch completo (Base -> Update -> DLC)
# ----------------------------------------------------------------------------
TITLE_ID_RE = re.compile(r"\[([0-9a-fA-F]{16})\]")
VERSION_RE = re.compile(r"\[v(\d+)\]", re.IGNORECASE)


def _title_info(name: str) -> tuple[str | None, str]:
    """Devuelve (title_id_base, tipo) a partir del nombre. El tipo sale del title id si existe."""
    m = TITLE_ID_RE.search(name)
    st = detect_switch_type(name)
    if st == "NRO":
        return None, "NRO"
    if not m:
        return None, st
    tid = int(m.group(1), 16)
    base = tid & ~0x1FFF
    low = tid & 0x1FFF
    if low == 0:
        kind = "BASE"
    elif low == 0x800:
        kind = "UPD"
    elif low & 0x1000:
        kind = "DLC"
    else:
        kind = st or "BASE"
    return f"{base:016X}", kind


def _clean_title(name: str) -> str:
    stem = re.sub(r"\.(nsp|nsz|xci|xcz|nro)$", "", name, flags=re.IGNORECASE)
    stem = re.sub(r"\[[^\]]*\]", " ", stem)
    stem = re.sub(r"\([^\)]*\)", " ", stem)
    stem = re.sub(r"\b(update|upd|dlc|base)\b", " ", stem, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", stem).strip(" -_") or name


def _collect_switch_files(paths: list[str], data_root: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    norm_root = os.path.normpath(data_root)
    for raw in paths:
        safe = resolve_safe_path(raw, data_root)
        if os.path.isdir(safe):
            for dirpath, dirnames, filenames in os.walk(safe, followlinks=False):
                dirnames[:] = sorted(
                    d
                    for d in dirnames
                    if not d.startswith(".") and not (os.path.normpath(dirpath) == norm_root and d in VIRTUAL_ROOT_DIRS)
                )
                for fn in sorted(filenames):
                    full = os.path.join(dirpath, fn)
                    if fn.lower().endswith(SWITCH_EXTENSIONS) and os.path.isfile(full) and full not in seen:
                        seen.add(full)
                        found.append(full)
        elif os.path.isfile(safe) and safe.lower().endswith(SWITCH_EXTENSIONS) and safe not in seen:
            seen.add(safe)
            found.append(safe)
    return found


def plan_game_upload(
    paths: list[str],
    data_root: str,
    remote_dest: str,
    active_sources: set[str],
) -> dict[str, Any]:
    """Prepara las entradas FTP de uno o varios juegos completos en orden Base -> UPD -> DLC.

    Devuelve {entries, warnings, groups}. Las entradas van a `remote_dest/<fichero>` (estructura plana,
    como espera DBI) y se omiten los ficheros que ya están activos en la cola.
    """
    from .mover_worker import file_signature

    remote_dest = normalize_remote_path(remote_dest or "/")
    files = _collect_switch_files(paths, data_root)
    warnings: list[str] = []
    groups: dict[str, dict[str, Any]] = {}

    for fpath in files:
        name = os.path.basename(fpath)
        tid, stype = _title_info(name)
        if stype == "NRO":
            key = "NRO"
        else:
            key = tid or f"name:{normalize_text(_clean_title(name))}"
        grp = groups.setdefault(
            key,
            {"title_id": tid or "", "name": "", "files": [], "base": False, "upd": 0, "dlc": 0},
        )
        grp["files"].append((stype, fpath))
        if stype == "BASE":
            grp["base"] = True
            grp["name"] = _clean_title(name)
        elif stype == "UPD":
            grp["upd"] += 1
        elif stype == "DLC":
            grp["dlc"] += 1
        if not grp["name"]:
            grp["name"] = "Homebrew (.nro)" if key == "NRO" else _clean_title(name)

    type_order = {"BASE": 0, "UPD": 1, "DLC": 2, "NRO": 3}

    def version_of(path: str) -> int:
        m = VERSION_RE.search(os.path.basename(path))
        return int(m.group(1)) if m else 0

    entries: list[dict[str, Any]] = []
    out_groups: list[dict[str, Any]] = []
    for key, grp in sorted(groups.items(), key=lambda kv: (kv[0] == "NRO", kv[1]["name"].lower())):
        if key != "NRO" and not grp["base"] and (grp["upd"] or grp["dlc"]):
            warnings.append(
                f"'{grp['name']}': hay {grp['upd']} update(s) y {grp['dlc']} DLC sin el juego base en la selección"
            )
        ordered = sorted(
            grp["files"],
            key=lambda tf: (type_order.get(tf[0], 4), version_of(tf[1]), os.path.basename(tf[1]).lower()),
        )
        for stype, fpath in ordered:
            if fpath in active_sources:
                warnings.append(f"Ya estaba en la cola: {os.path.basename(fpath)}")
                continue
            try:
                size = os.path.getsize(fpath)
                sig = file_signature(fpath)
            except OSError:
                continue
            entries.append(
                {
                    "kind": "ftp",
                    "operation": "subir",
                    "source_path": fpath,
                    "dest_path": normalize_remote_path(f"{remote_dest}/{os.path.basename(fpath)}"),
                    "size_bytes": size,
                    "source_sig": sig,
                    "cleanup_dir": "",
                    "switch_type": stype if stype in type_order else detect_switch_type(fpath),
                }
            )
        out_groups.append(
            {
                "title_id": grp["title_id"],
                "name": grp["name"],
                "base": grp["base"],
                "upd": grp["upd"],
                "dlc": grp["dlc"],
            }
        )
    if not files:
        warnings.append("No se encontraron ficheros de Switch (.nsp, .nsz, .xci, .xcz, .nro) en la selección")
    return {"entries": entries, "warnings": warnings, "groups": out_groups}
