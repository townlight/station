# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API for the channel program log (cable automation CA-1).

The guide editor (CA-5) drives these endpoints: slot CRUD, an on-demand
materialization trigger, and the merged per-channel log view that includes
honestly-recorded skips (conflicts, unplayable assets) for the operator to
resolve.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from civiccast.auth.roles import require_any_role
from civiccast.programlog.models import ProgramSlot, SlotOccurrence, SlotRecurrence
from civiccast.schedule.router import get_schedule_store

staff_router = APIRouter(prefix="/api/staff/programlog", tags=["staff", "programlog"])
public_router = APIRouter(prefix="/api/public/programlog", tags=["public", "programlog"])

_DB_NOT_READY_DETAIL = (
    "Durable storage is not ready. Open Setup and choose Prepare storage, "
    "or set DATABASE_URL for a technical deployment."
)
_PUBLIC_GUIDE_UNAVAILABLE_DETAIL = "The program guide is temporarily unavailable."


def get_program_log_store() -> Any:
    """DI seam: the app factory overrides this with the Postgres-backed store."""


def get_program_log_materializer() -> Any:
    """DI seam: the app factory overrides this with the wired materializer."""


def get_program_log_asset_titler() -> Any:
    """DI seam: callable resolving an asset id to a row with .title/.duration_seconds."""


def _require_store(store: Any) -> Any:
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY_DETAIL
        )
    return store


class ProgramSlotCreate(BaseModel):
    """Create payload for a recurring (or one-shot) program slot."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    asset_id: Annotated[str, Field(min_length=1, max_length=64)]
    recurrence: SlotRecurrence
    first_start_at: datetime
    duration_seconds: Annotated[int, Field(gt=0, le=1_209_600)] | None = None
    title_override: Annotated[str, Field(max_length=200)] | None = None
    repeat_until: datetime | None = None

    @field_validator("first_start_at", "repeat_until")
    @classmethod
    def _tz_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware (UTC recommended)")
        return value


class ProgramSlotUpdate(BaseModel):
    """Partial update for a slot; future occurrences re-materialize on the next scan."""

    model_config = ConfigDict(extra="forbid")

    recurrence: SlotRecurrence | None = None
    first_start_at: datetime | None = None
    duration_seconds: Annotated[int, Field(gt=0, le=1_209_600)] | None = None
    title_override: Annotated[str, Field(max_length=200)] | None = None
    repeat_until: datetime | None = None

    @field_validator("first_start_at", "repeat_until")
    @classmethod
    def _tz_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware (UTC recommended)")
        return value


class MaterializeResult(BaseModel):
    """Outcome of an on-demand materialization run."""

    model_config = ConfigDict(extra="forbid")

    scheduled: int
    skipped_conflict: int
    skipped_asset: int


class ChannelLogEntry(BaseModel):
    """One row of the merged per-channel program log."""

    model_config = ConfigDict(extra="forbid")

    occurrence_id: str
    slot_id: str
    channel_id: str
    asset_id: str
    title_override: str | None
    occurrence_start: datetime
    duration_seconds: int | None
    schedule_item_id: str | None
    # status: "scheduled" (materialized slot occurrence) | "manual" (a directly
    # scheduled item with no recurring slot, F-RC4-2) — both committable — or a
    # skip state ("skipped_conflict" / "skipped_asset" / "cancelled").
    status: str
    detail: str


@staff_router.post(
    "/slots",
    response_model=ProgramSlot,
    summary="Create a program slot and materialize its upcoming occurrences",
    dependencies=[Depends(require_any_role("meeting_operator", "support_admin"))],
)
def create_slot(
    payload: ProgramSlotCreate,
    store: Any = Depends(get_program_log_store),
    materializer: Any = Depends(get_program_log_materializer),
) -> ProgramSlot:
    store = _require_store(store)
    now = datetime.now(UTC)
    slot = store.create_slot(
        ProgramSlot(
            slot_id="cps_" + secrets.token_urlsafe(12).replace("-", "").replace("_", ""),
            channel_id=payload.channel_id,
            asset_id=payload.asset_id,
            title_override=payload.title_override,
            recurrence=payload.recurrence,
            first_start_at=payload.first_start_at,
            recurrence_anchor_local=(
                materializer.local_anchor(payload.first_start_at)
                if materializer is not None
                else payload.first_start_at.astimezone(UTC).replace(tzinfo=None)
            ),
            duration_seconds=payload.duration_seconds,
            repeat_until=payload.repeat_until,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
    )
    if materializer is not None:
        materializer.run_once(now=now)
    return slot  # type: ignore[no-any-return]


@staff_router.get(
    "/slots",
    response_model=list[ProgramSlot],
    summary="List program slots, optionally per channel",
)
def list_slots(
    channel_id: str | None = None,
    store: Any = Depends(get_program_log_store),
) -> list[ProgramSlot]:
    store = _require_store(store)
    return store.list_slots(channel_id=channel_id)  # type: ignore[no-any-return]


@staff_router.get(
    "/slots/{slot_id}",
    response_model=ProgramSlot,
    summary="Get one program slot",
    responses={404: {"description": "Slot not found"}},
)
def get_slot(slot_id: str, store: Any = Depends(get_program_log_store)) -> ProgramSlot:
    store = _require_store(store)
    slot = store.get_slot(slot_id)
    if slot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Program slot not found: {slot_id}"
        )
    return slot  # type: ignore[no-any-return]


@staff_router.patch(
    "/slots/{slot_id}",
    response_model=ProgramSlot,
    summary="Update a program slot (future occurrences re-materialize)",
    dependencies=[Depends(require_any_role("meeting_operator", "support_admin"))],
    responses={404: {"description": "Slot not found"}},
)
def update_slot(
    slot_id: str,
    payload: ProgramSlotUpdate,
    store: Any = Depends(get_program_log_store),
    materializer: Any = Depends(get_program_log_materializer),
) -> ProgramSlot:
    store = _require_store(store)
    slot = store.get_slot(slot_id)
    if slot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Program slot not found: {slot_id}"
        )
    updates = payload.model_dump(exclude_unset=True)
    if "first_start_at" in updates and updates["first_start_at"] != slot.first_start_at:
        updates["recurrence_anchor_local"] = (
            materializer.local_anchor(updates["first_start_at"])
            if materializer is not None
            else updates["first_start_at"].astimezone(UTC).replace(tzinfo=None)
        )
    updates["updated_at"] = datetime.now(UTC)
    updated = store.update_slot(slot.model_copy(update=updates))
    if materializer is not None:
        materializer.run_once(now=updates["updated_at"])
    return updated  # type: ignore[no-any-return]


@staff_router.post(
    "/slots/{slot_id}/disable",
    response_model=list[SlotOccurrence],
    summary="Disable a slot and cancel its future materialized airings",
    dependencies=[Depends(require_any_role("meeting_operator", "support_admin"))],
    responses={404: {"description": "Slot not found"}},
)
def disable_slot(
    slot_id: str,
    store: Any = Depends(get_program_log_store),
    materializer: Any = Depends(get_program_log_materializer),
) -> list[SlotOccurrence]:
    store = _require_store(store)
    if store.get_slot(slot_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Program slot not found: {slot_id}"
        )
    if materializer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY_DETAIL
        )
    return materializer.disable_slot(slot_id)  # type: ignore[no-any-return]


@staff_router.post(
    "/materialize",
    response_model=MaterializeResult,
    summary="Materialize all due occurrences now (refresh the guide)",
    dependencies=[Depends(require_any_role("meeting_operator", "support_admin"))],
)
def materialize_now(
    materializer: Any = Depends(get_program_log_materializer),
) -> MaterializeResult:
    if materializer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY_DETAIL
        )
    processed: list[SlotOccurrence] = materializer.run_once()
    return MaterializeResult(
        scheduled=sum(1 for o in processed if o.status == "scheduled"),
        skipped_conflict=sum(1 for o in processed if o.status == "skipped_conflict"),
        skipped_asset=sum(1 for o in processed if o.status == "skipped_asset"),
    )


@staff_router.get(
    "/channels/{channel_id}/log",
    response_model=list[ChannelLogEntry],
    summary="Merged program log for one channel, including recorded skips",
)
def channel_log(
    channel_id: str,
    hours: int = Query(default=72, ge=1, le=24 * 14),
    store: Any = Depends(get_program_log_store),
    schedule_store: Any = Depends(get_schedule_store),
) -> list[ChannelLogEntry]:
    store = _require_store(store)
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=hours)
    entries: list[ChannelLogEntry] = []
    covered_schedule_item_ids: set[str] = set()
    for slot in store.list_slots(channel_id=channel_id):
        for occurrence in store.list_occurrences(slot_id=slot.slot_id):
            if occurrence.occurrence_start > horizon:
                continue
            if occurrence.schedule_item_id is not None:
                covered_schedule_item_ids.add(str(occurrence.schedule_item_id))
            entries.append(
                ChannelLogEntry(
                    occurrence_id=occurrence.occurrence_id,
                    slot_id=slot.slot_id,
                    channel_id=slot.channel_id,
                    asset_id=slot.asset_id,
                    title_override=slot.title_override,
                    occurrence_start=occurrence.occurrence_start,
                    duration_seconds=slot.duration_seconds,
                    schedule_item_id=occurrence.schedule_item_id,
                    status=occurrence.status,
                    detail=occurrence.detail,
                )
            )
    # F-RC4-2: manually-created schedule items never produce a SlotOccurrence
    # (that link only ever runs slot -> item, never the reverse), so before
    # this fix they were structurally invisible to Commit-to-Air even though
    # commit only needs a valid schedule_item_id. Surface committable manual
    # items directly from the schedule store, deduped against any that a slot
    # already covers. Synthetic occurrence_id (manual:<id>) is stable and
    # opaque provenance; the commit path never looks it up.
    if schedule_store is not None:
        for item in schedule_store.list(channel_id=channel_id, states=("scheduled", "published")):
            item_id = str(item.id)
            if item_id in covered_schedule_item_ids:
                continue
            if item.scheduled_at < now or item.scheduled_at > horizon:
                continue
            entries.append(
                ChannelLogEntry(
                    occurrence_id=f"manual:{item_id}",
                    slot_id="",
                    channel_id=item.channel_id,
                    asset_id=item.asset_id,
                    title_override=item.asset_title,
                    occurrence_start=item.scheduled_at,
                    duration_seconds=item.duration_seconds,
                    schedule_item_id=item_id,
                    status="manual",
                    detail="Scheduled directly (no recurring slot).",
                )
            )
    entries.sort(key=lambda entry: (entry.occurrence_start, entry.occurrence_id))
    return entries


class PublicGuideEntry(BaseModel):
    """One sanitized resident-facing guide entry: what airs, when, for how long."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    title: str
    starts_at: datetime
    duration_seconds: int | None


@public_router.get(
    "/channels/{channel_id}/guide",
    response_model=list[PublicGuideEntry],
    summary="Resident-facing channel guide: scheduled airings only, no internal detail",
)
def public_guide(
    channel_id: str,
    hours: int = Query(default=72, ge=1, le=24 * 14),
    store: Any = Depends(get_program_log_store),
    titler: Any = Depends(get_program_log_asset_titler),
) -> list[PublicGuideEntry]:
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_PUBLIC_GUIDE_UNAVAILABLE_DETAIL,
        )
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=hours)
    entries: list[PublicGuideEntry] = []
    for slot in store.list_slots(channel_id=channel_id):
        title = slot.title_override
        duration = slot.duration_seconds
        if (title is None or duration is None) and titler is not None:
            asset = titler(slot.asset_id)
            if asset is not None:
                title = title or getattr(asset, "title", None)
                if duration is None:
                    duration = getattr(asset, "duration_seconds", None)
        title = title or slot.asset_id
        for occurrence in store.list_occurrences(slot_id=slot.slot_id):
            if occurrence.status != "scheduled":
                continue
            if occurrence.occurrence_start < now or occurrence.occurrence_start > horizon:
                continue
            entries.append(
                PublicGuideEntry(
                    channel_id=slot.channel_id,
                    title=title,
                    starts_at=occurrence.occurrence_start,
                    duration_seconds=duration,
                )
            )
    entries.sort(key=lambda entry: (entry.starts_at, entry.title))
    return entries
