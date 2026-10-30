# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Playout supervisor contract for the egress data-plane process."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from civiccast.egress.cg_bridge import EgressCgOverlayProof
from civiccast.egress.daemon import CgOverlayProofProvider, EgressDaemon, SourcePlanProvider
from civiccast.egress.errors import ConfigInvalidError, SourcePrepareError
from civiccast.egress.models import EgressCommand, EgressSourcePlan, TakeoverSession

_LOG = logging.getLogger(__name__)

LookAheadSourcePlanProvider = Callable[[str, int], Sequence[EgressSourcePlan]]


class TakeoverSessionReader(Protocol):
    """The read surface the supervisor needs to consume a takeover command:
    the channel's open (``returned_at IS NULL``) session, whose
    ``source_plan_json`` is the live plan to force on air."""

    def get_active(self, channel_id: str) -> TakeoverSession | None: ...


class PlayoutSupervisor(EgressDaemon):
    """Production playout supervisor for the egress data-plane process.

    The supervisor owns source selection separately from encoder ownership so
    the runtime can keep upcoming sources ready before a boundary arrives.
    """

    def __init__(
        self,
        *args: Any,
        source_plan_provider: SourcePlanProvider,
        lookahead_source_plan_provider: LookAheadSourcePlanProvider | None = None,
        lookahead_window: int = 2,
        takeover_audit_store: TakeoverSessionReader | None = None,
        **kwargs: Any,
    ) -> None:
        if lookahead_window < 1:
            raise ValueError("lookahead_window must be at least 1")
        cg_overlay_proof_provider = kwargs.pop("cg_overlay_proof_provider", None)
        self._upstream_source_plan_provider = source_plan_provider
        self._lookahead_source_plan_provider = lookahead_source_plan_provider
        self._upstream_cg_overlay_proof_provider: CgOverlayProofProvider | None = (
            cg_overlay_proof_provider
        )
        self._lookahead_window = lookahead_window
        # S5: source of the open takeover session whose plan a "takeover"
        # command forces live. None → the supervisor logs + no-ops takeover
        # commands (it cannot force a plan it can't read).
        self._takeover_audit_store = takeover_audit_store
        self._source_lookahead: dict[str, list[EgressSourcePlan]] = {}
        self._live_takeover_plans: dict[str, EgressSourcePlan] = {}
        self._forced_slate_reasons: dict[str, str] = {}
        self._supervisor_cg_overlay_proofs: dict[str, EgressCgOverlayProof] = {}
        super().__init__(
            *args,
            source_plan_provider=self._next_source_plan,
            cg_overlay_proof_provider=self._cg_overlay_proof_for_channel,
            **kwargs,
        )

    def _process_command(self, command: EgressCommand) -> None:
        """Consume S5 takeover/handback commands; delegate the rest to the daemon.

        The takeover command carries no payload — the live plan is read from the
        channel's open takeover session (persisted by the API before the command
        was queued). Calling ``request_live_takeover`` sets the live plan as the
        active source and drives the swap/reload; the as-aired proof is emitted
        by the engine's source-change path when it actually swaps.
        """
        if command.action == "takeover":
            config = self._store.get_config(command.channel_id)
            if config is not None and not config.enabled:
                _LOG.info(
                    "channel %s: ignoring queued takeover because the channel is disabled",
                    command.channel_id,
                )
                return
            self._consume_takeover(command.channel_id)
            return
        if command.action == "handback":
            config = self._store.get_config(command.channel_id)
            if config is not None and not config.enabled:
                _LOG.info(
                    "channel %s: ignoring queued handback because the channel is disabled",
                    command.channel_id,
                )
                return
            self.request_live_handback(channel_id=command.channel_id)
            return
        super()._process_command(command)

    def _consume_takeover(self, channel_id: str) -> None:
        if self._takeover_audit_store is None:
            _LOG.error(
                "Takeover command for %s but no takeover audit store is wired; "
                "cannot read the live source plan. Ignoring.",
                channel_id,
            )
            return
        session = self._takeover_audit_store.get_active(channel_id)
        if session is None:
            _LOG.warning(
                "Takeover command for %s but no open takeover session exists; "
                "nothing to put live. Ignoring.",
                channel_id,
            )
            return
        try:
            plan = EgressSourcePlan.model_validate_json(session.source_plan_json)
        except ValueError:
            _LOG.exception(
                "Takeover session %s for %s has an unreadable source plan; ignoring.",
                session.session_id,
                channel_id,
            )
            return
        self._operator_stopped_channels.discard(channel_id)
        self.request_live_takeover(channel_id=channel_id, live_source_plan=plan)

    def request_live_takeover(
        self,
        *,
        channel_id: str,
        live_source_plan: EgressSourcePlan,
    ) -> None:
        """Make a live source the active source at the next handoff boundary."""

        _validate_live_takeover_plan(channel_id=channel_id, plan=live_source_plan)
        self._forced_slate_reasons.pop(channel_id, None)
        self._live_takeover_plans[channel_id] = live_source_plan
        self._source_lookahead.pop(channel_id, None)
        # A live takeover changes the PROGRAM-LEG CONTENT (scheduled -> live), which
        # is a seamless content-reload, not a selector pad toggle: CivicCast airs a
        # single pre-switched live feed (S16 delegates production switching to the
        # external switcher), so there is no always-hot 'live' pad. _request_reload
        # resolves _next_source_plan -> the live plan just set above and rebuilds the
        # program leg in place (0-CC, D-S1-6); on the ffmpeg path it is the existing
        # terminate+restart reload.
        self._request_reload(channel_id)

    def request_live_handback(self, *, channel_id: str) -> None:
        """Return from live takeover to the scheduled look-ahead source."""

        self._live_takeover_plans.pop(channel_id, None)
        self._source_lookahead.pop(channel_id, None)
        # Symmetric to takeover: clearing the live plan makes _next_source_plan
        # resolve the scheduled source again, and a content-reload rebuilds the
        # program leg back to scheduled content seamlessly (the slate pad is
        # untouched). Not a pad toggle — the program-leg content changes.
        # End is the live source's boundary. An open contribution feed has no
        # natural EOS to await, even when the schedule currently resolves empty.
        self._request_reload(channel_id, handoff_at=datetime.now(UTC))

    def request_fallback_slate(
        self,
        *,
        channel_id: str,
        reason: str,
    ) -> None:
        """Fall to the configured slate source at the next handoff boundary."""

        if not reason:
            raise ValueError("reason is required for fallback slate.")
        self._forced_slate_reasons[channel_id] = reason
        self._live_takeover_plans.pop(channel_id, None)
        self._source_lookahead.pop(channel_id, None)
        self._reload_or_swap(channel_id, "slate")

    def request_slate_exit(self, *, channel_id: str) -> None:
        """Exit forced fallback slate and return to scheduled look-ahead."""

        self._forced_slate_reasons.pop(channel_id, None)
        self._source_lookahead.pop(channel_id, None)
        self._reload_or_swap(channel_id, "program")

    def has_fallback_slate_reason(self, *, channel_id: str, reason: str) -> bool:
        """Whether the current forced slate is the one a caller requested."""

        return self._forced_slate_reasons.get(channel_id) == reason

    def clear_fallback_slate(self, *, channel_id: str, reason: str) -> bool:
        """Drop a matching forced-slate request without switching output.

        Terminal stop/drain intent uses this to discard a pending automatic
        recovery slate. The reason guard prevents cleanup from removing a newer
        operator-requested override.
        """

        if not self.has_fallback_slate_reason(channel_id=channel_id, reason=reason):
            return False
        self._forced_slate_reasons.pop(channel_id, None)
        self._source_lookahead.pop(channel_id, None)
        return True

    def _reload_or_swap(self, channel_id: str, role: str) -> None:
        """Toggle between the two always-hot legs (``program`` pad 0, ``slate``
        pad 1) via an in-place selector swap when the encoder strategy supports it
        (the GStreamer engine), else the terminate+restart reload (the ffmpeg path).

        Used for the slate detour (``request_fallback_slate`` / ``request_slate_exit``),
        where the program-leg *content* is unchanged. Live takeover/handback do NOT
        come through here — they change the program-leg content, so they drive a
        seamless content-reload (``_request_reload``) instead of a pad toggle."""
        strategy = getattr(self, "_encoder_strategy", None)
        if strategy is not None and getattr(strategy, "supports_live_swap", False):
            strategy.swap_role(channel_id, self._work_dir, role)
        else:
            self._request_reload(channel_id)

    def raise_cg_emergency_overlay(self, *, proof: EgressCgOverlayProof) -> None:
        """Raise an emergency CG overlay proof through the egress evidence path."""

        self._supervisor_cg_overlay_proofs[proof.channel_id] = proof
        self._sync_cg_overlay_now(proof.channel_id)

    def clear_cg_emergency_overlay(self, *, channel_id: str) -> None:
        """Clear a supervisor-managed emergency CG overlay proof."""

        self._supervisor_cg_overlay_proofs.pop(channel_id, None)
        self._sync_cg_overlay_now(channel_id)

    def has_manual_override(self, channel_id: str) -> bool:
        """B1 fix: True while an operator live takeover or forced fallback slate is
        active for this channel.

        Neither writes a state-row transition the automation's plan-rollover pass
        (``ChannelAutomationService._check_plan_rollover``) could otherwise key off:
        a live takeover's content-reload keeps the state row ON_AIR under the new
        (live) content (``request_live_takeover`` -> ``_request_reload`` ->
        ``_try_content_reload`` writes ON_AIR, same as any other reload), and a
        forced slate detour on the GStreamer path is a bare ``swap_role`` pad
        toggle that writes NO state row at all (``_reload_or_swap`` above). Without
        this signal, the rollover pass reads a stale/misleading ON_AIR row and
        fights the operator: reloading the SCHEDULED plan over a live takeover
        forces a mid-event SRT/NDI reconnect, and reloading over a forced slate
        (whose ``_next_source_plan`` raises ``SourcePrepareError`` below) latches a
        ``daemon._pending_reloads`` entry that silently drops the operator's slate
        once it resolves. Overrides ``EgressDaemon.has_manual_override`` (always
        False there -- the base daemon has no notion of either)."""

        return channel_id in self._live_takeover_plans or channel_id in self._forced_slate_reasons

    def _next_source_plan(self, channel_id: str) -> EgressSourcePlan | None:
        forced_slate_reason = self._forced_slate_reasons.get(channel_id)
        if forced_slate_reason is not None:
            raise SourcePrepareError(forced_slate_reason)
        live_plan = self._live_takeover_plans.get(channel_id)
        if live_plan is not None:
            return live_plan
        queue = self._source_lookahead.setdefault(channel_id, [])
        if not queue:
            self._refresh_source_lookahead(channel_id)
            queue = self._source_lookahead.setdefault(channel_id, [])
        if not queue:
            return None
        return queue.pop(0)

    def _refresh_source_lookahead(self, channel_id: str) -> None:
        if self._lookahead_source_plan_provider is None:
            plan = self._upstream_source_plan_provider(channel_id)
            self._source_lookahead[channel_id] = [] if plan is None else [plan]
            return
        plans = tuple(self._lookahead_source_plan_provider(channel_id, self._lookahead_window))
        combined_plan = _combine_lookahead_plans(
            channel_id=channel_id,
            plans=plans[: self._lookahead_window],
        )
        self._source_lookahead[channel_id] = [] if combined_plan is None else [combined_plan]

    def _cg_overlay_proof_for_channel(self, channel_id: str) -> EgressCgOverlayProof | None:
        if channel_id in self._supervisor_cg_overlay_proofs:
            return self._supervisor_cg_overlay_proofs[channel_id]
        if self._upstream_cg_overlay_proof_provider is None:
            return None
        return self._upstream_cg_overlay_proof_provider(channel_id)

    def _sync_cg_overlay_now(self, channel_id: str) -> None:
        state = self._store.read_state(channel_id)
        if state is None:
            return
        self._sync_cg_overlay_proof(channel_id, state.state)


def _combine_lookahead_plans(
    *,
    channel_id: str,
    plans: Sequence[EgressSourcePlan],
) -> EgressSourcePlan | None:
    segments = []
    for plan in plans:
        if plan.channel_id != channel_id:
            raise ConfigInvalidError(
                f"Look-ahead source plan channel {plan.channel_id!r} does not match "
                f"requested channel {channel_id!r}."
            )
        segments.extend(plan.segments)
    if not segments:
        return None
    return EgressSourcePlan(channel_id=channel_id, segments=segments)


def _validate_live_takeover_plan(*, channel_id: str, plan: EgressSourcePlan) -> None:
    if plan.channel_id != channel_id:
        raise ConfigInvalidError(
            f"Live takeover source plan channel {plan.channel_id!r} does not match "
            f"requested channel {channel_id!r}."
        )
    if not plan.segments:
        raise ConfigInvalidError("Live takeover source plan must include at least one segment.")
    if plan.segments[0].kind != "live":
        raise ConfigInvalidError("Live takeover source plan must begin with a live segment.")


__all__ = ["LookAheadSourcePlanProvider", "PlayoutSupervisor"]
