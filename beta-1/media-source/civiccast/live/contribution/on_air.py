# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Legacy channel-takeover callback retained for older callers.

Current application wiring supplies ``GuestProgramRouter.on_air`` to
``ContributionService.put_on_air``. That path composes guest media and performs
take/handback. This older helper only requests takeover of an already configured
channel source; it does not construct the current guest compositor.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from civiccast.egress.takeover_service import AlreadyLiveError
from civiccast.live.contribution.models import ContributionRoom, RemoteGuestSession

_LOG = logging.getLogger(__name__)

# Legacy callback type for callers that already provide their composed source.
ChannelGoLive = Callable[[str], None]


def build_contribution_on_air_hook(
    take_live: ChannelGoLive | None,
) -> Callable[[RemoteGuestSession, ContributionRoom], None]:
    """Build the legacy callback; the app uses GuestProgramRouter instead."""

    def _hook(session: RemoteGuestSession, room: ContributionRoom) -> None:
        if take_live is None:
            _LOG.info(
                "Guest %s on-air in room %s (channel %s); no engine takeover wired "
                "— the operator airs the composited feed.",
                session.session_id,
                room.room_id,
                room.channel_id,
            )
            return
        try:
            take_live(room.channel_id)
        except AlreadyLiveError:
            _LOG.info(
                "Channel %s is already live; guest %s joins the existing composition.",
                room.channel_id,
                session.session_id,
            )

    return _hook


__all__ = ["ChannelGoLive", "build_contribution_on_air_hook"]
