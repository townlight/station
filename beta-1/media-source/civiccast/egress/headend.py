# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Named cable-headend delivery profiles (cable automation CA-6).

Every number here is traceable to published vendor or standards
documentation (each profile lists its sources); none of it is tailored to
any one station. The operator supplies only what their carriage agreement
dictates: the destination address/port and, where the agreement sets one,
the constant multiplex rate.

How the pieces map onto the existing egress pipeline:

- Codec, resolution, GOP, and audio land on :class:`CanonicalProfile` —
  sources are conformed to it at prepare time, so the persistent encoder
  can stream-copy.
- The constant multiplex rate (what cable plants actually require — TelVue
  documents that the *mux* must be constant, not the video elementary
  stream) rides the sink's allowlisted ``-muxrate`` extra arg; the mpegts
  muxer null-pads to that rate even over ``-c copy``.
- Datagram sizing (1316 = 7 x 188-byte TS packets) is owned by the
  ``udp-ts`` sink.

One preset is not a cable delivery at all: ``local-rehearsal-hls`` adds an
``hls`` sink (served by ``civiccast.stream.media_router`` at
``/media/live/{channel_id}/playlist.m3u8``) so a first-time operator can watch
the channel in the resident portal with no headend and no CDN. It only adds the
sink — it never rewrites the channel's canonical encode profile or loudness
target, so applying it after a cable preset leaves the cable feed untouched.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from civiccast.egress.loudness_plan import REGIME_DEFAULTS
from civiccast.egress.models import (
    CanonicalProfile,
    EgressConfig,
    EgressSinkSpec,
    LoudnessRegime,
)

HeadendTransport = Literal["udp-unicast", "udp-multicast", "file-drop", "local-hls"]

_FIELD_PROOF_BOUNDARY = (
    "Built from published vendor documentation; not field-proven against a "
    "real cable headend until the first-station beta."
)

_HEADEND_SINK_LABEL = "Cable headend"
LOCAL_HLS_PROFILE_ID = "local-rehearsal-hls"
LOCAL_HLS_SINK_LABEL = "Web preview (HLS)"
# Directory name under the egress work dir that holds each channel's rolling
# live-HLS window when the operator leaves the preset's folder blank.
LOCAL_HLS_WORK_SUBDIR = "live-hls"
# Optional explicit root for local-HLS output folders. When set, every
# ``local-rehearsal-hls`` destination must live under it (and the blank-
# destination default becomes ``<root>/<channel>``); when unset the station's
# egress work dir is the root. See ``local_hls_root``.
LOCAL_HLS_ROOT_ENV = "CIVICCAST_LIVE_HLS_ROOT"


def local_hls_root() -> Path:
    """The one directory a local-HLS preview folder is allowed to live under.

    ``/media/live/{channel}/...`` is a public, unauthenticated file server
    that serves whatever directory the channel's ``hls`` sink names, so the
    preset that writes that sink must not accept an arbitrary path (review
    round 2, MAJOR 4: a ``C:\\`` or UNC destination would have exposed that
    tree to every resident). ``CIVICCAST_LIVE_HLS_ROOT`` names an explicit
    root when a deployer wants the preview somewhere else; otherwise the root
    is the station's egress work dir (``CIVICCAST_EGRESS_WORK_DIR``, else
    ``%LOCALAPPDATA%\\CivicCast\\egress``) -- the folder the daemon already
    owns. Imported lazily: ``egress.automation`` pulls the whole daemon in,
    which this registry module must not do at import time.
    """
    configured = os.environ.get(LOCAL_HLS_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    from civiccast.egress.automation import default_egress_work_dir

    return default_egress_work_dir().expanduser()


def default_local_hls_directory(channel_id: str) -> str:
    """Default manifest + segment folder for a channel's local HLS preview.

    ``<egress work dir>/live-hls/<channel>`` -- the same egress work dir the
    daemon already resolves for its plans, prepared segments and slates, so the
    daemon (which writes it via ``egress.hls_relay`` / ``egress.sinks.HlsSink``)
    and the API process (which serves it via ``stream.media_router``) agree on
    one absolute path without the operator typing one. With an explicit
    ``CIVICCAST_LIVE_HLS_ROOT`` the default is ``<root>/<channel>`` instead, so
    the blank-destination path always satisfies :func:`local_hls_root`'s
    containment rule.
    """
    if os.environ.get(LOCAL_HLS_ROOT_ENV, "").strip():
        return str(local_hls_root() / channel_id)
    return str(local_hls_root() / LOCAL_HLS_WORK_SUBDIR / channel_id)


class HeadendProfile(BaseModel):
    """One named, citable cable-delivery preset."""

    model_config = ConfigDict(extra="forbid")

    profile_id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    vendor: Annotated[str, Field(min_length=1, max_length=120)]
    source_urls: list[str]
    canonical_profile: CanonicalProfile
    muxrate_kbps: Annotated[int, Field(ge=0)]
    transport: HeadendTransport
    # S11b: the cable loudness regime a station normalises to for this delivery
    # (ATSC A/85 -24 LKFS, CALM Act). Applied to the headend sink + the channel
    # target at apply time so the conform produces a -24 LKFS program.
    recommended_loudness_regime: LoudnessRegime = "atsc-a85"
    pkt_size: Annotated[int, Field(gt=0)] = 1316
    min_port: Annotated[int, Field(ge=1, le=65_535)] = 1
    mpegts_extra_args: list[str] = Field(default_factory=list)
    operator_must_supply: list[str] = Field(default_factory=list)
    not_claimed: list[str] = Field(default_factory=lambda: [_FIELD_PROOF_BOUNDARY])


def _profiles() -> dict[str, HeadendProfile]:
    generic_operator_inputs = [
        "Destination address and UDP port from your cable operator or headend integrator.",
        "Constant multiplex rate (muxrate) if your carriage agreement sets one.",
    ]
    return {
        profile.profile_id: profile
        for profile in (
            HeadendProfile(
                profile_id="generic-udp-spts",
                label="Generic CBR SPTS over UDP",
                vendor="Any headend that ingests a constant-rate MPEG transport stream",
                source_urls=[
                    "https://telvue.com/knowledgebase/feed-setup-encoder-configuration/",
                    "https://www.pixeltools.com/tech_tip_cablelabs.html",
                ],
                canonical_profile=CanonicalProfile(
                    width=1280,
                    height=720,
                    fps=30,
                    video_codec="h264",
                    video_bitrate_kbps=5000,
                    gop_size=30,
                    audio_codec="ac3",
                    audio_bitrate_kbps=192,
                    audio_sample_rate=48_000,
                    audio_channels=2,
                ),
                muxrate_kbps=8000,
                transport="udp-unicast",
                operator_must_supply=generic_operator_inputs,
            ),
            HeadendProfile(
                profile_id="comcast-mtd-sd",
                label="Comcast MTD - SD (MPEG-2, CableLabs SD numbers)",
                vendor="Comcast Technology Solutions, Managed Terrestrial Distribution",
                source_urls=[
                    "https://www.comcasttechnologysolutions.com/managed-terrestrial-distribution",
                    "https://www.pixeltools.com/tech_tip_cablelabs.html",
                ],
                # CableLabs VOD SD: MPEG-2 MP@ML, aggregate SPTS (PAT+PMT+
                # video+one audio+data) <= 3.75 Mbps, GOP nominally 15 for
                # 30 fps material and closed to start, Dolby Digital 192 kbps
                # two-channel at 48 kHz.
                canonical_profile=CanonicalProfile(
                    width=720,
                    height=480,
                    fps=30,
                    video_codec="mpeg2video",
                    video_bitrate_kbps=3180,
                    gop_size=15,
                    audio_codec="ac3",
                    audio_bitrate_kbps=192,
                    audio_sample_rate=48_000,
                    audio_channels=2,
                ),
                muxrate_kbps=3750,
                transport="udp-multicast",
                operator_must_supply=[
                    "Multicast group address and UDP port assigned by Comcast.",
                ],
            ),
            HeadendProfile(
                profile_id="comcast-mtd-hd",
                label="Comcast MTD - HD (H.264)",
                vendor="Comcast Technology Solutions, Managed Terrestrial Distribution",
                source_urls=[
                    "https://www.comcasttechnologysolutions.com/managed-terrestrial-distribution",
                ],
                # Comcast MTD lists HD as MPEG-2 or MPEG-4; this preset ships
                # the MPEG-4/H.264 lane. The default muxrate is a placeholder
                # ceiling - the real rate comes from the carriage agreement.
                canonical_profile=CanonicalProfile(
                    width=1920,
                    height=1080,
                    fps=30,
                    video_codec="h264",
                    video_bitrate_kbps=10_000,
                    gop_size=30,
                    audio_codec="ac3",
                    audio_bitrate_kbps=384,
                    audio_sample_rate=48_000,
                    audio_channels=2,
                ),
                muxrate_kbps=12_000,
                transport="udp-multicast",
                operator_must_supply=[
                    "Multicast group address and UDP port assigned by Comcast.",
                    "Constant multiplex rate from your carriage agreement (the 12 Mbps default is a placeholder).",
                ],
            ),
            HeadendProfile(
                profile_id="telvue-hypercaster-ip",
                label="TelVue HyperCaster - IP transport stream input",
                vendor="TelVue HyperCaster",
                source_urls=[
                    "https://telvue.com/knowledgebase/feed-setup-encoder-configuration/",
                    "https://telvue.com/knowledgebase/preparing-content-for-the-hypercaster/",
                    "https://telvue.com/knowledgebase/configure-inout-ports/",
                ],
                # TelVue KB: TS over UDP, unicast or multicast (224.0.0.0 to
                # 239.255.255.255), IP port 1024-65535, constant multiplex
                # rate, MPEG-2 or H.264 video, MPEG-1 Layer II / AC-3 / AAC
                # audio.
                canonical_profile=CanonicalProfile(
                    width=1280,
                    height=720,
                    fps=30,
                    video_codec="h264",
                    video_bitrate_kbps=5000,
                    gop_size=30,
                    audio_codec="ac3",
                    audio_bitrate_kbps=192,
                    audio_sample_rate=48_000,
                    audio_channels=2,
                ),
                muxrate_kbps=8000,
                transport="udp-unicast",
                min_port=1024,
                operator_must_supply=[
                    "HyperCaster feed address and port (1024-65535) configured on the receiving feed.",
                    "Match the feed's Max Bit Rate to this muxrate.",
                ],
            ),
            HeadendProfile(
                profile_id="harmonic-spectrum-ts",
                label="Harmonic Spectrum - transport stream ingest",
                vendor="Harmonic Spectrum X / XE",
                source_urls=[
                    "https://www.harmonicinc.com/hubfs/datasheet/spectrum-x.pdf",
                    "https://www.harmonicinc.com/hubfs/datasheet/spectrum-xe.pdf",
                ],
                # Harmonic datasheets: TS ingest over IP, MPEG-2 / MPEG-4 AVC
                # (HEVC also listed), CBR encode supported.
                canonical_profile=CanonicalProfile(
                    width=1920,
                    height=1080,
                    fps=30,
                    video_codec="h264",
                    video_bitrate_kbps=8000,
                    gop_size=30,
                    audio_codec="ac3",
                    audio_bitrate_kbps=192,
                    audio_sample_rate=48_000,
                    audio_channels=2,
                ),
                muxrate_kbps=10_000,
                transport="udp-unicast",
                operator_must_supply=generic_operator_inputs,
            ),
            HeadendProfile(
                profile_id="leightronix-file-drop",
                label="Leightronix UltraNEXUS - file handoff",
                vendor="Leightronix UltraNEXUS-HD",
                source_urls=[
                    "https://www.leightronix.com/ultranexus-hd-series.html",
                    "https://support.leightronix.com/ultranexus-hd",
                ],
                # UltraNEXUS-HD decodes H.264 HD/SD and MPEG-2 SD from files;
                # delivery is a watched-folder/file handoff, so there is no
                # network mux rate to hold.
                canonical_profile=CanonicalProfile(
                    width=1280,
                    height=720,
                    fps=30,
                    video_codec="h264",
                    video_bitrate_kbps=8000,
                    gop_size=30,
                    audio_codec="ac3",
                    audio_bitrate_kbps=192,
                    audio_sample_rate=48_000,
                    audio_channels=2,
                ),
                muxrate_kbps=0,
                transport="file-drop",
                operator_must_supply=[
                    "Drop-folder path the UltraNEXUS ingests from.",
                ],
            ),
            HeadendProfile(
                profile_id=LOCAL_HLS_PROFILE_ID,
                label="Local rehearsal (web preview, HLS)",
                vendor="CivicCast resident portal (built-in HLS player, no headend)",
                source_urls=[
                    "https://datatracker.ietf.org/doc/html/rfc8216",
                    "https://ffmpeg.org/ffmpeg-formats.html#hls-2",
                ],
                # The hls sink re-encodes its own leg (HlsSink pins keyframes
                # to its 2 s segment cadence), so the canonical profile is left
                # alone at apply time; these are the module defaults, listed
                # so the preset card reads like the others.
                canonical_profile=CanonicalProfile(),
                muxrate_kbps=0,
                transport="local-hls",
                recommended_loudness_regime="streaming",
                operator_must_supply=[
                    "Nothing. Optionally a local folder for the manifest and segments; "
                    "blank uses the station's egress work folder (live-hls/<channel>). "
                    "A typed folder must be an absolute path under that work folder "
                    "(or under CIVICCAST_LIVE_HLS_ROOT): the resident portal serves it "
                    "publicly, so network (UNC), relative and elsewhere paths are refused.",
                ],
                not_claimed=[
                    "Web preview only: nothing here reaches a cable headend, and it is "
                    "not field-proven as a cable delivery.",
                    "Not a CDN. Serves this station's own /media/live/<channel>/playlist.m3u8; "
                    "the surge switch still hands heavy audiences to a CDN when configured.",
                ],
            ),
        )
    }


HEADEND_PROFILES: dict[str, HeadendProfile] = _profiles()


def list_headend_profiles() -> list[HeadendProfile]:
    """All named profiles, stable order."""

    return [HEADEND_PROFILES[key] for key in sorted(HEADEND_PROFILES)]


def get_headend_profile(profile_id: str) -> HeadendProfile | None:
    return HEADEND_PROFILES.get(profile_id)


def apply_headend_profile(
    config: EgressConfig,
    profile: HeadendProfile,
    *,
    destination_uri: str,
    muxrate_kbps_override: int | None = None,
    keep_existing_sinks: bool = False,
    label: str | None = None,
) -> EgressConfig:
    """Return a new config carrying the profile's encode + delivery sink.

    Validates the destination against the profile's transport before
    anything is persisted, so a typo'd address fails loudly instead of
    silently streaming into the void.

    ``label`` defaults to ``"Cable headend"`` for cable transports and
    ``"Web preview (HLS)"`` for ``local-hls``; with ``keep_existing_sinks`` the
    sink carrying that label (and, for ``local-hls``, any other ``hls`` sink —
    the media router serves one per channel) is replaced, the rest are kept.
    """

    if profile.transport == "local-hls":
        return _apply_local_hls_profile(
            config,
            profile,
            destination_uri=destination_uri,
            keep_existing_sinks=keep_existing_sinks,
            label=label or LOCAL_HLS_SINK_LABEL,
        )
    label = label or _HEADEND_SINK_LABEL
    _validate_destination(profile, destination_uri)
    muxrate_kbps = muxrate_kbps_override or profile.muxrate_kbps
    if profile.transport == "file-drop":
        sink = EgressSinkSpec(
            kind="file",
            label=label,
            uri=destination_uri,
            extra_output_args=list(profile.mpegts_extra_args),
            loudness_regime=profile.recommended_loudness_regime,
        )
    else:
        sink = EgressSinkSpec(
            kind="udp-ts",
            label=label,
            uri=destination_uri,
            extra_output_args=[
                "-muxrate",
                f"{muxrate_kbps}k",
                *profile.mpegts_extra_args,
            ],
            loudness_regime=profile.recommended_loudness_regime,
        )
    kept = (
        [existing for existing in config.sinks if existing.label != label]
        if keep_existing_sinks
        else []
    )
    # S11b parity decision 1: normalise the channel to the cable destination's
    # loudness (ATSC A/85 -24 LKFS). The headend sink then matches this baseline
    # (no per-sink re-encode needed, so channel branding still stream-copies),
    # while any kept streaming sink can still declare its own -16 LUFS regime.
    channel_target = REGIME_DEFAULTS.get(
        profile.recommended_loudness_regime, config.loudness_target_lufs
    )
    return config.model_copy(
        update={
            "canonical_profile": profile.canonical_profile.model_copy(),
            "loudness_target_lufs": channel_target,
            "sinks": [*kept, sink],
        }
    )


def _apply_local_hls_profile(
    config: EgressConfig,
    profile: HeadendProfile,
    *,
    destination_uri: str,
    keep_existing_sinks: bool,
    label: str,
) -> EgressConfig:
    """Add the local web-preview ``hls`` sink; leave encode + loudness alone."""

    directory = destination_uri.strip() or default_local_hls_directory(config.channel_id)
    _validate_destination(profile, directory)
    # Persist the contained, normalised absolute path (not the operator's raw
    # spelling): the daemon writes and the media router serves exactly this.
    directory = str(resolve_local_hls_directory(directory))
    sink = EgressSinkSpec(
        kind="hls",
        label=label,
        uri=directory,
        loudness_regime=profile.recommended_loudness_regime,
    )
    kept = (
        [
            existing
            for existing in config.sinks
            if existing.label != label and existing.kind != "hls"
        ]
        if keep_existing_sinks
        else []
    )
    return config.model_copy(update={"sinks": [*kept, sink]})


def _is_local_directory_uri(value: str) -> bool:
    """Shape check only: a directory path or ``file://`` uri, not a network address.

    Deliberately narrow. It answers "is this spelled like a local folder"; it
    does NOT decide whether the folder may be served. UNC spellings fail here
    too, but the containment decision (inside :func:`local_hls_root`, not
    relative, not traversing out) is :func:`resolve_local_hls_directory`, which
    ``_validate_destination`` runs right after this for every ``local-hls``
    destination.
    """
    if _is_unc(value):
        return False
    scheme = urlsplit(value).scheme.lower()
    if scheme in {"", "file"}:
        return True
    path = PureWindowsPath(value)
    return bool(path.drive and path.root)  # ``C:\...`` parses as scheme "c"


def _is_unc(value: str) -> bool:
    """True for ``\\\\server\\share``, ``//server/share`` and ``file://server/...``."""

    stripped = value.strip()
    if stripped.startswith(("\\\\", "//")):
        return True
    parsed = urlsplit(stripped)
    if parsed.scheme.lower() == "file" and parsed.netloc not in ("", "localhost"):
        return True
    # ``PureWindowsPath`` parses a UNC drive as ``\\\\server\\share``.
    return PureWindowsPath(stripped).drive.startswith("\\\\")


def _local_path_from_uri(value: str) -> str:
    """``file:///C:/x`` -> ``C:/x``; ``file:///srv/x`` -> ``/srv/x``; plain paths pass."""

    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() != "file":
        return value.strip()
    raw = unquote(parsed.path)
    if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]  # strip the leading slash off a Windows drive path
    return raw


# Successful resolutions are memoised for this long, keyed by the destination
# AND the two environment variables that decide the root. ``resolve()`` is a
# ``_getfinalpathname`` round trip per call (measured 194 us on the beta
# station class of box, against ~13 us for the playlist ``is_file`` check next
# to it), and round 3 put this call on the two public hot paths the surge
# switch exists to survive: ``/media/live/{channel}/{file}`` (every segment
# fetch) and ``/api/public/live/current`` (every resident's 4 s poll). Only
# successes are cached: a rejection is the rare operator-error path and must
# stay live so a corrected root or folder is seen at once. A junction swapped
# under a cached folder inside the TTL is still caught, because the media
# router re-resolves each FILE against the cached directory and 404s anything
# that leaves it (review round 3 delta, MINOR 6).
#
# Two residuals of that stale window are accepted rather than fixed (review
# round 4 delta, MINOR 5). Both are bounded by the TTL above:
#   - ``live/router.py`` can advertise a ``manifest_url`` for a playlist that
#     exists behind a repointed junction, and the media router then 404s it.
#     That is an INCONSISTENCY, not a disclosure -- no byte outside the root
#     is ever served -- and it self-corrects on the resident's next 4 s poll.
#   - the one memo consumer that WRITES, ``daemon._discard_stale_hls_
#     playlists``, is not allowed to tolerate it and passes ``use_cache=
#     False``: a write followed through a stale resolution would ``unlink``
#     outside the root, which no re-check downstream would catch.
_RESOLVE_CACHE_TTL_SECONDS = 5.0
# The memo key: the destination, then the two root environment variables in
# the order of ``_ROOT_ENV_NAMES``. Both the alias and the spelled-out build
# below are load-bearing for CI's ``mypy civiccast`` (review round 4 delta,
# CI note): a star-unpacked generator -- ``(destination_uri, *(os.environ...
# for name in _ROOT_ENV_NAMES))`` -- infers ``tuple[str, ...]``, which does
# not type-check against a fixed-width key, because the unpack throws the
# LENGTH away. Naming the two variables keeps the arity in the type.
_ResolveCacheKey = tuple[str, str, str]
_ROOT_ENV_NAMES: tuple[str, str] = (LOCAL_HLS_ROOT_ENV, "CIVICCAST_EGRESS_WORK_DIR")
_resolve_cache: dict[_ResolveCacheKey, tuple[float, Path]] = {}
_resolve_cache_lock = threading.Lock()


def clear_local_hls_resolution_cache() -> None:
    """Forget memoised resolutions (tests; a root moved under a live process)."""
    with _resolve_cache_lock:
        _resolve_cache.clear()


def resolve_local_hls_directory(destination_uri: str, *, use_cache: bool = True) -> Path:
    """Resolve a ``local-hls`` destination to an absolute path under the root.

    Raises ``ValueError`` (the apply endpoint maps it to 422) for a UNC path, a
    relative path, or an absolute path outside :func:`local_hls_root`. Every
    message starts with "local HLS folder" and says what is wrong, so the
    Channels screen's error line reads as an instruction, not a stack trace.
    ``/media/live/{channel}/...`` serves this folder to the public with no
    authentication, so this is the containment boundary for that file server.
    A successful resolution is memoised for ``_RESOLVE_CACHE_TTL_SECONDS``.

    ``use_cache=False`` re-resolves regardless of the memo (and refreshes it).
    The readers on the hot paths tolerate a stale memo because every file they
    serve is re-checked against the cached folder; a caller that WRITES on the
    strength of the resolution (``daemon._discard_stale_hls_playlists``) must
    not, because a folder swapped for a junction inside the TTL would steer
    the write outside the root (review round 4 delta, MINOR 5).
    """
    hls_root_env, work_dir_env = _ROOT_ENV_NAMES
    key: _ResolveCacheKey = (
        destination_uri,
        os.environ.get(hls_root_env, ""),
        os.environ.get(work_dir_env, ""),
    )
    now = time.monotonic()
    if use_cache:
        with _resolve_cache_lock:
            cached = _resolve_cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
    resolved = _resolve_local_hls_directory_uncached(destination_uri)
    with _resolve_cache_lock:
        _resolve_cache[key] = (now + _RESOLVE_CACHE_TTL_SECONDS, resolved)
    return resolved


def _resolve_local_hls_directory_uncached(destination_uri: str) -> Path:
    raw = destination_uri.strip()
    if not raw:
        raise ValueError("local HLS folder: the destination is blank")
    if _is_unc(raw):
        raise ValueError(
            "local HLS folder: a UNC/network path is not allowed; use a folder on "
            f"this station under {local_hls_root()}"
        )
    if not _is_local_directory_uri(raw):
        raise ValueError(
            "local HLS folder: the destination must be a directory path or file:// uri, "
            "not a network address"
        )
    candidate = Path(_local_path_from_uri(raw)).expanduser()
    if not candidate.is_absolute():
        raise ValueError(
            f"local HLS folder: {raw!r} is a relative path; use an absolute folder "
            f"under {local_hls_root()}"
        )
    root = local_hls_root().resolve()
    resolved = candidate.resolve()
    if resolved != root and not resolved.is_relative_to(root):
        raise ValueError(
            f"local HLS folder: {raw!r} is outside the allowed root {root}; the resident "
            "portal serves this folder publicly, so it must stay under the station's "
            f"egress work dir (or the {LOCAL_HLS_ROOT_ENV} root)"
        )
    return resolved


def _validate_destination(profile: HeadendProfile, destination_uri: str) -> None:
    parsed = urlsplit(destination_uri)
    scheme = parsed.scheme.lower()
    if profile.transport == "local-hls":
        # A UNC spelling is let through to resolve_local_hls_directory so the
        # operator reads the specific "UNC/network path is not allowed" line
        # rather than the generic shape complaint.
        if not _is_unc(destination_uri) and not _is_local_directory_uri(destination_uri):
            raise ValueError(
                f"profile {profile.profile_id} writes HLS to a local folder; "
                "the destination must be a directory path or file:// uri, not a network address"
            )
        resolve_local_hls_directory(destination_uri)
        return
    if profile.transport == "file-drop":
        if scheme not in {"", "file"}:
            raise ValueError(
                f"profile {profile.profile_id} delivers files; "
                "the destination must be a filesystem path or file:// uri"
            )
        return
    if scheme != "udp":
        raise ValueError(
            f"profile {profile.profile_id} streams over udp://; got {scheme or 'no scheme'!r}"
        )
    port = parsed.port
    if port is None:
        raise ValueError("destination must include an explicit UDP port")
    if port < profile.min_port:
        raise ValueError(
            f"profile {profile.profile_id} requires a destination port between "
            f"{profile.min_port} and 65535 (vendor-documented range)"
        )
    multicast = _is_multicast_host(parsed.hostname or "")
    if profile.transport == "udp-multicast" and not multicast:
        raise ValueError(
            f"profile {profile.profile_id} expects a multicast group (224.0.0.0 to 239.255.255.255)"
        )


def _is_multicast_host(host: str) -> bool:
    first_octet = host.split(".", 1)[0]
    return first_octet.isdigit() and 224 <= int(first_octet) <= 239
