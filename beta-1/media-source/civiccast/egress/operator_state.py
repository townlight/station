# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Truthful operator projections of persisted egress state."""

from __future__ import annotations

from collections.abc import Callable

from civiccast.egress.models import EgressStateRow


def operator_state_projection(
    state: EgressStateRow | None,
    *,
    pid_is_alive: Callable[[int], bool | None] | None = None,
) -> EgressStateRow | None:
    """Show a persisted PID only after confirming that it still exists.

    Store rows remain historical daemon observations.  This is deliberately an
    API projection, so daemon seams and their process doubles keep their exact
    stored semantics. An unavailable probe cannot verify a displayed PID.
    """
    if state is None or state.pid is None:
        return state
    if pid_is_alive is None:
        try:
            import psutil

            pid_is_alive = psutil.pid_exists
        except Exception:
            return state.model_copy(update={"pid": None})
    try:
        alive = pid_is_alive(state.pid)
    except Exception:
        return state.model_copy(update={"pid": None})
    return state if alive is True else state.model_copy(update={"pid": None})
