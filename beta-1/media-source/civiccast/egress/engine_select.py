# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Select the egress encoder strategy (the GStreamer engine vs. ffmpeg-concat).

A deployment-level choice, via the ``CIVICCAST_EGRESS_ENGINE`` environment
variable (matching the project's other ``CIVICCAST_*`` flags). Default is the
GStreamer engine (S15) -- it alone fixes continuity bug #151 (the persistent
pipeline never restarts the mux on plan/reload boundaries) and is the engine
the native station bootstrap ships. Existing deployments that still need the
legacy ffmpeg concat path can opt back in with
``CIVICCAST_EGRESS_ENGINE=ffmpeg-concat``; that path -- and the
GStreamer -> FFmpeg -> slate degraded-mode fallback chain
(``civiccast.native.station_runtime`` / ``civiccast.egress.daemon``) -- is
unchanged by this default flip.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from civiccast.egress.encoder_strategy import ConcatEncoderStrategy, EncoderStrategy

_DEFAULT = "gstreamer"
_FFMPEG_ALIASES = {"ffmpeg-concat", "ffmpeg", "concat"}
_GSTREAMER_ALIASES = {"gstreamer", "gst"}


def selected_engine_name(engine: str | None = None) -> str:
    """The normalized engine name (explicit arg, else ``CIVICCAST_EGRESS_ENGINE``,
    else ``_DEFAULT``). A blank/whitespace-only env value is treated the same as an
    unset one -- both resolve to ``_DEFAULT`` -- so an empty
    ``CIVICCAST_EGRESS_ENGINE=`` in a deployment's env file never silently pins the
    OTHER engine just because it happens to be a member of ``_FFMPEG_ALIASES``."""
    raw = engine or os.environ.get("CIVICCAST_EGRESS_ENGINE") or _DEFAULT
    return raw.strip().lower() or _DEFAULT


def gstreamer_engine_selected(engine: str | None = None) -> bool:
    """True when the GStreamer engine is selected (it alone muxes secondary audio PIDs
    and embeds CEA-708). Lets surfaces avoid advertising capabilities the ffmpeg engine
    cannot emit (e.g. a SAP audio-track toggle)."""
    return selected_engine_name(engine) in _GSTREAMER_ALIASES


def build_encoder_strategy(
    engine: str | None = None,
    *,
    audio_tracks_provider: Callable[[str], list[Any]] | None = None,
    emergency_provider: Callable[[str], Any] | None = None,
) -> EncoderStrategy:
    """Build the configured ``EncoderStrategy``.

    Anything unset, or ``CIVICCAST_EGRESS_ENGINE=gstreamer`` (default) → the
    GStreamer engine (a per-channel worker process); ``ffmpeg-concat`` → the
    legacy ffmpeg concat strategy. Raises on an unrecognized value rather than
    silently falling back, so a typo is caught. ``audio_tracks_provider`` (S11
    gap 9) is passed to the gst engine so it can mux a channel's secondary
    audio (SAP/descriptive) PIDs; ignored on the ffmpeg path.
    """
    name = selected_engine_name(engine)
    if name in _GSTREAMER_ALIASES:
        # Imported lazily so the ffmpeg path never pulls in the gst package.
        from civiccast.egress.gst.strategy import GstPlayoutStrategy

        return GstPlayoutStrategy(
            audio_tracks_provider=audio_tracks_provider, emergency_provider=emergency_provider
        )
    if name in _FFMPEG_ALIASES:
        return ConcatEncoderStrategy()
    raise ValueError(
        f"unknown CIVICCAST_EGRESS_ENGINE={name!r} — use 'gstreamer' (default) or 'ffmpeg-concat'"
    )
