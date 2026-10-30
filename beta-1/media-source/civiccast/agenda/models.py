# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S25 meeting-agenda pydantic + SQLAlchemy models.

Two durable entities (migration ``0058_meeting_agenda``):

* ``MeetingAgenda`` — one agenda per (station, meeting asset). The status is
  a strict ``draft``/``published`` literal (DB ``CHECK`` enforced). A draft
  agenda is operator-only; the public read endpoint never returns it (DC-6).
  ``source_doc_url`` is an optional URL the operator typed into the editor;
  the public portal renders it beside the player when present.
* ``AgendaItem`` — ordered items under an agenda. Sorting is by ``order``
  (ints, unique per agenda — store enforces). ``video_timecode_s`` is the
  seek point (None until the operator scrubs to it or imports it). The
  optional ``number`` / ``doc_anchor`` / ``notes`` are display metadata.

Pydantic shapes pair with ``*Db`` SQLAlchemy twins via ``from civiccast.db
import Base``. ``station_id`` / ``meeting_asset_id`` are loose string columns
(no SQLAlchemy ``relationship``), matching the eas / ai_models / metadata /
reporting / underwriting convention.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

# A stable id / slug: lowercase machine token (matches the underwriting / metadata
# / reporting Slug semantics).
Slug = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")]

AgendaStatus = Literal["draft", "published"]
AGENDA_STATUSES: tuple[str, ...] = ("draft", "published")

# Allowed URL schemes for ``source_doc_url``. The public portal renders this
# value as an ``<a href>`` beside the player, so anything outside ``http`` /
# ``https`` is a stored-XSS vector (``javascript:``, ``data:``, ``vbscript:``)
# or a local-file leak (``file:``). The allowlist is intentionally tight —
# operators link to a station-controlled object store, not arbitrary URI
# schemes.
_ALLOWED_SOURCE_DOC_URL_SCHEMES: tuple[str, ...] = ("http", "https")


def validate_source_doc_url(value: str | None) -> str | None:
    """Normalize and validate ``source_doc_url`` input.

    * Strips leading/trailing whitespace.
    * Coerces the empty string to ``None`` so the portal's "render link?"
      check stays a single ``is None`` test (E-4 / Q-3 defense in depth).
    * Rejects any URL whose scheme is not ``http`` or ``https`` — the
      explicit allowlist defends the public portal against ``javascript:``,
      ``data:``, ``file:``, ``vbscript:``, ``about:``, etc. (E-1).

    Returns the trimmed string (or ``None``); raises ``ValueError`` with a
    message that names the allowed schemes so the operator sees a
    diagnostic, not a generic 422.
    """
    if value is None:
        return None
    if not isinstance(value, str):  # pragma: no cover - pydantic guards this
        raise ValueError("source_doc_url must be a string or null.")
    trimmed = value.strip()
    if trimmed == "":
        return None
    # Find the scheme — anything up to (but not including) the first ``:``.
    # We do not use ``urlparse`` here because some malicious schemes
    # (e.g. ``javascript:`` with no ``//``) parse oddly across versions; a
    # straight prefix check is harder to bypass.
    scheme_sep = trimmed.find(":")
    if scheme_sep <= 0:
        raise ValueError(
            f"source_doc_url must be an absolute URL with scheme http or https (got {value!r})."
        )
    scheme = trimmed[:scheme_sep].lower()
    if scheme not in _ALLOWED_SOURCE_DOC_URL_SCHEMES:
        raise ValueError(
            "source_doc_url must use scheme http or https; "
            f"got scheme {scheme!r} (allowed: {', '.join(_ALLOWED_SOURCE_DOC_URL_SCHEMES)})."
        )
    return trimmed


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pydantic domain models
# ---------------------------------------------------------------------------


class MeetingAgenda(BaseModel):
    """One agenda for one meeting asset. Status gates the public endpoint."""

    model_config = ConfigDict(extra="forbid")

    agenda_id: Slug
    station_id: Slug
    meeting_asset_id: Slug
    source_recording_job_id: Slug | None = None
    # Optional source PDF / URL displayed beside the player when present.
    # The validator enforces an http/https scheme allowlist (E-1 / Q-3) and
    # coerces the empty string to ``None`` (E-4).
    source_doc_url: Annotated[str | None, Field(default=None, max_length=2000)] = None
    status: AgendaStatus = "draft"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("source_doc_url", mode="before")
    @classmethod
    def _check_source_doc_url(cls, v: object) -> str | None:
        return validate_source_doc_url(v)  # type: ignore[arg-type]


class MeetingAgendaInput(BaseModel):
    """Create-an-agenda request body (POST /api/staff/agendas)."""

    model_config = ConfigDict(extra="forbid")

    agenda_id: Slug
    station_id: Slug
    meeting_asset_id: Slug
    source_recording_job_id: Slug | None = None
    source_doc_url: Annotated[str | None, Field(default=None, max_length=2000)] = None
    # New agendas are always drafts — the publish gate lives on the dedicated
    # PATCH path so a single create cannot bypass review (DC-1 + DC-6).

    @field_validator("source_doc_url", mode="before")
    @classmethod
    def _check_source_doc_url(cls, v: object) -> str | None:
        return validate_source_doc_url(v)  # type: ignore[arg-type]


class MeetingAgendaUpdate(BaseModel):
    """Patch-an-agenda request body (absent keys unchanged).

    ``agenda_id`` / ``station_id`` / ``meeting_asset_id`` are set at creation
    and not editable here.
    """

    model_config = ConfigDict(extra="forbid")

    source_doc_url: Annotated[str | None, Field(default=None, max_length=2000)] = None
    status: AgendaStatus | None = None

    @field_validator("source_doc_url", mode="before")
    @classmethod
    def _check_source_doc_url(cls, v: object) -> str | None:
        return validate_source_doc_url(v)  # type: ignore[arg-type]


class AgendaItem(BaseModel):
    """One ordered item under an agenda.

    The ``video_timecode_s`` (seconds from the meeting video's beginning) is the
    seek point the player jumps to when the viewer clicks the item. None means
    the operator hasn't scrubbed to it yet — the item is still navigable in the
    sidebar but won't seek anywhere.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: Slug
    agenda_id: Slug
    order: int = Field(ge=0)
    number: Annotated[str | None, Field(default=None, max_length=40)] = None
    title: Annotated[str, Field(min_length=1, max_length=400)]
    video_timecode_s: int | None = Field(default=None, ge=0)
    doc_anchor: Annotated[str | None, Field(default=None, max_length=200)] = None
    notes: Annotated[str | None, Field(default=None, max_length=5000)] = None
    # Set only by heuristic/AI-assisted import paths (currently the PDF
    # import heuristic in civiccast.agenda.pdf_import); None for
    # operator-authored items and exact plain-text imports, where there is
    # nothing to be uncertain about. 0.0-1.0; the operator console can
    # surface this to flag low-confidence rows for review before publish
    # (AI/agenda non-negotiables Spec Sec4.2).
    confidence: Annotated[float | None, Field(default=None, ge=0.0, le=1.0)] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class AgendaChapterSyncSkip(BaseModel):
    """One source chapter left untouched because its agenda position is occupied."""

    order: int = Field(ge=0)
    chapter_title: Annotated[str, Field(min_length=1, max_length=400)]
    existing_item_title: Annotated[str, Field(min_length=1, max_length=400)]


class AgendaChapterSyncResult(BaseModel):
    """Detailed, idempotent chapter-sync outcome for the operator UI."""

    chapter_count: int = Field(ge=0)
    added_items: list[AgendaItem] = Field(default_factory=list)
    skipped_existing: list[AgendaChapterSyncSkip] = Field(default_factory=list)


class AgendaItemInput(BaseModel):
    """Create-an-item request body."""

    model_config = ConfigDict(extra="forbid")

    item_id: Slug
    agenda_id: Slug
    order: int = Field(ge=0)
    number: Annotated[str | None, Field(default=None, max_length=40)] = None
    title: Annotated[str, Field(min_length=1, max_length=400)]
    video_timecode_s: int | None = Field(default=None, ge=0)
    doc_anchor: Annotated[str | None, Field(default=None, max_length=200)] = None
    notes: Annotated[str | None, Field(default=None, max_length=5000)] = None
    confidence: Annotated[float | None, Field(default=None, ge=0.0, le=1.0)] = None


class AgendaItemUpdate(BaseModel):
    """Patch-an-item request body (absent keys unchanged).

    ``item_id`` / ``agenda_id`` are set at creation and not editable here.
    """

    model_config = ConfigDict(extra="forbid")

    order: int | None = Field(default=None, ge=0)
    number: Annotated[str | None, Field(default=None, max_length=40)] = None
    title: Annotated[str | None, Field(default=None, min_length=1, max_length=400)] = None
    video_timecode_s: int | None = Field(default=None, ge=0)
    doc_anchor: Annotated[str | None, Field(default=None, max_length=200)] = None
    notes: Annotated[str | None, Field(default=None, max_length=5000)] = None
    # Explicit null clears it -- the operator's normal "I reviewed this
    # heuristic guess and it's correct" action.
    confidence: Annotated[float | None, Field(default=None, ge=0.0, le=1.0)] = None


# ---------------------------------------------------------------------------
# Public projection (the public endpoint returns this shape, NOT the staff one)
# ---------------------------------------------------------------------------


class PublicAgendaItem(BaseModel):
    """Public projection of one agenda item.

    Drops engine-internal timestamps (``created_at`` / ``updated_at``) and
    operator-facing fields (``notes``). The public view exposes only what the
    portal player needs to render the item + seek to its timecode.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: Slug
    order: int
    number: str | None = None
    title: str
    video_timecode_s: float | None = None
    doc_anchor: str | None = None


class PublicMeetingAgenda(BaseModel):
    """Public projection — only ever returned for ``status="published"`` agendas."""

    model_config = ConfigDict(extra="forbid")

    agenda_id: Slug
    meeting_asset_id: Slug
    source_doc_url: str | None = None
    items: list[PublicAgendaItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SQLAlchemy ORM twins (schema applied by migration 0058, not here)
# ---------------------------------------------------------------------------


class MeetingAgendaDb(Base):
    """Durable meeting-agenda row.

    A station can have at most one agenda per meeting asset — the
    ``(station_id, meeting_asset_id)`` unique constraint enforces it so the
    public endpoint's single-row lookup by ``meeting_asset_id`` cannot return
    ambiguous results.
    """

    __tablename__ = "meeting_agendas"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published')",
            name="meeting_agendas_status_check",
        ),
        UniqueConstraint(
            "station_id", "meeting_asset_id", name="meeting_agendas_station_asset_unique"
        ),
        UniqueConstraint(
            "station_id", "source_recording_job_id", name="meeting_agendas_station_job_unique"
        ),
        Index("ix_meeting_agendas_station", "station_id"),
        Index("ix_meeting_agendas_asset", "meeting_asset_id"),
    )

    agenda_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    station_id: Mapped[str] = mapped_column(String(120), nullable=False)
    meeting_asset_id: Mapped[str] = mapped_column(String(120), nullable=False)
    source_recording_job_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_doc_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class AgendaItemDb(Base):
    """Durable agenda-item row.

    ``(agenda_id, order)`` is unique so the operator-defined ordering survives
    every read without needing a tiebreaker. ``(agenda_id, video_timecode_s)``
    is the read index the public projection scans (asc by timecode for the
    player's chapter list, asc by order for the agenda sidebar).
    """

    __tablename__ = "agenda_items"
    __table_args__ = (
        UniqueConstraint("agenda_id", "order", name="agenda_items_agenda_order_unique"),
        Index("ix_agenda_items_agenda_order", "agenda_id", "order"),
        Index("ix_agenda_items_agenda_timecode", "agenda_id", "video_timecode_s"),
    )

    item_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    agenda_id: Mapped[str] = mapped_column(String(120), nullable=False)
    order: Mapped[int] = mapped_column("order", Integer, nullable=False)
    number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    video_timecode_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    doc_anchor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


__all__ = [
    "AGENDA_STATUSES",
    "AgendaChapterSyncResult",
    "AgendaChapterSyncSkip",
    "AgendaItem",
    "AgendaItemDb",
    "AgendaItemInput",
    "AgendaItemUpdate",
    "AgendaStatus",
    "MeetingAgenda",
    "MeetingAgendaDb",
    "MeetingAgendaInput",
    "MeetingAgendaUpdate",
    "PublicAgendaItem",
    "PublicMeetingAgenda",
    "Slug",
    "validate_source_doc_url",
]
