"""Configuración persistente y validación segura de rutas locales."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

DEFAULT_SETTINGS: dict[str, Any] = {
    # Mover / Copiar local
    "mover_origin": "",
    "mover_dest": "",
    "mover_keep_structure": True,
    "mover_operation": "copiar",  # copiar | mover
    "mover_limit_kbps": 0,
    "mover_retries": 3,
    "mover_retry_wait": 10,
    "mover_verify": "tamano",  # tamano | checksum | no
    "mover_conflict": "omitir",  # omitir | sobrescribir | renombrar
    "mover_favorites": [],
    # FTP / Switch
    "ftp_host": "",
    "ftp_port": 5000,
    "ftp_user": "",
    "ftp_password": "",
    "ftp_local_root": "",
    "ftp_remote_dir": "/",
    "ftp_keep_structure": True,
    "ftp_tls": False,
    "ftp_passive": True,
    "ftp_limit_kbps": 500,
    "ftp_retries": 3,
    "ftp_retry_wait": 10,
    "ftp_verify_size": False,
    # Papelera del explorador: días que se conservan los elementos borrados (0 = borrado directo)
    "trash_days": 7,
    "trash_enabled": True,
}


def parse_favorites(raw: Any) -> list[str]:
    """Acepta favoritos como lista, como texto JSON (array) o como texto con una ruta por línea/coma."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x) for x in data]
        except json.JSONDecodeError:
            pass
    parts = [p.strip() for chunk in text.splitlines() for p in chunk.split(",")]
    return [p for p in parts if p]


def get_data_root() -> str:
    """Devuelve la ruta raíz permitida para el explorador local."""
    root = os.environ.get("DATA_ROOT", "/host")
    return os.path.normpath(os.path.abspath(root))


def get_config_dir() -> str:
    """Devuelve el directorio donde se persisten configuración y base de datos."""
    cfg = os.environ.get("CONFIG_DIR", "/config")
    os.makedirs(cfg, exist_ok=True)
    return cfg


def resolve_safe_path(path: str | None, data_root: str | None = None) -> str:
    """Resuelve una ruta local asegurando que permanece dentro de DATA_ROOT."""
    root = os.path.normpath(os.path.abspath(data_root or get_data_root()))
    if not path or not path.strip():
        return root

    raw = path.strip()
    candidate = raw if os.path.isabs(raw) else os.path.join(root, raw)
    resolved = os.path.normpath(os.path.abspath(candidate))

    try:
        common = os.path.commonpath([root, resolved])
    except ValueError as exc:
        raise ValueError("Ruta inválida") from exc

    if common != root:
        raise ValueError(f"La ruta está fuera de DATA_ROOT ({root})")
    return resolved


def normalize_remote_path(path: str | None) -> str:
    """Normaliza una ruta remota FTP estilo POSIX."""
    if not path:
        return "/"
    parts = [p for p in path.strip().replace("\\", "/").split("/") if p and p != "."]
    clean: list[str] = []
    for part in parts:
        if part == "..":
            if clean:
                clean.pop()
        else:
            clean.append(part)
    return "/" + "/".join(clean)


def sanitize_settings(raw: dict[str, Any], data_root: str | None = None) -> dict[str, Any]:
    """Valida y normaliza todos los campos de configuración."""
    root = os.path.normpath(os.path.abspath(data_root or get_data_root()))
    merged = dict(DEFAULT_SETTINGS)
    merged.update(raw)

    # Rutas locales con fallback seguro a root
    for key in ("mover_origin", "mover_dest", "ftp_local_root"):
        val = str(merged.get(key) or "").strip()
        if not val:
            merged[key] = root
        else:
            try:
                merged[key] = resolve_safe_path(val, root)
            except ValueError:
                merged[key] = root

    favs: list[str] = []
    for item in parse_favorites(merged.get("mover_favorites")):
        s = str(item).strip()
        if not s:
            continue
        try:
            safe = resolve_safe_path(s, root)
            if safe not in favs:
                favs.append(safe)
        except ValueError:
            continue
    merged["mover_favorites"] = favs

    op = str(merged.get("mover_operation") or "copiar").lower()
    merged["mover_operation"] = op if op in ("copiar", "mover") else "copiar"

    verify = str(merged.get("mover_verify") or "tamano").lower()
    merged["mover_verify"] = verify if verify in ("tamano", "checksum", "no") else "tamano"

    conflict = str(merged.get("mover_conflict") or "omitir").lower()
    merged["mover_conflict"] = conflict if conflict in ("omitir", "sobrescribir", "renombrar") else "omitir"

    for bool_key in (
        "mover_keep_structure",
        "ftp_keep_structure",
        "ftp_tls",
        "ftp_passive",
        "ftp_verify_size",
    ):
        merged[bool_key] = bool(merged.get(bool_key))

    merged["mover_limit_kbps"] = max(0, int(merged.get("mover_limit_kbps") or 0))
    merged["mover_retries"] = max(0, int(merged.get("mover_retries") or 0))
    merged["mover_retry_wait"] = max(1, int(merged.get("mover_retry_wait") or 10))

    merged["ftp_host"] = str(merged.get("ftp_host") or "").strip()
    port = int(merged.get("ftp_port") or 5000)
    merged["ftp_port"] = port if 1 <= port <= 65535 else 5000
    merged["ftp_user"] = str(merged.get("ftp_user") or "").strip()
    merged["ftp_password"] = str(merged.get("ftp_password") or "")
    merged["ftp_remote_dir"] = normalize_remote_path(str(merged.get("ftp_remote_dir") or "/"))
    merged["ftp_limit_kbps"] = max(0, int(merged.get("ftp_limit_kbps") or 0))
    merged["ftp_retries"] = max(0, int(merged.get("ftp_retries") or 0))
    merged["ftp_retry_wait"] = max(1, int(merged.get("ftp_retry_wait") or 10))
    try:
        trash_days = int(merged.get("trash_days", 7))
    except (TypeError, ValueError):
        trash_days = 7
    merged["trash_days"] = max(0, min(trash_days, 365))
    raw_enabled = merged.get("trash_enabled", True)
    if isinstance(raw_enabled, str):
        raw_enabled = raw_enabled.strip().lower() not in ("0", "false", "no", "off", "")
    merged["trash_enabled"] = bool(raw_enabled)

    return merged


class SettingsStore:
    """Almacén thread-safe para settings.json."""

    def __init__(self, config_dir: str | None = None, data_root: str | None = None) -> None:
        self.config_dir = config_dir or get_config_dir()
        self.data_root = os.path.normpath(os.path.abspath(data_root or get_data_root()))
        self.path = Path(self.config_dir) / "settings.json"
        self._lock = threading.Lock()
        self._settings = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return sanitize_settings(data, self.data_root)
            except (OSError, json.JSONDecodeError):
                pass
        initial = sanitize_settings({}, self.data_root)
        self._write(initial)
        return initial

    def _write(self, data: dict[str, Any]) -> None:
        os.makedirs(self.config_dir, exist_ok=True)
        tmp = Path(self.config_dir) / "settings.json.tmp"
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def get_public(self) -> dict[str, Any]:
        """Devuelve la configuración ocultando la contraseña FTP en texto plano."""
        with self._lock:
            data = dict(self._settings)
        has_pw = bool(data.get("ftp_password"))
        data["ftp_password"] = ""
        data["ftp_has_password"] = has_pw
        data["data_root"] = self.data_root
        return data

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            candidate = dict(self._settings)
            for k, v in patch.items():
                if k == "ftp_password":
                    # Solo sobrescribir si el usuario envía una nueva contraseña o pide limpiarla
                    if v is not None and v != "":
                        candidate[k] = v
                    elif patch.get("clear_ftp_password"):
                        candidate[k] = ""
                elif k in DEFAULT_SETTINGS:
                    candidate[k] = v
            cleaned = sanitize_settings(candidate, self.data_root)
            self._write(cleaned)
            self._settings = cleaned
            return dict(self._settings)


def effective_trash_days(settings: dict) -> int:
    """Días de papelera efectivos: 0 (borrado directo) si la papelera está desactivada."""
    if not settings.get("trash_enabled", True):
        return 0
    try:
        return max(0, int(settings.get("trash_days", 7)))
    except (TypeError, ValueError):
        return 7
