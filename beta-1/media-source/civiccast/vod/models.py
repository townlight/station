# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Pydantic models for VOD asset metadata and embed API responses."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, HttpUrl, field_validator

# Environment variable that disables the HTTPS-only enforcement on
# ``manifest_url``. Set to "1" / "true" / "yes" for local development against
# a plain-HTTP origin (the dev portal-public Vite server, a local nginx
# stub). Spec §4.1's UX non-negotiables and §15 (security & privacy) imply
# HTTPS-only for any public-facing surface; the escape hatch is documented
# in CONTRIBUTING.md and ``apps/portal-public/README.md``.
_INSECURE_OK_ENV = "CIVICCAST_ALLOW_INSECURE_MANIFEST"


def _allow_insecure_manifest() -> bool:
    """Read the ``CIVICCAST_ALLOW_INSECURE_MANIFEST`` env var on every call.

    Reading on every call (not at module import) is intentional — tests
    monkeypatch the environment per case. The cost is one ``os.environ.get``
    per validation pass; immeasurable next to the network round-trip the
    URL itself implies.
    """
    return os.environ.get(_INSECURE_OK_ENV, "").lower() in {"1", "true", "yes"}


def _is_loopback_http_url(raw: str) -> bool:
    """True for ``http://`` URLs whose host is a fixed loopback name.

    VOD local-serve (no CDN configured) populates ``manifest_url`` with the
    app's own ``http://127.0.0.1:<port>/...`` URL by default — that traffic
    never leaves the machine, so it is exempted from the https-only rule
    the same way the dev escape hatch is, but WITHOUT weakening the rule
    for any real external host (only these three fixed names qualify).
    """
    return urlparse(raw).hostname in {"127.0.0.1", "localhost", "::1"}


def _enforce_https_manifest(value: Any) -> Any:
    """Reject ``http://`` URLs unless exempted.

    Used as a Pydantic ``field_validator`` on every ``manifest_url`` /
    ``poster_url`` declaration in this module. Lets ``HttpUrl``'s native
    parsing run first by accepting ``Any`` and returning the original
    value — Pydantic's later coercion step does the URL normalisation.

    Two independent exemptions:
    - ``CIVICCAST_ALLOW_INSECURE_MANIFEST=1`` (dev escape hatch, any host).
    - A loopback host (``127.0.0.1`` / ``localhost`` / ``::1``), always —
      what CivicCast's own local HLS serving uses by default.

    None passes through untouched (the field may be optional in some
    models, e.g. ``AssetMetadata.manifest_url`` becomes nullable in the
    schedule module's peer for uploaded-but-not-packaged assets).
    """
    if value is None:
        return value
    raw = str(value).strip()
    if raw.lower().startswith("http://"):
        if _allow_insecure_manifest() or _is_loopback_http_url(raw):
            return value
        raise ValueError(
            "manifest_url and poster_url must use https://. "
            "Plain http:// is rejected to prevent residents loading civic "
            "video over an insecure transport. "
            f"For local development against a plain-HTTP origin, set "
            f"{_INSECURE_OK_ENV}=1; do not set this in production."
        )
    return value


def _validate_manifest_reference(value: Any) -> str:
    """Validate a CDN URL or CivicCast same-origin local media path."""
    raw = str(value).strip()
    if raw.startswith("/media/vod/"):
        if "\\" in raw or ".." in raw or "?" in raw or "#" in raw:
            raise ValueError("Local manifest paths must remain inside /media/vod/.")
        if not raw.endswith("/playlist.m3u8"):
            raise ValueError("Local manifest paths must name playlist.m3u8.")
        return raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("manifest_url must be an https:// URL or a same-origin /media/vod/ path.")
    _enforce_https_manifest(raw)
    return raw


def public_manifest_reference(asset_id: str, value: Any) -> str:
    """Normalize legacy loopback local-package URLs to same-origin paths.

    A loopback URL is meaningful only on the CivicCast host. Returning its
    canonical local path lets every resident browser resolve the media against
    the public origin it actually used, while true CDN URLs remain unchanged.
    """
    raw = str(value).strip()
    expected_path = f"/media/vod/{asset_id}/playlist.m3u8"
    parsed = urlparse(raw)
    if raw == expected_path:
        return raw
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.path == expected_path:
        return expected_path
    return raw


def _normalize_meeting_body(value: str | None) -> str | None:
    """Strip padding; reject control characters and whitespace-only values.

    Audit QA-002: the portal's meeting-body facet matches values by strict
    equality, so server-side normalization keeps one body one facet option
    regardless of who wrote the tag (the console trims; raw API writers
    did not). Shared by the schedule peer model so the rules cannot drift.
    """

    if value is None:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("meeting_body cannot contain control characters.")
    stripped = value.strip()
    if not stripped:
        raise ValueError("meeting_body cannot be blank; send null to clear the tag.")
    return stripped


class AssetMetadata(BaseModel):
    """Public metadata for a single VOD asset.

    Stored under the asset's canonical id; consumed by the public portal,
    the embed-widget endpoint, and any external syndication target.

    HTTPS-only enforcement on ``manifest_url`` and ``poster_url`` lands at
    Sprint 0.3 (cleanup batch C). Plain ``http://`` URLs are rejected
    unless ``CIVICCAST_ALLOW_INSECURE_MANIFEST=1`` is set in the
    environment (dev escape hatch).
    """

    asset_id: str = Field(
        ...,
        description="Canonical asset identifier (URL-safe).",
        pattern=r"^[a-z0-9][a-z0-9-]{2,63}$",
    )
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    # Option b (#107 remainder): the meeting body this recording belongs to
    # (e.g. "City Council"). NULL = untagged; the portal derives its browse
    # facet from values in use.
    meeting_body: str | None = Field(default=None, min_length=1, max_length=120)
    manifest_url: str = Field(
        ...,
        description="HLS playlist: public HTTPS URL or same-origin /media/vod/ path.",
    )
    poster_url: HttpUrl | None = Field(
        default=None, description="Poster image URL shown before play (HTTPS)."
    )
    duration_seconds: int | None = Field(
        default=None, ge=0, description="VOD duration in whole seconds."
    )
    published_at: datetime | None = Field(
        default=None, description="Publication timestamp (UTC, ISO 8601)."
    )

    @field_validator("manifest_url", mode="before")
    @classmethod
    def _manifest_reference(cls, value: Any) -> str:
        return _validate_manifest_reference(value)

    @field_validator("poster_url", mode="before")
    @classmethod
    def _https_only(cls, value: Any) -> Any:
        return _enforce_https_manifest(value)

    @field_validator("meeting_body")
    @classmethod
    def _meeting_body_clean(cls, value: str | None) -> str | None:
        return _normalize_meeting_body(value)


class AssetEmbedResponse(BaseModel):
    """Response payload for ``GET /api/public/embed/{asset_id}``.

    Caller-friendly shape: includes both the raw manifest URL (for custom
    integrations) and a ready-to-paste iframe HTML snippet (for blog
    posts, civic portals, council member sites).
    """

    asset_id: str
    title: str
    manifest_url: str
    poster_url: HttpUrl | None
    portal_url: HttpUrl = Field(..., description="Public portal page for this asset.")
    embed_html: str = Field(..., description="iframe HTML snippet for embedding.")
    embed_width: int = Field(default=640, ge=160, le=4096)
    embed_height: int = Field(default=360, ge=90, le=4096)

    # NOTE: ``portal_url`` is intentionally NOT validated here. It is
    # synthesized at response time from ``request.url`` in
    # ``vod.router.get_embed`` — for a station running behind plain HTTP
    # (against project policy but possible in dev), the response should
    # reflect that, not refuse to render. The HTTPS enforcement on
    # operator-stored URLs is the actual mitigation.
    @field_validator("manifest_url", mode="before")
    @classmethod
    def _manifest_reference(cls, value: Any) -> str:
        return _validate_manifest_reference(value)

    @field_validator("poster_url", mode="before")
    @classmethod
    def _https_only(cls, value: Any) -> Any:
        return _enforce_https_manifest(value)
