# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S17 remote-contribution orchestration service (build step 9 slice 3b).

Owns the room / invite / guest-session lifecycle on top of the data layer
(slice 3a) and the VDO.Ninja URL bridge:

* **Rooms** — create (idle) -> open (mints the director URL) -> live (a guest
  lifecycle tag) -> closed. Closing a room does not imply provider disconnect.
* **Invites** — mint a single-use, expiring ``GuestInvite`` with guest publish +
  compositor view URLs; public token-gated resolution that consumes the token
  exactly once (race-safe) and creates a HELD guest session.
* **Waiting room** (Scott decision S17 §10.6) — every guest lands HELD
  (``admitted_at`` null) on join; the operator must ``admit`` before ``on_air``.
  Public-comment-from-home guests must accept terms before the token resolves.
* **Media controls** — targeted browser commands are recorded as unverified;
  the VDO.Ninja director iframe does not acknowledge command completion.

All cross-entity refs are soft strings; the legality of every guest-session
transition is enforced here (the store just writes the resolved row).
"""

from __future__ import annotations

import contextlib
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict

from civiccast.live.contribution.bridge import (
    NullVdoNinjaBridge,
    VdoDiagnostics,
    VdoNinjaBridge,
)
from civiccast.live.contribution.coprocess import ALERT_GUEST_DROP
from civiccast.live.contribution.models import (
    ContributionRole,
    ContributionRoom,
    GuestInvite,
    MediaControlAction,
    RemoteGuestSession,
)
from civiccast.live.contribution.store import (
    ContributionStore,
    ContributionStoreError,
    GuestSessionNotFoundError,
    InviteNotFoundError,
    RoomNotFoundError,
)

# The terms a public-comment-from-home guest accepts before joining. Bumping
# this string invalidates prior acceptances for future invites (re-accept).
CONTRIBUTION_TERMS_VERSION = "2026-06-remote-contribution-v1"
_DEFAULT_INVITE_TTL = timedelta(hours=4)
_JOIN_PROOF_BOUNDARY = (
    "Guest session created from a consumed single-use invite; CivicCast does "
    "not authoritatively observe VDO connection state or media flow."
)

# Rooms a guest may still join through.
_JOINABLE_ROOM_STATES = ("open", "live")
# Rooms an operator may still mint invites against.
_INVITABLE_ROOM_STATES = ("idle", "open", "live")
# Guest-session states past which no operator action is legal.
_TERMINAL_GUEST_STATES = ("dropped", "ended")


class ContributionServiceError(ContributionStoreError):
    """Base error for remote-contribution service operations."""


class RoomNotOpenError(ContributionServiceError):
    """Raised when a guest tries to join a room that is not open/live."""


class RoomClosedError(ContributionServiceError):
    """Raised when minting an invite against a closing/closed room."""


class ChannelNotConfiguredError(ContributionServiceError):
    """Raised when a contribution room references no configured egress channel."""


class ChannelDisabledError(ContributionServiceError):
    """Raised when a contribution room references a disabled egress channel."""


class InviteExpiredError(ContributionServiceError):
    """Raised when an invite token has passed its expiry."""


class InviteConsumedError(ContributionServiceError):
    """Raised when a single-use invite token has already been consumed."""


class GuestNotAdmittedError(ContributionServiceError):
    """Raised when putting a guest on-air before the operator admitted them."""


class InvalidGuestTransitionError(ContributionServiceError):
    """Raised when an operator action is illegal for the guest's current state."""


class RoomGuestLimitError(ContributionServiceError):
    """Raised when a join would exceed the room's ``max_guests``."""


class TakeoverHookError(ContributionServiceError):
    """Raised when the engine takeover hook fails during ``put_on_air``.

    The service has already reverted guest/room state to the pre-on-air
    snapshot; the operator receives a 503 and may retry or bring the channel
    live manually."""


class InviteJoinView(BaseModel):
    """Result of resolving a public invite token.

    Either the join is ready (``needs_terms`` false, ``view_url`` + ``session_id``
    populated, the token now consumed) or the guest must accept terms first
    (``needs_terms`` true, token NOT consumed, ``terms_version`` to present)."""

    model_config = ConfigDict(extra="forbid")

    needs_terms: bool
    room_name: str
    guest_display_name: str
    role: ContributionRole
    view_url: str | None = None
    session_id: str | None = None
    terms_version: str | None = None


def _aware(dt: datetime) -> datetime:
    # SQLite returns tz-naive datetimes; production Postgres returns tz-aware.
    # Normalise to UTC-aware before any comparison (the S8 _last_sent_at lesson).
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class ContributionService:
    """Room / invite / guest-session orchestration for S17."""

    def __init__(
        self,
        store: ContributionStore,
        bridge: VdoNinjaBridge | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        token_factory: Callable[[], str] | None = None,
        on_air_hook: Callable[[RemoteGuestSession, ContributionRoom], None] | None = None,
        off_air_hook: Callable[[RemoteGuestSession, ContributionRoom], None] | None = None,
        audio_mute_hook: Callable[[RemoteGuestSession, ContributionRoom, bool], None] | None = None,
        alert_hook: Callable[[str, str], None] | None = None,
        channel_enabled: Callable[[str], bool | None] | None = None,
        invite_ttl: timedelta = _DEFAULT_INVITE_TTL,
        terms_version: str = CONTRIBUTION_TERMS_VERSION,
    ) -> None:
        self._store = store
        self._bridge = bridge or NullVdoNinjaBridge()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id = id_factory or (lambda: uuid.uuid4().hex)
        # 32 bytes -> ~43 url-safe chars, comfortably over the ≥32 floor.
        self._token = token_factory or (lambda: secrets.token_urlsafe(32))
        self._on_air_hook = on_air_hook
        self._off_air_hook = off_air_hook
        self._audio_mute_hook = audio_mute_hook
        # ``None`` means no matching configured channel. Failing closed when
        # the app has not wired the validator prevents free-text ids from
        # creating rooms that can never be switched live.
        self._channel_enabled = channel_enabled or (lambda _channel_id: None)
        # (kind, detail) -> S8; emitted when an ON-AIR guest is dropped (S17 §6).
        self._alert_hook = alert_hook
        self._invite_ttl = invite_ttl
        self._terms_version = terms_version

    # --- rooms -----------------------------------------------------------

    def create_room(
        self,
        *,
        channel_id: str,
        name: str,
        max_guests: int = 6,
        compositor_target: str = "gst_compositor",
        room_id: str | None = None,
        vdo_room_name: str | None = None,
    ) -> ContributionRoom:
        enabled = self._channel_enabled(channel_id)
        if enabled is None:
            raise ChannelNotConfiguredError(
                f"Channel {channel_id!r} has no configured egress channel."
            )
        if not enabled:
            raise ChannelDisabledError(
                f"Channel {channel_id!r} is disabled and cannot host a contribution room."
            )

        now = self._clock()
        room = ContributionRoom(
            room_id=room_id or f"room_{self._id()}",
            channel_id=channel_id,
            name=name,
            # The VDO room token is never reused across rooms (S17 §3); derive a
            # fresh opaque value unless the caller pinned one.
            vdo_room_name=vdo_room_name or f"vdo_{self._id()}",
            max_guests=max_guests,
            state="idle",
            compositor_target=compositor_target,  # type: ignore[arg-type]
            created_at=now,
            updated_at=now,
        )
        return self._store.upsert_room(room)

    def get_room(self, room_id: str) -> ContributionRoom | None:
        return self._store.get_room(room_id)

    def list_rooms(self, *, channel_id: str | None = None) -> list[ContributionRoom]:
        return self._store.list_rooms(channel_id=channel_id)

    def open_room(self, room_id: str) -> tuple[ContributionRoom, str]:
        """Open a room and return (room, director_url). Idempotent if already
        open/live. Raises VdoBridgeError (router -> 503) if the tier is not
        configured — an operator cannot 'open' a room with no VDO behind it."""
        room = self._require_room(room_id)
        director_url = self._bridge.director_url(room)  # may raise VdoBridgeError
        if room.state not in _JOINABLE_ROOM_STATES:
            room = self._store.set_room_state(room_id, "open", updated_at=self._clock())
        return room, director_url

    def close_room(self, room_id: str) -> ContributionRoom:
        """Return active guests to program, then close the room record.

        VDO.Ninja currently exposes no authoritative disconnect observation to
        this service. Guests remain connected in the lifecycle until a
        separate observed/manual update marks them disconnected.
        """
        self._require_room(room_id)
        for guest in self._store.list_sessions(room_id=room_id, active_only=True):
            if guest.state in {"on_air", "muted"}:
                self.take_off_air(guest.session_id)
        now = self._clock()
        return self._store.set_room_state(room_id, "closed", updated_at=now)

    # --- invites ---------------------------------------------------------

    def mint_invite(
        self,
        *,
        room_id: str,
        guest_display_name: str,
        role: ContributionRole,
        ttl: timedelta | None = None,
    ) -> GuestInvite:
        room = self._require_room(room_id)
        if room.state not in _INVITABLE_ROOM_STATES:
            raise RoomClosedError(f"room {room_id} is {room.state}; cannot mint invites")
        now = self._clock()
        token = self._token()
        urls = self._bridge.guest_urls(room, invite_token=token, role=role)  # may raise
        invite = GuestInvite(
            invite_id=f"inv_{self._id()}",
            room_id=room_id,
            guest_display_name=guest_display_name,
            role=role,
            invite_token=token,
            push_url=urls.push_url,
            view_url=urls.view_url,
            expires_at=now + (ttl or self._invite_ttl),
            created_at=now,
        )
        return self._store.create_invite(invite)

    def list_invites_for_room(self, room_id: str) -> list[GuestInvite]:
        return self._store.list_invites_for_room(room_id)

    def accept_terms(self, invite_token: str) -> GuestInvite:
        """Public: record terms acceptance for an invite before the guest joins.

        Validated against the live invite (not expired / not consumed)."""
        invite = self._require_live_invite(invite_token)
        return self._store.record_invite_terms(
            invite.invite_id,
            terms_agreement_id=f"agr_{self._id()}",
            terms_version=self._terms_version,
        )

    def resolve_invite(self, invite_token: str) -> InviteJoinView:
        """Public token-gated join resolution. Consumes the single-use token and
        creates a HELD guest session ONCE the join can actually proceed; returns
        a needs-terms view (token NOT consumed) when terms are still required."""
        # Re-validates even after terms acceptance — invite may expire between steps.
        invite = self._require_live_invite(invite_token)
        room = self._store.get_room(invite.room_id)
        if room is None:
            raise RoomNotFoundError(invite.room_id)
        if room.state not in _JOINABLE_ROOM_STATES:
            raise RoomNotOpenError(f"room {room.room_id} is {room.state}; not accepting guests")

        if invite.role == "public_comment" and invite.terms_agreement_id is None:
            return InviteJoinView(
                needs_terms=True,
                room_name=room.name,
                guest_display_name=invite.guest_display_name,
                role=invite.role,
                terms_version=self._terms_version,
            )

        active = self._store.list_sessions(room_id=room.room_id, active_only=True)
        if len(active) >= room.max_guests:
            raise RoomGuestLimitError(
                f"room {room.room_id} is at its {room.max_guests}-guest limit"
            )
        if invite.view_url is None:
            raise ContributionServiceError("invite has no view URL; mint a new invite")

        now = self._clock()
        if not self._store.consume_invite_token(invite_token, consumed_at=now):
            # Lost the guarded-UPDATE race to a concurrent resolve.
            raise InviteConsumedError(invite.invite_id)

        guest = RemoteGuestSession(
            session_id=f"gs_{self._id()}",
            room_id=room.room_id,
            invite_id=invite.invite_id,
            guest_display_name=invite.guest_display_name,
            state="connected",  # present in the room, HELD until admitted
            connection_quality="unknown",
            joined_at=now,
            proof_boundary=_JOIN_PROOF_BOUNDARY,
        )
        self._store.create_session(guest)
        return InviteJoinView(
            needs_terms=False,
            room_name=room.name,
            guest_display_name=invite.guest_display_name,
            role=invite.role,
            view_url=invite.view_url,
            session_id=guest.session_id,
        )

    # --- guest-session operator actions ----------------------------------

    def admit_guest(self, session_id: str) -> RemoteGuestSession:
        """Admit a held guest out of the waiting room (idempotent)."""
        guest = self._require_active_guest(session_id)
        if guest.admitted_at is not None:
            return guest
        admitted = guest.model_copy(update={"admitted_at": self._clock()})
        return self._store.save_session(admitted)

    def put_on_air(self, session_id: str) -> RemoteGuestSession:
        """Compose and route an admitted guest before accepting on-air state."""
        guest = self._require_active_guest(session_id)
        if guest.admitted_at is None:
            raise GuestNotAdmittedError(session_id)
        if guest.state in ("on_air", "muted"):
            return guest
        room = self._require_room(guest.room_id)
        now = self._clock()
        on_air = guest.model_copy(
            update={
                "state": "on_air",
                "on_air_at": guest.on_air_at or now,
            }
        )
        saved = self._store.save_session(on_air)
        changed_room = room.state != "live"
        if changed_room:
            self._store.set_room_state(room.room_id, "live", updated_at=now)
        if self._on_air_hook is not None:
            try:
                self._on_air_hook(saved, room)
            except Exception as exc:
                self._store.save_session(guest)
                if changed_room:
                    self._store.set_room_state(room.room_id, room.state, updated_at=now)
                raise TakeoverHookError("Guest media could not be routed to air") from exc
        return saved

    def mute_guest(self, session_id: str) -> RemoteGuestSession:
        guest = self._require_active_guest(session_id)
        if guest.state not in ("on_air", "muted"):
            raise InvalidGuestTransitionError(f"cannot mute a guest in state {guest.state}")
        if guest.state == "muted":
            return guest
        if self._audio_mute_hook is not None:
            try:
                self._audio_mute_hook(guest, self._require_room(guest.room_id), True)
            except Exception as exc:
                raise TakeoverHookError("Guest audio could not be muted on air") from exc
        return self._store.save_session(guest.model_copy(update={"state": "muted"}))

    def take_off_air(self, session_id: str) -> RemoteGuestSession:
        """Remove a guest from the composition and return to program if last."""
        guest = self._require_active_guest(session_id)
        if guest.state not in ("on_air", "muted"):
            raise InvalidGuestTransitionError(
                f"cannot take a guest off-air from state {guest.state}"
            )
        if self._off_air_hook is not None:
            try:
                self._off_air_hook(guest, self._require_room(guest.room_id))
            except Exception as exc:
                raise TakeoverHookError("Guest media could not be taken off air") from exc
        saved = self._store.save_session(guest.model_copy(update={"state": "connected"}))
        self._restore_room_state_if_empty(guest.room_id)
        return saved

    def drop_guest(self, session_id: str) -> RemoteGuestSession:
        """Remove an on-air guest, then record the drop and alert."""
        guest = self._require_active_guest(session_id)
        was_on_air = guest.state in ("on_air", "muted")
        if was_on_air and self._off_air_hook is not None:
            try:
                self._off_air_hook(guest, self._require_room(guest.room_id))
            except Exception as exc:
                raise TakeoverHookError("Guest drop could not return the channel") from exc
        dropped = guest.model_copy(update={"state": "dropped", "ended_at": self._clock()})
        saved = self._store.save_session(dropped)
        if was_on_air:
            self._restore_room_state_if_empty(guest.room_id)
        if was_on_air and self._alert_hook is not None:
            self._emit_alert(
                ALERT_GUEST_DROP,
                f"On-air guest '{guest.guest_display_name}' was dropped from room {guest.room_id}.",
            )
        return saved

    def update_connection_quality(self, session_id: str, quality: str) -> RemoteGuestSession:
        """Record an advisory connection-quality reading (never gates anything)."""
        guest = self._require_active_guest(session_id)
        return self._store.save_session(guest.model_copy(update={"connection_quality": quality}))

    def record_media_control_request(
        self, session_id: str, action: MediaControlAction
    ) -> RemoteGuestSession:
        """Record that the operator browser sent a control request to VDO.

        The VDO.Ninja v30.2 iframe API has no callback for these commands, so
        this intentionally records only an unverified request.
        """
        guest = self._require_active_guest(session_id)
        next_state = guest.state
        if action in {"audio_mute", "audio_unmute"} and guest.state in {"on_air", "muted"}:
            muted = action == "audio_mute"
            next_state = "muted" if muted else "on_air"
            if next_state != guest.state and self._audio_mute_hook is not None:
                try:
                    self._audio_mute_hook(guest, self._require_room(guest.room_id), muted)
                except Exception as exc:
                    raise TakeoverHookError("Guest audio control could not reach air") from exc
        return self._store.save_session(
            guest.model_copy(
                update={
                    "media_control_state": "sent_unverified",
                    "media_control_action": action,
                    "media_control_requested_at": self._clock(),
                    "state": next_state,
                }
            )
        )

    def _restore_room_state_if_empty(self, room_id: str) -> None:
        sessions = self._store.list_sessions(room_id=room_id, active_only=True)
        if any(guest.state in {"on_air", "muted"} for guest in sessions):
            return
        room = self._require_room(room_id)
        if room.state == "live":
            self._store.set_room_state(room_id, "open", updated_at=self._clock())

    def list_sessions(
        self, *, room_id: str | None = None, active_only: bool = False
    ) -> list[RemoteGuestSession]:
        return self._store.list_sessions(room_id=room_id, active_only=active_only)

    def get_session(self, session_id: str) -> RemoteGuestSession | None:
        return self._store.get_session(session_id)

    # --- diagnostics -----------------------------------------------------

    def diagnostics(self) -> VdoDiagnostics:
        return self._bridge.diagnostics()

    def test_turn_connectivity(self) -> VdoDiagnostics:
        """Probe TURN reachability right now and return refreshed diagnostics
        (the operator console's "Test TURN connectivity" button)."""
        return self._bridge.test_turn_connectivity()

    # --- helpers ---------------------------------------------------------

    def _emit_alert(self, kind: str, detail: str) -> None:
        if self._alert_hook is None:
            return
        # An alert-sink failure must never fail the guest drop (the app's hook
        # logs its own failures; this just guards against the hook raising).
        with contextlib.suppress(Exception):
            self._alert_hook(kind, detail)

    def _require_room(self, room_id: str) -> ContributionRoom:
        room = self._store.get_room(room_id)
        if room is None:
            raise RoomNotFoundError(room_id)
        return room

    def _require_live_invite(self, invite_token: str) -> GuestInvite:
        invite = self._store.get_invite_by_token(invite_token)
        if invite is None:
            raise InviteNotFoundError(invite_token)
        if _aware(invite.expires_at) <= self._clock():
            raise InviteExpiredError(invite.invite_id)
        if invite.consumed_at is not None:
            raise InviteConsumedError(invite.invite_id)
        return invite

    def _require_active_guest(self, session_id: str) -> RemoteGuestSession:
        guest = self._store.get_session(session_id)
        if guest is None:
            raise GuestSessionNotFoundError(session_id)
        if guest.state in _TERMINAL_GUEST_STATES:
            raise InvalidGuestTransitionError(f"guest {session_id} is {guest.state} (terminal)")
        return guest


__all__ = [
    "CONTRIBUTION_TERMS_VERSION",
    "ChannelDisabledError",
    "ChannelNotConfiguredError",
    "ContributionService",
    "ContributionServiceError",
    "GuestNotAdmittedError",
    "InvalidGuestTransitionError",
    "InviteConsumedError",
    "InviteExpiredError",
    "InviteJoinView",
    "RoomClosedError",
    "RoomGuestLimitError",
    "RoomNotOpenError",
    "TakeoverHookError",
]
