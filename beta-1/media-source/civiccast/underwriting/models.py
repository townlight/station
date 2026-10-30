# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S24 underwriting-spot pydantic + SQLAlchemy models.

Three durable entities (migration ``0057_underwriting_spots``):

* ``UnderwritingSpot`` — a sponsoring entity's :15/:30 acknowledgment video
  (linked to an existing ``Asset``, S7) plus the operator's editorial 47 CFR
  73.503 attestation (``fcc_compliant_ack``). The ack is an editorial gate the
  station can REQUIRE at the API surface: when the env
  ``CIVICCAST_REQUIRE_FCC_ACK=1`` is set, the router's create / patch routes
  refuse to persist a spot whose ``fcc_compliant_ack`` is False (422 with a
  47 CFR 73.503 explanation). See ``civiccast.underwriting.router`` for the
  enforcement site. Code does NOT police content for CTAs / price /
  qualitative claims — that's human review.
* ``SpotFlight`` — flight window (``start_date``/``end_date``, inclusive) +
  optional per-day cap + optional ``daypart_block_id`` (loose ref to an S19
  ``ScheduleBlock``) + the channels the flight is in scope for.
* ``SpotPlacement`` — the resolved insertion the trafficking compiler placed
  (slice 2): one row per materialized program-log break/interstitial slot,
  carrying the ``schedule_item_id`` so downstream as-run can attribute aired
  seconds back to the placement → flight → spot → underwriter (slice 3).

``UnderwriterAffidavit`` is NOT a table — it's a report view over S23's
``as_run_log`` joined through ``spot_placements`` to ``spot_flights`` to
``underwriting_spots`` (slice 3 ``service.py``).

Pydantic shapes pair with ``*Db`` SQLAlchemy twins via ``from civiccast.db
import Base``. ``station_id`` / ``channel_id`` / ``asset_id`` /
``schedule_item_id`` / ``daypart_block_id`` are loose string columns (no
SQLAlchemy ``relationship``), matching the eas / ai_models / metadata /
reporting convention.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

# A stable id / slug: lowercase machine token (matches the metadata / reporting
# Slug — bound to ``Slug`` semantics rather than re-imported so this module
# stays self-contained at the type layer).
Slug = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")]


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pydantic domain models
# ---------------------------------------------------------------------------


class UnderwritingSpot(BaseModel):
    """One underwriting acknowledgment (:15/:30 video + sponsor identity).

    The ``fcc_compliant_ack`` checkbox surfaces the 47 CFR 73.503 reminder
    text in the operator UI; when the station sets
    ``CIVICCAST_REQUIRE_FCC_ACK=1``, the router refuses to persist a spot
    whose ack is False (422). No code inspects the asset for CTAs / price /
    qualitative claims — content review is human.
    """

    model_config = ConfigDict(extra="forbid")

    spot_id: Slug
    station_id: Slug
    # The sponsoring entity (free-form business name — not a Slug because real
    # underwriters are "Carter & Sons, LLC" and "PNC Bank N.A.", not slugs).
    underwriter: Annotated[str, Field(min_length=1, max_length=200)]
    # The :15/:30 acknowledgment Asset (S7) — loose ref, matches the
    # ``as_run_log.asset_id`` convention.
    asset_id: Slug
    # Editorial attestation (DC-5). Defaults False so a station policy that
    # requires the ack does not accidentally accept an un-attested spot.
    fcc_compliant_ack: bool = False
    review_notes: Annotated[str | None, Field(default=None, max_length=2000)] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class UnderwritingSpotInput(BaseModel):
    """Create-a-spot request body (POST /api/staff/underwriting/spots)."""

    model_config = ConfigDict(extra="forbid")

    spot_id: Slug
    station_id: Slug
    underwriter: Annotated[str, Field(min_length=1, max_length=200)]
    asset_id: Slug
    fcc_compliant_ack: bool = False
    review_notes: Annotated[str | None, Field(default=None, max_length=2000)] = None


class UnderwritingSpotUpdate(BaseModel):
    """Patch-a-spot request body (absent keys leave the stored value unchanged).

    ``spot_id`` / ``station_id`` are set at creation and not editable here.
    """

    model_config = ConfigDict(extra="forbid")

    underwriter: Annotated[str | None, Field(default=None, min_length=1, max_length=200)] = None
    asset_id: Slug | None = None
    fcc_compliant_ack: bool | None = None
    review_notes: Annotated[str | None, Field(default=None, max_length=2000)] = None


class SpotFlight(BaseModel):
    """A spot's flight window + cadence + channel scope.

    ``start_date`` and ``end_date`` are inclusive day bounds; the trafficking
    compiler treats a flight as live when ``start_date <= today <= end_date``.
    ``frequency_cap_per_day`` (when set) caps placements per channel per day;
    ``daypart_block_id`` is a loose ref to an S19 ``ScheduleBlock`` (when set,
    the compiler narrows placements to that block's hour-of-day window).
    ``channels`` is at least one channel slug — a flight with no channel scope
    is not buildable.
    """

    model_config = ConfigDict(extra="forbid")

    flight_id: Slug
    spot_id: Slug
    start_date: date
    end_date: date
    frequency_cap_per_day: int | None = Field(default=None, ge=1, le=1440)
    daypart_block_id: Slug | None = None
    channels: list[Slug] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("channels")
    @classmethod
    def _channels_unique_nonempty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("channels must list at least one channel slug")
        if len(value) != len(set(value)):
            raise ValueError("channels must not contain duplicates")
        return sorted(value)

    @model_validator(mode="after")
    def _date_order(self) -> SpotFlight:
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class SpotFlightInput(BaseModel):
    """Create-a-flight request body."""

    model_config = ConfigDict(extra="forbid")

    flight_id: Slug
    spot_id: Slug
    start_date: date
    end_date: date
    frequency_cap_per_day: int | None = Field(default=None, ge=1, le=1440)
    daypart_block_id: Slug | None = None
    channels: list[Slug] = Field(default_factory=list)

    @field_validator("channels")
    @classmethod
    def _channels_unique_nonempty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("channels must list at least one channel slug")
        if len(value) != len(set(value)):
            raise ValueError("channels must not contain duplicates")
        return sorted(value)

    @model_validator(mode="after")
    def _date_order(self) -> SpotFlightInput:
        # Q-1: pin the same end>=start invariant at the Input layer so a bad
        # POST surfaces as FastAPI 422, not a downstream pydantic
        # ValidationError raised inside the endpoint (which becomes a 500).
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


class SpotFlightUpdate(BaseModel):
    """Patch-a-flight request body (absent keys unchanged).

    ``flight_id`` / ``spot_id`` are set at creation and not editable here.
    """

    model_config = ConfigDict(extra="forbid")

    start_date: date | None = None
    end_date: date | None = None
    frequency_cap_per_day: int | None = Field(default=None, ge=1, le=1440)
    daypart_block_id: Slug | None = None
    channels: list[Slug] | None = None

    @field_validator("channels")
    @classmethod
    def _channels_unique_nonempty(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("channels must list at least one channel slug")
        if len(value) != len(set(value)):
            raise ValueError("channels must not contain duplicates")
        return sorted(value)


class SpotPlacement(BaseModel):
    """One materialized break/interstitial insertion the trafficking compiler made.

    The placement carries the ``schedule_item_id`` of the materialized
    program-log row so the as-run log → affidavit join in slice 3 can
    attribute each aired second back through ``spot_placements`` →
    ``spot_flights`` → ``underwriting_spots`` → ``underwriter``.
    """

    model_config = ConfigDict(extra="forbid")

    placement_id: Slug
    flight_id: Slug
    channel_id: Slug
    scheduled_at: datetime
    schedule_item_id: Slug
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# SQLAlchemy ORM twins (schema applied by migration 0057, not here)
# ---------------------------------------------------------------------------


class UnderwritingSpotDb(Base):
    """Durable underwriting-spot row. ``(station_id, underwriter)`` is indexed
    for the affidavit join (per-underwriter aggregations are the report's hot path)."""

    __tablename__ = "underwriting_spots"
    __table_args__ = (
        Index("ix_underwriting_spots_station", "station_id"),
        Index("ix_underwriting_spots_station_underwriter", "station_id", "underwriter"),
        Index("ix_underwriting_spots_asset", "asset_id"),
    )

    spot_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(120), nullable=False)
    underwriter: Mapped[str] = mapped_column(String(200), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(120), nullable=False)
    fcc_compliant_ack: Mapped[bool] = mapped_column(default=False, nullable=False)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class SpotFlightDb(Base):
    """Durable flight row. ``channels`` lives in a denormalized newline-separated
    text column (small list, bounded by the channel-count of a station — never
    enough rows to justify a join table). The store splits on read."""

    __tablename__ = "spot_flights"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="spot_flights_date_order_check"),
        CheckConstraint(
            "frequency_cap_per_day IS NULL OR (frequency_cap_per_day >= 1 "
            "AND frequency_cap_per_day <= 1440)",
            name="spot_flights_freq_cap_range_check",
        ),
        Index("ix_spot_flights_spot", "spot_id"),
        Index("ix_spot_flights_window", "start_date", "end_date"),
    )

    flight_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    spot_id: Mapped[str] = mapped_column(String(120), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    frequency_cap_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daypart_block_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Newline-joined channel slugs (sorted-unique at the model boundary).
    channels: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class SpotPlacementDb(Base):
    """Durable placement row. ``(channel_id, scheduled_at)`` is the hot index
    for the "upcoming + aired insertions per channel" view (DC-1); ``flight_id``
    indexes the per-flight rollup; ``schedule_item_id`` is the foreign-by-
    convention link the affidavit join uses to walk to as-run rows."""

    __tablename__ = "spot_placements"
    __table_args__ = (
        Index("ix_spot_placements_channel_scheduled", "channel_id", "scheduled_at"),
        Index("ix_spot_placements_flight", "flight_id"),
        Index("ix_spot_placements_schedule_item", "schedule_item_id"),
    )

    placement_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    flight_id: Mapped[str] = mapped_column(String(120), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(80), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schedule_item_id: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


__all__ = [
    "Slug",
    "SpotFlight",
    "SpotFlightDb",
    "SpotFlightInput",
    "SpotFlightUpdate",
    "SpotPlacement",
    "SpotPlacementDb",
    "UnderwritingSpot",
    "UnderwritingSpotDb",
    "UnderwritingSpotInput",
    "UnderwritingSpotUpdate",
]
