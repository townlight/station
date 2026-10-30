# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""One place that decides what endpoint shape each live source type accepts.

Before WP-07 the rule lived in exactly one caller --
``civiccast.installer.service._source_setup_endpoint``, which validates the
Setup wizard's ``kind`` (usb-hdmi / phone-app / encoder / ndi) rather than the
stored ``source_type``. The staff API's own
:class:`~civiccast.live.models.LiveSourceCreate` accepted ``HttpUrl | str`` and
checked only that ``source_type`` was one of the four literals, so
``POST /api/staff/live/sources`` happily stored ``source_type="srt"`` with an
``http://`` endpoint, an ``rtsp`` type pointed at an NDI name, or a URL with
``user:password@`` embedded in it. Nothing downstream re-checked: the probe's
``_build_probe_command`` fails closed on an unrecognized shape, but only after
the row had already been advertised to the operator as a configured source.

This module is the single rule. It is keyed on the persisted ``source_type``
(not on a wizard ``kind``) so create, update, and any future importer all get
the same answer, and it returns the *normalized* endpoint the store should
persist so two spellings of the same address cannot produce two different rows.

Credential capability is decided here too, because "which endpoint shapes are
legal" and "which of them may carry a credential handle" are the same question
asked twice -- see :data:`CREDENTIAL_SUPPORTED_SOURCE_TYPES` and
``docs/adr/0025-live-source-observed-readiness.md``.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "CREDENTIAL_SUPPORTED_SOURCE_TYPES",
    "CREDENTIAL_UNSUPPORTED_REASON",
    "SUPPORTED_SCHEMES_BY_SOURCE_TYPE",
    "EndpointValidationError",
    "credential_support_reason",
    "normalize_endpoint",
    "supports_credentials",
]

#: URL schemes each stored ``source_type`` accepts. ``ndi`` has no URL form --
#: it carries a bare NDI source name, optionally written ``ndi://<name>``.
#:
#: ``rtmps`` maps to ``rtmp`` and ``rtsps`` to ``rtsp`` deliberately: the
#: Setup wizard has always stored the TLS spellings under the plain
#: ``source_type`` (``civiccast.installer.service._source_setup_endpoint``
#: returns ``"rtmp"`` for an ``rtmps://`` address), and the schema's
#: ``live_sources_source_type_check`` CHECK constraint only allows the four
#: plain values. Rejecting ``rtmps`` here would have made every TLS source an
#: existing station already configured fail validation on its next edit.
SUPPORTED_SCHEMES_BY_SOURCE_TYPE: Final[dict[str, frozenset[str]]] = {
    "rtmp": frozenset({"rtmp", "rtmps"}),
    "rtsp": frozenset({"rtsp", "rtsps"}),
    "srt": frozenset({"srt"}),
    "ndi": frozenset(),
}

#: Source types whose authenticated shape CivicCast can actually execute
#: without exposing the secret.
#:
#: Only SRT qualifies. Its passphrase is a first-class option on both runtimes
#: this product drives -- FFmpeg's libsrt protocol option ``-passphrase`` and
#: GStreamer's ``srtsrc passphrase=`` property -- so the secret never has to be
#: interpolated into the endpoint URL that gets persisted, logged, returned in
#: an ingest plan, or written into proof output.
#:
#: RTSP and RTMP are deliberately excluded. Neither FFmpeg's RTSP demuxer nor
#: its RTMP protocol accepts a username/password anywhere except inside the
#: URL itself, so an authenticated RTSP/RTMP source could only be probed by
#: building ``rtsp://user:secret@host/...`` -- exactly the plaintext-in-a-URL
#: shape the station is forbidden to construct. (GStreamer's ``rtspsrc`` does
#: have ``user-id``/``user-pw`` properties, but a source CivicCast cannot
#: *probe* can never become observed-ready, so playout capability alone is not
#: enough.) NDI has no credential concept at all.
#:
#: Consequence, enforced in :func:`normalize_endpoint`'s caller and in the
#: operator UI: an rtsp/rtmp/ndi row may not carry a ``credentials_handle``,
#: and the UI's credential control for those types is disabled with the copy
#: in :data:`CREDENTIAL_UNSUPPORTED_REASON`.
CREDENTIAL_SUPPORTED_SOURCE_TYPES: Final[frozenset[str]] = frozenset({"srt"})

CREDENTIAL_UNSUPPORTED_REASON: Final[dict[str, str]] = {
    "rtmp": (
        "CivicCast cannot check an RTMP source that needs a username and password. "
        "The only way to send them is inside the address itself, and CivicCast will "
        "not store or run a password inside an address. Use an unauthenticated RTMP "
        "address on the station network, or use SRT with a passphrase instead."
    ),
    "rtsp": (
        "CivicCast cannot check an RTSP camera that needs a username and password. "
        "The only way to send them is inside the address itself, and CivicCast will "
        "not store or run a password inside an address. Give this camera a "
        "no-password stream on the station network, or use SRT with a passphrase "
        "instead."
    ),
    "ndi": (
        "NDI sources on the station network do not use a username, password, or "
        "passphrase. Leave the stored credential empty."
    ),
}

_MAX_ENDPOINT_CHARS: Final[int] = 2048


class EndpointValidationError(ValueError):
    """An endpoint does not match the shape its source type accepts.

    Carries an operator-readable message. Raised as a ``ValueError`` subclass
    so Pydantic field validators surface it as a 422 without extra plumbing.
    """


def supports_credentials(source_type: str) -> bool:
    """Whether ``source_type`` may carry a ``credentials_handle``."""
    return source_type in CREDENTIAL_SUPPORTED_SOURCE_TYPES


def credential_support_reason(source_type: str) -> str | None:
    """Operator copy explaining why ``source_type`` cannot take a credential.

    ``None`` for a source type that can.
    """
    if supports_credentials(source_type):
        return None
    return CREDENTIAL_UNSUPPORTED_REASON.get(
        source_type,
        "CivicCast cannot run a stored credential for this source type.",
    )


def normalize_endpoint(source_type: str, endpoint_url: str) -> str:
    """Return the canonical stored endpoint for ``source_type``.

    Raises :class:`EndpointValidationError` when the endpoint does not match
    the one shape this source type supports. The message is written for the
    operator staring at the form, not for a log line.
    """
    if source_type not in SUPPORTED_SCHEMES_BY_SOURCE_TYPE:
        raise EndpointValidationError(
            f"{source_type!r} is not a source type CivicCast supports. "
            "Choose RTMP, RTSP, NDI, or SRT."
        )

    trimmed = (endpoint_url or "").strip()
    if not trimmed:
        raise EndpointValidationError(
            "Enter the stream address (or the NDI source name) for this source."
        )
    if len(trimmed) > _MAX_ENDPOINT_CHARS:
        raise EndpointValidationError(
            f"This address is longer than {_MAX_ENDPOINT_CHARS} characters. "
            "Paste only the stream address."
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in trimmed):
        raise EndpointValidationError(
            "The stream address cannot contain control characters. Retype it by hand "
            "instead of pasting from a document."
        )
    if source_type == "ndi":
        return _normalize_ndi(trimmed)
    return _normalize_url(source_type, trimmed)


def _normalize_ndi(trimmed: str) -> str:
    name = trimmed.removeprefix("ndi://").strip()
    if not name:
        raise EndpointValidationError(
            "Enter the NDI source name exactly as the NDI sender advertises it "
            "(for example COUNCIL-CAM (Channel 1))."
        )
    if "/" in name or "\\" in name or ".." in name:
        raise EndpointValidationError(
            "Enter a single NDI source name, not a path or a URL. NDI sources are "
            "discovered by name on the station network."
        )
    if "@" in name:
        raise EndpointValidationError(
            "An NDI source name cannot contain '@'. NDI sources on the station "
            "network do not use a username or password."
        )
    return f"ndi://{name}"


def _has_query_key(query: str, key: str) -> bool:
    # parse_qsl would drop a valueless key; a bare "?passphrase" is still an
    # operator trying to put the secret in the address, so match on the raw
    # key names instead.
    return any(part.split("=", 1)[0].strip().lower() == key for part in query.split("&") if part)


def _normalize_url(source_type: str, trimmed: str) -> str:
    # Whitespace is rejected for URLs only. NDI source names routinely contain
    # spaces and parentheses -- the NDI sender advertises them that way
    # ("COUNCIL-CAM (Channel 1)") -- so the NDI branch must not inherit this.
    if any(ch.isspace() for ch in trimmed):
        raise EndpointValidationError(
            "The stream address cannot contain spaces. Check for a stray space in the "
            "middle or at the end of what you pasted."
        )
    allowed = SUPPORTED_SCHEMES_BY_SOURCE_TYPE[source_type]
    parsed = urlsplit(trimmed)
    scheme = parsed.scheme.lower()
    if scheme not in allowed:
        readable = " or ".join(f"{value}://" for value in sorted(allowed))
        raise EndpointValidationError(
            f"A {source_type.upper()} source needs a {readable} address; this one "
            f"starts with {(parsed.scheme or trimmed.split(':', 1)[0])!r}. "
            "Change the source type or paste the right address."
        )
    if not parsed.netloc:
        raise EndpointValidationError(
            f"This {source_type.upper()} address has no host. Use "
            f"{scheme}://<address>[:<port>] as the encoder or camera reports it."
        )
    if parsed.username or parsed.password:
        raise EndpointValidationError(
            "Do not put a username or password inside the stream address. CivicCast "
            "will not store or run a password inside an address. Remove the "
            "'user:password@' part; for SRT, store a passphrase instead."
        )
    if source_type == "srt" and (
        _has_query_key(parsed.query, "passphrase") or _has_query_key(parsed.fragment, "passphrase")
    ):
        raise EndpointValidationError(
            "Do not put the SRT passphrase in the address. Leave it out and store the "
            "passphrase in the station credential store instead -- CivicCast passes it "
            "to the encoder without ever writing it into an address."
        )
    if parsed.fragment:
        # A non-empty fragment is never a legal part of any address this
        # module accepts. Rejecting only a fragment that happens to spell
        # "passphrase=..." would still store (and later re-emit, in plaintext)
        # any other secret an operator pastes after a '#' -- e.g.
        # ``srt://host:9000#hunter2`` -- so any fragment at all is refused
        # rather than silently persisted.
        raise EndpointValidationError(
            "The stream address cannot contain anything after '#'. CivicCast will not "
            "store or run a password, passphrase, or token pasted into an address -- "
            "remove it, and for SRT, store a passphrase in the station credential store "
            "instead."
        )
    # Re-emit through urlunsplit so the stored value is canonical (lowercased
    # scheme, no fragment) and two spellings of one address cannot become two
    # rows the operator has to tell apart.
    return urlunsplit((scheme, parsed.netloc, parsed.path, parsed.query, ""))
