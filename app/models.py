"""Esquemas Pydantic de entrada/salida para la API REST."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MkdirRequest(BaseModel):
    parent: str
    name: str = Field(..., min_length=1, max_length=255)


class RenameRequest(BaseModel):
    path: str
    new_name: str = Field(..., min_length=1, max_length=255)


class DeletePathsRequest(BaseModel):
    paths: list[str] = Field(..., min_length=1)
    permanent: bool = False


class TrashIdsRequest(BaseModel):
    ids: list[int] | None = None


class AddGameRequest(BaseModel):
    paths: list[str] = Field(..., min_length=1)
    dest_dir: str | None = None


class ChangeDestRequest(BaseModel):
    item_ids: list[int] = Field(..., min_length=1)
    dest_dir: str = Field(..., min_length=1)


class AddQueueRequest(BaseModel):
    kind: Literal["mover", "ftp"]
    paths: list[str] = Field(..., min_length=1)
    recursive: bool = False
    operation: Literal["copiar", "mover"] | None = None
    dest_dir: str | None = None  # None o "" = destino automático según configuración
    only_switch: bool = False
    base_browse_dir: str | None = None


class BackupStartRequest(BaseModel):
    dest_dir: str | None = None  # None = carpeta de backups de Configuración
    incremental: bool | None = None  # None = ajuste de Configuración
    extra_exclude: list[str] | None = None  # Exclusiones adicionales solo para esta ejecución
    simulate: bool = False  # Solo recorre la SD y calcula qué se copiaría


class BackupRestoreRequest(BaseModel):
    path: str = Field(..., min_length=1)  # backup (carpeta o .tar.gz) o carpeta/fichero dentro de él
    start: bool = False  # arrancar la cola FTP tras encolar


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
    mover_favorites: list[str] | str | None = None

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
    ftp_backup_dir: str | None = None
    ftp_backup_remote_root: str | None = None
    ftp_backup_incremental: bool | None = None
    ftp_backup_compress: bool | None = None
    ftp_backup_keep: int | None = None
    ftp_backup_exclude: list[str] | str | None = None
    ftp_backup_exclude_ext: list[str] | str | None = None
    trash_days: int | None = None
    trash_enabled: bool | None = None

