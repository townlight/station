# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""BunnyCDN storage adapter.

Implements the ``CDNAdapter`` protocol against BunnyCDN's Storage API.
Per ADR 0006: BunnyCDN is the v1 default CDN. Credentials are two values:
  - ``storage_zone_name``: the BunnyCDN storage zone name.
  - ``access_key``: the Storage Zone API key (not the account API key).
  - ``cdn_hostname``: the pull-zone hostname (e.g. 'your-zone.b-cdn.net').

The Storage API base URL is ``https://storage.bunnycdn.com/{zone}/{key}``.
The public CDN URL is ``https://{cdn_hostname}/{key}``.

Operator setup: USER-MANUAL.md §CDN Configuration (BunnyCDN).
"""

from __future__ import annotations

from pathlib import Path

import httpx

from civiccast.stream.cdn import cache_control

__all__ = ["BunnyCDNAdapter", "BunnyCDNError"]

_STORAGE_BASE = "https://storage.bunnycdn.com"
_DEFAULT_TIMEOUT = 120  # seconds — generous for large HLS segment batches


class BunnyCDNError(RuntimeError):
    """HTTP or network error from BunnyCDN Storage API."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BunnyCDNAdapter:
    """CDN adapter backed by BunnyCDN Storage + pull-zone.

    Args:
        storage_zone_name: BunnyCDN storage zone name.
        access_key: Storage Zone API key.
        cdn_hostname: Pull-zone hostname (no scheme), e.g. 'myzone.b-cdn.net'.
        timeout: HTTP timeout in seconds per request (default 120 s).
    """

    def __init__(
        self,
        storage_zone_name: str,
        access_key: str,
        cdn_hostname: str,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        if not storage_zone_name or not access_key or not cdn_hostname:
            raise ValueError(
                "BunnyCDNAdapter requires storage_zone_name, access_key, and cdn_hostname. "
                "See USER-MANUAL.md §CDN Configuration (BunnyCDN)."
            )
        self._zone = storage_zone_name
        self._key = access_key
        self._hostname = cdn_hostname.rstrip("/")
        self._timeout = timeout

    def upload_file(self, local_path: Path, remote_key: str) -> str:
        """Upload ``local_path`` to BunnyCDN at ``remote_key``.

        Raises ``BunnyCDNError`` on HTTP or network error.
        """
        remote_key = remote_key.lstrip("/")
        url = f"{_STORAGE_BASE}/{self._zone}/{remote_key}"
        with local_path.open("rb") as fh:
            try:
                response = httpx.put(
                    url,
                    content=fh,
                    headers={
                        "AccessKey": self._key,
                        "Cache-Control": cache_control(remote_key),
                    },
                    timeout=self._timeout,
                )
            except httpx.TransportError as exc:
                raise BunnyCDNError(f"Network error uploading {remote_key}: {exc}") from exc
        if response.status_code not in (200, 201):
            raise BunnyCDNError(
                f"BunnyCDN returned HTTP {response.status_code} for PUT {remote_key}",
                status_code=response.status_code,
            )
        return self.public_url(remote_key)

    def delete_file(self, remote_key: str) -> None:
        """Delete ``remote_key`` from BunnyCDN storage. Silent on 404."""
        remote_key = remote_key.lstrip("/")
        url = f"{_STORAGE_BASE}/{self._zone}/{remote_key}"
        try:
            response = httpx.delete(
                url,
                headers={"AccessKey": self._key},
                timeout=self._timeout,
            )
        except httpx.TransportError as exc:
            raise BunnyCDNError(f"Network error deleting {remote_key}: {exc}") from exc
        if response.status_code not in (200, 204, 404):
            raise BunnyCDNError(
                f"BunnyCDN returned HTTP {response.status_code} for DELETE {remote_key}",
                status_code=response.status_code,
            )

    def public_url(self, remote_key: str) -> str:
        """Return the pull-zone public URL for ``remote_key``."""
        remote_key = remote_key.lstrip("/")
        return f"https://{self._hostname}/{remote_key}"

    def health_check(self) -> bool:
        """Return True if the storage zone is reachable with these credentials.

        Lists the storage-zone root; HTTP 200 means the AccessKey is valid for
        the zone. Network or HTTP errors return False rather than raising, so
        the operator "Test connection" action renders a single pass/fail and no
        error message can leak the key.
        """
        url = f"{_STORAGE_BASE}/{self._zone}/"
        try:
            response = httpx.get(url, headers={"AccessKey": self._key}, timeout=self._timeout)
        except httpx.TransportError:
            return False
        return response.status_code == 200
