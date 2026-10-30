# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Production server-side media probe for the live go-on-air pre-flight.

Bug B2: ``civiccast.live.preflight.PreflightEvaluator``'s ``live_source``
check requires a ``source_probe`` callable (see that module's docstring
and ``SourceProbe`` type alias). Without one, the check fails CLOSED with
``REASON_LIVE_SOURCE_NOT_PROBED`` -- correct behavior for an evaluator
that must never silently claim media is arriving when nobody checked.
But the only ``source_probe`` implementation anywhere in this tree was
``civiccast.installer.service._probe_sample_rehearsal_media``, wired
*only* into the installer's private System Health rehearsal, which
validates a local recorded sample file, not a live feed. ``civiccast.
app._resolve_preflight_evaluator`` -- the constructor used by every real
station's running service -- built ``PreflightEvaluator(_session_factory)``
with no probe at all. Net effect: a correctly configured RTMP/RTSP/SRT/NDI
source on a real station could never pass the live_source check, so
go-on-air always 409'd from the product UI (spec §12 station-acceptance:
"schedules a day and commits to air; interrupts with live and returns
safely" -- unreachable without this).

This module is that real probe. It asks ``ffprobe`` to open the
configured :class:`~civiccast.live.models.LiveSource`'s endpoint and
confirms CivicCast can actually see a video or audio stream before the
station commits to air -- the same "does media actually arrive" contract
the rehearsal probe already enforces for its local sample file, extended
to the live network sources the product ships.

Bounded timeout is load-bearing: :func:`probe_live_source` runs inline in
the ``POST /go-on-air`` HTTP request path
(``civiccast.live.router.go_on_air`` -> ``PreflightEvaluator.evaluate``).
An encoder that never answers must not hang that request forever, so the
subprocess is killed at ``timeout_seconds`` wall-clock regardless of what
the target protocol's own timeout options do or don't honor.
``CIVICCAST_LIVE_SOURCE_PROBE_TIMEOUT_SECONDS`` overrides the default
(read by :func:`build_source_probe`, not by :func:`probe_live_source`
itself, which always requires an explicit value from its caller).

Credentials (WP-07 -- this paragraph replaces the "not resolved here"
limitation this docstring used to carry). ``LiveSource.credentials_handle``
is now resolved **at probe time** through :mod:`civiccast.live.secrets`, the
station's OS credential store, by an injected resolver -- the same seam
:mod:`civiccast.ai_models.dispatch` and ``civiccast.egress.gst.bridge`` use.
Only SRT can carry one; see
:data:`civiccast.live.source_endpoints.CREDENTIAL_SUPPORTED_SOURCE_TYPES` for
why RTSP and RTMP authenticated shapes are rejected at validation instead
(neither FFmpeg protocol accepts a username/password anywhere except inside
the URL, and this product will not build a URL with a password in it). The
SRT passphrase is handed to ffprobe as the libsrt protocol option
``-passphrase`` -- never appended to the endpoint URL, never written to a row,
never logged, and stripped from every operator-facing detail string by
:func:`civiccast.live.secrets.redact_secrets` before it leaves this module. A
handle that resolves to nothing fails the probe closed
(``credentials_unresolved``) rather than quietly probing unauthenticated.

Residual exposure, stated rather than glossed: the passphrase is one argv
element of a short-lived child process, so it is visible to another process
running as the same Windows user for the duration of the probe. That is the
narrowest channel FFmpeg offers; the alternative (a query parameter on the
URL) would additionally reach logs, ingest plans, and proof output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from civiccast.live.secrets import SecretResolver, load_live_source_secret, redact_secrets
from civiccast.live.source_endpoints import credential_support_reason, supports_credentials

if TYPE_CHECKING:
    from civiccast.live.models import LiveSource

__all__ = [
    "DEFAULT_SOURCE_PROBE_TIMEOUT_SECONDS",
    "PROBE_ERROR_CODES",
    "ProbeObservation",
    "SourceProbeFn",
    "build_source_probe",
    "observe_live_source",
    "probe_live_source",
]

SourceProbeFn = Callable[["LiveSource"], "tuple[bool, str | None]"]

# Stable machine codes persisted in ``live_sources.probe_error_code``. The UI
# branches on these; the prose in ``probe_detail`` is for the human and may be
# reworded without breaking a consumer.
ERROR_FFPROBE_MISSING: Final = "ffprobe_missing"
ERROR_UNSUPPORTED_SHAPE: Final = "unsupported_source_shape"
ERROR_TIMEOUT: Final = "probe_timeout"
ERROR_START_FAILED: Final = "ffprobe_start_failed"
ERROR_PROBE_REFUSED: Final = "probe_refused"
ERROR_UNREADABLE_OUTPUT: Final = "unreadable_output"
ERROR_NO_MEDIA_STREAMS: Final = "no_media_streams"
ERROR_CREDENTIALS_UNRESOLVED: Final = "credentials_unresolved"
ERROR_CREDENTIALS_UNSUPPORTED: Final = "credentials_unsupported"

PROBE_ERROR_CODES: Final[tuple[str, ...]] = (
    ERROR_FFPROBE_MISSING,
    ERROR_UNSUPPORTED_SHAPE,
    ERROR_TIMEOUT,
    ERROR_START_FAILED,
    ERROR_PROBE_REFUSED,
    ERROR_UNREADABLE_OUTPUT,
    ERROR_NO_MEDIA_STREAMS,
    ERROR_CREDENTIALS_UNRESOLVED,
    ERROR_CREDENTIALS_UNSUPPORTED,
)


@dataclass(frozen=True)
class ProbeObservation:
    """One probe's outcome, in the shape the durable row wants.

    ``detail`` is already truncated and secret-redacted; it is safe to
    persist, to return in an API response, and to put in front of an operator.
    """

    ok: bool
    detail: str
    error_code: str | None = None


_FFPROBE_EXECUTABLE = "ffprobe"
_NDI_DEMUXER = "libndi_newtek"

#: Wall-clock ceiling for a single probe when the caller (or the
#: ``CIVICCAST_LIVE_SOURCE_PROBE_TIMEOUT_SECONDS`` env var) doesn't
#: specify one. Long enough for a real encoder to hand back its first
#: frames over SRT/RTMP on a LAN; short enough that an operator staring
#: at the "Go on air" button gets an answer, not a hang.
DEFAULT_SOURCE_PROBE_TIMEOUT_SECONDS = 8.0

# Bounds ffprobe's own stream analysis so a source that DOES connect but
# never stabilizes (e.g. a stalled encoder handshake) can't consume the
# whole timeout budget just forming its answer. Belt-and-suspenders with
# the subprocess-level timeout below, which is the authoritative bound.
_PROBE_ANALYZEDURATION_US = "5000000"  # 5s of analysis
_PROBE_PROBESIZE_BYTES = "5000000"  # 5 MB

# Truncate ffprobe stderr in the operator-facing message -- enough to be
# actionable ("Connection refused", "401 Unauthorized"), not enough to
# dump a raw protocol trace into a 409 body.
_MAX_DETAIL_CHARS = 400


def _timeout_from_env(default: float) -> float:
    raw = os.environ.get("CIVICCAST_LIVE_SOURCE_PROBE_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _describe(source: LiveSource) -> str:
    return f"{source.source_type} source {source.name!r} ({source.live_source_id})"


def _build_probe_command(
    source: LiveSource,
    *,
    credential_args: tuple[str, ...] = (),
) -> list[str] | None:
    """Return the full ffprobe argv for ``source``, or ``None`` if this
    source type/endpoint shape has no probe path yet (fails closed).

    ``credential_args`` are protocol options (currently only SRT's
    ``-passphrase <value>``) and are placed BEFORE ``-i``, which is where
    FFmpeg reads per-protocol AVOptions. They are never merged into the
    endpoint URL.
    """
    endpoint = source.endpoint_url.strip()
    if not endpoint:
        return None

    base = [
        _FFPROBE_EXECUTABLE,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        "-analyzeduration",
        _PROBE_ANALYZEDURATION_US,
        "-probesize",
        _PROBE_PROBESIZE_BYTES,
        *credential_args,
    ]

    if source.source_type == "ndi":
        name = endpoint.removeprefix("ndi://").strip()
        if not name:
            return None
        return [*base, "-f", _NDI_DEMUXER, "-i", name]

    if source.source_type in ("rtmp", "rtsp", "srt"):
        return [*base, "-i", endpoint]

    return None


def _resolve_credentials(
    source: LiveSource,
    resolve_secret: SecretResolver | None,
) -> tuple[tuple[str, ...], tuple[str, ...], ProbeObservation | None]:
    """Resolve ``source.credentials_handle`` into ffprobe protocol options.

    Returns ``(argv_fragment, secret_values, failure)``. ``failure`` is a
    ready-made failed observation when the credential cannot be honoured --
    fail closed, because probing unauthenticated and reporting "ready" would
    be a readiness claim about a stream the station cannot actually open.
    ``secret_values`` is handed to the redactor so no resolved secret can
    survive into a detail string.
    """
    handle = (getattr(source, "credentials_handle", None) or "").strip()
    if not handle:
        return (), (), None

    label = _describe(source)
    if not supports_credentials(source.source_type):
        # Validation rejects this shape at write time; a row that predates the
        # rule (or one written straight into the DB) still must not be probed
        # as if the credential were being applied.
        return (
            (),
            (),
            ProbeObservation(
                ok=False,
                detail=f"Cannot check {label}: {credential_support_reason(source.source_type)}",
                error_code=ERROR_CREDENTIALS_UNSUPPORTED,
            ),
        )

    secret: str | None = None
    if resolve_secret is not None:
        try:
            secret = resolve_secret(handle)
        except Exception:
            # The handle, not the secret, is all that can be named here.
            secret = None
    if not secret:
        return (
            (),
            (),
            ProbeObservation(
                ok=False,
                detail=(
                    f"Cannot check {label}: the stored credential {handle!r} is not in this "
                    "station's credential store. Re-enter the SRT passphrase for this source, "
                    "then check it again."
                ),
                error_code=ERROR_CREDENTIALS_UNRESOLVED,
            ),
        )
    return ("-passphrase", secret), (secret,), None


def observe_live_source(
    source: LiveSource,
    *,
    timeout_seconds: float,
    resolve_secret: SecretResolver | None = None,
) -> ProbeObservation:
    """Probe ``source`` and return a persistable, redacted observation.

    Never raises for probe-outcome reasons. Every branch produces a stable
    ``error_code`` so the operator UI can branch on the cause, and a ``detail``
    sentence that names the source and says what to do about it.
    """
    label = _describe(source)

    if shutil.which(_FFPROBE_EXECUTABLE) is None:
        return ProbeObservation(
            ok=False,
            detail=(
                f"Cannot check {label}: ffprobe is not installed on this station. "
                "Install ffmpeg (which ships ffprobe) and verify with 'civiccast doctor'."
            ),
            error_code=ERROR_FFPROBE_MISSING,
        )

    credential_args, secret_values, failure = _resolve_credentials(source, resolve_secret)
    if failure is not None:
        return failure

    cmd = _build_probe_command(source, credential_args=credential_args)
    if cmd is None:
        return ProbeObservation(
            ok=False,
            detail=(
                f"Cannot check {label}: source type {source.source_type!r} with endpoint "
                f"{source.endpoint_url!r} is not recognized by the server-side probe."
            ),
            error_code=ERROR_UNSUPPORTED_SHAPE,
        )

    def _safe(text: str) -> str:
        return redact_secrets(text, secret_values)

    try:
        completed = subprocess.run(  # noqa: S603 -- fixed executable name, no shell, argv built above
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return ProbeObservation(
            ok=False,
            detail=_safe(
                f"{label} did not respond within {timeout_seconds:.0f}s. Confirm the encoder is "
                f"powered on and reachable at {source.endpoint_url!r}, then check it again."
            ),
            error_code=ERROR_TIMEOUT,
        )
    except OSError as exc:
        return ProbeObservation(
            ok=False,
            detail=_safe(f"Could not start ffprobe to check {label}: {exc}"),
            error_code=ERROR_START_FAILED,
        )

    if completed.returncode != 0:
        raw = (completed.stderr or completed.stdout or "").strip()[-_MAX_DETAIL_CHARS:]
        return ProbeObservation(
            ok=False,
            detail=_safe(
                f"{label} did not respond to a server-side media probe"
                + (f": {raw}" if raw else ".")
            ),
            error_code=ERROR_PROBE_REFUSED,
        )

    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return ProbeObservation(
            ok=False,
            detail=f"{label} probe returned unreadable output; treating it as unavailable.",
            error_code=ERROR_UNREADABLE_OUTPUT,
        )

    streams = data.get("streams") if isinstance(data, dict) else None
    streams = streams if isinstance(streams, list) else []
    has_video = any(isinstance(s, dict) and s.get("codec_type") == "video" for s in streams)
    has_audio = any(isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams)

    if not has_video and not has_audio:
        return ProbeObservation(
            ok=False,
            detail=f"{label} answered the probe but sent no video or audio stream.",
            error_code=ERROR_NO_MEDIA_STREAMS,
        )

    kinds = " + ".join(
        kind for kind, present in (("video", has_video), ("audio", has_audio)) if present
    )
    return ProbeObservation(
        ok=True,
        detail=f"{label} is delivering {kinds}; server-side media probe passed.",
    )


def probe_live_source(
    source: LiveSource,
    *,
    timeout_seconds: float,
    resolve_secret: SecretResolver | None = None,
) -> tuple[bool, str | None]:
    """Probe whether ``source`` is currently delivering media.

    The ``(ok, message)`` shape ``civiccast.live.preflight.SourceProbe``
    expects. :func:`observe_live_source` is the same probe with the error code
    kept; this wrapper is what pre-flight and the installer rehearsal path use.
    """
    observation = observe_live_source(
        source, timeout_seconds=timeout_seconds, resolve_secret=resolve_secret
    )
    return observation.ok, observation.detail


def build_source_probe(
    *,
    timeout_seconds: float | None = None,
    resolve_secret: SecretResolver | None = None,
) -> SourceProbeFn:
    """Build the ``SourceProbe`` callable ``PreflightEvaluator`` expects.

    ``civiccast.app._resolve_preflight_evaluator`` calls this with no
    arguments so every real station gets a real probe;
    ``CIVICCAST_LIVE_SOURCE_PROBE_TIMEOUT_SECONDS`` (default
    :data:`DEFAULT_SOURCE_PROBE_TIMEOUT_SECONDS`) tunes it without a code
    change. Tests and the installer's rehearsal path pass their own probe
    (or ``source_probe_override``) instead of calling this.

    ``resolve_secret`` defaults to the station's OS credential store
    (:func:`civiccast.live.secrets.load_live_source_secret`) -- the same
    "bind the real resolver at the wiring layer, never inside the adapter"
    posture ``civiccast.ai_models.dispatch`` uses. The resolver is CALLED per
    probe, not at build time, so rotating a passphrase in the credential store
    takes effect on the next check without restarting the service.
    """
    resolved_timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else _timeout_from_env(DEFAULT_SOURCE_PROBE_TIMEOUT_SECONDS)
    )
    resolver = resolve_secret if resolve_secret is not None else load_live_source_secret

    def _probe(source: LiveSource) -> tuple[bool, str | None]:
        return probe_live_source(source, timeout_seconds=resolved_timeout, resolve_secret=resolver)

    return _probe
