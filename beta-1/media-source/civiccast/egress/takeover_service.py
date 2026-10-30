# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Live-takeover orchestration (CivicCast 3.0 — S5).

Wires the proven engine primitives into a controllable, audited operation:

* **take** — build the live source plan, record a :class:`TakeoverSession`
  (open), and queue an ``EgressCommand(action="takeover")`` for the daemon
  (which calls ``supervisor.request_live_takeover``).
* **handback** — queue ``EgressCommand(action="handback")`` and close the
  session.
* **state** — the runtime :class:`ManualRouteState` (active session +
  can_takeover / can_return), derived from the audit store + live readiness.
* **audit** — the channel's takeover history.

The service touches no engine process directly; it persists + enqueues, and the
daemon consumes the commands (S5 daemon slice). ``clock`` / ``id_factory`` are
injectable for deterministic tests; ``ingest_plan_provider`` is injected so the
service stays decoupled from the live relay-config store.

WP-07 adds one more injected seam, ``readiness_verifier``, called in **take**
after the source plan is built and *before* the audit row is written or the
command is queued. Ordering is the whole point: an audit row is the station's
durable record that a takeover happened, and the queued command moves air as
soon as the daemon reads it, so neither may exist for a source that has gone
stale, started failing, or been edited since the operator's ingest plan was
built. See ``civiccast.live.readiness_service``.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from civiccast.egress.errors import SourcePrepareError
from civiccast.egress.live_takeover import build_live_takeover_source_plan
from civiccast.egress.models import EgressCommand, ManualRouteState, TakeoverSession
from civiccast.egress.store import EgressStore
from civiccast.egress.takeover_store import PostgresTakeoverAuditStore
from civiccast.live.models import RELAY_HEALTH_READY, LiveIngestPath, LiveIngestPlan

IngestPlanProvider = Callable[[str], LiveIngestPlan]


class ReadinessVerdict(Protocol):
    """What a :data:`ReadinessVerifier` hands back.

    Structural, not a base class, so this module stays decoupled from the live
    package exactly as ``ingest_plan_provider`` already is. The concrete
    implementation is
    ``civiccast.live.readiness_service.TakeoverReadiness``.
    """

    @property
    def ok(self) -> bool: ...

    @property
    def reason(self) -> str: ...

    @property
    def secret_ref(self) -> str | None: ...


#: Last gate before air changes. Called with the channel, the selected ingest
#: path id, and the endpoint the plan actually offered. The real
#: implementation is
#: ``LiveSourceReadinessService.verify_for_takeover``, which re-reads the row,
#: refuses a source that changed under the plan, performs or reuses a bounded
#: fresh probe, and returns the source's credential HANDLE (never its secret)
#: so the engine can open an authenticated SRT feed.
ReadinessVerifier = Callable[[str, str, str], ReadinessVerdict]


class AlreadyLiveError(RuntimeError):
    """The channel is already under live takeover (→ 409)."""


class NotInTakeoverError(RuntimeError):
    """The channel is not currently under live takeover (→ 404)."""


class TakeoverNotReadyError(RuntimeError):
    """No ready live source could be prepared for takeover (→ 422)."""


def _default_token() -> str:
    return secrets.token_urlsafe(12)


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _selected_path(plan: LiveIngestPlan, path_id: str) -> LiveIngestPath | None:
    for candidate in (plan.local_default, *plan.relay_paths):
        if candidate.path_id == path_id:
            return candidate
    return None


def _live_source_ready(plan: LiveIngestPlan) -> bool:
    return any(
        path.enabled and path.health_state == RELAY_HEALTH_READY
        for path in (plan.local_default, *plan.relay_paths)
    )


class TakeoverService:
    """Orchestrates live takeover / handback for one CivicCast deployment."""

    def __init__(
        self,
        audit_store: PostgresTakeoverAuditStore,
        egress_store: EgressStore,
        ingest_plan_provider: IngestPlanProvider,
        *,
        clock: Callable[[], datetime] = _default_clock,
        id_factory: Callable[[], str] = _default_token,
        readiness_verifier: ReadinessVerifier | None = None,
    ) -> None:
        self._audit = audit_store
        self._egress = egress_store
        self._ingest_plan_provider = ingest_plan_provider
        self._clock = clock
        self._id_factory = id_factory
        # ``None`` is not fail-open. The plan's own ``health_state`` is already
        # a fail-closed floor -- WP-07 made it derive from the source's
        # persisted probe observation rather than from the row's existence, and
        # build_live_takeover_source_plan still refuses anything that is not
        # ``ready``. The verifier ADDS the freshness re-check and the
        # plan-built-then-source-edited race check on top of that floor; a
        # caller that omits it (an in-memory test, the deprecated no-DB path)
        # gets the floor, never a bypass.
        self._readiness_verifier = readiness_verifier

    def take(
        self,
        *,
        channel_id: str,
        operator_id: str,
        operator_name: str | None = None,
        reason: str | None = None,
        path_id: str | None = None,
        duration_seconds: float = 3600.0,
    ) -> TakeoverSession:
        """Begin a live takeover. Raises AlreadyLiveError (409) if one is open,
        TakeoverNotReadyError (422) if the channel is missing/disabled or no
        ready live source can be prepared."""
        session = self.prepare_take(
            channel_id=channel_id,
            operator_id=operator_id,
            operator_name=operator_name,
            reason=reason,
            path_id=path_id,
            duration_seconds=duration_seconds,
        )
        now = session.took_over_at
        token = session.session_id.removeprefix("takeover-")
        stored = self._audit.append(session)
        # Queue the engine command AFTER the durable session exists, so the undo
        # trail is never lost (mirrors the commit gate's persist-before-dispatch).
        self._egress.enqueue_command(
            EgressCommand(
                channel_id=channel_id,
                action="takeover",
                issued_at=now,
                issued_by=operator_id,
                command_id=f"takeover-{token}",
            )
        )
        return stored

    def prepare_take(
        self,
        *,
        channel_id: str,
        operator_id: str,
        operator_name: str | None = None,
        reason: str | None = None,
        path_id: str | None = None,
        duration_seconds: float = 3600.0,
    ) -> TakeoverSession:
        """Validate and build a takeover without persisting or dispatching it.

        Live Room commits this plan together with its session transition and
        engine command, so a failed transition cannot leave an orphaned cut.
        """
        config = self._egress.get_config(channel_id)
        if config is None:
            raise TakeoverNotReadyError(f"Channel {channel_id!r} has no configured egress channel.")
        if not config.enabled:
            raise TakeoverNotReadyError(
                f"Channel {channel_id!r} is disabled and cannot be taken live."
            )

        if self._audit.get_active(channel_id) is not None:
            raise AlreadyLiveError(f"Channel {channel_id!r} is already under live takeover.")

        ingest_plan = self._ingest_plan_provider(channel_id)
        try:
            plan = build_live_takeover_source_plan(
                channel_id=channel_id,
                ingest_plan=ingest_plan,
                path_id=path_id,
                duration_seconds=duration_seconds,
            )
        except (SourcePrepareError, ValueError) as exc:
            raise TakeoverNotReadyError(str(exc)) from exc

        segment = plan.segments[0]
        # WP-07: revalidate BEFORE any durable side effect. Nothing below this
        # point is undoable from the operator's seat -- the audit row is a
        # public record of a takeover that happened, and the queued command
        # will move air the moment the daemon picks it up. A source that went
        # stale, failed, or was edited between the ingest plan the operator
        # saw and this request must produce neither.
        if self._readiness_verifier is not None and segment.source_ref:
            selected = _selected_path(ingest_plan, segment.source_ref)
            verdict = self._readiness_verifier(
                channel_id,
                segment.source_ref,
                selected.endpoint_url if selected is not None else "",
            )
            if not verdict.ok:
                raise TakeoverNotReadyError(verdict.reason)
            if verdict.secret_ref:
                # A handle, never a secret: this plan is serialized into the
                # durable takeover audit row and into the engine's graph file.
                segment = segment.model_copy(update={"secret_ref": verdict.secret_ref})
                plan = plan.model_copy(update={"segments": [segment, *plan.segments[1:]]})

        now = self._clock()
        token = self._id_factory()
        return TakeoverSession(
            session_id=f"takeover-{token}",
            channel_id=channel_id,
            source_ref=segment.source_ref or path_id or ingest_plan.recommended_path_id,
            source_label=segment.label,
            operator_id=operator_id,
            operator_name=operator_name,
            reason=reason,
            took_over_at=now,
            returned_at=None,
            source_plan_json=plan.model_dump_json(),
            notes=None,
        )

    def handback(
        self,
        *,
        channel_id: str,
        operator_id: str,
        notes: str | None = None,
    ) -> TakeoverSession:
        """Return a channel from takeover to its scheduled source. Raises
        NotInTakeoverError (404) if the channel is not currently live."""
        active = self._audit.get_active(channel_id)
        if active is None:
            raise NotInTakeoverError(f"Channel {channel_id!r} is not under live takeover.")

        now = self._clock()
        token = self._id_factory()
        self._egress.enqueue_command(
            EgressCommand(
                channel_id=channel_id,
                action="handback",
                issued_at=now,
                issued_by=operator_id,
                command_id=f"handback-{token}",
            )
        )
        closed = self._audit.close(active.session_id, returned_at=now, notes=notes)
        # close() can only return None if the row vanished between get_active and
        # close (vanishingly unlikely); fall back to the observed active session.
        return closed if closed is not None else active

    def state(self, channel_id: str) -> ManualRouteState:
        """Return the channel's manual-route state (active session + can flags)."""
        active = self._audit.get_active(channel_id)
        ready = False
        if active is None:
            # Only probe live readiness when not already live (cheap + relevant).
            try:
                ready = _live_source_ready(self._ingest_plan_provider(channel_id))
            except Exception:  # readiness is best-effort; never fail the state read
                ready = False
        return ManualRouteState(
            channel_id=channel_id,
            active_session=active,
            can_takeover=active is None and ready,
            can_return=active is not None,
        )

    def audit(self, channel_id: str, *, limit: int = 50) -> list[TakeoverSession]:
        """Return the channel's takeover history (most recent first)."""
        return self._audit.list_by_channel(channel_id, limit=limit)
