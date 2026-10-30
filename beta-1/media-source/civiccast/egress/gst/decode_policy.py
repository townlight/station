# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Gi-free CPU-decode policy for the playout engine.

Kept out of ``engine.py`` so it is unit-testable without a GStreamer install --
``engine.py`` cannot be imported at all without ``gi``, which is exactly why the
stale name list below went unnoticed until a Gate A run.

Why the policy exists: decodebin autoplugs the highest-ranked decoder that
matches. A GPU decoder that REGISTERS on a machine with no working GPU
video-decode path (a VM, Windows Sandbox, a WARP/Basic-Render-Driver adapter)
prerolls and then delivers no buffers -- the pipeline reaches PLAYING, nothing
ever leaves the mux, and the engine's stall watchdog quits the worker ~10s later
with no bus ERROR to explain it. Keeping decode on the CPU by default makes
playout depend on nothing but the CPU unless an operator opts in.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Protocol

#: Names demoted through ``GST_PLUGIN_FEATURE_RANK``, which is read during registry
#: scan -- i.e. BEFORE any code of ours can look at the registry. This is the first
#: line of defence; ``demote_hardware_decoders`` below is the one that cannot go stale.
CPU_DECODE_FEATURE_RANK = ",".join(
    (
        "nvh264dec:0",
        "nvh265dec:0",
        "nvav1dec:0",
        "cudah264dec:0",
        "cudah265dec:0",
        "vaapih264dec:0",
        "vaapih265dec:0",
        "vah264dec:0",
        "vah265dec:0",
        "d3d11h264dec:0",
        "d3d11h265dec:0",
        "d3d11av1dec:0",
        "d3d11vp9dec:0",
        # Gate A T4 root cause (2026-09, kit e1acfe6): the shipped Windows runtime
        # bundles gstd3d12.dll, whose decoders register at rank 258 -- ABOVE d3d11's
        # 257 and above every software decoder (avdec_h264 is 256). The list named
        # only the d3d11/nv/va/cuda families, so on the shipped closure decodebin
        # still autoplugged `d3d12h264dec` and this whole policy was a no-op for
        # H.264. Measured against that kit's own GStreamer registry.
        "d3d12h264dec:0",
        "d3d12h265dec:0",
        "d3d12av1dec:0",
        "d3d12vp9dec:0",
    )
)

_DECODER_KLASS_PREFIX = "Codec/Decoder"
_HARDWARE_KLASS_MARKER = "/Hardware"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


class _Feature(Protocol):
    """The slice of ``Gst.PluginFeature`` this policy needs (kept gi-free)."""

    def get_name(self) -> str: ...
    def get_metadata(self, key: str) -> str: ...
    def get_rank(self) -> int: ...
    def set_rank(self, rank: int) -> None: ...


def hardware_decode_opt_in(environ: dict[str, str] | None = None) -> bool:
    """Has the operator opted into GPU decode for this process?"""
    env = os.environ if environ is None else environ
    return env.get("CIVICCAST_GST_ALLOW_HARDWARE_DECODE", "").strip().lower() in _TRUTHY


def prefer_cpu_decoders_by_default() -> None:
    """Publish the rank list into the environment before GStreamer scans its registry.

    ``setdefault``: an explicit ``GST_PLUGIN_FEATURE_RANK`` from the operator wins.
    """
    if hardware_decode_opt_in():
        return
    os.environ.setdefault("GST_PLUGIN_FEATURE_RANK", CPU_DECODE_FEATURE_RANK)


def is_hardware_decoder(klass: str) -> bool:
    """Is this factory ``klass`` metadata a hardware VIDEO/AUDIO decoder?

    Encoders are deliberately out of scope: encoder selection is the separate,
    explicit ``decide_encoder`` pre-flight (``encoder_probe.py``), which the
    operator's ``allow_software_fallback`` setting governs.
    """
    return klass.startswith(_DECODER_KLASS_PREFIX) and _HARDWARE_KLASS_MARKER in klass


def demote_hardware_decoders(features: Iterable[_Feature]) -> list[str]:
    """Set rank 0 on every registered hardware decoder; return the names demoted.

    This reads the registry that actually exists on THIS machine and demotes by
    klass metadata, so a bundled hardware decoder nobody added to
    ``CPU_DECODE_FEATURE_RANK`` can never win autoplug. A no-op when the operator
    has opted into hardware decode.
    """
    if hardware_decode_opt_in():
        return []
    demoted: list[str] = []
    for feature in features:
        if not is_hardware_decoder(feature.get_metadata("klass") or ""):
            continue
        if feature.get_rank() == 0:
            continue
        feature.set_rank(0)
        demoted.append(feature.get_name())
    return demoted
