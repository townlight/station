# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""EAS display decisions + render mapping (S11c).

Turns a normalized alert into an on-channel display decision and an
``EmergencyOverlay`` for the EXISTING CG/overlay render path (no parallel renderer).
Policy (decision 3): crawl/overlay can auto-surface for severe+ alerts; a
``forced_slate`` (full-screen pre-emption) ALWAYS requires an operator confirmation —
CivicCast never auto-preempts and is never an EAS device. ``active_emergency_overlay``
is what the public ``/api/public/cg/emergency-overlay`` endpoint reads (the formerly
mock overlay, now driven by real ingested alerts), labeled emergency info, never "EAS".
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from civiccast.cg.models import EmergencyOverlay
from civiccast.eas.models import (
    SEVERITY_RANK,
    EasCapAlert,
    EasDisplayDecision,
    EasDisplayMode,
    EasSeverity,
    severity_at_or_above,
)
from civiccast.eas.store import AlertNotFoundError, EasStore

Clock = Callable[[], datetime]
OverlaySeverity = Literal["watch", "warning", "emergency"]

# Alerts at/above this floor auto-surface (crawl/overlay). Below it, display is
# operator-initiated only.
AUTO_SURFACE_FLOOR: EasSeverity = "severe"

# CAP severity → the resident player's EmergencyOverlay severity band.
_OVERLAY_SEVERITY: dict[str, OverlaySeverity] = {
    "extreme": "emergency",
    "severe": "warning",
    "moderate": "watch",
    "minor": "watch",
    "unknown": "watch",
}


class EasDisplayError(RuntimeError):
    """Raised when a display request is not permitted (e.g. unconfirmed forced slate)."""


def overlay_severity_for(cap_severity: str) -> OverlaySeverity:
    return _OVERLAY_SEVERITY.get(cap_severity, "watch")


def recommended_mode(alert: EasCapAlert) -> EasDisplayMode:
    """The auto-surface display mode for an alert (never ``forced_slate`` — that needs
    a human). Extreme → overlay; severe → crawl."""
    return "overlay" if alert.severity == "extreme" else "crawl"


def alert_to_emergency_overlay(alert: EasCapAlert, *, overlay_id: str) -> EmergencyOverlay:
    """Map a normalized alert to the existing EmergencyOverlay render contract."""
    title = (alert.headline or alert.event or "Public safety alert")[:160]
    message = alert.headline or alert.description or alert.event
    instructions = alert.instruction or "Monitor local authorities for more information."
    return EmergencyOverlay(
        overlay_id=overlay_id,
        severity=overlay_severity_for(alert.severity),
        title=title,
        message=message,
        instructions=instructions,
        cellular_fallback_enabled=False,
        aria_live="assertive",
    )


class EasDisplayService:
    """Create/clear display decisions and resolve the active overlay for a channel."""

    def __init__(self, store: EasStore, *, clock: Clock = lambda: datetime.now(UTC)) -> None:
        self._store = store
        self._clock = clock

    @staticmethod
    def _overlay_id(channel_id: str, alert_id: str) -> str:
        return f"eas-{channel_id}-{alert_id}"[:120]

    @staticmethod
    def _decision_id(channel_id: str, alert_id: str) -> str:
        return f"eas-decision-{channel_id}-{alert_id}"[:160]

    def surface_alert(
        self,
        *,
        channel_id: str,
        alert_id: str,
        mode: EasDisplayMode,
        decided_by: str,
        operator_confirmed: bool = False,
    ) -> EasDisplayDecision:
        """Display an alert on a channel. A ``forced_slate`` requires
        ``operator_confirmed`` AND a non-auto operator (decision 3 — no auto pre-empt)."""
        alert = self._store.get_alert(alert_id)
        if alert is None:
            raise AlertNotFoundError(f"EAS alert {alert_id!r} not found")
        if mode == "forced_slate" and (not operator_confirmed or decided_by == "auto"):
            raise EasDisplayError(
                "A forced full-screen slate must be confirmed by an operator; "
                "CivicCast never auto-preempts (it is not an EAS device)."
            )
        now = self._clock()
        decision = EasDisplayDecision(
            decision_id=self._decision_id(channel_id, alert_id),
            alert_id=alert_id,
            channel_id=channel_id,
            mode=mode,
            state="displayed",
            decided_by=decided_by,
            auto_surfaced=decided_by == "auto",
            overlay_id=self._overlay_id(channel_id, alert_id),
            reason=f"{alert.event} ({alert.severity})"[:500],
            displayed_at=now,
            expires_at=alert.expires,
        )
        return self._store.upsert_decision(decision)

    def auto_surface_active(self, *, channel_id: str) -> list[EasDisplayDecision]:
        """Auto-surface (crawl/overlay) every active severe+ alert not already shown
        on this channel. Never creates a forced slate. Idempotent per (channel, alert)."""
        # An operator clear is authoritative until a new CAP alert/update arrives.
        shown = {d.alert_id for d in self._store.list_decisions(channel_id=channel_id)}
        decisions: list[EasDisplayDecision] = []
        for alert in self._store.list_alerts(active_only=True):
            if alert.alert_id in shown:
                continue
            if not severity_at_or_above(alert.severity, AUTO_SURFACE_FLOOR):
                continue
            decisions.append(
                self.surface_alert(
                    channel_id=channel_id,
                    alert_id=alert.alert_id,
                    mode=recommended_mode(alert),
                    decided_by="auto",
                )
            )
        return decisions

    def clear_decision(self, decision_id: str) -> EasDisplayDecision:
        """Clear a displayed alert (operator action, or when its alert expires)."""
        return self._store.set_decision_state(decision_id, "cleared", cleared_at=self._clock())

    def active_emergency_overlay(self, channel_id: str) -> EmergencyOverlay | None:
        """The highest-severity active+displayed alert's overlay for ``channel_id``.

        This is what the public emergency-overlay endpoint renders. Returns None when
        nothing is being displayed (the player shows no banner). Decisions whose alert
        is no longer active (expired/superseded/cancelled) are skipped."""
        presentation = self.active_presentation(channel_id)
        return presentation[1] if presentation is not None else None

    def active_presentation(
        self, channel_id: str
    ) -> tuple[EasDisplayMode, EmergencyOverlay] | None:
        """Resolve mode and real content; expiration does not depend on the poll loop."""
        best_alert: EasCapAlert | None = None
        best_decision: EasDisplayDecision | None = None
        now = self._clock()

        def utc(value: datetime) -> datetime:
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

        for decision in self._store.list_decisions(channel_id=channel_id, state="displayed"):
            alert = self._store.get_alert(decision.alert_id)
            if alert is None or alert.status != "active":
                continue
            if alert.expires is not None and utc(alert.expires) <= utc(now):
                continue
            if alert.effective is not None and utc(alert.effective) > utc(now):
                continue
            if best_alert is None or SEVERITY_RANK.get(alert.severity, 0) > SEVERITY_RANK.get(
                best_alert.severity, 0
            ):
                best_alert = alert
                best_decision = decision
        if best_alert is None:
            return None
        assert best_decision is not None
        return best_decision.mode, alert_to_emergency_overlay(
            best_alert, overlay_id=self._overlay_id(channel_id, best_alert.alert_id)
        )
