# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Program-log persistence models (cable automation CA-1).

A :class:`ProgramSlot` is the operator's intent ("Council replay, Fridays at
19:00 station time"); a :class:`SlotOccurrence` records each materialized instance and
points at the real ``schedule_items`` row the playout path consumes. The
UNIQUE (slot_id, occurrence_start) key makes materialization idempotent.

Occurrence timestamps are persisted as UTC instants. Recurrence calendar dates
and wall-clock times use the configured station timezone, including explicit
handling of skipped spring-forward clocks and the first fall-back fold.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

SlotRecurrence = Literal["once", "daily", "weekly", "weekdays"]
OccurrenceStatus = Literal["scheduled", "skipped_conflict", "skipped_asset", "cancelled"]

RECURRENCE_VALUES: tuple[str, ...] = ("once", "daily", "weekly", "weekdays")


class ProgramSlot(BaseModel):
    """One recurring (or one-shot) program placement on a channel."""

    model_config = ConfigDict(extra="forbid")

    slot_id: Annotated[str, Field(min_length=1, max_length=120)]
    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    asset_id: Annotated[str, Field(min_length=1, max_length=64)]
    title_override: Annotated[str, Field(max_length=200)] | None = None
    recurrence: SlotRecurrence
    first_start_at: datetime
    # Server-derived local calendar/clock anchor, independent of mutable station TZ.
    # Null legacy rows adopt the station zone on their first materialization.
    recurrence_anchor_local: datetime | None = Field(
        default=None,
        description="Original station-local calendar/clock anchor without a timezone offset.",
        json_schema_extra={"readOnly": True},
    )
    duration_seconds: Annotated[int, Field(gt=0, le=1_209_600)] | None = None
    repeat_until: datetime | None = None
    enabled: bool = True
    created_at: datetime
    updated_at: datetime


class SlotOccurrence(BaseModel):
    """One materialized instance of a slot inside the rolling horizon."""

    model_config = ConfigDict(extra="forbid")

    occurrence_id: Annotated[str, Field(min_length=1, max_length=120)]
    slot_id: Annotated[str, Field(min_length=1, max_length=120)]
    occurrence_start: datetime
    schedule_item_id: Annotated[str, Field(max_length=64)] | None = None
    status: OccurrenceStatus
    detail: str = ""
    created_at: datetime


class ChannelProgramSlotDb(Base):
    """Durable row for an operator-defined program slot."""

    __tablename__ = "channel_program_slots"

    slot_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title_override: Mapped[str | None] = mapped_column(String(200), nullable=True)
    recurrence: Mapped[str] = mapped_column(String(16), nullable=False)
    first_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recurrence_anchor_local: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    repeat_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ProgramSlotOccurrenceDb(Base):
    """Durable idempotency + audit row for one materialized occurrence."""

    __tablename__ = "program_slot_occurrences"
    __table_args__ = {"sqlite_autoincrement": False}  # noqa: RUF012 - SQLAlchemy declarative API

    occurrence_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    slot_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    occurrence_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schedule_item_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduled", server_default="scheduled"
    )
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
