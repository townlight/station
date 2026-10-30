# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S11b per-sink loudness resolution (parity decision 1).

The incumbent PEG workflow normalizes per destination from one show (cable -24 LKFS, streaming
-16 LUFS). Each :class:`~civiccast.egress.models.EgressSinkSpec` carries a
loudness regime; this module resolves every sink's *effective* target plus the
standard label to report, and whether that target differs from the channel's
conform baseline (so egress can normalize that sink's audio instead of copying
the shared program audio).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from civiccast.egress.models import (
    EgressConfig,
    EgressSinkKind,
    EgressSinkSpec,
    LoudnessRegime,
)

# Standard target per non-inherit regime (LUFS / LKFS — same ITU-R BS.1770 meter).
REGIME_DEFAULTS: dict[str, float] = {
    "streaming": -16.0,
    "atsc-a85": -24.0,
    "ebu-r128": -23.0,
}
# Standards label reported alongside the measurement per regime.
REGIME_STANDARD_LABEL: dict[str, str] = {
    "streaming": "Streaming -16 LUFS (ITU-R BS.1770)",
    "atsc-a85": "ATSC A/85 -24 LKFS (CALM Act)",
    "ebu-r128": "EBU R128 -23 LUFS",
}
# Short label for the operator UI chip.
REGIME_SHORT_LABEL: dict[str, str] = {
    "streaming": "Streaming -16",
    "atsc-a85": "Cable -24",
    "ebu-r128": "Broadcast -23",
    "inherit": "Inherit",
}
_GENERIC_STANDARD = "ITU-R BS.1770 / EBU R128"
_EPS = 1e-9


def standard_label_for_target(target_lufs: float) -> str:
    """Map a bare target to the matching regime's standard label, else a generic one."""
    for regime, default in REGIME_DEFAULTS.items():
        if abs(target_lufs - default) < _EPS:
            return REGIME_STANDARD_LABEL[regime]
    return f"{_GENERIC_STANDARD} (target {target_lufs:g} LUFS)"


class SinkLoudnessResolution(BaseModel):
    """The resolved loudness target + reporting metadata for one egress sink."""

    model_config = ConfigDict(extra="forbid")

    label: str
    kind: EgressSinkKind
    regime: LoudnessRegime
    effective_target_lufs: float
    tolerance_lufs: float
    standard_label: str
    short_label: str
    # True when the regime/target was chosen for this sink rather than inherited.
    explicit: bool
    # True when the sink's target differs from the channel conform baseline, so
    # egress must normalize this sink's audio rather than copy the program audio.
    requires_reencode: bool
    # S11c gap-B: strip the EAS attention tone (853/960 Hz) on this sink. Carried from
    # the sink spec; egress applies the notch only on OTT sinks (cable passes through).
    eas_tone_strip_enabled: bool = True


class ChannelLoudnessPlan(BaseModel):
    """Per-sink loudness plan for one channel (the GET loudness-plan response)."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str
    baseline_target_lufs: float
    baseline_tolerance_lufs: float
    sinks: list[SinkLoudnessResolution]


def resolve_sink_loudness(
    sink: EgressSinkSpec,
    *,
    channel_target_lufs: float,
    channel_tolerance_lufs: float,
) -> SinkLoudnessResolution:
    """Resolve one sink's effective loudness target, label, and re-encode need.

    ``inherit`` falls back to the channel target (today's behaviour); an explicit
    ``loudness_target_lufs`` always wins over the regime default.
    """
    regime: LoudnessRegime = sink.loudness_regime
    if regime == "inherit":
        if sink.loudness_target_lufs is not None:
            effective = sink.loudness_target_lufs
            standard = standard_label_for_target(effective)
            explicit = True
        else:
            effective = channel_target_lufs
            standard = standard_label_for_target(channel_target_lufs)
            explicit = False
    else:
        effective = (
            sink.loudness_target_lufs
            if sink.loudness_target_lufs is not None
            else REGIME_DEFAULTS[regime]
        )
        standard = REGIME_STANDARD_LABEL[regime]
        explicit = True
    tolerance = (
        sink.loudness_tolerance_lufs
        if sink.loudness_tolerance_lufs is not None
        else channel_tolerance_lufs
    )
    return SinkLoudnessResolution(
        label=sink.label,
        kind=sink.kind,
        regime=regime,
        effective_target_lufs=effective,
        tolerance_lufs=tolerance,
        standard_label=standard,
        short_label=REGIME_SHORT_LABEL[regime],
        explicit=explicit,
        requires_reencode=abs(effective - channel_target_lufs) > _EPS,
        eas_tone_strip_enabled=sink.eas_tone_strip_enabled,
    )


def build_loudness_plan(config: EgressConfig) -> ChannelLoudnessPlan:
    """Resolve the per-sink loudness plan for a channel's egress config."""
    return ChannelLoudnessPlan(
        channel_id=config.channel_id,
        baseline_target_lufs=config.loudness_target_lufs,
        baseline_tolerance_lufs=config.loudness_tolerance_lufs,
        sinks=[
            resolve_sink_loudness(
                sink,
                channel_target_lufs=config.loudness_target_lufs,
                channel_tolerance_lufs=config.loudness_tolerance_lufs,
            )
            for sink in config.sinks
        ],
    )
