# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Real Internet Archive client (Beta sprint B5, decision #6).

Uploads through archive.org's S3-like API (``s3.us.archive.org``) using the
station's IA keys. Selected with ``CIVICCAST_PROVIDER_INTERNET_ARCHIVE=real``;
the deterministic mock remains the default. Credentials are read from the
environment and validated fail-fast — a missing key stops resolution with the
exact variable names, never a silent mock fallback.

No credentials, tokens, or secrets live in code or tests; contract tests use
``httpx.MockTransport`` and never call archive.org.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from civiccast.archive.models import ArchiveProof
from civiccast.platform.provider_settings import setting

_S3_ENDPOINT = "https://s3.us.archive.org"
_DETAILS_BASE = "https://archive.org/details"

__all__ = ["InternetArchiveClient", "InternetArchiveSettings"]


@dataclass(frozen=True)
class InternetArchiveSettings:
    """Station IA credentials and item naming, read from the environment."""

    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    collection: str = "opensource_movies"
    item_prefix: str = "civiccast"

    @classmethod
    def from_env(cls) -> InternetArchiveSettings:
        access_key = setting(
            "internet-archive", "access_key", "CIVICCAST_IA_ACCESS_KEY", ""
        ).strip()
        secret_key = setting(
            "internet-archive", "secret_key", "CIVICCAST_IA_SECRET_KEY", ""
        ).strip()
        missing = [
            name
            for name, value in (
                ("CIVICCAST_IA_ACCESS_KEY", access_key),
                ("CIVICCAST_IA_SECRET_KEY", secret_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "CIVICCAST_PROVIDER_INTERNET_ARCHIVE=real requires "
                f"{', '.join(missing)} to be set (archive.org S3 keys; see "
                "docs/ops/cdn-and-providers.md)."
            )
        defaults = cls(access_key=access_key, secret_key=secret_key)
        return cls(
            access_key=access_key,
            secret_key=secret_key,
            collection=os.environ.get("CIVICCAST_IA_COLLECTION", "").strip() or defaults.collection,
            item_prefix=os.environ.get("CIVICCAST_IA_ITEM_PREFIX", "").strip()
            or defaults.item_prefix,
        )


class InternetArchiveClient:
    """Archive client satisfying the same protocol as the mock.

    ``upload`` takes payload bytes (the registry call-site contract);
    ``upload_path`` streams a local media file so full recordings are not
    read into memory. Both PUT to
    ``{S3}/{item_prefix}-{asset_id}/{filename}`` with auto-make-bucket and
    collection/mediatype metadata headers, and return an :class:`ArchiveProof`
    only when archive.org acknowledged the write (non-2xx raises).
    """

    def __init__(
        self,
        settings: InternetArchiveSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 600.0,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, timeout=self._timeout_seconds)

    def _headers(self, *, title: str) -> dict[str, str]:
        return {
            "authorization": f"LOW {self._settings.access_key}:{self._settings.secret_key}",
            "x-amz-auto-make-bucket": "1",
            "x-archive-meta01-collection": self._settings.collection,
            "x-archive-meta-mediatype": "movies",
            "x-archive-meta-title": title,
        }

    def _identifier(self, asset_id: str) -> str:
        return f"{self._settings.item_prefix}-{asset_id}"

    def upload(self, *, asset_id: str, payload: bytes) -> ArchiveProof:
        identifier = self._identifier(asset_id)
        url = f"{_S3_ENDPOINT}/{identifier}/{asset_id}.bin"
        with self._client() as client:
            response = client.put(url, content=payload, headers=self._headers(title=asset_id))
            response.raise_for_status()
        digest = hashlib.sha256()
        digest.update(asset_id.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        return ArchiveProof(
            target_type="internet_archive",
            target_url_or_path=f"{_DETAILS_BASE}/{identifier}",
            verification_hash=f"sha256:{digest.hexdigest()}",
            credential_posture="informal_per_station",
        )

    def upload_path(self, *, asset_id: str, path: Path) -> ArchiveProof:
        identifier = self._identifier(asset_id)
        url = f"{_S3_ENDPOINT}/{identifier}/{path.name}"
        digest = hashlib.sha256()
        digest.update(asset_id.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            with self._client() as client:
                response = client.put(url, content=handle, headers=self._headers(title=asset_id))
                response.raise_for_status()
            handle.seek(0)
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return ArchiveProof(
            target_type="internet_archive",
            target_url_or_path=f"{_DETAILS_BASE}/{identifier}",
            verification_hash=f"sha256:{digest.hexdigest()}",
            credential_posture="informal_per_station",
        )
