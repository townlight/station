# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CDN adapter protocol and factory.

Per ADR 0006: ``BunnyCDNAdapter`` is the v1 default; ``CloudflareR2Adapter``
is the shipped optional DDoS-protection alternate.  All CDN code in
``civiccast.stream`` goes through the ``CDNAdapter`` protocol — never
raw HTTP calls to provider APIs outside this package.

The active adapter is selected by the ``cdn.provider`` config key.
Currently: ``"bunny"`` (default), ``"stub"`` (testing), or
``"cloudflare_r2"`` when the optional dependency is installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["CDNAdapter", "cache_control"]

# HLS segments are immutable once written; cache them for a year.
_SEGMENT_MAX_AGE = 31_536_000


def cache_control(remote_key: str) -> str:
    """The ``Cache-Control`` a CDN edge should apply when serving ``remote_key``.

    Live HLS splits into two very different cacheability classes, and getting
    this wrong stalls a CDN-fronted broadcast:

    * the **media playlist** (``*.m3u8``) is rewritten every segment (~2s), and a
      stale copy can reference a segment the origin has already evicted — so it
      gets a tiny max-age. The edge still absorbs polling bursts from thousands of
      viewers, but never serves a manifest more than ~1s stale (well inside the
      rolling window), so it never points at a gone segment.
    * **segments** (``*.ts`` / ``*.m4s``) are immutable once written, so they get
      a long immutable max-age — the edge serves them for the whole broadcast
      without revalidating, which is where the CDN offload actually comes from.

    Without an explicit value, provider edges apply their own default TTL
    (commonly minutes to hours): a live playlist cached for minutes is the
    classic live-HLS-over-CDN stall. Adapters pass this on every upload.
    """
    if remote_key.endswith(".m3u8"):
        return "max-age=1"
    return f"public, max-age={_SEGMENT_MAX_AGE}, immutable"


# Concrete adapters — imported via their submodules. The R2 adapter
# requires the ``cloudflare-r2`` optional extra; importing it lazily
# from this package avoids forcing boto3 onto stations that don't use R2.


@runtime_checkable
class CDNAdapter(Protocol):
    """Protocol that every CDN adapter must satisfy.

    Upload semantics are fire-and-forget at the method level; retry/backoff
    is the caller's responsibility (Sprint 0.7 publish pipeline wires this up).
    """

    def upload_file(self, local_path: Path, remote_key: str) -> str:
        """Upload ``local_path`` to the CDN at ``remote_key``.

        Returns the public URL for the uploaded object.
        ``remote_key`` uses forward slashes regardless of OS.
        """
        ...

    def delete_file(self, remote_key: str) -> None:
        """Delete the object at ``remote_key`` from the CDN.

        Silent no-op if the object does not exist.
        """
        ...

    def public_url(self, remote_key: str) -> str:
        """Return the public URL for ``remote_key`` without uploading.

        Used to pre-compute manifest URLs before upload begins.
        """
        ...

    def health_check(self) -> bool:
        """Return True if the CDN is reachable and the credentials work.

        Backs the operator setup "Test connection" action and ``civiccast
        doctor``: it lets an operator confirm entered credentials actually
        reach the CDN before publishing depends on them. Implementations
        catch their own network/API errors and return False rather than
        raising, so a caller can render a single pass/fail.
        """
        ...
