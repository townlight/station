# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Decide whether a GStreamer encoder factory is a real hardware encoder.

Hardware encoders (``mfh264enc``, ``mfh265enc``, ``nvh264enc``, ...) report a
factory ``klass`` metadata string containing ``"/Hardware"`` (e.g.
``"Codec/Encoder/Video/Hardware"``). Software encoders (``openh264enc``,
``x264enc``, ...) do not. An absent factory returns no metadata at all.

The decision function below is pure and gi-free so it can be unit-tested
without a GStreamer install; ``gst_factory_klass`` is a thin, lazily-imported
shim over the real registry for production use.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from civiccast.egress.errors import EncoderUnavailableError
from civiccast.egress.gst.bridge import gst_encoder_name


def hardware_encoder_available(factory_name: str, *, lookup: Callable[[str], str | None]) -> bool:
    """Return True if ``factory_name`` is a registered hardware encoder.

    ``lookup`` returns the factory's GStreamer "klass" metadata string, or
    None if the factory is not registered.
    """
    klass = lookup(factory_name)
    return klass is not None and "/Hardware" in klass


def gst_factory_klass(name: str) -> str | None:
    """Look up the real GStreamer factory's "klass" metadata string.

    Imports ``gi`` lazily so importing this module never requires GStreamer.
    The control plane probes before the worker's engine can bootstrap its own
    process, so initialize the same installed closure here as well.
    """
    from civiccast.native.gstreamer_runtime import bootstrap_installed_gstreamer_runtime

    bootstrap_installed_gstreamer_runtime()
    import gi  # type: ignore[import-not-found]

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # type: ignore[import-not-found]

    Gst.init(None)

    factory = Gst.ElementFactory.find(name)
    return None if factory is None else factory.get_metadata("klass") or ""


def probe_hardware_encoder(factory_name: str) -> bool:
    """Convenience wrapper: is ``factory_name`` a real hardware encoder on this machine?"""
    return hardware_encoder_available(factory_name, lookup=gst_factory_klass)


#: The CG board overlay element (S15 §5 CG-lite). gdk-pixbuf ships in
#: gst-plugins-good; unlike ``textoverlay`` it needs no pango, so it is the
#: pango-free board raster path the S15 constraint calls for.
CG_OVERLAY_ELEMENT = "gdkpixbufoverlay"


def element_registered(factory_name: str, *, lookup: Callable[[str], str | None]) -> bool:
    """Return True if ``factory_name`` exists in the GStreamer registry at all."""
    return lookup(factory_name) is not None


def probe_element_registered(factory_name: str) -> bool:
    """Convenience wrapper: is ``factory_name`` registered on this machine?"""
    return element_registered(factory_name, lookup=gst_factory_klass)


# Encoders that are already software (never gated on hardware presence).
_SOFTWARE_ENCODERS = frozenset({"openh264enc", "x264enc", "x265enc"})
# The bundled, non-GPL software H.264 encoder used when hardware is absent and the
# operator has opted into software fallback.
_SOFTWARE_FALLBACK_H264 = "openh264enc"

_REFUSE_H264 = (
    "No hardware video encoder was found on this machine. To broadcast on the CPU "
    "instead (slower), turn on 'Allow software (CPU) encoding fallback' in this "
    "channel's settings, then start the channel again. HEVC/H.265 needs hardware "
    "and is not available this way."
)
_REFUSE_HEVC = (
    "No hardware HEVC/H.265 encoder is available on this machine. HEVC requires a "
    "hardware encoder -- there is no software HEVC in this build. Switch this channel "
    "to H.264, or run it on a machine that has a hardware HEVC encoder."
)
_FALLBACK_WARNING = (
    "No hardware video encoder was found; 'allow_software_fallback' is on, so this "
    "channel is encoding on the CPU (openh264enc). This is slower and may not keep "
    "up with live on a weak CPU."
)


@dataclass(frozen=True)
class EncoderDecision:
    """Outcome of the native-Windows encoder pre-flight.

    ``encoder_override`` is a GStreamer factory name to force instead of the
    channel's configured encoder (None = use the configured one unchanged).
    ``warning`` is a prominent operator/log message when a degraded path is taken.
    """

    encoder_override: str | None
    warning: str | None


def decide_encoder(
    *,
    codec: str,
    is_windows: bool,
    allow_software_fallback: bool,
    probe: Callable[[str], bool],
) -> EncoderDecision:
    """Pure native-Windows encoder-selection policy (no gi, no platform read).

    ``codec`` is the channel's configured ffmpeg codec name (e.g. ``h264_vaapi``).
    Declarative ``h264`` automatically selects NVIDIA then Windows hardware;
    explicit encoder names retain their configured selection.
    ``probe`` reports whether a GStreamer factory is available hardware on this box.

    On non-Windows this is a no-op (the WSL/Linux path is byte-unchanged). On
    Windows: an encoder that resolves to present hardware proceeds (H.264 or HEVC --
    MF HEVC works via the NV12 input pinned in bridge._apply_encoder_fixups). When the
    hardware encoder is ABSENT, H.264 falls back to software (openh264enc) if opted in
    or is refused with remediation; HEVC is refused outright (no software HEVC ships).
    Raises ``EncoderUnavailableError`` on refusal.
    """
    if not is_windows:
        return EncoderDecision(encoder_override=None, warning=None)
    if codec.strip().lower() == "h264":
        # Declarative H.264 requests automatic selection. Explicit encoder names
        # continue to pin the operator's choice.
        for candidate in ("nvh264enc", "mfh264enc"):
            if probe(candidate):
                return EncoderDecision(encoder_override=candidate, warning=None)
        # Automatic H.264 previously selected software unconditionally. Preserve
        # those working CPU-only stations; the fallback opt-in applies to an
        # explicit hardware request, as before.
        return EncoderDecision(
            _SOFTWARE_FALLBACK_H264,
            "No hardware H.264 encoder was found; automatic H.264 is using the CPU (openh264enc).",
        )
    intended = gst_encoder_name(codec, is_windows=True)
    # Software encoders (openh264enc/x264enc/x265enc) are used unchanged and are NOT
    # subject to the hardware gate below. This MUST precede the "265" HEVC check:
    # x265enc contains "265" but is a CPU encoder with no Media Foundation/NV12
    # dependency, so it must not be swept into the hardware-HEVC refusal. On Windows we
    # pin the resolved factory explicitly so the built graph matches this decision
    # exactly, never a second independent resolution in the graph builder.
    if intended in _SOFTWARE_ENCODERS:
        return EncoderDecision(encoder_override=intended, warning=None)
    if probe(intended):
        # Hardware encoder present (H.264 OR HEVC). MF HEVC now works via the NV12 input
        # pinned in bridge._apply_encoder_fixups, so HEVC is no longer refused up-front.
        return EncoderDecision(encoder_override=intended, warning=None)
    # Configured hardware encoder is ABSENT on this machine:
    if "265" in intended:
        # No software HEVC ships in this build, so HEVC cannot fall back -- refuse.
        raise EncoderUnavailableError(_REFUSE_HEVC)
    if allow_software_fallback:
        return EncoderDecision(encoder_override=_SOFTWARE_FALLBACK_H264, warning=_FALLBACK_WARNING)
    raise EncoderUnavailableError(_REFUSE_H264)
