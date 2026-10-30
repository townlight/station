# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Operator endpoints for encrypted station backup management."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from civiccast.auth.roles import require_any_role
from civiccast.dr.station_backup import BackupSchedule, StationBackupService

staff_router = APIRouter(
    prefix="/api/staff/backups",
    tags=["staff", "backup"],
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
_ARCHIVE_ID = re.compile(r"^station-backup-[0-9TZ-]+-[0-9a-f]{8}$")


class CreateBackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: str = Field(min_length=12, max_length=1024)


class VerifyBackupRequest(CreateBackupRequest):
    backup_id: str = Field(min_length=1, max_length=128)


class ScheduleRequest(BackupSchedule):
    pass


class ScheduledPassphraseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passphrase: str = Field(min_length=12, max_length=1024)


def _service(request: Request) -> StationBackupService:
    service = getattr(request.app.state, "station_backup_service", None)
    if not isinstance(service, StationBackupService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Backup service is unavailable"
        )
    return service


def _artifact_payload(item: Any) -> dict[str, Any]:
    return {
        "backup_id": item.backup_id,
        "file_name": item.path.name,
        "created_at": item.created_at.isoformat(),
        "size_bytes": item.size_bytes,
    }


@staff_router.get("")
def list_station_backups(request: Request) -> dict[str, Any]:
    service = _service(request)
    return {"backups": [_artifact_payload(item) for item in service.list_backups()]}


@staff_router.get("/status")
def station_backup_status(request: Request) -> dict[str, Any]:
    status_data = _service(request).status()
    if isinstance(status_data.get("last_success"), dict):
        last_success = dict(status_data["last_success"])
        path_value = last_success.pop("path", None)
        if path_value:
            last_success["file_name"] = Path(str(path_value)).name
        status_data["last_success"] = last_success
    status_data["backups"] = [
        {key: value for key, value in item.items() if key != "path"}
        | {"file_name": Path(str(item["path"])).name}
        for item in status_data["backups"]
    ]
    return status_data


@staff_router.post("/create")
def create_station_backup(body: CreateBackupRequest, request: Request) -> dict[str, Any]:
    service = _service(request)
    try:
        return _artifact_payload(service.create_backup(body.passphrase))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=_public_error(exc)) from exc


@staff_router.post("/verify")
def verify_station_backup(body: VerifyBackupRequest, request: Request) -> dict[str, Any]:
    if not _ARCHIVE_ID.fullmatch(body.backup_id):
        raise HTTPException(status_code=400, detail="Backup identifier is invalid")
    service = _service(request)
    artifact = next(
        (item for item in service.list_backups() if item.backup_id == body.backup_id), None
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Backup was not found")
    try:
        result = service.verify_backup(artifact.path, body.passphrase)
        return {
            "backup_id": result.backup_id,
            "valid": result.valid,
            "database_engine": result.database_engine,
            "files_verified": result.files_verified,
            "created_at": result.created_at.isoformat(),
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_public_error(exc)) from exc


@staff_router.put("/schedule")
def update_station_backup_schedule(body: ScheduleRequest, request: Request) -> dict[str, Any]:
    service = _service(request)
    try:
        return service.set_schedule(body).model_dump(mode="json")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@staff_router.put("/schedule/passphrase")
def set_scheduled_passphrase(body: ScheduledPassphraseRequest, request: Request) -> dict[str, bool]:
    service = _service(request)
    try:
        service.set_scheduled_passphrase(body.passphrase)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Could not save the scheduled backup passphrase. "
                "Check the station credentials folder permissions and try again."
            ),
        ) from exc
    return {"configured": service.has_scheduled_passphrase()}


@staff_router.get("/{backup_id}/download")
def download_station_backup(backup_id: str, request: Request) -> StreamingResponse:
    if not _ARCHIVE_ID.fullmatch(backup_id):
        raise HTTPException(status_code=400, detail="Backup identifier is invalid")
    artifact = next(
        (item for item in _service(request).list_backups() if item.backup_id == backup_id), None
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Backup was not found")

    def chunks() -> Iterator[bytes]:
        with artifact.path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                yield block

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{artifact.path.name}"'},
    )


def _public_error(exc: Exception) -> str:
    # Exception text can contain URLs with database passwords or provider
    # responses. Return a stable operator-safe message; detailed diagnostics
    # remain with the local service log.
    if isinstance(exc, ValueError):
        message = str(exc)
        if "://" not in message and not re.search(r"pass(word|phrase|wd)\s*[=:]", message, re.I):
            return message[:240]
    return f"Backup operation failed ({type(exc).__name__})"
