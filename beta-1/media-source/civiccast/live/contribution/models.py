# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S17 Remote Contribution entities (CivicCast 3.0 — build step 9).

Pydantic domain contracts + their SQLAlchemy ORM peers for the remote-guest
contribution tier. Migration ``0048_remote_contribution`` creates the tables.

Entities (S17 §3):

* ``ContributionRoom`` — a named, channel-scoped WebRTC room (the VDO.Ninja
  "room" the operator publishes guests into). Maps 1:1 to a browser-source slot
  in the compositor.
* ``GuestInvite`` — a single-use, expiring invite to ONE remote participant.
  ``invite_token`` is the capability (≥32 chars, single-use); the public join
  endpoints are gated by it, never by an auth role. ``role`` is a *contribution*
  role (council_member / presenter / public_comment) — NOT one of the five auth
  roles. Reuses the ``contribute/`` terms-acceptance pattern: public comment from
  home must accept terms before joining.
* ``RemoteGuestSession`` — the live, per-guest connection record. A guest lands
  in a held "waiting room" on connect (``admitted_at`` is null); the operator
  must admit before the guest can be put on-air (Scott decision S17 §10.6 = yes).
  ``connection_quality`` is advisory only (from VDO.Ninja stats).

No hard foreign keys: ``room_id`` / ``invite_id`` are soft string references
resolved in the store (matching the cg / schedule / control_room modules). A
session/invite audit row must outlive the room it named; deleting a room must
not cascade into the sessions that recorded a guest's time on-air.

The composited guest output reuses the existing ``live/`` ``LiveSource``
``ndi`` / ``srt`` kinds (S17 §6, Scott decision §10.3) — **no schema co-edit** to
``live_sources_source_type_check``; S17 adds NO ``"webrtc"`` source kind in V1.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

# --- shared literals ---------------------------------------------------------

# A room's lifecycle: idle (created, not opened) → open (VDO director session up,
# accepting guests) → live (≥1 guest on-air) → closing → closed.
RoomState = Literal["idle", "open", "live", "closing", "closed"]

# Where the guest browser-source is composited (S17 §6 step 3). gst_compositor =
# the in-engine GStreamer ``wpesrc`` path (Scott decision S17 §10.1, V1 default);
# obs_browser_source = the documented premium OBS path.
CompositorTarget = Literal["obs_browser_source", "gst_compositor"]

# A *contribution* role (who the guest is on the show) — explicitly NOT one of
# the five auth roles. Drives display + the moderation posture, not access.
ContributionRole = Literal["council_member", "presenter", "public_comment"]

# Per-guest connection lifecycle. A guest is held at ``connected`` (waiting room)
# until the operator admits (``admitted_at`` set) and then puts them ``on_air``.
GuestSessionState = Literal[
    "invited", "joining", "connected", "on_air", "muted", "dropped", "ended"
]

# Advisory-only signal derived from VDO.Ninja stats — never gates anything.
ConnectionQuality = Literal["unknown", "good", "degraded", "poor"]

# The pinned VDO.Ninja director iframe does not acknowledge targeted media
# commands. Keep that boundary explicit: this records that CivicCast asked the
# browser to send a command, never that VDO or the compositor applied it.
MediaControlState = Literal["unknown", "sent_unverified"]
MediaControlAction = Literal[
    "audio_mute",
    "audio_unmute",
    "video_mute",
    "video_unmute",
    "disconnect",
]

# The single-use invite token must be unguessable. Mirrors the contribute/
# receipt-token discipline but with a higher floor (S17 §3: ≥32 chars).
INVITE_TOKEN_MIN_LENGTH = 32


# ---------------------------------------------------------------------------
# Pydantic domain models
# ---------------------------------------------------------------------------


class ContributionRoom(BaseModel):
    """A named, channel-scoped WebRTC contribution room."""

    model_config = ConfigDict(extra="forbid")

    room_id: Annotated[str, Field(min_length=1, max_length=120)]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    name: Annotated[str, Field(min_length=1, max_length=200)]
    # The opaque VDO.Ninja room token. Never reused across rooms (S17 §3).
    vdo_room_name: Annotated[str, Field(min_length=1, max_length=200)]
    max_guests: Annotated[int, Field(default=6, ge=1, le=50)] = 6
    state: RoomState = "idle"
    compositor_target: CompositorTarget = "gst_compositor"
    created_at: datetime
    updated_at: datetime


class GuestInvite(BaseModel):
    """A single-use, expiring invite to one remote participant. The token is the
    capability — the public join endpoints validate + consume it, no auth role."""

    model_config = ConfigDict(extra="forbid")

    invite_id: Annotated[str, Field(min_length=1, max_length=120)]
    room_id: Annotated[str, Field(min_length=1, max_length=120)]
    guest_display_name: Annotated[str, Field(min_length=1, max_length=200)]
    role: ContributionRole
    invite_token: Annotated[str, Field(min_length=INVITE_TOKEN_MIN_LENGTH, max_length=200)]
    # The VDO.Ninja director/guest URLs the IFRAME API mints. ``view_url`` is what
    # the guest opens in any browser (no install); ``push_url`` is the director
    # ingest URL the compositor consumes.
    push_url: Annotated[str | None, Field(default=None, max_length=1000)] = None
    view_url: Annotated[str | None, Field(default=None, max_length=1000)] = None
    # Terms acceptance (reuse the contribute/ terms pattern) — recorded before a
    # public-comment-from-home guest may join. Null until accepted.
    terms_agreement_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    terms_version: Annotated[str | None, Field(default=None, max_length=40)] = None
    expires_at: datetime
    consumed_at: datetime | None = None
    created_at: datetime


class RemoteGuestSession(BaseModel):
    """The live, per-guest connection record (one per guest actually connecting)."""

    model_config = ConfigDict(extra="forbid")

    session_id: Annotated[str, Field(min_length=1, max_length=120)]
    room_id: Annotated[str, Field(min_length=1, max_length=120)]
    invite_id: Annotated[str, Field(min_length=1, max_length=120)]
    guest_display_name: Annotated[str, Field(min_length=1, max_length=200)]
    state: GuestSessionState = "invited"
    connection_quality: ConnectionQuality = "unknown"
    # Waiting-room admission (Scott decision S17 §10.6). Null = held; the operator
    # must admit before on-air. Set once when the operator admits the guest.
    admitted_at: datetime | None = None
    joined_at: datetime | None = None
    on_air_at: datetime | None = None
    ended_at: datetime | None = None
    media_control_state: MediaControlState = "unknown"
    media_control_action: MediaControlAction | None = None
    media_control_requested_at: datetime | None = None
    proof_boundary: Annotated[str, Field(min_length=1, max_length=300)]


# ---------------------------------------------------------------------------
# SQLAlchemy ORM peers (single global metadata; migration 0048 creates tables)
# ---------------------------------------------------------------------------


class ContributionRoomDb(Base):
    """Durable contribution-room row."""

    __tablename__ = "contribution_rooms"

    room_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    channel_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    vdo_room_name: Mapped[str] = mapped_column(String(200), nullable=False)
    max_guests: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="idle")
    compositor_target: Mapped[str] = mapped_column(
        String(30), nullable=False, default="gst_compositor"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class GuestInviteDb(Base):
    """Durable single-use guest-invite row. ``invite_token`` is uniquely indexed
    so the public token lookup is O(1) and the single-use consume is a guarded
    ``UPDATE ... WHERE consumed_at IS NULL``."""

    __tablename__ = "guest_invites"

    invite_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    room_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    guest_display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    invite_token: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    push_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    view_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    terms_agreement_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    terms_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class RemoteGuestSessionDb(Base):
    """Durable per-guest session row (append-on-join, mutated through its
    state machine; outlives the room it named — soft ref, no cascade)."""

    __tablename__ = "remote_guest_sessions"

    session_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    room_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    invite_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    guest_display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="invited")
    connection_quality: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    admitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    on_air_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    media_control_state: Mapped[str] = mapped_column(
        String(30), nullable=False, default="unknown", server_default="unknown"
    )
    media_control_action: Mapped[str | None] = mapped_column(String(30), nullable=True)
    media_control_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    proof_boundary: Mapped[str] = mapped_column(Text, nullable=False)


__all__ = [
    "INVITE_TOKEN_MIN_LENGTH",
    "CompositorTarget",
    "ConnectionQuality",
    "ContributionRole",
    "ContributionRoom",
    "ContributionRoomDb",
    "GuestInvite",
    "GuestInviteDb",
    "GuestSessionState",
    "MediaControlAction",
    "MediaControlState",
    "RemoteGuestSession",
    "RemoteGuestSessionDb",
    "RoomState",
]
