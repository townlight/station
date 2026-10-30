# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Observed readiness for a configured live source.

The defect this module exists to close (audit ENG-003): a
:class:`~civiccast.live.models.LiveSource` row counted as ``ready`` merely
because it existed. ``civiccast.live.relay._source_path`` stamped
``health_state=RELAY_HEALTH_READY`` on every configured row, and that health
value is the *only* gate
``civiccast.egress.live_takeover.build_live_takeover_source_plan`` applies
before a manual takeover changes what is on air. A camera that had been
unplugged for a week, or an address with a typo in it, was indistinguishable
from a live encoder right up to the moment air went black.

Readiness here is an *observation*, persisted on the row
(``probe_state`` / ``probe_observed_at`` / ``probe_detail`` /
``probe_error_code`` / ``probe_last_success_at``, migration
``0086_live_source_probe_state``) and aged against a TTL. Four states, and the
distinction between the last two is the whole point:

``never_probed``
    Nobody has looked. Not the same as broken, and not the same as ready.
``ready``
    A probe saw media within the TTL.
``stale``
    A probe saw media, but longer ago than the TTL. The last answer is not
    evidence about now.
``failed``
    The most recent probe did not see media.

Only ``ready`` may take over air. ``stale`` is re-probed at takeover time
rather than refused outright (that is the operator's realistic path: open Run
Meeting, see a five-minute-old observation, press Take), but the re-probe is
what decides -- the stale observation never is.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final, Literal

__all__ = [
    "DEFAULT_READINESS_TTL_SECONDS",
    "MAX_READINESS_TTL_SECONDS",
    "MIN_READINESS_TTL_SECONDS",
    "PROBE_STATES",
    "PROBE_STATE_FAILED",
    "PROBE_STATE_NEVER_PROBED",
    "PROBE_STATE_READY",
    "READINESS_TTL_ENV_VAR",
    "ProbeStateValue",
    "ReadinessValue",
    "next_action_for",
    "observation_age_seconds",
    "readiness_state",
    "readiness_ttl_seconds",
]

PROBE_STATE_NEVER_PROBED: Final = "never_probed"
PROBE_STATE_READY: Final = "ready"
PROBE_STATE_FAILED: Final = "failed"

#: Persisted values of ``live_sources.probe_state``. ``stale`` is deliberately
#: NOT one of them: staleness is a function of the clock, not a fact anyone can
#: write down. Persisting it would create a row that stays "stale" forever
#: after a successful probe simply because nothing rewrote the column.
PROBE_STATES: Final[tuple[str, ...]] = (
    PROBE_STATE_NEVER_PROBED,
    PROBE_STATE_READY,
    PROBE_STATE_FAILED,
)

ProbeStateValue = Literal["never_probed", "ready", "failed"]
ReadinessValue = Literal["never_probed", "ready", "stale", "failed"]

READINESS_TTL_ENV_VAR: Final = "CIVICCAST_LIVE_SOURCE_READINESS_TTL_SECONDS"

#: How long a successful observation stands for. Thirty seconds is short
#: enough that "ready" means "ready now" during the minute before a meeting
#: gavels in, and long enough that an operator clicking through the Live Room
#: does not re-probe every encoder on every render.
DEFAULT_READINESS_TTL_SECONDS: Final = 30
MIN_READINESS_TTL_SECONDS: Final = 5
MAX_READINESS_TTL_SECONDS: Final = 300

#: How far ahead of "now" a persisted ``probe_observed_at`` may sit before
#: :func:`readiness_state` stops trusting it. Ordinary clock imprecision
#: between the process that stamped the row and the process reading it is a
#: few seconds at most; this tolerates that without arguing about it. Anything
#: past it is not skew, it is a clock that moved backwards (an NTP correction,
#: a VM snapshot restore, a manually-set system clock) -- see
#: ``readiness_state``'s future-observation guard below.
_FUTURE_OBSERVATION_TOLERANCE_SECONDS: Final = 5.0


def readiness_ttl_seconds(env: Mapping[str, str] | None = None) -> int:
    """Resolve the readiness TTL, clamped to the accepted 5-300s range.

    Out-of-range and unparseable values clamp/fall back rather than raising:
    this is read on the request path that renders the Live Room, and a
    mistyped env var must not take the operator's source list down. The
    clamped value is what every surface reports, so the UI and the takeover
    gate can never disagree about which TTL was applied.
    """
    raw = (env if env is not None else os.environ).get(READINESS_TTL_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_READINESS_TTL_SECONDS
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_READINESS_TTL_SECONDS
    return max(MIN_READINESS_TTL_SECONDS, min(MAX_READINESS_TTL_SECONDS, value))


def observation_age_seconds(
    observed_at: datetime | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Seconds since ``observed_at``, or ``None`` when nothing was observed.

    Clamped at zero: a row stamped by a clock slightly ahead of this process
    must read as "just now", never as a negative age the UI would render as a
    future observation.
    """
    if observed_at is None:
        return None
    moment = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        # SQLite round-trips DateTime(timezone=True) as naive UTC.
        observed_at = observed_at.replace(tzinfo=UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - observed_at).total_seconds())


def readiness_state(
    probe_state: str | None,
    observed_at: datetime | None,
    *,
    ttl_seconds: int,
    now: datetime | None = None,
) -> ReadinessValue:
    """Derive the operator-facing readiness state from the persisted row.

    Fails closed on anything it does not recognize: an unknown or missing
    ``probe_state``, or a ``ready`` row with no observation timestamp (which
    would mean a partially written row), reads as ``never_probed`` rather than
    as ready. A ``probe_observed_at`` timestamped meaningfully ahead of ``now``
    (a backwards clock correction after the row was stamped) reads as
    ``stale`` rather than ``ready``: :func:`observation_age_seconds` clamps a
    negative age to zero, which on its own would leave the row reading
    "just checked" for the entire span of the clock jump.
    """
    if probe_state == PROBE_STATE_FAILED:
        return "failed"
    if probe_state != PROBE_STATE_READY:
        return "never_probed"
    if observed_at is None:
        return "never_probed"
    moment = now or datetime.now(UTC)
    aware_observed_at = (
        observed_at if observed_at.tzinfo is not None else observed_at.replace(tzinfo=UTC)
    )
    aware_moment = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    raw_delta_seconds = (aware_moment - aware_observed_at).total_seconds()
    if raw_delta_seconds < -_FUTURE_OBSERVATION_TOLERANCE_SECONDS:
        return "stale"
    age = observation_age_seconds(observed_at, now=now)
    if age is None:
        return "never_probed"
    return "ready" if age <= ttl_seconds else "stale"


def next_action_for(
    readiness: ReadinessValue,
    *,
    source_name: str,
    detail: str | None = None,
) -> str:
    """The one thing the operator should do next, in plain words.

    Every state gets a concrete instruction. "Not ready" with no next step is
    the failure this product's UX rules call a bug, not a status.
    """
    if readiness == "ready":
        return f"{source_name} is delivering media. You can take air with it."
    if readiness == "stale":
        return (
            f"The last check of {source_name} is older than the readiness window. "
            "Choose Check source to confirm it is still delivering before you take air."
        )
    if readiness == "failed":
        remedy = detail.strip() if detail and detail.strip() else None
        return (
            f"{source_name} did not answer the last check"
            + (f": {remedy}" if remedy else ".")
            + " Fix the encoder or the address, then choose Check source."
        )
    return (
        f"{source_name} has never been checked. Choose Check source to confirm "
        "CivicCast can see it before you take air."
    )
