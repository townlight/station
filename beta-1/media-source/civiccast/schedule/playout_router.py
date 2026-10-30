# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API for the Commit-to-Air gate (CivicCast 3.0 — S4 slice 4).

Two write endpoints, both gated to ``publish_operator`` / ``setup_admin``:

* ``POST /api/staff/playout/prepare-commit`` — read-only dry-run. Returns a
  :class:`CommitToAirPlan` (with ``dry_run_passed`` + the reasons it would or
  would not air). An *unplayable* item is reported as ``dry_run_passed=False``
  with a 200 — not a 422 — so the operator sees *why* it cannot air rather than
  a bare rejection. Only a genuinely absent schedule item is a 404.

* ``POST /api/staff/playout/commit`` — approve and air. 201 with the queued
  :class:`CommitToAirReport`; 409 when a conflict appeared since the operator
  reviewed; 422 when the asset became unplayable since review; 404 when the
  schedule item is gone.

**plan_id, as built (spec reconciliation).** S4 §4 specified the commit body as
``{plan_id, operator_notes}``, implying the server remembers each prepared plan.
A server-side plan cache is fragile — it is lost on restart and, critically, is
**not shared across uvicorn workers**, so a commit could hit a worker that never
saw the prepare. Re-running the dry-run is cheap and is already the spec's
step-1 race check, so the commit body instead echoes the identifying params the
prepare response already carries (``channel_id`` / ``occurrence_id`` /
``schedule_item_id``) plus an optional ``plan_id`` for audit correlation. The
re-run dry-run is the authoritative race check (→ 409 / 422); the spec's
``400 plan-expired`` is therefore subsumed and not a distinct status here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from civiccast.auth.models import OperatorIdentity
from civiccast.auth.roles import require_any_role
from civiccast.schedule.commit_models import CommitToAirPlan, CommitToAirReport
from civiccast.schedule.commit_service import (
    CommitConflictError,
    CommitReportNotFoundError,
    CommitService,
)
from civiccast.schedule.store import ScheduleItemNotFoundError

# Writes (commit, rollback) require the right to put content on / off air.
_WRITE_ROLES = ("publish_operator", "setup_admin")
# Read-only diagnostics additionally allow support_admin (spec §4).
_READ_ROLES = ("publish_operator", "setup_admin", "support_admin")

_DB_NOT_READY_DESCRIPTION = "Durable storage not ready -- run Setup storage or set DATABASE_URL"
_DB_NOT_READY_DETAIL = (
    "Durable storage is not ready. Open Setup and choose Prepare storage, "
    "or set DATABASE_URL for a technical deployment."
)


def get_commit_service() -> Any:
    """FastAPI dependency for the Commit-to-Air service.

    Returns None when ``DATABASE_URL`` is not configured; the handlers
    translate None into HTTP 503. The app factory in ``civiccast.app``
    overrides this with a real :class:`CommitService` when durable storage is
    active. Typed as ``Any`` to avoid a router→service→store import cycle.
    """


staff_router = APIRouter(prefix="/api/staff/playout", tags=["playout"])


def _service_or_503(service: Any) -> CommitService:
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY_DETAIL
        )
    return cast(CommitService, service)


class PrepareCommitRequest(BaseModel):
    """Body for ``POST /api/staff/playout/prepare-commit``."""

    channel_id: str = Field(..., min_length=1, max_length=80)
    occurrence_id: str = Field(..., min_length=1, max_length=120)
    schedule_item_id: uuid.UUID


class CommitRequest(BaseModel):
    """Body for ``POST /api/staff/playout/commit``.

    Carries the identifying params (the prepare response already holds them)
    plus an optional ``plan_id`` echoed for audit correlation — see the module
    docstring on why the server does not look the plan up.
    """

    channel_id: str = Field(..., min_length=1, max_length=80)
    occurrence_id: str = Field(..., min_length=1, max_length=120)
    schedule_item_id: uuid.UUID
    operator_notes: str | None = Field(default=None, max_length=2000)
    plan_id: str | None = Field(default=None, max_length=120)


def _operator_id(request: Request) -> str:
    """Read the verified operator id (require_any_role has already 401'd if absent)."""
    identity = getattr(request.state, "operator_identity", None)
    if not isinstance(identity, OperatorIdentity):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Staff identity is required for this action.",
        )
    return identity.operator_id


@staff_router.post(
    "/prepare-commit",
    response_model=CommitToAirPlan,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Dry-run a commit-to-air for an occurrence (no change made)",
    responses={
        404: {"description": "Schedule item not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def prepare_commit(
    body: PrepareCommitRequest,
    request: Request,
    service: Any = Depends(get_commit_service),
) -> CommitToAirPlan:
    """Return the dry-run plan. Unplayable / conflicting is reported in the plan
    (``dry_run_passed=False``), not raised — only a missing item is a 404."""
    commit_service = _service_or_503(service)
    operator_id = _operator_id(request)
    try:
        return commit_service.prepare(
            channel_id=body.channel_id,
            occurrence_id=body.occurrence_id,
            schedule_item_id=body.schedule_item_id,
            operator_id=operator_id,
        )
    except ScheduleItemNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Schedule item not found: {body.schedule_item_id}",
        ) from exc


@staff_router.post(
    "/commit",
    response_model=CommitToAirReport,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Approve and air a committed occurrence",
    responses={
        404: {"description": "Schedule item not found"},
        409: {"description": "A schedule conflict appeared since the plan was reviewed"},
        422: {"description": "The asset became unplayable since the plan was reviewed"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def commit(
    body: CommitRequest,
    request: Request,
    service: Any = Depends(get_commit_service),
) -> CommitToAirReport:
    """Re-check, persist the approval, and dispatch the engine nudge.

    Returns 201 with the report (``dispatch_status`` is ``queued`` on success,
    or ``error`` in the body if the engine nudge failed — the approval is still
    durably recorded). 409/422 on a re-check failure; 404 if the item is gone.
    """
    commit_service = _service_or_503(service)
    operator_id = _operator_id(request)
    try:
        return commit_service.commit(
            channel_id=body.channel_id,
            occurrence_id=body.occurrence_id,
            schedule_item_id=body.schedule_item_id,
            operator_id=operator_id,
            operator_notes=body.operator_notes,
        )
    except ScheduleItemNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Schedule item not found: {body.schedule_item_id}",
        ) from exc
    except CommitConflictError as exc:
        if exc.plan.conflicts_detected:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": str(exc),
                    "conflicts": [c.model_dump(mode="json") for c in exc.plan.conflicts_detected],
                },
            ) from exc
        # No conflict — the item became unplayable (missing media) since review.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


class RollbackRequest(BaseModel):
    """Body for ``POST /api/staff/playout/rollback/{report_id}``."""

    reason: str = Field(..., min_length=1, max_length=2000)


@staff_router.get(
    "/commits",
    response_model=list[CommitToAirReport],
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    summary="List a channel's commit-to-air reports (most recent first)",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def list_commits(
    channel_id: str,
    service: Any = Depends(get_commit_service),
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    limit: int = 50,
) -> list[CommitToAirReport]:
    """Read-only list, filtered by ``channel_id`` and an optional ``approved_at``
    date range. Ordered newest-committed first."""
    commit_service = _service_or_503(service)
    return commit_service.list_commits(
        channel_id=channel_id, start_at=start_at, end_at=end_at, limit=limit
    )


@staff_router.get(
    "/commits/{report_id}",
    response_model=CommitToAirReport,
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    summary="Get one commit-to-air report",
    responses={
        404: {"description": "Report not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_commit(
    report_id: str,
    service: Any = Depends(get_commit_service),
) -> CommitToAirReport:
    """Read-only detail for one report. 404 if absent."""
    commit_service = _service_or_503(service)
    result = commit_service.get_commit(report_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Commit report not found: {report_id}",
        )
    return result


@staff_router.post(
    "/rollback/{report_id}",
    response_model=CommitToAirReport,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Roll back (undo) a committed airing",
    responses={
        404: {"description": "Report not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def rollback(
    report_id: str,
    body: RollbackRequest,
    request: Request,
    service: Any = Depends(get_commit_service),
) -> CommitToAirReport:
    """Cancel the linked schedule item, hand back to the engine, and mark the
    report ``cancelled`` with the operator's reason. 404 if the report is gone."""
    commit_service = _service_or_503(service)
    operator_id = _operator_id(request)
    try:
        return commit_service.rollback(
            report_id=report_id, reason=body.reason, operator_id=operator_id
        )
    except CommitReportNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Commit report not found: {report_id}",
        ) from exc
