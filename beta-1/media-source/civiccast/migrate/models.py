# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Normalized import model + the batch/provenance ledger.

Two families of type live here:

1. **Normalized inventory** (:class:`ImportedShow`, :class:`ImportedScheduleItem`,
   :class:`ImportedPlaylist`, :class:`NormalizedInventory`) — the
   source-agnostic shape every :class:`~civiccast.migrate.adapters.SourceAdapter`
   maps its incumbent system's export into. Nothing downstream of this line
   (the diff planner, the writer, the router) knows Cablecast (or TelVue, or
   Castus) exists.

2. **Plan + ledger** (:class:`ImportPlan` and friends, :class:`ImportBatch`,
   :class:`ImportBatchItemDb`) — the dry-run/apply/rollback contract.
   ``ImportBatchItemDb`` is the provenance ledger: one row per real
   ``civiccast.assets`` / ``civiccast.schedule_items`` row an apply call
   created, carrying ``(import_batch_id, source_system, source_ref)``. It is
   the ONLY new durable state this module owns — the imported shows and
   schedule items themselves land in the schedule module's real tables
   (see :mod:`civiccast.migrate.service`), never a parallel database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

# Machine token shared by every id in this module (batch ids, entity ids
# referenced from the ledger). Matches the Slug convention used by
# agenda / metadata / reporting / underwriting.
Slug = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")]

# Stable cross-source id convention (frozen 0.4.0 coordinator decision --
# do not change without a migration plan for already-applied batches):
#
#   asset_id   = "<source_system>-show-<safe_ref>"
#   channel_id = "<source_system>-ch-<safe_ref>"
#
# ``<safe_ref>`` is the source's own ``source_ref``/``channel_ref`` lowercased
# and stripped to ``[a-z0-9-]``. Every adapter (Cablecast, TelVue, Castus,
# Leightronix) shares this ONE convention via
# :func:`civiccast.migrate.service._asset_id` /
# :func:`civiccast.migrate.service._channel_id` -- an adapter never computes
# its own id shape, it only supplies ``source_ref``/``channel_ref`` strings.


def _now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    """A fresh lowercase-hex id — used for batch ids and plan ids alike."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Normalized inventory — source-agnostic
# ---------------------------------------------------------------------------


class ImportedShow(BaseModel):
    """One program/show as reported by an incumbent system.

    ``source_ref`` is the incumbent system's own identifier for this show
    (e.g. Cablecast's numeric show id, as a string) — it is NOT a CivicCast
    asset id. The diff planner derives the CivicCast ``asset_id`` from
    ``(source_system, source_ref)`` deterministically so re-running a dry-run
    against the same source always proposes the same target id.
    """

    model_config = ConfigDict(extra="forbid")

    source_ref: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    producer: str | None = Field(default=None, max_length=200)
    category: str | None = Field(default=None, max_length=120)
    duration_seconds: int | None = Field(default=None, ge=0)
    # Air-history metadata: the incumbent system's own "when this last/first
    # aired" timestamp, carried through as-is (not authoritative schedule
    # data — that's ImportedScheduleItem's job).
    air_date: datetime | None = None
    # Pointer into the source system's media storage (a reel/media
    # reference URL, or an exact served file path when the source exposes
    # one). NEVER a copied file — see the module docstring.
    media_ref: str | None = Field(default=None, max_length=2000)
    # Fields a source's real-world export carries that this normalized shape
    # has no sourced/confirmed meaning for (e.g. an undocumented or
    # operator-templated column). Carried through as raw strings, NEVER
    # given guessed semantics -- see each file-based adapter's module
    # docstring (TelVue / Castus / Leightronix) for what lands here and why.
    raw_extra: dict[str, Any] | None = Field(default=None)


class ImportedScheduleItem(BaseModel):
    """One scheduled/aired run of a show, as reported by an incumbent system."""

    model_config = ConfigDict(extra="forbid")

    source_ref: str = Field(min_length=1, max_length=120)
    show_source_ref: str = Field(min_length=1, max_length=120)
    channel_ref: str | None = Field(default=None, max_length=120)
    scheduled_at: datetime
    duration_seconds: int | None = Field(default=None, ge=0)
    # See ImportedShow.raw_extra.
    raw_extra: dict[str, Any] | None = Field(default=None)


class ImportedPlaylist(BaseModel):
    """A named grouping of shows (Cablecast calls this a "project"/series)."""

    model_config = ConfigDict(extra="forbid")

    source_ref: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    item_source_refs: list[str] = Field(default_factory=list)


class NormalizedInventory(BaseModel):
    """The full export of one incumbent system, in CivicCast's own shape."""

    model_config = ConfigDict(extra="forbid")

    source_system: str = Field(min_length=1, max_length=60)
    shows: list[ImportedShow] = Field(default_factory=list)
    schedule_items: list[ImportedScheduleItem] = Field(default_factory=list)
    playlists: list[ImportedPlaylist] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Dry-run diff plan
# ---------------------------------------------------------------------------


class PlanShow(BaseModel):
    """A show the plan proposes to create as a real CivicCast asset."""

    model_config = ConfigDict(extra="forbid")

    source_ref: str
    asset_id: str
    title: str
    description: str | None = None
    category: str | None = None
    duration_seconds: int | None = None
    media_ref: str | None = None


class PlanScheduleItem(BaseModel):
    """A schedule item the plan proposes to create as a real schedule row."""

    model_config = ConfigDict(extra="forbid")

    source_ref: str
    show_source_ref: str
    asset_id: str
    channel_id: str
    scheduled_at: datetime
    duration_seconds: int


class PlanConflict(BaseModel):
    """Something in the inventory collides with data already in CivicCast."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["show", "schedule_item"]
    source_ref: str
    reason: str


class PlanSkip(BaseModel):
    """Something in the inventory cannot be imported as-is."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["show", "schedule_item", "playlist"]
    source_ref: str
    reason: str


class ImportPlan(BaseModel):
    """The typed diff a dry-run produces.

    Apply is a SEPARATE explicit call that takes this exact plan back
    (``POST /api/staff/migrate/apply``) — nothing is written by dry-run
    itself, and there is no server-side plan cache to expire or leak: the
    plan is self-contained and the caller round-trips it. Re-running
    ``apply`` with the same plan twice is not idempotent by itself (calling
    it twice creates two batches); ``rollback`` is how a mistaken apply is
    undone.
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(default_factory=new_id)
    source_system: str
    generated_at: datetime = Field(default_factory=_now)
    shows_to_create: list[PlanShow] = Field(default_factory=list)
    schedule_items_to_create: list[PlanScheduleItem] = Field(default_factory=list)
    conflicts: list[PlanConflict] = Field(default_factory=list)
    skipped: list[PlanSkip] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Apply result
# ---------------------------------------------------------------------------


class ApplyOutcome(BaseModel):
    """One row that failed to apply even though the plan proposed it.

    Rare in practice (dry-run already checked for conflicts) but real: a
    second import batch can race the first between dry-run and apply.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["show", "schedule_item"]
    source_ref: str
    reason: str


class ImportBatch(BaseModel):
    """One apply (or rollback) of an import, as returned by the API."""

    model_config = ConfigDict(extra="forbid")

    import_batch_id: Slug
    source_system: str
    status: Literal["applied", "rolled_back"]
    shows_created: int = 0
    schedule_items_created: int = 0
    apply_failures: list[ApplyOutcome] = Field(default_factory=list)
    created_at: datetime
    rolled_back_at: datetime | None = None


class ImportBatchItem(BaseModel):
    """One provenance ledger row (what an apply call actually created)."""

    model_config = ConfigDict(extra="forbid")

    import_batch_id: Slug
    entity_type: Literal["asset", "schedule_item"]
    entity_id: str
    source_ref: str
    created_at: datetime


# ---------------------------------------------------------------------------
# SQLAlchemy ORM — the provenance ledger (migration 0068_migrate_batches)
# ---------------------------------------------------------------------------


class ImportBatchDb(Base):
    """One row per apply call. ``status`` flips to ``rolled_back`` in place —
    the ledger row is never deleted so ``GET /batches`` keeps a full history
    of "this batch happened, then was undone" (audit trail, not just a
    scratch table)."""

    __tablename__ = "import_batches"

    import_batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_system: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="applied")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ImportBatchItemDb(Base):
    """One row per real asset/schedule_item row an apply call created.

    This is the provenance ledger rollback reads to know EXACTLY which
    rows to delete — never a broader "everything matching this batch's
    shape" scan, which could catch pre-existing data that merely looks
    similar.
    """

    __tablename__ = "import_batch_items"
    __table_args__ = (Index("ix_import_batch_items_batch", "import_batch_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=new_id)
    import_batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


# ---------------------------------------------------------------------------
# API request bodies
# ---------------------------------------------------------------------------


class ConnectionInfo(BaseModel):
    """Connection details for a source-system dry-run.

    Two shapes, by ``source_system``:

    * ``cablecast`` is a live network source: ``base_url`` is the station's
      ``.../cablecastapi/v1`` endpoint. ``username``/``password`` are
      optional — Cablecast public sites commonly answer read-only requests
      without auth (that's how they render their own public schedule
      pages); private servers pass basic auth credentials here.
    * ``telvue`` / ``castus`` / ``leightronix`` are FILE-based sources —
      these vendors' stations export a schedule file to a local operator,
      not a network API. ``schedule_file`` carries that export's raw text
      content (staff pastes/uploads it; nothing is fetched over the
      network). See each adapter's module docstring in
      :mod:`civiccast.migrate.adapters` for the sourced format it parses.
    """

    model_config = ConfigDict(extra="forbid")

    source_system: Literal["cablecast", "telvue", "castus", "leightronix"] = "cablecast"
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    username: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, max_length=200, repr=False)
    schedule_file: str | None = Field(default=None, max_length=2_000_000)

    @model_validator(mode="after")
    def _required_field_matches_source_system(self) -> ConnectionInfo:
        if self.source_system == "cablecast":
            if not self.base_url:
                raise ValueError("cablecast requires base_url")
        elif not self.schedule_file:
            raise ValueError(f"{self.source_system} requires schedule_file")
        return self


class DryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection: ConnectionInfo


class ApplyRequest(BaseModel):
    """Apply takes the dry-run's plan back verbatim (see :class:`ImportPlan`)."""

    model_config = ConfigDict(extra="forbid")

    plan: ImportPlan


class MigrationRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    import_batch_id: Slug


__all__ = [
    "ApplyOutcome",
    "ApplyRequest",
    "ConnectionInfo",
    "DryRunRequest",
    "ImportBatch",
    "ImportBatchDb",
    "ImportBatchItem",
    "ImportBatchItemDb",
    "ImportPlan",
    "ImportedPlaylist",
    "ImportedScheduleItem",
    "ImportedShow",
    "MigrationRollbackRequest",
    "NormalizedInventory",
    "PlanConflict",
    "PlanScheduleItem",
    "PlanShow",
    "PlanSkip",
    "Slug",
    "new_id",
]
