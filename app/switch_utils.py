"""Utilidades para Nintendo Switch (insignias, ordenación y escáner FTP en LAN)."""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

SWITCH_EXTENSIONS = (".nsp", ".nsz", ".xci", ".xcz", ".nro")
IGNORED_IFACE_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "tun", "tap", "wg", "tailscale")


def detect_switch_type(path_or_name: str) -> str:
    """Devuelve 'BASE', 'UPD', 'DLC', 'NRO' o '' según el nombre del paquete."""
    name = path_or_name.rsplit("/", 1)[-1]
    lower = name.lower()
    if not lower.endswith(SWITCH_EXTENSIONS):
        return ""
    if lower.endswith(".nro"):
        return "NRO"
    if re.search(r"(\[upd\]|\bupdate\b|\bupd\b|\[v[1-9]\d*\]|\bv[1-9]\d{4,}\b)", lower):
        return "UPD"
    if re.search(r"(\[dlc\]|\bdlc\b)", lower):
        return "DLC"
    return "BASE"


def switch_sort_key(item: dict[str, Any]) -> tuple[int, str, int, str]:
    """Clave de ordenación para agrupar por juego: BASE -> UPD -> DLC -> NRO -> otros."""
    status = item.get("status", "PEND")
    # Mantener completados/errores al final tras los activos
    status_group = 0 if status in ("PROC", "PEND", "REAN", "PAUS") else (1 if status == "ERR" else 2)
    name = item.get("source_path", "").rsplit("/", 1)[-1]
    lower = name.lower()

    st_type = item.get("switch_type") or detect_switch_type(name)
    type_order = {"BASE": 0, "UPD": 1, "DLC": 2, "NRO": 3}.get(st_type, 4)

    # Extraer título base aproximado quitando corchetes/etiquetas de versión/extensión
    stem = re.sub(r"\.(nsp|nsz|xci|xcz|nro)$", "", lower)
    stem = re.sub(r"\[[^\]]*\]", " ", stem)
    stem = re.sub(r"\([^\)]*\)", " ", stem)
    stem = re.sub(r"\b(update|upd|dlc|base|v\d+)\b", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()

    return (status_group, stem, type_order, lower)


def _extract_prefix_24(ip_str: str) -> str | None:
    try:
        ip = ipaddress.IPv4Address(ip_str.strip())
    except ipaddress.AddressValueError:
        return None
    if ip.is_loopback:
        return None
    octets = str(ip).split(".")
    return ".".join(octets[:3])


def get_local_lan_info() -> tuple[list[str], set[str]]:
    """Obtiene prefijos /24 de interfaces físicas activas y todas las IPs locales."""
    prefixes: list[str] = []
    local_ips: set[str] = {"127.0.0.1"}

    try:
        out = subprocess.check_output(
            ["ip", "-o", "-4", "addr", "show", "up"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            iface = parts[1]
            ip_cidr = parts[3]
            ip_only = ip_cidr.split("/", 1)[0]
            local_ips.add(ip_only)
            if iface.startswith(IGNORED_IFACE_PREFIXES):
                continue
            pref = _extract_prefix_24(ip_only)
            if pref and pref not in prefixes:
                prefixes.append(pref)
    except (OSError, subprocess.SubprocessError):
        pass

    if not prefixes:
        try:
            out = subprocess.check_output(
                ["hostname", "-I"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
            for token in out.split():
                local_ips.add(token)
                pref = _extract_prefix_24(token)
                if pref and pref not in prefixes:
                    prefixes.append(pref)
        except (OSError, subprocess.SubprocessError):
            pass

    return prefixes, local_ips


def _probe_tcp_ftp(ip: str, port: int, timeout_s: float = 1.8) -> dict[str, Any] | None:
    """Comprueba si un host tiene el puerto abierto y lee el banner FTP si responde."""
    try:
        with socket.create_connection((ip, port), timeout=timeout_s) as sock:
            sock.settimeout(1.2)
            banner = ""
            try:
                raw = sock.recv(256)
                banner = raw.decode("utf-8", errors="replace").strip().splitlines()[0]
            except (OSError, IndexError):
                banner = ""
            return {
                "ip": ip,
                "port": port,
                "banner": banner,
                "is_ftp": banner.startswith("220"),
            }
    except OSError:
        return None


def scan_switch_ftp(
    ports: list[int] | None = None,
    extra_subnets: list[str] | None = None,
    timeout_s: float = 1.8,
) -> dict[str, Any]:
    """Escanea las subredes LAN buscando servidores FTP (p. ej. DBI/ftpd en Switch)."""
    prefixes, local_ips = get_local_lan_info()
    for sub in extra_subnets or []:
        cleaned = sub.strip().rstrip(".")
        if re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", cleaned):
            if cleaned not in prefixes:
                prefixes.append(cleaned)
        else:
            pref = _extract_prefix_24(cleaned)
            if pref and pref not in prefixes:
                prefixes.append(pref)

    scan_ports = ports or [5000]
    results: list[dict[str, Any]] = []
    if not prefixes:
        return {"prefixes": [], "ports": scan_ports, "found": []}

    targets = [
        (f"{pref}.{host}", port)
        for port in scan_ports
        for pref in prefixes
        for host in range(1, 255)
        if f"{pref}.{host}" not in local_ips
    ]

    with ThreadPoolExecutor(max_workers=50) as pool:
        futures = [pool.submit(_probe_tcp_ftp, ip, port, timeout_s) for ip, port in targets]
        for fut in as_completed(futures):
            hit = fut.result()
            if hit:
                results.append(hit)

    # Priorizar los que respondieron banner FTP 220 y puerto 5000
    results.sort(key=lambda r: (not r["is_ftp"], r["port"] != 5000, r["ip"]))
    return {"prefixes": prefixes, "ports": scan_ports, "found": results}

