# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API for query-driven auto-scheduling (CivicCast 3.0 — S18 slice 5a).

CRUD for the three S18 entities under ``/api/staff/auto-schedule``:

* ``/saved-searches`` — named asset queries (a :class:`SavedSearch`);
* ``/blocks``         — daypart windows (a :class:`ScheduleBlock`, gap 4);
* ``/rules``          — auto-schedule rules (an :class:`AutoScheduleRule`).

Writes (POST/PUT/DELETE) require ``publish_operator`` / ``setup_admin`` — the
same gate S4's commit-to-air uses, because a rule ultimately decides what airs.
Reads additionally allow ``support_admin``. Mutating a daypart block or a rule
with an out-of-contract value (e.g. an empty weekday set, a rolling window
outside 14-60) surfaces as a 422 (the domain model's validators run when the
service builds it). The preview (dry-run) and compile endpoints are slice 5b.

DI: :func:`get_autoschedule_service` returns None until the app factory wires a
real :class:`AutoScheduleService` (handlers return 503 in the meantime),
mirroring ``playout_router.get_commit_service``.
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ValidationError

from civiccast.auth.roles import require_any_role
from civiccast.schedule.autoschedule_models import (
    PICK_NEWEST,
    AssetQuery,
    AutoScheduleRule,
    PickStrategyValue,
    SavedSearch,
    ScheduleBlock,
)
from civiccast.schedule.autoschedule_service import AutoScheduleService, CompileReport, RulePreview

_WRITE_ROLES = ("publish_operator", "setup_admin")
_READ_ROLES = ("publish_operator", "setup_admin", "support_admin")

_DB_NOT_READY_DESCRIPTION = "Durable storage not ready -- run Setup storage or set DATABASE_URL"
_DB_NOT_READY_DETAIL = (
    "Durable storage is not ready. Open Setup and choose Prepare storage, "
    "or set DATABASE_URL for a technical deployment."
)


def get_autoschedule_service() -> Any:
    """FastAPI dependency for the auto-schedule service.

    Returns None when ``DATABASE_URL`` is not configured (handlers translate
    None to HTTP 503). The app factory overrides this with a real
    :class:`AutoScheduleService`. Typed ``Any`` to avoid a router→service→store
    import cycle.
    """


staff_router = APIRouter(prefix="/api/staff/auto-schedule", tags=["auto-schedule"])


def _require_service(service: Any) -> AutoScheduleService:
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY_DETAIL
        )
    return cast(AutoScheduleService, service)


# ---------------------------------------------------------------------------
# Request bodies (operator-supplied fields only; ids + timestamps are server-set)
# ---------------------------------------------------------------------------


class SavedSearchInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    query: AssetQuery = Field(default_factory=AssetQuery)


class ScheduleBlockInput(BaseModel):
    # channel_id is a slug, matching ScheduleItemCreate's pattern — a rule's
    # materialized items would otherwise hit a channel id the schedule API
    # rejects (slice-4 audit watch-item #2).
    channel_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    name: str = Field(..., min_length=1, max_length=200)
    start_minute: int = Field(..., ge=0, lt=24 * 60)
    end_minute: int = Field(..., gt=0, le=24 * 60)
    days_of_week: list[int] = Field(default_factory=list)
    active_from: date | None = None
    active_until: date | None = None
    enabled: bool = True


class AutoScheduleRuleInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    saved_search_id: str = Field(..., min_length=1, max_length=64)
    # Slug (see ScheduleBlockInput.channel_id); the id refs hold ss_/sb_ tokens
    # with underscores, so they keep plain length bounds, not the slug pattern.
    channel_id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    schedule_block_id: str = Field(..., min_length=1, max_length=64)
    pick_strategy: PickStrategyValue = PICK_NEWEST
    rolling_window_days: int = Field(default=30, ge=14, le=60)
    repeat_prevention_days: int = Field(default=0, ge=0)
    priority: int = Field(default=100, ge=0)
    enabled: bool = True


def _validation_422(exc: ValidationError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
    )


# ---------------------------------------------------------------------------
# SavedSearch
# ---------------------------------------------------------------------------


@staff_router.get(
    "/saved-searches",
    response_model=list[SavedSearch],
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    summary="List saved searches",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def list_saved_searches(service: Any = Depends(get_autoschedule_service)) -> list[SavedSearch]:
    return _require_service(service).list_saved_searches()


@staff_router.post(
    "/saved-searches",
    response_model=SavedSearch,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Create a saved search",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def create_saved_search(
    body: SavedSearchInput, service: Any = Depends(get_autoschedule_service)
) -> SavedSearch:
    return _require_service(service).create_saved_search(
        name=body.name, description=body.description, query=body.query
    )


@staff_router.get(
    "/saved-searches/{saved_search_id}",
    response_model=SavedSearch,
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    summary="Get one saved search",
    responses={404: {"description": "Not found"}, 503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def get_saved_search(
    saved_search_id: str, service: Any = Depends(get_autoschedule_service)
) -> SavedSearch:
    result = _require_service(service).get_saved_search(saved_search_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Saved search not found: {saved_search_id}")
    return result


@staff_router.put(
    "/saved-searches/{saved_search_id}",
    response_model=SavedSearch,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Update a saved search",
    responses={404: {"description": "Not found"}, 503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def update_saved_search(
    saved_search_id: str,
    body: SavedSearchInput,
    service: Any = Depends(get_autoschedule_service),
) -> SavedSearch:
    result = _require_service(service).update_saved_search(
        saved_search_id, name=body.name, description=body.description, query=body.query
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Saved search not found: {saved_search_id}")
    return result


@staff_router.delete(
    "/saved-searches/{saved_search_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Delete a saved search",
    responses={404: {"description": "Not found"}, 503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def delete_saved_search(
    saved_search_id: str, service: Any = Depends(get_autoschedule_service)
) -> None:
    if not _require_service(service).delete_saved_search(saved_search_id):
        raise HTTPException(status_code=404, detail=f"Saved search not found: {saved_search_id}")


# ---------------------------------------------------------------------------
# ScheduleBlock
# ---------------------------------------------------------------------------


@staff_router.get(
    "/blocks",
    response_model=list[ScheduleBlock],
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    summary="List daypart blocks",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def list_blocks(
    service: Any = Depends(get_autoschedule_service),
    channel_id: str | None = None,
    enabled_only: bool = False,
) -> list[ScheduleBlock]:
    return _require_service(service).list_schedule_blocks(
        channel_id=channel_id, enabled_only=enabled_only
    )


@staff_router.post(
    "/blocks",
    response_model=ScheduleBlock,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Create a daypart block",
    responses={
        422: {"description": "Invalid daypart (weekday set / window / dates)"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def create_block(
    body: ScheduleBlockInput, service: Any = Depends(get_autoschedule_service)
) -> ScheduleBlock:
    svc = _require_service(service)
    try:
        return svc.create_schedule_block(**body.model_dump())
    except ValidationError as exc:
        raise _validation_422(exc) from exc


@staff_router.get(
    "/blocks/{block_id}",
    response_model=ScheduleBlock,
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    summary="Get one daypart block",
    responses={404: {"description": "Not found"}, 503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def get_block(block_id: str, service: Any = Depends(get_autoschedule_service)) -> ScheduleBlock:
    result = _require_service(service).get_schedule_block(block_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Daypart block not found: {block_id}")
    return result


@staff_router.put(
    "/blocks/{block_id}",
    response_model=ScheduleBlock,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Update a daypart block",
    responses={
        404: {"description": "Not found"},
        422: {"description": "Invalid daypart (weekday set / window / dates)"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def update_block(
    block_id: str, body: ScheduleBlockInput, service: Any = Depends(get_autoschedule_service)
) -> ScheduleBlock:
    svc = _require_service(service)
    try:
        result = svc.update_schedule_block(block_id, **body.model_dump())
    except ValidationError as exc:
        raise _validation_422(exc) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"Daypart block not found: {block_id}")
    return result


@staff_router.delete(
    "/blocks/{block_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Delete a daypart block",
    responses={404: {"description": "Not found"}, 503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def delete_block(block_id: str, service: Any = Depends(get_autoschedule_service)) -> None:
    if not _require_service(service).delete_schedule_block(block_id):
        raise HTTPException(status_code=404, detail=f"Daypart block not found: {block_id}")


# ---------------------------------------------------------------------------
# AutoScheduleRule
# ---------------------------------------------------------------------------


@staff_router.get(
    "/rules",
    response_model=list[AutoScheduleRule],
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    summary="List auto-schedule rules",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def list_rules(
    service: Any = Depends(get_autoschedule_service),
    channel_id: str | None = None,
    enabled_only: bool = False,
) -> list[AutoScheduleRule]:
    return _require_service(service).list_rules(channel_id=channel_id, enabled_only=enabled_only)


@staff_router.post(
    "/rules",
    response_model=AutoScheduleRule,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Create an auto-schedule rule",
    responses={
        422: {"description": "Invalid rule (pick strategy / window bounds)"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def create_rule(
    body: AutoScheduleRuleInput, service: Any = Depends(get_autoschedule_service)
) -> AutoScheduleRule:
    svc = _require_service(service)
    try:
        return svc.create_rule(**body.model_dump())
    except ValidationError as exc:
        raise _validation_422(exc) from exc


@staff_router.get(
    "/rules/{rule_id}",
    response_model=AutoScheduleRule,
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    summary="Get one auto-schedule rule",
    responses={404: {"description": "Not found"}, 503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def get_rule(rule_id: str, service: Any = Depends(get_autoschedule_service)) -> AutoScheduleRule:
    result = _require_service(service).get_rule(rule_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Auto-schedule rule not found: {rule_id}")
    return result


@staff_router.put(
    "/rules/{rule_id}",
    response_model=AutoScheduleRule,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Update an auto-schedule rule",
    responses={
        404: {"description": "Not found"},
        422: {"description": "Invalid rule (pick strategy / window bounds)"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def update_rule(
    rule_id: str, body: AutoScheduleRuleInput, service: Any = Depends(get_autoschedule_service)
) -> AutoScheduleRule:
    svc = _require_service(service)
    try:
        result = svc.update_rule(rule_id, **body.model_dump())
    except ValidationError as exc:
        raise _validation_422(exc) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"Auto-schedule rule not found: {rule_id}")
    return result


@staff_router.delete(
    "/rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Delete an auto-schedule rule",
    responses={404: {"description": "Not found"}, 503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def delete_rule(rule_id: str, service: Any = Depends(get_autoschedule_service)) -> None:
    if not _require_service(service).delete_rule(rule_id):
        raise HTTPException(status_code=404, detail=f"Auto-schedule rule not found: {rule_id}")


# ---------------------------------------------------------------------------
# Preview (dry-run) + compile
# ---------------------------------------------------------------------------


@staff_router.post(
    "/rules/{rule_id}/preview",
    response_model=RulePreview,
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    summary="Dry-run a rule: the slots it would fill, without writing",
    responses={404: {"description": "Not found"}, 503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def preview_rule(rule_id: str, service: Any = Depends(get_autoschedule_service)) -> RulePreview:
    """Read-only: shows what a compile would do with this rule (occupancy,
    repeat-prevention, and the picked asset per slot). Creates nothing."""
    svc = _require_service(service)
    try:
        result = svc.preview_rule(rule_id)
    except RuntimeError as exc:  # no session factory → storage not ready
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY_DETAIL
        ) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"Auto-schedule rule not found: {rule_id}")
    return result


@staff_router.post(
    "/compile",
    response_model=CompileReport,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    summary="Compile all enabled rules into schedule_items now",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def compile_now(service: Any = Depends(get_autoschedule_service)) -> CompileReport:
    """Run the rolling-window materializer for every enabled rule. The created
    items are ``scheduled`` and still flow through the S4 commit gate."""
    svc = _require_service(service)
    try:
        return svc.compile()
    except RuntimeError as exc:  # no session factory → storage not ready
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY_DETAIL
        ) from exc
