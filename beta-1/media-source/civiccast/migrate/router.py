# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""0.4.0 migration staff API — dry-run, apply, rollback, batch history.

Follows the ``setup_admin``-gated installer router convention: every route
requires the ``setup_admin`` product role (migrating a station's history is
a one-time, high-stakes operator action, not a day-to-day producer task).
The DI seam (``get_migration_service``) returns ``None`` at import so the
module opens no database; the app factory overrides it in
``_wire_durable_stores`` — an unwired surface is a 503, never a silent 200
against storage that is not there (same convention as agenda / underwriting
/ paywall).
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from civiccast.auth.roles import require_any_role
from civiccast.migrate.adapters import (
    CablecastAdapter,
    CablecastConnection,
    CastusAdapter,
    CastusConnection,
    LeightronixAdapter,
    LeightronixConnection,
    SourceAdapter,
    SourceFormatError,
    TelvueAdapter,
    TelvueConnection,
)
from civiccast.migrate.models import (
    ApplyRequest,
    ConnectionInfo,
    DryRunRequest,
    ImportBatch,
    ImportPlan,
    MigrationRollbackRequest,
)
from civiccast.migrate.service import MigrationService
from civiccast.migrate.store import BatchAlreadyRolledBackError, BatchNotFoundError

_DB_NOT_READY = "Durable storage is not ready yet."
_SETUP_ADMIN = ("setup_admin",)
_SETUP_ADMIN_EXTRA = {"x-required-roles": list(_SETUP_ADMIN)}


def _build_adapter(conn: ConnectionInfo) -> SourceAdapter:
    """One adapter per ``source_system`` -- Cablecast is a live network
    source; TelVue/Castus/Leightronix are file-based (see
    :class:`civiccast.migrate.models.ConnectionInfo`)."""
    if conn.source_system == "cablecast":
        assert conn.base_url is not None  # enforced by ConnectionInfo's validator
        return CablecastAdapter(
            CablecastConnection(
                base_url=conn.base_url, username=conn.username, password=conn.password
            )
        )
    assert conn.schedule_file is not None  # enforced by ConnectionInfo's validator
    if conn.source_system == "telvue":
        return TelvueAdapter(TelvueConnection(schedule_csv=conn.schedule_file))
    if conn.source_system == "castus":
        return CastusAdapter(CastusConnection(schedule_csv=conn.schedule_file))
    return LeightronixAdapter(LeightronixConnection(schedule_csv=conn.schedule_file))


staff_router = APIRouter(prefix="/api/staff/migrate", tags=["staff", "migrate"])


def get_migration_service() -> MigrationService | None:
    """DI seam — overridden by ``_wire_durable_stores`` in the app factory."""
    return None


def _require_service(svc: MigrationService | None) -> MigrationService:
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return svc


@staff_router.post(
    "/dry-run",
    response_model=ImportPlan,
    summary="Fetch a source system's inventory and produce a typed import diff (no writes)",
    dependencies=[Depends(require_any_role(*_SETUP_ADMIN))],
    openapi_extra=_SETUP_ADMIN_EXTRA,
    responses={
        400: {"description": "Source file could not be parsed as that vendor's export format"},
        502: {"description": "Could not reach the source system"},
        503: {"description": _DB_NOT_READY},
    },
)
def dry_run(
    payload: DryRunRequest,
    svc: MigrationService | None = Depends(get_migration_service),
) -> ImportPlan:
    """Read-only. Fetches (or parses, for file-based sources) the source
    system's inventory and diffs it against CivicCast's real stores —
    nothing is written. Feed the returned plan back to ``POST /apply`` to
    actually create anything."""
    resolved = _require_service(svc)
    conn = payload.connection
    adapter = _build_adapter(conn)
    try:
        inventory = adapter.fetch_inventory()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach source system at {conn.base_url!r}: {exc}",
        ) from exc
    except SourceFormatError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return resolved.dry_run(inventory)


@staff_router.post(
    "/apply",
    response_model=ImportBatch,
    status_code=status.HTTP_201_CREATED,
    summary="Apply a dry-run plan: create the proposed shows + schedule items for real",
    dependencies=[Depends(require_any_role(*_SETUP_ADMIN))],
    openapi_extra=_SETUP_ADMIN_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def apply(
    payload: ApplyRequest,
    svc: MigrationService | None = Depends(get_migration_service),
) -> ImportBatch:
    """Applies EXACTLY the plan given — a plan that has gone stale since its
    dry-run (a conflicting row landed in the meantime) will surface those
    rows in the response's ``apply_failures`` rather than fail the whole
    call. Every row created is tagged under one ``import_batch_id`` for
    ``POST /rollback``."""
    return _require_service(svc).apply(payload.plan)


@staff_router.post(
    "/rollback",
    response_model=ImportBatch,
    summary="Undo an apply: delete exactly the rows that batch created",
    dependencies=[Depends(require_any_role(*_SETUP_ADMIN))],
    openapi_extra=_SETUP_ADMIN_EXTRA,
    responses={
        404: {"description": "Import batch not found"},
        409: {"description": "Import batch was already rolled back"},
        503: {"description": _DB_NOT_READY},
    },
)
def rollback(
    payload: MigrationRollbackRequest,
    svc: MigrationService | None = Depends(get_migration_service),
) -> ImportBatch:
    resolved = _require_service(svc)
    try:
        return resolved.rollback(payload.import_batch_id)
    except BatchNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BatchAlreadyRolledBackError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.get(
    "/batches",
    response_model=list[ImportBatch],
    summary="List every import batch (applied and rolled back)",
    dependencies=[Depends(require_any_role(*_SETUP_ADMIN))],
    openapi_extra=_SETUP_ADMIN_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def list_batches(
    svc: MigrationService | None = Depends(get_migration_service),
) -> list[ImportBatch]:
    return _require_service(svc).list_batches()


__all__ = ["get_migration_service", "staff_router"]
