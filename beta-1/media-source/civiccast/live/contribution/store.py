# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable persistence for the S17 remote-contribution tier.

Per-request store over the single global session factory (same lazy posture as
the cg / schedule / control_room stores). Soft string references, no hard FKs:
a guest-session row outlives the room it named, and deleting a room does not
cascade into the sessions that recorded a guest's time on-air.

The single-use invite consume is a **guarded UPDATE** (``WHERE consumed_at IS
NULL``) so two racing public requests for the same token can never both win —
exactly one observes ``rowcount == 1`` and consumes it.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from civiccast.live.contribution.models import (
    ContributionRoom,
    ContributionRoomDb,
    GuestInvite,
    GuestInviteDb,
    RemoteGuestSession,
    RemoteGuestSessionDb,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]

# A guest session is "active" until it is dropped or ended.
_TERMINAL_SESSION_STATES = ("dropped", "ended")


class ContributionStoreError(RuntimeError):
    """Base error for remote-contribution persistence failures."""


class RoomNotFoundError(ContributionStoreError):
    """Raised when a contribution-room id does not resolve."""


class InviteNotFoundError(ContributionStoreError):
    """Raised when a guest-invite id does not resolve."""


class GuestSessionNotFoundError(ContributionStoreError):
    """Raised when a remote-guest-session id does not resolve."""


def _now() -> datetime:
    return datetime.now(UTC)


def _room_to_model(row: ContributionRoomDb) -> ContributionRoom:
    return ContributionRoom(
        room_id=row.room_id,
        channel_id=row.channel_id,
        name=row.name,
        vdo_room_name=row.vdo_room_name,
        max_guests=row.max_guests,
        state=row.state,  # type: ignore[arg-type]
        compositor_target=row.compositor_target,  # type: ignore[arg-type]
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _invite_to_model(row: GuestInviteDb) -> GuestInvite:
    return GuestInvite(
        invite_id=row.invite_id,
        room_id=row.room_id,
        guest_display_name=row.guest_display_name,
        role=row.role,  # type: ignore[arg-type]
        invite_token=row.invite_token,
        push_url=row.push_url,
        view_url=row.view_url,
        terms_agreement_id=row.terms_agreement_id,
        terms_version=row.terms_version,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        created_at=row.created_at,
    )


def _session_to_model(row: RemoteGuestSessionDb) -> RemoteGuestSession:
    return RemoteGuestSession(
        session_id=row.session_id,
        room_id=row.room_id,
        invite_id=row.invite_id,
        guest_display_name=row.guest_display_name,
        state=row.state,  # type: ignore[arg-type]
        connection_quality=row.connection_quality,  # type: ignore[arg-type]
        admitted_at=row.admitted_at,
        joined_at=row.joined_at,
        on_air_at=row.on_air_at,
        ended_at=row.ended_at,
        media_control_state=row.media_control_state,  # type: ignore[arg-type]
        media_control_action=row.media_control_action,  # type: ignore[arg-type]
        media_control_requested_at=row.media_control_requested_at,
        proof_boundary=row.proof_boundary,
    )


class ContributionStore:
    """CRUD + single-use invite consume + guest-session state for S17."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def _session(self) -> AbstractContextManager[Session]:
        return self._session_factory()

    # --- rooms -----------------------------------------------------------

    def upsert_room(self, room: ContributionRoom) -> ContributionRoom:
        with self._session() as session:
            row = session.get(ContributionRoomDb, room.room_id)
            if row is None:
                row = ContributionRoomDb(room_id=room.room_id, created_at=room.created_at)
                session.add(row)
            row.channel_id = room.channel_id
            row.name = room.name
            row.vdo_room_name = room.vdo_room_name
            row.max_guests = room.max_guests
            row.state = room.state
            row.compositor_target = room.compositor_target
            row.updated_at = room.updated_at
            session.commit()
            return _room_to_model(row)

    def get_room(self, room_id: str) -> ContributionRoom | None:
        with self._session() as session:
            row = session.get(ContributionRoomDb, room_id)
            return _room_to_model(row) if row is not None else None

    def list_rooms(self, *, channel_id: str | None = None) -> list[ContributionRoom]:
        with self._session() as session:
            stmt = select(ContributionRoomDb)
            if channel_id is not None:
                stmt = stmt.where(ContributionRoomDb.channel_id == channel_id)
            rows = session.execute(stmt.order_by(ContributionRoomDb.name)).scalars().all()
            return [_room_to_model(r) for r in rows]

    def set_room_state(
        self, room_id: str, state: str, *, updated_at: datetime | None = None
    ) -> ContributionRoom:
        with self._session() as session:
            row = session.get(ContributionRoomDb, room_id)
            if row is None:
                raise RoomNotFoundError(room_id)
            row.state = state
            row.updated_at = updated_at or _now()
            session.commit()
            return _room_to_model(row)

    def delete_room(self, room_id: str) -> None:
        with self._session() as session:
            row = session.get(ContributionRoomDb, room_id)
            if row is None:
                raise RoomNotFoundError(room_id)
            session.delete(row)
            session.commit()

    # --- invites ---------------------------------------------------------

    def create_invite(self, invite: GuestInvite) -> GuestInvite:
        with self._session() as session:
            row = GuestInviteDb(
                invite_id=invite.invite_id,
                room_id=invite.room_id,
                guest_display_name=invite.guest_display_name,
                role=invite.role,
                invite_token=invite.invite_token,
                push_url=invite.push_url,
                view_url=invite.view_url,
                terms_agreement_id=invite.terms_agreement_id,
                terms_version=invite.terms_version,
                expires_at=invite.expires_at,
                consumed_at=invite.consumed_at,
                created_at=invite.created_at,
            )
            session.add(row)
            session.commit()
            return _invite_to_model(row)

    def get_invite(self, invite_id: str) -> GuestInvite | None:
        with self._session() as session:
            row = session.get(GuestInviteDb, invite_id)
            return _invite_to_model(row) if row is not None else None

    def get_invite_by_token(self, invite_token: str) -> GuestInvite | None:
        with self._session() as session:
            row = (
                session.execute(
                    select(GuestInviteDb).where(GuestInviteDb.invite_token == invite_token)
                )
                .scalars()
                .first()
            )
            return _invite_to_model(row) if row is not None else None

    def list_invites_for_room(self, room_id: str) -> list[GuestInvite]:
        with self._session() as session:
            rows = (
                session.execute(
                    select(GuestInviteDb)
                    .where(GuestInviteDb.room_id == room_id)
                    .order_by(GuestInviteDb.created_at.desc())
                )
                .scalars()
                .all()
            )
            return [_invite_to_model(r) for r in rows]

    def record_invite_terms(
        self, invite_id: str, *, terms_agreement_id: str, terms_version: str
    ) -> GuestInvite:
        with self._session() as session:
            row = session.get(GuestInviteDb, invite_id)
            if row is None:
                raise InviteNotFoundError(invite_id)
            row.terms_agreement_id = terms_agreement_id
            row.terms_version = terms_version
            session.commit()
            return _invite_to_model(row)

    def consume_invite_token(
        self, invite_token: str, *, consumed_at: datetime | None = None
    ) -> bool:
        """Atomically mark a single-use invite consumed **and not expired**.

        Returns ``True`` iff THIS call won the race (``consumed_at`` was NULL,
        the token had not expired at ``when``, and ``consumed_at`` is now set).
        A second concurrent call, an already-consumed token, OR a token that
        expired between the caller's pre-check and this UPDATE all see
        ``rowcount == 0`` and return ``False`` — the caller maps that to 410
        Gone (identical operator-facing result for consumed vs. expired).

        Expiry is in the guarded WHERE (not just the caller's pre-check) to
        close the TOCTOU on the consume seam (ENG-009): the single-use grant is
        never issued for an expired token even under the microsecond race.
        """
        when = consumed_at or _now()
        with self._session() as session:
            result = session.execute(
                update(GuestInviteDb)
                .where(
                    GuestInviteDb.invite_token == invite_token,
                    GuestInviteDb.consumed_at.is_(None),
                    GuestInviteDb.expires_at > when,
                )
                .values(consumed_at=when)
            )
            session.commit()
            return bool(cast(CursorResult[object], result).rowcount == 1)

    # --- guest sessions --------------------------------------------------

    def create_session(self, guest_session: RemoteGuestSession) -> RemoteGuestSession:
        with self._session() as session:
            row = RemoteGuestSessionDb(
                session_id=guest_session.session_id,
                room_id=guest_session.room_id,
                invite_id=guest_session.invite_id,
                guest_display_name=guest_session.guest_display_name,
                state=guest_session.state,
                connection_quality=guest_session.connection_quality,
                admitted_at=guest_session.admitted_at,
                joined_at=guest_session.joined_at,
                on_air_at=guest_session.on_air_at,
                ended_at=guest_session.ended_at,
                media_control_state=guest_session.media_control_state,
                media_control_action=guest_session.media_control_action,
                media_control_requested_at=guest_session.media_control_requested_at,
                proof_boundary=guest_session.proof_boundary,
            )
            session.add(row)
            session.commit()
            return _session_to_model(row)

    def get_session(self, session_id: str) -> RemoteGuestSession | None:
        with self._session() as session:
            row = session.get(RemoteGuestSessionDb, session_id)
            return _session_to_model(row) if row is not None else None

    def get_session_for_invite(self, invite_id: str) -> RemoteGuestSession | None:
        with self._session() as session:
            row = (
                session.execute(
                    select(RemoteGuestSessionDb)
                    .where(RemoteGuestSessionDb.invite_id == invite_id)
                    .order_by(RemoteGuestSessionDb.session_id)
                )
                .scalars()
                .first()
            )
            return _session_to_model(row) if row is not None else None

    def list_sessions(
        self, *, room_id: str | None = None, active_only: bool = False
    ) -> list[RemoteGuestSession]:
        with self._session() as session:
            stmt = select(RemoteGuestSessionDb)
            if room_id is not None:
                stmt = stmt.where(RemoteGuestSessionDb.room_id == room_id)
            if active_only:
                stmt = stmt.where(RemoteGuestSessionDb.state.notin_(_TERMINAL_SESSION_STATES))
            rows = session.execute(stmt.order_by(RemoteGuestSessionDb.session_id)).scalars().all()
            return [_session_to_model(r) for r in rows]

    def save_session(self, guest_session: RemoteGuestSession) -> RemoteGuestSession:
        """Persist a guest-session state transition. The legality of the
        transition is enforced by the service (state machine); the store just
        writes the resolved row."""
        with self._session() as session:
            row = session.get(RemoteGuestSessionDb, guest_session.session_id)
            if row is None:
                raise GuestSessionNotFoundError(guest_session.session_id)
            row.state = guest_session.state
            row.connection_quality = guest_session.connection_quality
            row.admitted_at = guest_session.admitted_at
            row.joined_at = guest_session.joined_at
            row.on_air_at = guest_session.on_air_at
            row.ended_at = guest_session.ended_at
            row.media_control_state = guest_session.media_control_state
            row.media_control_action = guest_session.media_control_action
            row.media_control_requested_at = guest_session.media_control_requested_at
            session.commit()
            return _session_to_model(row)


__all__ = [
    "ContributionStore",
    "ContributionStoreError",
    "GuestSessionNotFoundError",
    "InviteNotFoundError",
    "RoomNotFoundError",
    "SessionFactory",
]
