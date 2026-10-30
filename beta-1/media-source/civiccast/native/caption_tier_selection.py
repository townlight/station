# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The runtime "which caption tier do I load" seam (WP1 adaptive-tier).

The owner ruling requires tier selection to be **explicit, logged, and
provable**, with no silent substitution: serving a lower tier than the
hardware qualifies for -- or any deviation from the selection policy -- is a
release-blocking defect.

The actual capacity-based selection POLICY (measuring hardware, deciding
large-v3 is safe vs. must fall back) is a LATER slice. This module provides
only the seam it will plug into: a pure function that takes what the (future)
policy already decided plus what is actually available on disk, and turns
that into either a tier to load or a fail-closed refusal -- logging every
branch so a silent swap can never happen unnoticed.

``station_runtime`` and the native model-provisioning code are the intended
callers once the capacity policy exists; this module has no filesystem or
process I/O of its own so it is provable without either.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

__all__ = [
    "CAPTIONED_DEGRADATION_EVENT",
    "TIER_SELECTED_EVENT",
    "TierSelectionDecision",
    "TierSelectionError",
    "TierSelectionResult",
    "select_caption_tier",
]

#: Log event name for an ordinary, requested-tier-available selection.
TIER_SELECTED_EVENT: Final[str] = "caption_tier_selected"
#: Log event name for a fallback to the floor tier. Named distinctly (not
#: just "fallback") so it can be alerted on and shown to operators as a
#: captioned-degradation event, per the owner ruling's anti-silent-swap
#: requirement.
CAPTIONED_DEGRADATION_EVENT: Final[str] = "captioned_degradation"


class TierSelectionError(RuntimeError):
    """No caption tier can be safely loaded under the given selection input.

    Raised when the mandatory floor tier is unavailable, or when the
    requested tier is unavailable and the selection policy did not
    explicitly authorize a floor-tier fallback. Fail closed: this function
    never silently substitutes a tier the caller did not ask for and did not
    authorize.
    """


@dataclass(frozen=True)
class TierSelectionDecision:
    """The external selection policy's input for one resolution.

    ``requested_tier_id`` is whatever the (future) capacity-based policy
    decided should be loaded -- this module does not compute it.
    ``allow_floor_fallback`` is that policy's explicit permission to
    substitute the floor tier if the requested tier is missing/corrupt;
    without it, an unavailable requested tier is a hard refusal, never a
    silent substitution. ``reason`` is free-text policy context carried into
    the log event.
    """

    requested_tier_id: str
    allow_floor_fallback: bool
    reason: str


@dataclass(frozen=True)
class TierSelectionResult:
    """The resolved tier to load, with the exact event to log."""

    tier_id: str
    fell_back_to_floor: bool
    log_event: dict[str, object]


def select_caption_tier(
    *,
    available_tier_ids: Collection[str],
    floor_tier_id: str,
    decision: TierSelectionDecision,
) -> TierSelectionResult:
    """Resolve which caption tier to load -- fail closed, log every branch.

    * If ``floor_tier_id`` is not in ``available_tier_ids`` at all, refuse
      unconditionally: the mandatory CPU baseline cannot be missing (raises
      :class:`TierSelectionError`).
    * If the requested tier is available, select it -- this is the normal
      case (large-v3, or whatever the policy asked for).
    * If the requested tier is unavailable and the decision does not
      authorize a floor fallback, refuse loudly (never auto-substitute).
    * If the requested tier is unavailable and the decision DOES authorize a
      floor fallback, load the floor tier and return a
      :data:`CAPTIONED_DEGRADATION_EVENT` log event so the substitution is
      provable, not silent.
    """

    available = set(available_tier_ids)
    if floor_tier_id not in available:
        raise TierSelectionError(
            f"caption tier selection refused: mandatory floor tier "
            f"{floor_tier_id!r} is not available"
        )

    if decision.requested_tier_id in available:
        return TierSelectionResult(
            tier_id=decision.requested_tier_id,
            fell_back_to_floor=False,
            log_event={
                "event": TIER_SELECTED_EVENT,
                "tier": decision.requested_tier_id,
                "requested": decision.requested_tier_id,
                "fallback": False,
                "reason": decision.reason,
            },
        )

    if not decision.allow_floor_fallback:
        raise TierSelectionError(
            f"caption tier selection refused: requested tier "
            f"{decision.requested_tier_id!r} is unavailable and the selection "
            "policy did not authorize floor-tier fallback"
        )

    return TierSelectionResult(
        tier_id=floor_tier_id,
        fell_back_to_floor=True,
        log_event={
            "event": CAPTIONED_DEGRADATION_EVENT,
            "tier": floor_tier_id,
            "requested": decision.requested_tier_id,
            "fallback": True,
            "reason": decision.reason,
        },
    )
