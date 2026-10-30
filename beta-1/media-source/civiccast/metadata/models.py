# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""User-defined custom metadata field models (S22).

Two durable entities on the single global Alembic chain (migration ``0054``):

* ``CustomFieldDef`` — an operator-defined field: a stable machine ``key`` (immutable
  once created so saved-searches/reports don't break), an editable ``label``, a typed
  kind (text/longtext/list/date/number/boolean/asset_ref/producer_ref), list ``options``,
  and ``required``/``searchable``/``api_exposed``/``order`` flags. Unique per
  ``(station_id, key)``.
* ``CustomFieldValue`` — one asset's value for one field. ``value`` is the canonical
  string (typed-validated against the def); ``value_num``/``value_date`` are denormalized
  copies populated for number/date fields so S19 saved-searches and S23 reporting can run
  numeric/date range scans on indexed columns. One value row per ``(asset_id, field_id)``.

These are additive metadata — they never alter core asset behavior, and a missing value
row is the natural, always-valid zero-state (the S22 key claim). ``asset_id`` is a loose
string FK (no SQLAlchemy ``relationship`` back to ``Asset``), matching the codebase's
loose ``asset_id`` convention (e.g. ``ScheduleItem.asset_id``) and keeping the core asset
write path import-clean.

Pydantic domain models pair with ``*Db`` SQLAlchemy ORM twins (no ``schema=`` in
``__table_args__``; the migration applies the schema). The ``type`` literal is enforced in
the DB by a ``CheckConstraint`` created in the migration.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

# The public asset projection the public-search endpoint filters over and extends. Reusing
# the VOD model (rather than redefining it) keeps the public boundary anchored to the one
# response_model that already governs ``/api/public/assets`` — the exact mechanism S22 must
# inherit (a non-whitelisted field physically cannot serialize out). Re-exported here so the
# metadata package + its tests have a single import for the public asset shape.
from civiccast.vod.models import AssetMetadata as AssetMetadata

# A stable id/slug: lowercase machine token, immutable once a def is created.
Slug = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")]

# The typed kinds a custom field can take (spec §3). ``list`` constrains to ``options``;
# ``number``/``date`` denormalize into value_num/value_date for range search;
# ``asset_ref``/``producer_ref`` must resolve to an existing entity (service-enforced).
CustomFieldType = Literal[
    "text",
    "longtext",
    "list",
    "date",
    "number",
    "boolean",
    "asset_ref",
    "producer_ref",
]

# The set of valid types, reused by the store/service for DB-check parity.
CUSTOM_FIELD_TYPES: tuple[str, ...] = (
    "text",
    "longtext",
    "list",
    "date",
    "number",
    "boolean",
    "asset_ref",
    "producer_ref",
)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pydantic domain models
# ---------------------------------------------------------------------------


class CustomFieldDef(BaseModel):
    """An operator-defined, typed custom metadata field."""

    model_config = ConfigDict(extra="forbid")

    field_id: Slug
    station_id: Slug
    # Stable machine key (immutable once created; the store rejects a change). The label
    # is the operator-facing display string and IS editable.
    key: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=200)]
    type: CustomFieldType
    options: list[str] = Field(default_factory=list)
    required: bool = False
    # ``searchable`` exposes the field as a search facet + S19 saved-search filter.
    searchable: bool = True
    # ``api_exposed`` gates whether the field's value reaches the public API/portal.
    api_exposed: bool = True
    order: int = 0
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class CustomFieldValue(BaseModel):
    """One asset's value for one custom field (canonical string + denormalized copies)."""

    model_config = ConfigDict(extra="forbid")

    asset_id: Slug
    field_id: Slug
    # Canonical string form (typed-validated against the def at write time).
    value: Annotated[str, Field(max_length=1000)]
    # Denormalized for range queries: value_num for type=number, value_date for type=date.
    value_num: float | None = None
    value_date: date | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# API request / response shapes (router slice)
# ---------------------------------------------------------------------------


class CustomFieldDefInput(BaseModel):
    """Create-a-field request body (POST /api/staff/custom-fields).

    ``field_id`` + ``station_id`` + the immutable ``key`` are set at creation. The router
    maps this to a full :class:`CustomFieldDef`; the store enforces key-immutability on any
    later update.
    """

    model_config = ConfigDict(extra="forbid")

    field_id: Slug
    station_id: Slug
    key: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=200)]
    type: CustomFieldType
    options: list[str] = Field(default_factory=list)
    required: bool = False
    searchable: bool = True
    api_exposed: bool = True
    order: int = 0


class CustomFieldDefUpdate(BaseModel):
    """Patch-a-field request body (PATCH /api/staff/custom-fields/{field_id}).

    ``key`` is intentionally PRESENT but immutable: sending a different ``key`` is a 409 at
    the store, not a silently-ignored no-op (the immutability contract must be observable).
    Every other attribute is editable. Absent keys leave the stored value unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    key: Annotated[str | None, Field(default=None, min_length=1, max_length=120)] = None
    label: Annotated[str | None, Field(default=None, min_length=1, max_length=200)] = None
    type: CustomFieldType | None = None
    options: list[str] | None = None
    required: bool | None = None
    searchable: bool | None = None
    api_exposed: bool | None = None
    order: int | None = None


class CustomFieldValueInput(BaseModel):
    """One value in a PUT-asset-values request (field_id + canonical string)."""

    model_config = ConfigDict(extra="forbid")

    field_id: Slug
    value: Annotated[str, Field(max_length=1000)]


class AssetCustomFieldsUpdate(BaseModel):
    """Full-replace set of an asset's custom-field values (PUT)."""

    model_config = ConfigDict(extra="forbid")

    values: list[CustomFieldValueInput] = Field(default_factory=list)


class PublicCustomFieldValue(BaseModel):
    """One exposed custom-field value on a public asset (key + label + canonical value).

    Carries the field ``key`` (the stable machine key the public ``cf.<key>`` filter uses)
    and the operator ``label`` (for facet display), plus the canonical ``value``. Only
    fields that are BOTH ``searchable`` and ``api_exposed`` are ever materialized here — the
    public-exposure boundary is a server-side filter, never a client concern (DC-5).
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    value: str


class PublicTranscriptCue(BaseModel):
    cue_id: str
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(gt=0)
    text: str


class PublicTranscript(BaseModel):
    asset_id: str
    available: bool
    cues: list[PublicTranscriptCue] = Field(default_factory=list)


class PublicSearchAsset(AssetMetadata):
    """A public asset plus its exposed custom-field values.

    Extends the VOD public projection (so the inherited HTTPS-only / whitelist guarantees
    still hold) with a ``custom_fields`` list assembled from searchable+api_exposed defs
    only. Because this is the endpoint's ``response_model``, a non-exposed value can never
    serialize out even if it were accidentally attached.
    """

    custom_fields: list[PublicCustomFieldValue] = Field(default_factory=list)
    transcript_matches: list[PublicTranscriptCue] = Field(default_factory=list)
    transcript_available: bool | None = None


# ---------------------------------------------------------------------------
# SQLAlchemy ORM twins (schema applied by migration 0054, not here)
# ---------------------------------------------------------------------------


class CustomFieldDefDb(Base):
    """Durable custom-field definition row. Unique on ``(station_id, key)``."""

    __tablename__ = "custom_field_defs"
    __table_args__ = (
        UniqueConstraint("station_id", "key", name="custom_field_defs_station_key_unique"),
        CheckConstraint(
            "type IN ('text', 'longtext', 'list', 'date', 'number', 'boolean', "
            "'asset_ref', 'producer_ref')",
            name="custom_field_defs_type_check",
        ),
        Index("ix_custom_field_defs_station_order", "station_id", "order"),
    )

    field_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(120), nullable=False)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    options: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    searchable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    api_exposed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # ``order`` is a SQL reserved word: pass the explicit column name positionally.
    order: Mapped[int] = mapped_column("order", Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class CustomFieldValueDb(Base):
    """Durable per-asset field value row. One row per ``(asset_id, field_id)``.

    ``value`` is a bounded ``String(1000)`` (not ``Text``) so the composite
    ``(field_id, value)`` B-tree index stays within Postgres' ~2704-byte index-entry cap;
    ``value_num``/``value_date`` carry the denormalized numeric/date copies that the
    separate single-column indexes serve for range scans.
    """

    __tablename__ = "custom_field_values"
    __table_args__ = (
        UniqueConstraint("asset_id", "field_id", name="custom_field_values_asset_field_unique"),
        Index("ix_custom_field_values_field_value", "field_id", "value"),
        Index("ix_custom_field_values_field_num", "field_id", "value_num"),
        Index("ix_custom_field_values_field_date", "field_id", "value_date"),
        Index("ix_custom_field_values_asset", "asset_id"),
    )

    # Deterministic surrogate PK minted from (asset_id, field_id) so upsert-by-PK works.
    value_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(120), nullable=False)
    field_id: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[str] = mapped_column(String(1000), nullable=False)
    value_num: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
