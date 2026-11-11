"""Esquemas Pydantic de entrada/salida para la API REST."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MkdirRequest(BaseModel):
    parent: str
    name: str = Field(..., min_length=1, max_length=255)


class AddQueueRequest(BaseModel):
    kind: Literal["mover", "ftp"]
    paths: list[str] = Field(..., min_length=1)
    recursive: bool = False
    operation: Literal["copiar", "mover"] | None = None
    dest_dir: str | None = None  # None o "" = destino automático según configuración
    only_switch: bool = False
    base_browse_dir: str | None = None


class MoveItemRequest(BaseModel):
    direction: Literal["up", "down", "top"]


class ScanSwitchRequest(BaseModel):
    ports: list[int] | None = None
    extra_subnets: list[str] | None = None


class SettingsUpdateRequest(BaseModel):
    mover_origin: str | None = None
    mover_dest: str | None = None
    mover_keep_structure: bool | None = None
    mover_operation: Literal["copiar", "mover"] | None = None
    mover_limit_kbps: int | None = None
    mover_retries: int | None = None
    mover_retry_wait: int | None = None
    mover_verify: Literal["tamano", "checksum", "no"] | None = None
    mover_conflict: Literal["omitir", "sobrescribir", "renombrar"] | None = None
    mover_favorites: list[str] | None = None

    ftp_host: str | None = None
    ftp_port: int | None = None
    ftp_user: str | None = None
    ftp_password: str | None = None
    clear_ftp_password: bool = False
    ftp_local_root: str | None = None
    ftp_remote_dir: str | None = None
    ftp_keep_structure: bool | None = None
    ftp_tls: bool | None = None
    ftp_passive: bool | None = None
    ftp_limit_kbps: int | None = None
    ftp_retries: int | None = None
    ftp_retry_wait: int | None = None
    ftp_verify_size: bool | None = None

