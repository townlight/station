# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Live-broadcast spine store: LiveSession + LiveSource + RecordingTarget.

Sprint 0.4 Slice 1 Commits 4 + 6. Owns:

* ``LiveSessionStore`` -- the LiveSession lifecycle:

      idle -> preflight -> on_air -> ending -> recorded

  Each transition is a conditional UPDATE filtered by the expected
  source state. Concurrent writers racing the same transition produce
  exactly one winner; the loser sees ``LiveSessionStateError`` because
  rowcount is zero after the predicate UPDATE missed. This is the v0.4
  release-plan operator guarantee for "Start/End Live Stream"
  idempotency on retry.

* ``LiveSourceStore`` (Slice 1 Commit 6) -- minimal CRUD over the
  configured ``live_sources`` rows so the staff router can list / get /
  create RTMP/RTSP/NDI/SRT descriptors. The pre-flight evaluator (Slice
  1 Commit 5) already reads ``live_sources`` directly; this store is the
  write-side seam.

* ``RecordingTargetStore`` (Slice 1 Commit 6) -- minimal CRUD over the
  ``recording_targets`` rows. Same posture as ``LiveSourceStore``.

This module does NOT include:

- Recording-finalization handler that writes a typed event row to
  ``live_session_events`` and inserts the recorded asset row in the
  same transaction (Slice 1 Commit 7).

The LiveSession transition set is intentionally forward-only. There is
no ``cancel_preflight`` (preflight -> idle) because operator-cancel
semantics are tied to the preflight evaluator contract that lands in
Slice 1 Commit 5; including a cancel transition here would commit to
a UX before the contract is fixed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.egress.models import EgressCommandDb, TakeoverAuditRecordDb, TakeoverSession
from civiccast.egress.takeover_service import AlreadyLiveError
from civiccast.live.models import (
    _LIVE_SESSION_STATES,
    LIVE_SESSION_STATE_ENDING,
    LIVE_SESSION_STATE_IDLE,
    LIVE_SESSION_STATE_ON_AIR,
    LIVE_SESSION_STATE_PREFLIGHT,
    LIVE_SESSION_STATE_RECORDED,
    LiveRelayConfig,
    LiveRelayConfigCreate,
    LiveRelayConfigResponse,
    LiveRelayHealthUpdate,
    LiveSession,
    LiveSessionCreate,
    LiveSessionResponse,
    LiveSource,
    LiveSourceCreate,
    LiveSourceResponse,
    LiveSourceUpdate,
    RecordingTarget,
    RecordingTargetCreate,
    RecordingTargetResponse,
    check_credential_shape,
)
from civiccast.live.readiness import (
    PROBE_STATE_FAILED,
    PROBE_STATE_NEVER_PROBED,
    PROBE_STATE_READY,
)
from civiccast.live.source_endpoints import normalize_endpoint

SessionFactory = Callable[[], AbstractContextManager[Session]]

#: ``live_sources.probe_detail`` is Text, but the operator-facing string is a
#: sentence, not a protocol trace. Truncating at the write keeps a pathological
#: ffprobe stderr out of the row (and out of every list response built from it).
_MAX_PROBE_DETAIL = 400


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class LiveSessionNotFoundError(Exception):
    """Raised when a lookup or transition targets a missing ``live_session_id``.

    Carries the requested id so a router layer can produce a 404 body
    that names the asked-for session without re-querying.
    """

    def __init__(self, live_session_id: str) -> None:
        self.live_session_id = live_session_id
        super().__init__(f"LiveSession {live_session_id!r} not found")


class LiveSessionStateError(Exception):
    """Raised when a transition is requested from a state that does not allow it.

    Carries the session id, the actual current state observed at the
    time the failed UPDATE was retried as a SELECT, and the name of the
    attempted transition. A router layer turns this into a 409 with a
    structured body of (current_state, attempted_transition).
    """

    def __init__(
        self,
        live_session_id: str,
        current_state: str,
        attempted_transition: str,
    ) -> None:
        self.live_session_id = live_session_id
        self.current_state = current_state
        self.attempted_transition = attempted_transition
        super().__init__(
            f"LiveSession {live_session_id!r} cannot {attempted_transition} "
            f"from state {current_state!r}"
        )


class LiveSessionAlreadyExistsError(Exception):
    """Raised when ``create_session`` is called with a ``live_session_id``
    that already exists. Mirrors the schedule store's
    :class:`civiccast.vod.store.AssetAlreadyExistsError` posture so a
    router can map both to 409 uniformly.
    """

    def __init__(self, live_session_id: str) -> None:
        self.live_session_id = live_session_id
        super().__init__(f"LiveSession {live_session_id!r} already exists")


class LiveSourceAlreadyExistsError(Exception):
    """Raised when a ``LiveSource`` create collides on primary key.

    Carries the requested id so the router layer can produce a 409 body
    that names the conflicting source without re-querying.
    """

    def __init__(self, live_source_id: str) -> None:
        self.live_source_id = live_source_id
        super().__init__(f"LiveSource {live_source_id!r} already exists")


class LiveSourceNotFoundError(Exception):
    """Raised when an update or probe-observation write targets a missing row.

    Carries the requested id so the router can produce a 404 that names the
    source, and so the takeover readiness check can fail closed with a reason
    ("the source you selected no longer exists") instead of a bare False.
    """

    def __init__(self, live_source_id: str) -> None:
        self.live_source_id = live_source_id
        super().__init__(f"LiveSource {live_source_id!r} not found")


class LiveSourceConcurrencyError(Exception):
    """Raised when a PATCH's ``expected_row_version`` does not match the row.

    Two operators editing the same camera from two Live Room windows is not
    hypothetical in a control room; the second save is refused with both
    versions so the UI can say "this source changed while you were editing"
    rather than silently discarding the first operator's work.
    """

    def __init__(self, live_source_id: str, *, expected: int, actual: int) -> None:
        self.live_source_id = live_source_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"LiveSource {live_source_id!r} was modified: expected row_version "
            f"{expected}, found {actual}"
        )


class LiveSourceProbeConflictError(Exception):
    """Raised when the row that was probed no longer matches what was probed.

    ``record_probe_observation`` accepts the ``row_version`` and
    ``endpoint_url`` the caller actually probed. If either no longer matches
    the row at write time -- an operator's PATCH landed in the window between
    reading the row and persisting the observation -- persisting ``ready`` (or
    any other verdict) derived from the OLD address would durably misreport
    the NEW one. This is raised instead, and no write happens: the caller must
    treat the probe as inconclusive, never as an answer about the row that now
    exists.
    """

    def __init__(self, live_source_id: str, *, reason: str) -> None:
        self.live_source_id = live_source_id
        self.reason = reason
        super().__init__(
            f"LiveSource {live_source_id!r} changed during probe ({reason}); "
            "the observation was discarded rather than recorded"
        )


class RecordingTargetAlreadyExistsError(Exception):
    """Raised when a ``RecordingTarget`` create collides on primary key."""

    def __init__(self, recording_target_id: str) -> None:
        self.recording_target_id = recording_target_id
        super().__init__(f"RecordingTarget {recording_target_id!r} already exists")


class LiveRelayConfigAlreadyExistsError(Exception):
    """Raised when a relay config create collides on primary key."""

    def __init__(self, relay_config_id: str) -> None:
        self.relay_config_id = relay_config_id
        super().__init__(f"LiveRelayConfig {relay_config_id!r} already exists")


class LiveRelayConfigNotFoundError(Exception):
    """Raised when a relay config lookup or health update targets a missing row."""

    def __init__(self, relay_config_id: str) -> None:
        self.relay_config_id = relay_config_id
        super().__init__(f"LiveRelayConfig {relay_config_id!r} not found")


# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------


# Maps transition method names to (expected current state, new state).
# Held module-private so the test surface can pin the transitions without
# inviting external callers to bypass the public methods.
_TRANSITIONS: dict[str, tuple[str, str]] = {
    "start_preflight": (LIVE_SESSION_STATE_IDLE, LIVE_SESSION_STATE_PREFLIGHT),
    "go_on_air": (LIVE_SESSION_STATE_PREFLIGHT, LIVE_SESSION_STATE_ON_AIR),
    "end_broadcast": (LIVE_SESSION_STATE_ON_AIR, LIVE_SESSION_STATE_ENDING),
    "mark_recorded": (LIVE_SESSION_STATE_ENDING, LIVE_SESSION_STATE_RECORDED),
}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class LiveSessionStore:
    """SA-backed live-session store with explicit state-machine transitions.

    Constructor takes a session-factory callable (same shape as
    :class:`civiccast.schedule.store.PostgresAssetStore`). Each method
    opens its own context-managed session so the caller never sees a
    leaked SA session and the store remains stateless between calls.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------ CRUD

    def create_session(self, payload: LiveSessionCreate) -> LiveSessionResponse:
        """Insert a new LiveSession at state ``idle``.

        Raises :class:`LiveSessionAlreadyExistsError` when the primary
        key collides. ``rollback()`` runs before the re-raise so the
        underlying SA session is reusable for the next call.
        """
        with self._session_factory() as session:
            row = LiveSession(
                live_session_id=payload.live_session_id,
                channel_id=payload.channel_id,
                title=payload.title,
                state=LIVE_SESSION_STATE_IDLE,
                notes=payload.notes,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise LiveSessionAlreadyExistsError(payload.live_session_id) from exc
            session.refresh(row)
            return LiveSessionResponse.model_validate(row)

    def get_session(self, live_session_id: str) -> LiveSessionResponse | None:
        """Return the LiveSession projection, or ``None`` if absent."""
        with self._session_factory() as session:
            row = session.execute(
                select(LiveSession).where(LiveSession.live_session_id == live_session_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            return LiveSessionResponse.model_validate(row)

    def list_sessions(
        self,
        *,
        channel_id: str | None = None,
        states: tuple[str, ...] | None = None,
    ) -> list[LiveSessionResponse]:
        """Return live sessions, optionally filtered by channel and state.

        The public portal consumes this through the resident-safe
        ``/api/public/live/current`` route. Ordering is newest-started first,
        then newest-created first so an on-air session wins over stale rows.
        """
        if states is not None:
            for state in states:
                if state not in _LIVE_SESSION_STATES:
                    raise ValueError(
                        f"Unknown live session state {state!r}; "
                        f"expected one of {_LIVE_SESSION_STATES}."
                    )

        with self._session_factory() as session:
            stmt = select(LiveSession).order_by(
                LiveSession.started_at.desc().nulls_last(),
                LiveSession.created_at.desc(),
                LiveSession.live_session_id.asc(),
            )
            if channel_id is not None:
                stmt = stmt.where(LiveSession.channel_id == channel_id)
            if states is not None:
                stmt = stmt.where(LiveSession.state.in_(states))
            rows = session.execute(stmt).scalars().all()
            return [LiveSessionResponse.model_validate(row) for row in rows]

    # ------------------------------------------------------- State transitions

    def start_preflight(self, live_session_id: str) -> LiveSessionResponse:
        """Transition ``idle`` -> ``preflight``.

        Raises :class:`LiveSessionNotFoundError` when no row exists.
        Raises :class:`LiveSessionStateError` when the session is in any
        state other than ``idle``.
        """
        return self._transition(live_session_id, "start_preflight")

    def go_on_air(
        self,
        live_session_id: str,
        *,
        now: datetime | None = None,
        takeover: TakeoverSession | None = None,
    ) -> LiveSessionResponse:
        """Transition ``preflight`` -> ``on_air``; stamp ``started_at`` and
        the resolved recording target (provenance, Beta sprint B1).

        The stamp records which target the recording is expected under so the
        finalization worker never has to guess. Resolution: the oldest
        non-rehearsal target whose URI is a usable local location (the same
        rules the worker applies). No resolvable target stamps NULL — the
        worker's never-appeared deadline surfaces that misconfiguration.

        ``now`` is injectable for deterministic tests; production code
        passes ``None`` and the store records ``datetime.now(UTC)``.
        """
        target_id, target_uri = self._resolve_recording_target()
        return self._transition(
            live_session_id,
            "go_on_air",
            takeover=takeover,
            extra_setters={
                "started_at": now or datetime.now(UTC),
                "recording_target_id": target_id,
                "recording_target_uri": target_uri,
            },
        )

    def _resolve_recording_target(self) -> tuple[str | None, str | None]:
        from civiccast.live.recording_paths import (
            REHEARSAL_RECORDING_TARGET_ID,
            local_recording_path,
        )

        with self._session_factory() as session:
            targets = session.execute(
                select(RecordingTarget).order_by(RecordingTarget.created_at.asc())
            ).scalars()
            for target in targets:
                if target.recording_target_id == REHEARSAL_RECORDING_TARGET_ID:
                    continue
                if local_recording_path(target.target_uri) is None:
                    continue
                return target.recording_target_id, target.target_uri
        return None, None

    def end_broadcast(
        self,
        live_session_id: str,
        *,
        now: datetime | None = None,
        operator_id: str | None = None,
    ) -> LiveSessionResponse:
        """Transition ``on_air`` -> ``ending`` and stamp ``ended_at``.

        With an operator identity, close this Live Room session's own takeover
        and queue its handback in the same transaction. Recording finalization
        observes the resulting ending state separately.
        """
        return self._transition(
            live_session_id,
            "end_broadcast",
            handback_operator_id=operator_id,
            extra_setters={"ended_at": now or datetime.now(UTC)},
        )

    def mark_recorded(self, live_session_id: str) -> LiveSessionResponse:
        """Transition ``ending`` -> ``recorded``.

        This is the terminal transition. Slice 1 Commit 7 will wrap
        ``mark_recorded`` together with a typed ``session.finalized``
        row insert into ``live_session_events`` and the
        ``ASSET_STATE_RECORDED`` asset-row insert inside a single
        transaction. This commit only advances the LiveSession state.
        """
        return self._transition(live_session_id, "mark_recorded")

    # --------------------------------------------------- Internal transition

    def _transition(
        self,
        live_session_id: str,
        transition_name: str,
        *,
        extra_setters: dict[str, object] | None = None,
        takeover: TakeoverSession | None = None,
        handback_operator_id: str | None = None,
    ) -> LiveSessionResponse:
        """Run a state transition as a conditional UPDATE.

        The UPDATE predicate is ``WHERE live_session_id = ? AND state = ?``.
        Two concurrent callers attempting the same transition race the
        UPDATE; exactly one wins (rowcount == 1) and the other sees
        rowcount == 0. The losing caller re-reads to distinguish
        "not found" from "wrong state" and raises the matching domain
        exception so the surface is structured for the router layer.
        """
        expected_state, new_state = _TRANSITIONS[transition_name]
        setters: dict[str, object] = {"state": new_state}
        if extra_setters is not None:
            setters.update(extra_setters)

        with self._session_factory() as session:
            result = session.execute(
                update(LiveSession)
                .where(LiveSession.live_session_id == live_session_id)
                .where(LiveSession.state == expected_state)
                .values(**setters)
            )
            # ``Result`` is the union return type SA gives at the public
            # surface; UPDATE statements always produce a ``CursorResult``
            # which carries ``rowcount``. Cast through Any to satisfy mypy
            # without dragging the cursor-result type into the public API.
            matched: int = result.rowcount  # type: ignore[attr-defined]
            if matched == 1:
                # Session state, takeover ownership and its engine command must
                # commit together. No successful response can omit dispatch;
                # a failed write cannot leave a session falsely on air.
                takeover_id = "live-room-" + hashlib.sha256(live_session_id.encode()).hexdigest()
                if takeover is not None:
                    current = session.execute(
                        select(LiveSession).where(LiveSession.live_session_id == live_session_id)
                    ).scalar_one()
                    if current.channel_id != takeover.channel_id:
                        raise ValueError("Live session and takeover channels must match.")
                    active = (
                        session.execute(
                            select(TakeoverAuditRecordDb).where(
                                TakeoverAuditRecordDb.channel_id == current.channel_id,
                                TakeoverAuditRecordDb.returned_at.is_(None),
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if active is not None:
                        raise AlreadyLiveError(f"Channel {current.channel_id!r} is already live.")
                    owned_takeover = takeover.model_copy(update={"session_id": takeover_id})
                    session.add(TakeoverAuditRecordDb.from_session(owned_takeover))
                    session.add(
                        EgressCommandDb(
                            command_id="takeover-" + takeover_id,
                            channel_id=takeover.channel_id,
                            action="takeover",
                            issued_at=takeover.took_over_at,
                            issued_by=takeover.operator_id,
                        )
                    )
                if handback_operator_id is not None:
                    active = session.get(TakeoverAuditRecordDb, takeover_id)
                    if active is not None and active.returned_at is None:
                        ended_at = setters["ended_at"]
                        assert isinstance(ended_at, datetime)
                        active.returned_at = ended_at
                        session.add(
                            EgressCommandDb(
                                command_id="handback-" + takeover_id,
                                channel_id=active.channel_id,
                                action="handback",
                                issued_at=ended_at,
                                issued_by=handback_operator_id,
                            )
                        )
                session.commit()
                updated_row = session.execute(
                    select(LiveSession).where(LiveSession.live_session_id == live_session_id)
                ).scalar_one()
                return LiveSessionResponse.model_validate(updated_row)

            # The predicate UPDATE matched zero rows. Either the session
            # does not exist or it is in a state other than ``expected_state``.
            session.rollback()
            current_row = session.execute(
                select(LiveSession).where(LiveSession.live_session_id == live_session_id)
            ).scalar_one_or_none()
            if current_row is None:
                raise LiveSessionNotFoundError(live_session_id)
            raise LiveSessionStateError(
                live_session_id=live_session_id,
                current_state=current_row.state,
                attempted_transition=transition_name,
            )


# ---------------------------------------------------------------------------
# LiveSourceStore -- minimal CRUD over live_sources (Slice 1 Commit 6)
# ---------------------------------------------------------------------------


class LiveSourceStore:
    """SA-backed store for configured live-input sources.

    ``create`` / ``get`` / ``list`` are the Slice 1 surface. WP-07 adds the
    two operations observed readiness needs: ``update`` (the operator edit
    flow the Slice 1 docstring deferred until "a later rung defines the
    operator-cancel + edit UX" -- the Live Room edit form is that UX) and
    ``record_probe_observation`` (the durable result of a probe). ``delete``
    remains out of scope; nothing in WP-07 needs it and the cancel UX it was
    waiting on still does not exist.

    Readiness invalidation lives here rather than in the router because it is
    a property of the write, not of the transport: any change to what would be
    probed -- endpoint, source type, channel, credential reference -- resets
    the row to ``never_probed`` in the same transaction that applies the edit,
    so there is no window in which the new endpoint carries the old
    endpoint's readiness.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def create(self, payload: LiveSourceCreate) -> LiveSourceResponse:
        """Insert a new ``LiveSource``. Raises :class:`LiveSourceAlreadyExistsError`
        on primary-key collision."""
        with self._session_factory() as session:
            row = LiveSource(
                live_source_id=payload.live_source_id,
                channel_id=payload.channel_id,
                name=payload.name,
                source_type=payload.source_type,
                # ``endpoint_url`` is ``HttpUrl | str`` at the Pydantic
                # surface; coerce to ``str`` for the DB column without
                # discarding the Pydantic validation that already ran.
                endpoint_url=str(payload.endpoint_url),
                credentials_handle=payload.credentials_handle,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise LiveSourceAlreadyExistsError(payload.live_source_id) from exc
            session.refresh(row)
            return LiveSourceResponse.model_validate(row)

    def get(self, live_source_id: str) -> LiveSourceResponse | None:
        """Return one ``LiveSource`` by id, or ``None`` if absent."""
        with self._session_factory() as session:
            row = session.execute(
                select(LiveSource).where(LiveSource.live_source_id == live_source_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            return LiveSourceResponse.model_validate(row)

    def list(self, *, channel_id: str | None = None) -> list[LiveSourceResponse]:
        """Return every ``LiveSource``, optionally filtered by channel.

        Ordering is ``created_at`` ascending so older operator-configured
        sources surface first in the operator UI list. ``channel_id`` is
        an exact-match filter; pass ``None`` to return every row.
        """
        with self._session_factory() as session:
            stmt = select(LiveSource)
            if channel_id is not None:
                stmt = stmt.where(LiveSource.channel_id == channel_id)
            stmt = stmt.order_by(LiveSource.created_at.asc(), LiveSource.live_source_id.asc())
            rows = session.execute(stmt).scalars().all()
            return [LiveSourceResponse.model_validate(row) for row in rows]

    def update(self, live_source_id: str, payload: LiveSourceUpdate) -> LiveSourceResponse:
        """Apply an operator edit, invalidating readiness when it needs to be.

        Raises :class:`LiveSourceNotFoundError` when the row is gone and
        :class:`LiveSourceConcurrencyError` when ``expected_row_version`` does
        not match, OR when a *different* commit lands between this method's
        own read of the row and its own write -- see the compare-and-swap
        note below. ``row_version`` is bumped on every applied edit so a
        second operator's stale PATCH is refused rather than silently
        winning.

        Endpoint/type validation is NOT re-implemented here: the merged
        (existing + requested) source type and endpoint are re-run through
        :class:`~civiccast.live.models.LiveSourceCreate`'s own validator via
        ``normalize_endpoint``, so changing only the source type of an
        existing row is checked against that row's stored endpoint rather than
        being accepted because the request body did not mention the endpoint.

        **Compare-and-swap, not read-then-write.** An earlier version of this
        method read the row, mutated the ORM object's attributes, and
        committed -- which checks ``expected_row_version`` against the value
        it read, but leaves a window between that read and the eventual
        ``UPDATE`` for a *different* concurrent commit to land unnoticed
        (hostile-review finding N1). The write below is a single
        ``UPDATE ... WHERE live_source_id = ? AND row_version = ?`` statement,
        where the version is the one this call just read: the database
        performs the check-and-write atomically, so there is no window at
        all, whether or not the caller supplied its own
        ``expected_row_version``. ``rowcount == 0`` means the row changed (or
        was deleted) between the read above and this statement.
        """
        with self._session_factory() as session:
            row = session.execute(
                select(LiveSource).where(LiveSource.live_source_id == live_source_id)
            ).scalar_one_or_none()
            if row is None:
                raise LiveSourceNotFoundError(live_source_id)
            if (
                payload.expected_row_version is not None
                and payload.expected_row_version != row.row_version
            ):
                raise LiveSourceConcurrencyError(
                    live_source_id,
                    expected=payload.expected_row_version,
                    actual=row.row_version,
                )

            read_version = row.row_version
            changed = payload.changed_fields()
            # ``LiveSourceUpdate.source_type`` is ``Optional`` at the model
            # (every field is optional-and-omittable), but ``changed_fields``
            # only ever includes "source_type" when ``payload.source_type``
            # is not ``None``. The extra ``is not None`` in this expression
            # is what lets mypy narrow it to ``str`` for the merged-row
            # validation below, rather than the two calls it feeds
            # (``normalize_endpoint`` / ``check_credential_shape``) accepting
            # a real ``str | None`` they would then have to null-check again.
            source_type: str = (
                payload.source_type
                if "source_type" in changed and payload.source_type is not None
                else row.source_type
            )
            endpoint_url = (
                str(payload.endpoint_url) if "endpoint_url" in changed else row.endpoint_url
            )
            if payload.clear_credentials_handle:
                credentials_handle: str | None = None
            elif "credentials_handle" in changed:
                credentials_handle = payload.credentials_handle
            else:
                credentials_handle = row.credentials_handle

            # Same rule as create, applied to the MERGED row. A request that
            # flips source_type from srt to rtsp must be judged against the
            # endpoint the row will actually hold afterwards.
            normalized_endpoint = normalize_endpoint(source_type, endpoint_url)
            check_credential_shape(source_type, credentials_handle)

            setters: dict[str, object] = {
                "source_type": source_type,
                "endpoint_url": normalized_endpoint,
                "credentials_handle": credentials_handle,
                "row_version": int(read_version or 1) + 1,
            }
            if "channel_id" in changed and payload.channel_id is not None:
                setters["channel_id"] = payload.channel_id
            if "name" in changed and payload.name is not None:
                setters["name"] = payload.name

            if payload.invalidates_readiness():
                # Immediately, in this same statement. A row whose address
                # just changed has never been probed at its new address, and
                # anything that reads it between now and the next probe --
                # including a takeover -- must see that.
                setters["probe_state"] = PROBE_STATE_NEVER_PROBED
                setters["probe_observed_at"] = None
                setters["probe_detail"] = None
                setters["probe_error_code"] = None
                # probe_last_success_at is deliberately NOT cleared: "this
                # camera last worked at 09:41" stays true and useful history
                # even though it no longer counts as readiness.

            result = session.execute(
                update(LiveSource)
                .where(LiveSource.live_source_id == live_source_id)
                .where(LiveSource.row_version == read_version)
                .values(**setters)
            )
            matched: int = result.rowcount  # type: ignore[attr-defined]
            if matched == 0:
                session.rollback()
                current = session.execute(
                    select(LiveSource).where(LiveSource.live_source_id == live_source_id)
                ).scalar_one_or_none()
                if current is None:
                    raise LiveSourceNotFoundError(live_source_id)
                raise LiveSourceConcurrencyError(
                    live_source_id,
                    expected=read_version,
                    actual=current.row_version,
                )
            session.commit()
            updated = session.execute(
                select(LiveSource).where(LiveSource.live_source_id == live_source_id)
            ).scalar_one()
            return LiveSourceResponse.model_validate(updated)

    def record_probe_observation(
        self,
        live_source_id: str,
        *,
        ok: bool,
        observed_at: datetime,
        detail: str | None,
        error_code: str | None,
        expected_row_version: int | None = None,
        expected_endpoint_url: str | None = None,
    ) -> LiveSourceResponse:
        """Persist the outcome of one probe against ``live_source_id``.

        ``probe_last_success_at`` advances only on success; a later failure
        leaves it standing so the operator can distinguish a source that never
        worked from one that stopped working.

        ``expected_row_version`` / ``expected_endpoint_url`` are the version and
        endpoint the caller actually probed, read at the top of its call before
        the (up to several seconds long) probe ran. When either is supplied and
        no longer matches the row at write time, the write is refused with
        :class:`LiveSourceProbeConflictError` rather than persisted: an
        operator's PATCH landing in that window (re-pointing the endpoint, or
        any other edit) must not be silently overwritten by a verdict about the
        address that used to be there. This is the guard against the
        probe-then-persist race the takeover gate and the explicit "Check
        source" action both go through.

        **Compare-and-swap, not read-then-write** (hostile-review finding
        N1, same defect class as :meth:`update`). The version/endpoint
        expectations are carried directly in the ``UPDATE``'s own ``WHERE``
        clause rather than checked against a prior ``SELECT`` and then
        written separately -- the database performs the check-and-write as
        one atomic statement, so there is no read-then-write window at all,
        not even the narrow one between this method's own read and its own
        write.
        """
        setters: dict[str, object] = {
            "probe_state": PROBE_STATE_READY if ok else PROBE_STATE_FAILED,
            "probe_observed_at": observed_at,
            "probe_detail": (detail or None) if detail is None else detail[:_MAX_PROBE_DETAIL],
            "probe_error_code": error_code,
        }
        if ok:
            setters["probe_last_success_at"] = observed_at

        with self._session_factory() as session:
            stmt = update(LiveSource).where(LiveSource.live_source_id == live_source_id)
            if expected_row_version is not None:
                stmt = stmt.where(LiveSource.row_version == expected_row_version)
            if expected_endpoint_url is not None:
                stmt = stmt.where(LiveSource.endpoint_url == expected_endpoint_url)
            result = session.execute(stmt.values(**setters))
            matched: int = result.rowcount  # type: ignore[attr-defined]
            if matched == 0:
                session.rollback()
                current = session.execute(
                    select(LiveSource).where(LiveSource.live_source_id == live_source_id)
                ).scalar_one_or_none()
                if current is None:
                    raise LiveSourceNotFoundError(live_source_id)
                # Distinguish which expectation actually broke, for the
                # conflict reason -- version wins when both were supplied and
                # both moved, since a version change already implies "this is
                # not the row I read."
                if expected_row_version is not None and current.row_version != expected_row_version:
                    reason = "row_version_changed"
                elif (
                    expected_endpoint_url is not None
                    and current.endpoint_url != expected_endpoint_url
                ):
                    reason = "endpoint_changed"
                else:
                    # Neither expectation was supplied (or both still match,
                    # e.g. some other column changed underneath a caller that
                    # gave no expectations at all) -- name it generically
                    # rather than guessing.
                    reason = "row_changed"
                raise LiveSourceProbeConflictError(live_source_id, reason=reason)
            session.commit()
            row = session.execute(
                select(LiveSource).where(LiveSource.live_source_id == live_source_id)
            ).scalar_one()
            return LiveSourceResponse.model_validate(row)


# ---------------------------------------------------------------------------
# LiveRelayConfigStore -- optional remote ingest / cloud relay targets
# ---------------------------------------------------------------------------


class LiveRelayConfigStore:
    """SA-backed store for optional outbound relay configuration.

    The zero-row posture is meaningful: stations continue to use the local
    self-hosted RTMP path. Rows here add optional cloud relay or direct
    syndication targets and health state for operator visibility.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def create(self, payload: LiveRelayConfigCreate) -> LiveRelayConfigResponse:
        """Insert a relay config row."""
        with self._session_factory() as session:
            row = LiveRelayConfig(
                relay_config_id=payload.relay_config_id,
                channel_id=payload.channel_id,
                name=payload.name,
                mode=payload.mode,
                endpoint_url=payload.endpoint_url,
                return_playback_url=payload.return_playback_url,
                provider=payload.provider,
                credentials_handle=payload.credentials_handle,
                enabled=payload.enabled,
                notes=payload.notes,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise LiveRelayConfigAlreadyExistsError(payload.relay_config_id) from exc
            session.refresh(row)
            return LiveRelayConfigResponse.model_validate(row)

    def get(self, relay_config_id: str) -> LiveRelayConfigResponse | None:
        """Return one relay config by id, or ``None`` if absent."""
        with self._session_factory() as session:
            row = session.execute(
                select(LiveRelayConfig).where(LiveRelayConfig.relay_config_id == relay_config_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            return LiveRelayConfigResponse.model_validate(row)

    def list(
        self,
        *,
        channel_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[LiveRelayConfigResponse]:
        """Return relay configs, optionally filtered by channel and enabled state."""
        with self._session_factory() as session:
            stmt = select(LiveRelayConfig)
            if channel_id is not None:
                stmt = stmt.where(LiveRelayConfig.channel_id == channel_id)
            if enabled is not None:
                stmt = stmt.where(LiveRelayConfig.enabled == enabled)
            stmt = stmt.order_by(
                LiveRelayConfig.created_at.asc(),
                LiveRelayConfig.relay_config_id.asc(),
            )
            rows = session.execute(stmt).scalars().all()
            return [LiveRelayConfigResponse.model_validate(row) for row in rows]

    def update_health(
        self,
        relay_config_id: str,
        payload: LiveRelayHealthUpdate,
    ) -> LiveRelayConfigResponse:
        """Update operator-visible health for a relay config."""
        setters: dict[str, object] = {
            "health_state": payload.health_state,
            "last_heartbeat_at": payload.last_heartbeat_at or datetime.now(UTC),
        }
        if payload.notes is not None:
            setters["notes"] = payload.notes

        with self._session_factory() as session:
            result = session.execute(
                update(LiveRelayConfig)
                .where(LiveRelayConfig.relay_config_id == relay_config_id)
                .values(**setters)
            )
            matched: int = result.rowcount  # type: ignore[attr-defined]
            if matched == 0:
                session.rollback()
                raise LiveRelayConfigNotFoundError(relay_config_id)
            session.commit()
            row = session.execute(
                select(LiveRelayConfig).where(LiveRelayConfig.relay_config_id == relay_config_id)
            ).scalar_one()
            return LiveRelayConfigResponse.model_validate(row)


# ---------------------------------------------------------------------------
# RecordingTargetStore -- minimal CRUD over recording_targets (Slice 1 Commit 6)
# ---------------------------------------------------------------------------


class RecordingTargetStore:
    """SA-backed store for recording-target descriptors.

    Same Slice 1 posture as ``LiveSourceStore``: create/get/list only.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def create(self, payload: RecordingTargetCreate) -> RecordingTargetResponse:
        """Insert a new ``RecordingTarget``. Raises
        :class:`RecordingTargetAlreadyExistsError` on primary-key collision."""
        with self._session_factory() as session:
            row = RecordingTarget(
                recording_target_id=payload.recording_target_id,
                name=payload.name,
                target_uri=payload.target_uri,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise RecordingTargetAlreadyExistsError(payload.recording_target_id) from exc
            session.refresh(row)
            return RecordingTargetResponse.model_validate(row)

    def get(self, recording_target_id: str) -> RecordingTargetResponse | None:
        """Return one ``RecordingTarget`` by id, or ``None`` if absent."""
        with self._session_factory() as session:
            row = session.execute(
                select(RecordingTarget).where(
                    RecordingTarget.recording_target_id == recording_target_id
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return RecordingTargetResponse.model_validate(row)

    def list(self) -> list[RecordingTargetResponse]:
        """Return every ``RecordingTarget`` ordered by ``created_at`` ascending."""
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(RecordingTarget).order_by(
                        RecordingTarget.created_at.asc(),
                        RecordingTarget.recording_target_id.asc(),
                    )
                )
                .scalars()
                .all()
            )
            return [RecordingTargetResponse.model_validate(row) for row in rows]
