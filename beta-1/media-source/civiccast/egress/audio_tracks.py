# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Secondary audio program tracks — SAP / descriptive audio (S11 gap 9, parity).

The incumbent PEG workflow has Multiple Audio Programs (MAP, v7.8+): a show can carry a primary
program plus secondary tracks (a second-language SAP, or descriptive/audio-description).
``AudioProgramTrack`` models those tracks per asset or per channel; the GStreamer engine
muxes each as an additional MPEG-TS audio PID (the TV "SAP" button), and web/OTT exposes
them as selectable audio renditions. Per-track loudness reuses the S11b loudness path.

Lives in the egress module so it shares the egress Alembic chain (migration ``0052``);
no new version_locations entry needed.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    String,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from civiccast.db import Base

# Where the track is attached: a specific asset, or a whole channel (a standing track).
AudioTrackScope = Literal["asset", "channel"]
# Track role. ``primary`` = the main program audio (one per target); ``sap`` = a
# secondary audio program (commonly a second language); ``descriptive`` = audio
# description for accessibility (announced as such on web/OTT).
AudioTrackKind = Literal["primary", "sap", "descriptive"]


def _now() -> datetime:
    return datetime.now(UTC)


class AudioProgramTrack(BaseModel):
    """One audio program track (primary / SAP / descriptive) for an asset or channel."""

    model_config = ConfigDict(extra="forbid")

    track_id: Annotated[str, Field(min_length=1, max_length=120)]
    scope: AudioTrackScope
    target_id: Annotated[str, Field(min_length=1, max_length=120)]
    kind: AudioTrackKind
    # BCP-47 language tag (e.g. "en", "es", "en-US"). The MPEG-TS ISO-639 language
    # descriptor + the HLS rendition LANGUAGE attribute are derived from this.
    language: Annotated[str, Field(min_length=2, max_length=35)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    # The alt-audio source (a file/stream URI). None for the ``primary`` track, which is
    # the program's own audio.
    source_uri: Annotated[str | None, Field(default=None, max_length=1000)] = None
    # Per-track loudness target (LUFS/LKFS). None = inherit the channel/sink target.
    loudness_target_lufs: Annotated[float | None, Field(default=None)] = None
    enabled: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class AudioProgramTrackDb(Base):
    """Durable audio-program-track row."""

    __tablename__ = "audio_program_tracks"
    __table_args__ = (
        CheckConstraint("scope IN ('asset', 'channel')", name="audio_program_tracks_scope_check"),
        CheckConstraint(
            "kind IN ('primary', 'sap', 'descriptive')", name="audio_program_tracks_kind_check"
        ),
        Index("ix_audio_program_tracks_target", "scope", "target_id"),
    )

    track_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    language: Mapped[str] = mapped_column(String(35), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    loudness_target_lufs: Mapped[float | None] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


SessionFactory = Callable[[], AbstractContextManager[Session]]


class AudioTrackStoreError(RuntimeError):
    """Base error for audio-track persistence failures."""


class AudioTrackNotFoundError(AudioTrackStoreError):
    """Raised when a track id does not resolve."""


class AudioTrackStore:
    """CRUD for audio program tracks."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def _session(self) -> AbstractContextManager[Session]:
        return self._session_factory()

    def upsert_track(self, track: AudioProgramTrack) -> AudioProgramTrack:
        with self._session() as session:
            row = session.get(AudioProgramTrackDb, track.track_id)
            if row is None:
                row = AudioProgramTrackDb(track_id=track.track_id, created_at=track.created_at)
                session.add(row)
            row.scope = track.scope
            row.target_id = track.target_id
            row.kind = track.kind
            row.language = track.language
            row.label = track.label
            row.source_uri = track.source_uri
            row.loudness_target_lufs = track.loudness_target_lufs
            row.enabled = track.enabled
            row.updated_at = _now()
            session.commit()
            return _track_to_model(row)

    def get_track(self, track_id: str) -> AudioProgramTrack | None:
        with self._session() as session:
            row = session.get(AudioProgramTrackDb, track_id)
            return _track_to_model(row) if row is not None else None

    def list_tracks(
        self,
        *,
        scope: AudioTrackScope | None = None,
        target_id: str | None = None,
        enabled_only: bool = False,
    ) -> list[AudioProgramTrack]:
        with self._session() as session:
            stmt = select(AudioProgramTrackDb)
            if scope is not None:
                stmt = stmt.where(AudioProgramTrackDb.scope == scope)
            if target_id is not None:
                stmt = stmt.where(AudioProgramTrackDb.target_id == target_id)
            if enabled_only:
                stmt = stmt.where(AudioProgramTrackDb.enabled.is_(True))
            stmt = stmt.order_by(AudioProgramTrackDb.kind, AudioProgramTrackDb.language)
            return [_track_to_model(r) for r in session.execute(stmt).scalars().all()]

    def delete_track(self, track_id: str) -> None:
        with self._session() as session:
            row = session.get(AudioProgramTrackDb, track_id)
            if row is None:
                raise AudioTrackNotFoundError(f"Audio program track {track_id!r} not found")
            session.delete(row)
            session.commit()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _track_to_model(row: AudioProgramTrackDb) -> AudioProgramTrack:
    return AudioProgramTrack(
        track_id=row.track_id,
        scope=row.scope,  # type: ignore[arg-type]
        target_id=row.target_id,
        kind=row.kind,  # type: ignore[arg-type]
        language=row.language,
        label=row.label,
        source_uri=row.source_uri,
        loudness_target_lufs=row.loudness_target_lufs,
        enabled=row.enabled,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )
