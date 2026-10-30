# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""SQLAlchemy 2.0 + Pydantic v2 models for the schedule module's Asset.

Per Director Decision 2 — pure SQLAlchemy 2.0 ``DeclarativeBase`` for the
persistence model, separate Pydantic v2 model for validation/serialization.
Round-trip is performed by :meth:`Asset.from_metadata` and
:meth:`Asset.to_metadata` so the persistence layer stays a pure mapping
object and the conversion is co-located with persistence concerns.

The schedule-local :class:`AssetMetadata` is a peer of
:class:`civiccast.vod.models.AssetMetadata`; field names and shapes match
exactly so consumers see one vocabulary. Long-term convergence of the two
models is queued in ``next-cleanup.md`` (per Decision 2's "What this does
NOT bind" note).

The store's public surface (:meth:`PostgresAssetStore.get`) returns the
v0.2 :class:`civiccast.vod.models.AssetMetadata` type; the schedule-local
peer here exists primarily to pin the round-trip contract and to give the
SA model a typed adapter that does not import from ``civiccast.vod`` for
its core conversion logic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base
from civiccast.schedule.retention_terms import (
    RETENTION_TERM_VALUE_ABSOLUTE_MAX as _RETENTION_TERM_VALUE_ABSOLUTE_MAX,
)
from civiccast.schedule.retention_terms import validate_term as _validate_retention_term
from civiccast.vod.models import AssetMetadata as VodAssetMetadata
from civiccast.vod.models import _enforce_https_manifest, _normalize_meeting_body

# Asset state machine values — check constraint enforced at DB level
# (migration 0002 ships pending_ingest / ingesting / validated / rejected;
# migration 0006 widens to include recorded for Sprint 0.4 live-broadcast
# finalization). Transitions:
#   pending_ingest -> ingesting -> validated | rejected
#   recorded (terminal; created by the live-broadcast finalization path
#   at Sprint 0.4 Slice 1; bypasses pending_ingest/ingesting/validated
#   because the asset is born packaged from the live session).
ASSET_STATE_PENDING = "pending_ingest"
ASSET_STATE_INGESTING = "ingesting"
ASSET_STATE_VALIDATED = "validated"
ASSET_STATE_REJECTED = "rejected"
ASSET_STATE_RECORDED = "recorded"
_ASSET_STATES = (
    ASSET_STATE_PENDING,
    ASSET_STATE_INGESTING,
    ASSET_STATE_VALIDATED,
    ASSET_STATE_REJECTED,
    ASSET_STATE_RECORDED,
)

# File-integrity status (4.0 media-library-hardening; migration 0044).
# ``ok`` is the default for every asset until the integrity scan
# (civiccast.schedule.media_integrity_worker) runs; ``missing`` is set by
# that scan when the backing file at ``file_path`` no longer exists;
# ``relinked`` is set by the relink endpoint once an operator points the
# asset at a new, validated path. Purely observational — never blocks
# reads, playback, or scheduling; see the worker's docstring for why.
FILE_STATUS_OK = "ok"
FILE_STATUS_MISSING = "missing"
FILE_STATUS_RELINKED = "relinked"
_FILE_STATUSES = (FILE_STATUS_OK, FILE_STATUS_MISSING, FILE_STATUS_RELINKED)
FileStatusValue = Literal["ok", "missing", "relinked"]

# Retention policies (Sprint 0.3 placeholder per release plan §0.3
# "retention placeholder"; spec §15.7 + §16 will tighten in Sprint 0.7
# when the archive module enforces them).
RETENTION_DEFAULT = "default"  # follow the channel's default policy
RETENTION_PERMANENT = "permanent"  # keep forever (council meetings, public records)
RETENTION_MEETING = "meeting"  # statutory meeting-record retention (state-specific)
RETENTION_SHORT = "short"  # ephemeral — drop after 30 days
_RETENTION_POLICIES = (
    RETENTION_DEFAULT,
    RETENTION_PERMANENT,
    RETENTION_MEETING,
    RETENTION_SHORT,
)

AssetStateValue = Literal[
    "pending_ingest",
    "ingesting",
    "validated",
    "rejected",
    "recorded",
]
RetentionPolicyValue = Literal["default", "permanent", "meeting", "short"]

# WP-08 value/unit/forever retention-term authoring (finalization plan
# section 6). ``retention_term_unit``/``retention_term_value`` are the new
# authoring contract; ``retention_policy``/``retention_until`` above remain
# the columns the enforcement worker actually reads (unchanged -- WP-08
# extends authoring, not enforcement). See
# ``civiccast.schedule.retention_terms`` for the arithmetic and
# ``Asset.retention_anchor_at`` below for the immutable anchor contract.
RetentionTermUnitValue = Literal["days", "weeks", "months", "years", "forever"]
_RETENTION_TERM_UNITS = ("days", "weeks", "months", "years", "forever")
ScheduleModeValue = Literal["premiere", "embargo"]
ScheduleStateValue = Literal["scheduled", "cancelled", "published"]

# ---------------------------------------------------------------------------
# Schedule item — premiere / embargo (Sprint 0.3 task 4)
# ---------------------------------------------------------------------------
# Per spec §8.3 + §9.1 + §1070: every scheduled item carries a mode and a
# state. The DB-level conflict detection (btree_gist EXCLUDE constraint,
# migration 0003 + 0005) applies only to time-range modes — embargo entries
# target a single moment, not a duration, and are explicitly exempt. ``live``
# was retired in migration 0005 per audit-team v0.3.0 ENG-004; Sprint 0.5
# live-ingest will model live events separately (see release plan §0.4 +
# §0.5 and the `SCHEDULE_MODE_LIVE_RETIRED` constant below).

SCHEDULE_MODE_PREMIERE = "premiere"
SCHEDULE_MODE_EMBARGO = "embargo"
_SCHEDULE_MODES = (SCHEDULE_MODE_PREMIERE, SCHEDULE_MODE_EMBARGO)

# ``live`` was a member of _SCHEDULE_MODES through v0.3.0 but was never
# wired end-to-end (the schedule_items.asset_id column points at a
# recorded asset, not a live source descriptor; the operator drawer
# never offered ``live`` as a mode option). The audit-team v0.3.0 pass
# (ENG-004 / UX-006) flagged this as pre-binding a Sprint 0.5 architectural
# decision. Migration 0005 drops it from the schema. The Sprint 0.5 live-
# ingest rung will design from scratch — likely with a separate
# ``live_events`` table or a polymorphic source descriptor — once the
# data shape is decided in an ADR.
SCHEDULE_MODE_LIVE_RETIRED = "live"

SCHEDULE_STATE_SCHEDULED = "scheduled"
SCHEDULE_STATE_CANCELLED = "cancelled"
SCHEDULE_STATE_PUBLISHED = "published"
_SCHEDULE_STATES = (
    SCHEDULE_STATE_SCHEDULED,
    SCHEDULE_STATE_CANCELLED,
    SCHEDULE_STATE_PUBLISHED,
)

# Modes that occupy a time range (and therefore can collide with each other).
# Embargo is a single-moment publish, not a duration; it never collides.
_TIME_RANGE_MODES = (SCHEDULE_MODE_PREMIERE,)


class AssetMetadata(BaseModel):
    """Schedule-local Pydantic peer of :class:`civiccast.vod.models.AssetMetadata`.

    Field names and shapes are identical to the v0.2 ``vod.models.AssetMetadata``
    so the two stay in lockstep. The peer exists per Director Decision 2 —
    long-term the two will converge; tracked in ``next-cleanup.md``.
    """

    asset_id: str = Field(
        ...,
        description="Canonical asset identifier (URL-safe).",
        pattern=r"^[a-z0-9][a-z0-9-]{2,63}$",
    )
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    # Option b (#107 remainder): the meeting body this recording belongs to
    # (e.g. "City Council"). NULL = untagged. The portal derives its browse
    # facet from the values actually in use - no fixed vocabulary.
    meeting_body: str | None = Field(default=None, min_length=1, max_length=120)
    manifest_url: HttpUrl = Field(..., description="HLS multivariant playlist URL (HTTPS).")
    poster_url: HttpUrl | None = Field(default=None)
    duration_seconds: int | None = Field(default=None, ge=0)
    published_at: datetime | None = Field(default=None)

    @field_validator("manifest_url", "poster_url", mode="before")
    @classmethod
    def _https_only(cls, value: Any) -> Any:
        return _enforce_https_manifest(value)


class UploadedAssetResponse(BaseModel):
    """Response model for the ``POST /api/staff/assets/upload`` endpoint.

    Represents an asset that has been uploaded and ingested by ffprobe but
    not yet packaged into an HLS stream — ``manifest_url`` is absent at this
    stage (the packager fills it in at Sprint 0.4). All ffprobe-derived
    fields are nullable: ffprobe may not be able to extract every field from
    every valid file.
    """

    asset_id: str = Field(..., description="Canonical asset identifier (URL-safe).")
    title: str
    description: str | None = None
    state: AssetStateValue = Field(..., description="Asset state machine value.")
    file_path: str = Field(..., description="Server-side path where the file was stored.")
    file_size_bytes: int = Field(..., ge=0)
    duration_seconds: int | None = None
    codec_video: str | None = None
    codec_audio: str | None = None
    width_px: int | None = None
    height_px: int | None = None
    bitrate_bps: int | None = None
    format_name: str | None = None


def _strip_control_chars(value: str) -> str:
    """Remove control characters that break log pipelines / serializers.

    Audit-team v0.3.0 — QA-014: chapter ``name`` and ``sub`` (and
    ``description`` / ``notes`` elsewhere) accept arbitrary text. React
    text-content rendering escapes them safely on display, but null
    bytes (``\\x00``) embedded in operator-supplied strings can corrupt
    journald JSON and Postgres COPY pipelines (which Sprint 0.4 audit
    log shipping will use). Strip the C0 control range except newline
    + tab — both are legitimate operator content.
    """
    return "".join(
        ch for ch in value if ch in ("\n", "\t") or (ord(ch) >= 0x20 and ord(ch) != 0x7F)
    )


class Chapter(BaseModel):
    """A single chapter marker on an asset's timeline.

    Sprint 0.3 task 5: chapters are operator-supplied via the trim/chapter
    editor; they're persisted as JSON on the Asset row (``chapters_json``)
    and serve as both navigation targets in the player and as metadata for
    the published asset's WebVTT chapter track (Sprint 0.4 packager).

    Audit-team v0.3.0:
    - QA-012 — chapter ``t`` must be strictly less than the asset
      duration. The editor enforces this UI-side; the validator
      (cross-field with ``Asset.duration_seconds``) lives in the store
      layer because Pydantic doesn't know the asset.
    - QA-014 — ``name`` and ``sub`` strip C0 control chars at the
      Pydantic boundary so the stored value is log-pipeline-safe.
    """

    t: float = Field(
        ...,
        ge=0,
        description="Chapter start time in seconds from the asset's beginning.",
    )
    name: str = Field(..., min_length=1, max_length=200)
    sub: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def _name_strip_controls(cls, value: str) -> str:
        cleaned = _strip_control_chars(value).strip()
        if not cleaned:
            raise ValueError("Chapter name cannot be empty after sanitization.")
        return cleaned

    @field_validator("sub")
    @classmethod
    def _sub_strip_controls(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _strip_control_chars(value)


class StaffAssetRow(BaseModel):
    """Row shape for the operator's asset library (``GET /api/staff/assets``).

    Sprint 0.3 task 3: the operator console needs to see every asset regardless
    of state — including ``pending_ingest`` rows mid-ffprobe and ``validated``
    rows uploaded but not yet packaged. The public ``GET /api/public/assets``
    endpoint requires both a package (``manifest_url IS NOT NULL``) and an
    explicit publication decision (``published_at IS NOT NULL``); this model
    exists as the wider operator-side projection that adds
    ``manifest_url`` and ``published_at`` to the upload-response surface so
    the library can show packaging state alongside ingest state.

    Sprint 0.3 task 5: also surfaces trim/chapter/retention metadata so the
    asset detail screen can render the editor without a second round-trip.
    """

    asset_id: str
    title: str
    description: str | None = None
    # Option b (#107 remainder): meeting-body category tag.
    meeting_body: str | None = None
    state: AssetStateValue
    manifest_url: str | None = None
    published_at: datetime | None = None
    file_path: str | None = None
    file_size_bytes: int | None = None
    duration_seconds: int | None = None
    codec_video: str | None = None
    codec_audio: str | None = None
    width_px: int | None = None
    height_px: int | None = None
    bitrate_bps: int | None = None
    format_name: str | None = None
    # Sprint 0.3 task 5 additions
    trim_in_seconds: float | None = None
    trim_out_seconds: float | None = None
    chapters: list[Chapter] = Field(default_factory=list)
    retention_policy: RetentionPolicyValue = "default"
    retention_until: datetime | None = None
    # WP-08: new value/unit/forever authoring contract, read-only alongside
    # the legacy fields above. ``None``/``None`` means this asset's
    # retention term has never been authored under the new contract (a
    # "legacy" row per the WP-08 plan) -- the operator UI falls back to
    # displaying ``retention_policy``/``retention_until`` for those rows.
    retention_term_unit: RetentionTermUnitValue | None = None
    retention_term_value: int | None = None
    # Immutable once captured -- first publication only, never cleared by
    # unpublish or moved by republish or by a term edit. See
    # ``Asset.retention_anchor_at`` for the full contract.
    retention_anchor_at: datetime | None = None
    # QA-008 (audit-team v0.3.0) — version for optimistic concurrency.
    # Returned in the GET response; the operator UI echoes the same value
    # back as ``AssetMetadataUpdate.expected_version`` on PATCH so a stale
    # value can be rejected (409).
    version: int = 1
    # Sprint 0.4 Slice 1 Commit 7 — link a recorded asset back to the
    # live session it was finalized from. Set on finalization-derived
    # assets; ``None`` for uploaded assets.
    source_live_session_id: str | None = None
    # 4.0 media-library-hardening additions.
    content_hash: str | None = None
    thumbnail_path: str | None = None
    file_status: FileStatusValue = "ok"
    file_status_checked_at: datetime | None = None


class AssetMetadataUpdate(BaseModel):
    """Request payload for ``PATCH /api/staff/assets/{asset_id}``.

    All fields optional - operators can update one or many at a time.
    Validation:

    - ``title`` length must be 1-200 if provided.
    - ``description`` <= 2000 if provided.
    - ``trim_in_seconds`` >= 0 if provided.
    - ``trim_out_seconds > trim_in_seconds`` when both are provided
      (also enforced by DB CHECK; surfaces here as 422 instead of 500).
    - ``chapters`` list is fully replaced on each PATCH (no partial
      patching of individual chapters; the editor sends the whole list).
    - ``retention_policy`` must be one of the four enumerated values.

    PATCH semantics — clarified after audit-team v0.3.0 ENG-008:

    - **Missing key** → "leave this field unchanged." The Pydantic
      ``model_dump(exclude_unset=True)`` payload will not include the
      field; the store skips it.
    - **Explicit ``null``** → "clear the field." For nullable fields
      (description, trim_in/out, chapters, retention_until) the value
      becomes NULL in the database. The previous v0.3 docstring claimed
      "explicit null = not in this PATCH" which contradicted the actual
      ``model_dump(exclude_unset=True)`` behaviour; the docstring is now
      the contract and the store implementation has been aligned.
    - **For ``chapters``**: an explicit ``[]`` is the way to clear all
      chapters. ``null`` also clears (treated identically). A list of
      chapters fully replaces — partial chapter editing is not
      supported.

    Optimistic concurrency (QA-008): ``expected_version`` is required.
    The client sends back the version it last observed; the store
    rejects with 409 if the row has advanced since.
    """

    expected_version: int = Field(
        ...,
        ge=1,
        description=(
            "Version number the client last observed (echoed from the GET "
            "or prior PATCH response). The store rejects with 409 if the "
            "row has been updated by another writer in the meantime."
        ),
    )
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    meeting_body: str | None = Field(default=None, min_length=1, max_length=120)
    trim_in_seconds: float | None = Field(default=None, ge=0)
    trim_out_seconds: float | None = Field(default=None, ge=1)
    chapters: list[Chapter] | None = None
    retention_policy: RetentionPolicyValue | None = None
    retention_until: datetime | None = None
    # WP-08 value/unit/forever authoring. Submit these two together
    # (``retention_term_value`` omitted/``None`` for ``forever``) to author
    # or convert a term; they cannot be combined with ``retention_policy``/
    # ``retention_until`` in the same PATCH -- pick one contract per
    # request so precedence is never ambiguous. ``retention_term_unit``
    # cannot be sent as an explicit ``null``: there is no "clear back to
    # legacy" operation in this contract.
    retention_term_unit: RetentionTermUnitValue | None = None
    # Coordinator-directed fix (follow-up commit, MAJOR finding 1): the
    # outer ``le=`` bound is the largest of the per-unit ceilings in
    # ``civiccast.schedule.retention_terms`` (200 years' worth of days) --
    # it rejects an absurd/overflow-risk value at the request-body layer,
    # before the tighter per-unit bound in ``_retention_term_shape`` below
    # (via ``validate_term``) even runs. Both together mean no value can
    # ever reach ``compute_retention_until``'s ``timedelta`` arithmetic in
    # a range that could raise ``OverflowError``.
    retention_term_value: int | None = Field(
        default=None, ge=1, le=_RETENTION_TERM_VALUE_ABSOLUTE_MAX
    )

    @field_validator("description")
    @classmethod
    def _description_strip_controls(cls, value: str | None) -> str | None:
        # QA-014: same control-char stripping as Chapter.name / .sub.
        if value is None:
            return value
        return _strip_control_chars(value)

    @field_validator("meeting_body")
    @classmethod
    def _meeting_body_normalized(cls, value: str | None) -> str | None:
        # Audit QA-002/UX-002: the portal facet derives by raw equality, so
        # padding/control chars from API writers would fork facet options.
        return _normalize_meeting_body(value)

    @field_validator("retention_policy", mode="before")
    @classmethod
    def _retention_known(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value not in _RETENTION_POLICIES:
            raise ValueError(
                f"retention_policy must be one of {_RETENTION_POLICIES}, got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _trim_window_ordered(self) -> AssetMetadataUpdate:
        if (
            self.trim_in_seconds is not None
            and self.trim_out_seconds is not None
            and self.trim_in_seconds >= self.trim_out_seconds
        ):
            raise ValueError("trim_in_seconds must be strictly less than trim_out_seconds.")
        return self

    @model_validator(mode="after")
    def _retention_term_shape(self) -> AssetMetadataUpdate:
        # WP-08: the new value/unit/forever contract and the legacy
        # retention_policy/retention_until contract are mutually exclusive
        # within a single PATCH, so the store never has to decide which one
        # wins. ``model_fields_set`` (not the resolved value) is what tells
        # us a key was actually present in the request payload, per the
        # class docstring's "missing key vs explicit null" PATCH contract.
        term_unit_set = "retention_term_unit" in self.model_fields_set
        term_value_set = "retention_term_value" in self.model_fields_set
        legacy_set = (
            "retention_policy" in self.model_fields_set
            or "retention_until" in self.model_fields_set
        )
        if term_unit_set and legacy_set:
            raise ValueError(
                "retention_term_unit cannot be combined with retention_policy or "
                "retention_until in the same update; submit the value/unit/forever "
                "term on its own."
            )
        if term_unit_set and self.retention_term_unit is None:
            raise ValueError(
                "retention_term_unit cannot be cleared with an explicit null; "
                "submit a replacement term to convert this asset's retention rule."
            )
        if term_value_set and not term_unit_set:
            raise ValueError(
                "retention_term_value requires retention_term_unit in the same update."
            )
        if term_unit_set:
            assert self.retention_term_unit is not None
            _validate_retention_term(self.retention_term_unit, self.retention_term_value)
        return self


class Asset(Base):
    """SQLAlchemy 2.0 declarative model for the ``civiccast.assets`` table.

    Inherits the ``civiccast`` schema namespace from ``Base.metadata`` per
    ADR 0008. Sprint 0.3 (asset upload + ffprobe ingest) adds:

      - ffprobe-extracted metadata columns (all nullable — ffprobe may not
        resolve every field for every valid file).
      - ``state`` column with a DB-level check constraint (migration 0002)
        enforcing the asset state machine values.
      - ``manifest_url`` is now nullable — uploaded assets have no HLS
        manifest until the packager runs (Sprint 0.4). The ``list()`` and
        ``get()`` store methods filter to packaged (manifest_url IS NOT NULL)
        assets for the public API.
    """

    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending_ingest', 'ingesting', 'validated', 'rejected', 'recorded')",
            name="assets_state_check",
        ),
        CheckConstraint(
            "retention_policy IN ('default', 'permanent', 'meeting', 'short')",
            name="assets_retention_policy_check",
        ),
        CheckConstraint(
            # Trim window must be coherent: in < out when both set; in >= 0
            # when set. Either can be NULL independently (NULL = "no trim
            # configured for this end of the window").
            "trim_in_seconds IS NULL OR trim_in_seconds >= 0",
            name="assets_trim_in_nonneg",
        ),
        CheckConstraint(
            # ENG-010 (audit-team v0.3.0): Pydantic gates trim_out_seconds
            # at the API surface (ge=1), but the DB CHECK was missing.
            # A direct-SQL backfill or a future writer that bypasses
            # Pydantic could land negative trim_out values. Defense in
            # depth at the column.
            "trim_out_seconds IS NULL OR trim_out_seconds >= 1",
            name="assets_trim_out_positive",
        ),
        CheckConstraint(
            "trim_in_seconds IS NULL OR trim_out_seconds IS NULL OR "
            "trim_in_seconds < trim_out_seconds",
            name="assets_trim_window_ordered",
        ),
        CheckConstraint(
            "file_status IN ('ok', 'missing', 'relinked')",
            name="assets_file_status_check",
        ),
        CheckConstraint(
            "retention_term_unit IS NULL OR retention_term_unit IN "
            "('days', 'weeks', 'months', 'years', 'forever')",
            name="assets_retention_term_unit_check",
        ),
        CheckConstraint(
            # forever carries no value; every finite unit requires a
            # positive one. A NULL unit (legacy, never authored under the
            # new contract) leaves value unconstrained by this check (it is
            # always NULL in practice, but nothing here depends on that).
            "retention_term_unit IS NULL "
            "OR (retention_term_unit = 'forever' AND retention_term_value IS NULL) "
            "OR (retention_term_unit != 'forever' AND retention_term_value IS NOT NULL "
            "AND retention_term_value > 0)",
            name="assets_retention_term_value_check",
        ),
    )

    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Option b (#107 remainder, migration 0037): meeting-body category tag.
    meeting_body: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # nullable since Sprint 0.3 — uploaded assets have no manifest until packaged
    manifest_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    poster_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ffprobe ingest fields — all nullable; populated by the upload endpoint
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    codec_video: Mapped[str | None] = mapped_column(String(50), nullable=True)
    codec_audio: Mapped[str | None] = mapped_column(String(50), nullable=True)
    width_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bitrate_bps: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    format_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default=ASSET_STATE_PENDING)

    # 4.0 media-library-hardening (migration 0044). content_hash is
    # ``sha256:<hex>`` (civiccast.schedule.ingest.hash_file), populated at
    # upload ingest; nullable because assets created via the manifest-only
    # POST /api/staff/assets have no backing file to hash. thumbnail_path
    # mirrors file_path's "server path, not URL" convention. file_status /
    # file_status_checked_at are written by the integrity scan and the
    # relink endpoint, never by ingest (a freshly-uploaded file is
    # presumptively present).
    content_hash: Mapped[str | None] = mapped_column(String(71), nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=FILE_STATUS_OK,
        server_default=FILE_STATUS_OK,
    )
    file_status_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Sprint 0.3 task 5: trim/chapter/metadata/retention. All non-destructive
    # — the original file is never modified; trim and chapter edits are
    # metadata applied at packaging time (Sprint 0.4). The columns ship now
    # so the operator UI can persist them and the migration cleanly seats
    # the contract for the Sprint 0.4 packager to read.
    trim_in_seconds: Mapped[float | None] = mapped_column(
        Numeric(10, 3, asdecimal=False),
        nullable=True,
    )
    trim_out_seconds: Mapped[float | None] = mapped_column(
        Numeric(10, 3, asdecimal=False),
        nullable=True,
    )
    # Chapters as JSON: list of {"t": float, "name": str, "sub": str | None}.
    # Stored as Text (serialized JSON) so SQLite + Postgres both work without
    # requiring the Postgres JSONB type. Operator-supplied; the trim editor
    # produces this list.
    chapters_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Retention placeholder per release plan §0.3 — Sprint 0.7 archive
    # module enforces these. The CHECK constraint above pins the value set.
    retention_policy: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=RETENTION_DEFAULT,
        server_default=RETENTION_DEFAULT,
    )
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # WP-08 value/unit/forever authoring (finalization plan section 6).
    # ``retention_term_unit``/``retention_term_value`` are the new
    # authoring contract, additive to the legacy pair above -- the
    # enforcement worker (``civiccast.schedule.retention_worker``) is
    # UNCHANGED and still reads only ``retention_policy``/
    # ``retention_until``; every write to the new columns keeps those two
    # legacy columns in sync (``compute_retention_until`` -> retention_until,
    # unit == 'forever' -> retention_policy = 'permanent', else 'default')
    # so enforcement never has to know the new contract exists. NULL/NULL
    # means "never authored under the new contract" (a legacy row).
    retention_term_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    retention_term_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Immutable once captured: the instant this asset was FIRST published
    # (``civiccast.schedule.store.PostgresAssetStore.mark_published`` sets
    # it only when it is still NULL). ``published_at`` cannot serve this
    # role itself -- unpublish clears ``published_at`` to NULL and republish
    # overwrites it with a new instant, so a policy anchored to
    # ``published_at`` would silently re-anchor on every unpublish/republish
    # cycle. ``retention_anchor_at`` never moves once set, including across
    # unpublish/republish and across every later term edit -- every
    # ``compute_retention_until`` call recomputes the deadline from this
    # SAME fixed point. For an asset that converts to the new contract
    # before ever being published, the store falls back to "now" at
    # conversion time and writes an audit-log entry recording the fallback
    # (finalization plan section 6, item 6).
    retention_anchor_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # S7 media lifecycle (CLAUDE.md §4.6 archival non-negotiable): a legal
    # hold blocks retention expiry outright. civiccast.schedule.retention_worker
    # skips any asset with legal_hold=True when flagging expired assets for
    # records-clerk disposition review -- a held asset is never queued for
    # disposition while the hold is active, regardless of retention_until.
    legal_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    legal_hold_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # QA-008 (audit-team v0.3.0): optimistic concurrency on PATCH.
    # Without this column, two operators editing the same asset's
    # chapters in different tabs both write a full chapter list and
    # the second saver silently overwrites the first. The PATCH
    # endpoint requires the client to echo back the version it last
    # observed; a stale value surfaces as 409.
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    # Sprint 0.4 Slice 1 Commit 7: link a recorded asset back to the
    # live session it was finalized from. Nullable -- only finalization-
    # derived assets carry it; uploaded assets stay at NULL. A partial
    # unique index (declared below as a module-level ``Index``) enforces
    # "at most one asset per source live session" so the table cannot
    # accumulate two assets for the same live session even if an
    # application-layer caller bypasses the event-row uniqueness guard.
    source_live_session_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    @classmethod
    def from_metadata(cls, meta: VodAssetMetadata | AssetMetadata) -> Asset:
        """Build an :class:`Asset` row from a Pydantic ``AssetMetadata``.

        Accepts either the schedule-local peer or the v0.2 ``vod.models``
        type — both have identical field shapes per Decision 2.
        """
        return cls(
            asset_id=meta.asset_id,
            title=meta.title,
            description=meta.description,
            # Direct attribute on purpose (audit ENG-011): if the peer
            # models drift, this fails loudly instead of dropping the field.
            meeting_body=meta.meeting_body,
            manifest_url=str(meta.manifest_url),
            poster_url=str(meta.poster_url) if meta.poster_url is not None else None,
            duration_seconds=meta.duration_seconds,
            published_at=meta.published_at,
        )

    @classmethod
    def from_upload(
        cls,
        *,
        asset_id: str,
        title: str,
        description: str | None,
        file_path: str,
        file_size_bytes: int,
        state: str,
        duration_seconds: int | None,
        codec_video: str | None,
        codec_audio: str | None,
        width_px: int | None,
        height_px: int | None,
        bitrate_bps: int | None,
        format_name: str | None,
        content_hash: str | None = None,
        thumbnail_path: str | None = None,
    ) -> Asset:
        """Build an :class:`Asset` row from a file upload + ffprobe result.

        Used by :meth:`PostgresAssetStore.ingest_upload`. ``manifest_url``
        is left None — the packager fills it in at Sprint 0.4.
        ``content_hash``/``thumbnail_path`` default to None (4.0
        media-library-hardening): callers that haven't computed them yet
        (or ffmpeg/hashing failed) still get a valid row.
        """
        return cls(
            asset_id=asset_id,
            title=title,
            description=description,
            manifest_url=None,
            file_path=file_path,
            file_size_bytes=file_size_bytes,
            state=state,
            duration_seconds=duration_seconds,
            codec_video=codec_video,
            codec_audio=codec_audio,
            width_px=width_px,
            height_px=height_px,
            bitrate_bps=bitrate_bps,
            format_name=format_name,
            content_hash=content_hash,
            thumbnail_path=thumbnail_path,
        )

    def to_metadata(self) -> VodAssetMetadata:
        """Round-trip back to the v0.2 :class:`vod.models.AssetMetadata`.

        The store's public surface returns the v0.2 type so callers see no
        type churn from the persistence layer. Callers must only invoke this
        on assets where ``manifest_url`` is not None (i.e., packaged assets).
        """
        # SQLite's DateTime(timezone=True) silently drops the tzinfo on
        # round-trip (no native timezone-aware datetime support). The
        # contract here is "all timestamps are UTC" (per ``vod.models``);
        # re-attach UTC when the round-tripped value came back naive so
        # the round-trip is byte-equal on both Postgres and SQLite.
        published_at = self.published_at
        if published_at is not None and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        return VodAssetMetadata.model_validate(
            {
                "asset_id": self.asset_id,
                "title": self.title,
                "description": self.description,
                "meeting_body": self.meeting_body,
                "manifest_url": self.manifest_url,
                "poster_url": self.poster_url,
                "duration_seconds": self.duration_seconds,
                "published_at": published_at,
            }
        )

    def to_staff_row(self) -> StaffAssetRow:
        """Return a :class:`StaffAssetRow` for the operator asset library.

        Includes ``manifest_url`` and ``published_at`` so the library can
        distinguish uploaded-but-not-packaged from packaged assets visually.
        Sprint 0.3 task 5: also surfaces trim/chapter/retention metadata.
        """
        import json

        published_at = self.published_at
        if published_at is not None and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=UTC)
        retention_until = self.retention_until
        if retention_until is not None and retention_until.tzinfo is None:
            retention_until = retention_until.replace(tzinfo=UTC)
        retention_anchor_at = self.retention_anchor_at
        if retention_anchor_at is not None and retention_anchor_at.tzinfo is None:
            retention_anchor_at = retention_anchor_at.replace(tzinfo=UTC)
        file_status_checked_at = self.file_status_checked_at
        if file_status_checked_at is not None and file_status_checked_at.tzinfo is None:
            file_status_checked_at = file_status_checked_at.replace(tzinfo=UTC)

        chapters: list[Chapter] = []
        if self.chapters_json:
            try:
                raw = json.loads(self.chapters_json)
                if isinstance(raw, list):
                    chapters = [Chapter.model_validate(item) for item in raw]
            except (json.JSONDecodeError, ValueError):
                # Don't blow up the library view on malformed JSON; an
                # invariant violation in the persistence layer should
                # surface as a separate alert, not block listing.
                chapters = []

        return StaffAssetRow(
            asset_id=self.asset_id,
            title=self.title,
            description=self.description,
            meeting_body=self.meeting_body,
            state=cast(AssetStateValue, self.state),
            manifest_url=self.manifest_url,
            published_at=published_at,
            file_path=self.file_path,
            file_size_bytes=self.file_size_bytes,
            duration_seconds=self.duration_seconds,
            codec_video=self.codec_video,
            codec_audio=self.codec_audio,
            width_px=self.width_px,
            height_px=self.height_px,
            bitrate_bps=self.bitrate_bps,
            format_name=self.format_name,
            trim_in_seconds=self.trim_in_seconds,
            trim_out_seconds=self.trim_out_seconds,
            chapters=chapters,
            retention_policy=cast(RetentionPolicyValue, self.retention_policy),
            retention_until=retention_until,
            retention_term_unit=cast("RetentionTermUnitValue | None", self.retention_term_unit),
            retention_term_value=self.retention_term_value,
            retention_anchor_at=retention_anchor_at,
            version=self.version,
            source_live_session_id=self.source_live_session_id,
            content_hash=self.content_hash,
            thumbnail_path=self.thumbnail_path,
            file_status=cast(FileStatusValue, self.file_status),
            file_status_checked_at=file_status_checked_at,
        )

    def to_upload_response(self) -> UploadedAssetResponse:
        """Return an :class:`UploadedAssetResponse` for the upload endpoint.

        Works for assets where ``manifest_url`` is None (pre-packaged state).
        """
        return UploadedAssetResponse(
            asset_id=self.asset_id,
            title=self.title,
            description=self.description,
            state=cast(AssetStateValue, self.state),
            file_path=self.file_path or "",
            file_size_bytes=self.file_size_bytes or 0,
            duration_seconds=self.duration_seconds,
            codec_video=self.codec_video,
            codec_audio=self.codec_audio,
            width_px=self.width_px,
            height_px=self.height_px,
            bitrate_bps=self.bitrate_bps,
            format_name=self.format_name,
        )


# Sprint 0.4 Slice 1 Commit 7: partial unique index enforcing "at most
# one asset per source live session." Declared as a module-level
# :class:`Index` rather than inside ``Asset.__table_args__`` so the
# ``postgresql_where`` / ``sqlite_where`` dialect kwargs are available.
# Mirrors migration ``0008_live_session_events_and_asset_link``'s
# ``assets_source_live_session_unique`` partial index.
Index(
    "assets_source_live_session_unique",
    Asset.source_live_session_id,
    unique=True,
    postgresql_where=Asset.source_live_session_id.isnot(None),
    sqlite_where=Asset.source_live_session_id.isnot(None),
)


# ===========================================================================
# Schedule items — premiere / embargo (Sprint 0.3 task 4)
# ===========================================================================


class ScheduleItemCreate(BaseModel):
    """Request payload for ``POST /api/staff/schedule``.

    Validation:

    - ``mode`` ∈ {premiere, embargo}. ``live`` was retired in migration
      0005 (audit-team v0.3.0 ENG-004); the Sprint 0.5 live-ingest rung
      will design the live-event data shape from scratch.
    - For ``premiere``, ``duration_seconds`` must be a positive integer
      and ≤ 14 days (1_209_600 seconds — see CHECK
      ``schedule_items_duration_matches_mode``). The cap is QA-010
      defense against a typo landing a 23-month channel block.
    - For ``embargo``, ``duration_seconds`` must be ``None`` — the
      embargo entry targets a single publish moment, not a duration
      (per spec §1070). The DB-level EXCLUDE constraint is written to
      skip embargo rows; we surface the same contract at the Pydantic
      surface so an operator UI cannot send a malformed embargo.
    - ``scheduled_at`` must be timezone-aware (UTC normalized at the
      store layer).
    - ``notes`` accepts arbitrary text up to 2000 chars; control
      characters other than newline + tab are stripped to keep the
      audit-log pipeline (Sprint 0.4) immune to embedded null bytes
      (QA-014).
    """

    asset_id: str = Field(
        ...,
        description="Asset to schedule. Must reference an existing asset row.",
        pattern=r"^[a-z0-9][a-z0-9-]{2,63}$",
    )
    channel_id: str = Field(
        ...,
        description=(
            "Operator-supplied channel identifier. The channels module is "
            "post-Sprint-0.3; v0.3 treats this as a free-form slug so the "
            "EXCLUDE constraint can partition by it."
        ),
        pattern=r"^[a-z0-9][a-z0-9-]{0,63}$",
    )
    mode: ScheduleModeValue = Field(..., description="One of: premiere | embargo")
    scheduled_at: datetime = Field(
        ...,
        description="When the event begins. Must be timezone-aware.",
    )
    duration_seconds: int | None = Field(
        default=None,
        ge=1,
        le=1_209_600,  # 14 days — QA-010
        description=(
            "Duration in whole seconds. Required for premiere; must be None "
            "for embargo. Capped at 14 days (1_209_600 seconds) to prevent "
            "a typo from blocking a channel for months."
        ),
    )
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("notes")
    @classmethod
    def _strip_notes_control_chars(cls, value: str | None) -> str | None:
        # QA-014/QA-015: strip control characters that break log
        # pipelines (null bytes corrupt journald JSON; embedded carriage
        # returns make grep results misleading) while preserving
        # newlines and tabs which operators legitimately use.
        if value is None:
            return value
        return "".join(
            ch for ch in value if ch in ("\n", "\t") or (ord(ch) >= 0x20 and ord(ch) != 0x7F)
        )

    @field_validator("mode", mode="before")
    @classmethod
    def _mode_must_be_known(cls, value: str) -> str:
        if value not in _SCHEDULE_MODES:
            raise ValueError(f"mode must be one of {_SCHEDULE_MODES}, got {value!r}")
        return value

    @field_validator("scheduled_at")
    @classmethod
    def _scheduled_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError(
                "scheduled_at must be timezone-aware (e.g., '2026-05-15T18:00:00-05:00')."
            )
        return value

    @model_validator(mode="after")
    def _duration_matches_mode(self) -> ScheduleItemCreate:
        if self.mode in _TIME_RANGE_MODES and self.duration_seconds is None:
            raise ValueError(f"duration_seconds is required for mode={self.mode!r}.")
        if self.mode == SCHEDULE_MODE_EMBARGO and self.duration_seconds is not None:
            raise ValueError(
                "duration_seconds must be None for embargo mode "
                "(an embargoed release targets a single publish moment, "
                "not a duration — see spec §1070)."
            )
        return self


class ScheduleItemResponse(BaseModel):
    """Response payload for the schedule endpoints.

    Audit-team v0.3.0 UX-003: ``asset_title`` is denormalized into the
    response so the operator-console schedule UI can render the human
    title (not the opaque asset_id) without a second round-trip per row.
    The store fills it via a JOIN at list/get time. ``None`` is allowed
    for legacy rows where the FK target may have been deleted; the UI
    falls back to the asset_id in that case.
    """

    id: uuid.UUID
    asset_id: str
    asset_title: str | None = None
    channel_id: str
    mode: ScheduleModeValue
    state: ScheduleStateValue
    scheduled_at: datetime
    duration_seconds: int | None
    notes: str | None
    created_at: datetime


class ScheduleItem(Base):
    """SQLAlchemy 2.0 declarative model for ``civiccast.schedule_items``.

    The btree_gist EXCLUDE constraint is added by Alembic migration 0003
    on Postgres only. SQLite test paths can exercise insert / list / cancel
    semantics; the conflict-detection contract is asserted exclusively
    against the real-Postgres testcontainers fixture (per ADR 0008's
    SQLite-vs-Postgres divergence note).

    Embargo entries are exempt from time-range conflict detection by
    spec §1070 — the EXCLUDE constraint's WHERE clause filters them out.
    """

    __tablename__ = "schedule_items"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('premiere', 'embargo')",
            name="schedule_items_mode_check",
        ),
        CheckConstraint(
            "state IN ('scheduled', 'cancelled', 'published')",
            name="schedule_items_state_check",
        ),
        CheckConstraint(
            # Embargo carries no duration; premiere must carry one.
            # Explicit IS NOT NULL is load-bearing: SQL three-valued logic
            # makes ``NULL > 0`` return NULL, which a bare
            # ``duration_seconds > 0`` check would treat as "constraint
            # not violated" (CHECK passes on NULL by spec). The
            # ``IS NOT NULL`` ensures duration is actually present
            # before comparing.
            #
            # Upper bound: 14 days (1_209_600 seconds). Audit-team
            # QA-010 — without an upper bound a typo (e.g. 999999 minutes)
            # could land a ~2-year schedule item that blocks the channel.
            # 14 days fits every realistic civic-broadcast scenario; the
            # cap can be raised in a future migration if a real workload
            # demands it.
            "(mode = 'embargo' AND duration_seconds IS NULL) OR "
            "(mode = 'premiere' "
            "AND duration_seconds IS NOT NULL "
            "AND duration_seconds > 0 "
            "AND duration_seconds <= 1209600)",
            name="schedule_items_duration_matches_mode",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True,
        default=uuid.uuid4,
    )
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=SCHEDULE_STATE_SCHEDULED,
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Denormalized end-of-window timestamp. Stored alongside
    # ``scheduled_at`` because Postgres' EXCLUDE constraint requires
    # IMMUTABLE expressions, and ``timestamptz + interval`` is STABLE
    # (its result depends on session TIME ZONE). Storing both endpoints
    # as plain columns side-steps the immutability requirement entirely.
    # The store keeps it in sync with ``scheduled_at + duration_seconds``
    # at write time; embargo entries leave it NULL.
    scheduled_at_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    def to_response(self, *, asset_title: str | None = None) -> ScheduleItemResponse:
        """Coerce to the response Pydantic model.

        SQLite drops tzinfo on round-trip; re-attach UTC when naive so
        ``scheduled_at`` and ``created_at`` always present as aware
        datetimes to API consumers (matching the contract on Postgres).

        ``asset_title`` is filled by the store via a JOIN at list/get
        time (audit-team v0.3.0 UX-003); when called without it the
        response carries ``None`` and the operator UI falls back to
        the asset_id.
        """
        scheduled_at = self.scheduled_at
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=UTC)
        created_at = self.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return ScheduleItemResponse(
            id=self.id,
            asset_id=self.asset_id,
            asset_title=asset_title,
            channel_id=self.channel_id,
            mode=cast(ScheduleModeValue, self.mode),
            state=cast(ScheduleStateValue, self.state),
            scheduled_at=scheduled_at,
            duration_seconds=self.duration_seconds,
            notes=self.notes,
            created_at=created_at,
        )


# S4 slice 1 (CivicCast 3.0): importing the commit-to-air row here registers
# ``civiccast.commit_to_air_reports`` with ``Base.metadata`` whenever the
# schedule models package is imported — the conftest + migration-reversibility
# fixtures rely on ``import civiccast.schedule.models`` as the single
# registration trigger for ``Base.metadata.create_all``. Re-exported so
# ``from civiccast.schedule.models import CommitToAirReportRow`` keeps working.
from civiccast.schedule.commit_models import (  # noqa: E402,F401
    CommitToAirReportRow,
)
