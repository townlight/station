# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Continuously publish a channel's rolling live HLS to a CDN (0.2.0 step 3).

The live analog of :meth:`finalization_worker.LiveFinalizationWorker._upload_package`:
while a channel is broadcasting, push its rolling HLS output (``civiccast.egress
.sinks.HlsSink`` writes ``seg%09d.ts`` + ``playlist.m3u8`` into a directory) to
the configured CDN, so viewers can be switched to the CDN edge under load (the
surge switch is a separate piece; this is the publish path it depends on).

Invariants, matching the VOD upload:

* **Segments first, manifest last** — a viewer never fetches a manifest that
  references a segment not yet on the CDN.
* **Evict what the window drops** — segments that roll out of the local sliding
  window are deleted from the CDN too, so the station pays only for the live
  window, not the whole broadcast.

The publisher is decoupled from the egress store: the caller resolves the
channel's live directory (the same resolution the media router uses) and the
active :class:`~civiccast.stream.cdn.CDNAdapter`, and drives :meth:`sync` on the
segment cadence. When no CDN is configured, no publisher is constructed and
live serving stays local (clean fallback).
"""

from __future__ import annotations

from pathlib import Path

from civiccast.stream.cdn import CDNAdapter

_MANIFEST_NAME = "playlist.m3u8"


class LiveCDNPublisher:
    """Pushes one channel's rolling live HLS directory to a CDN adapter."""

    def __init__(
        self,
        channel_id: str,
        live_dir: Path,
        cdn_adapter: CDNAdapter,
        *,
        key_prefix: str | None = None,
    ) -> None:
        self._channel_id = channel_id
        self._live_dir = live_dir
        self._cdn = cdn_adapter
        self._prefix = (key_prefix or f"live/{channel_id}").strip("/")
        self._uploaded: set[str] = set()

    @property
    def manifest_key(self) -> str:
        return f"{self._prefix}/{_MANIFEST_NAME}"

    def manifest_url(self) -> str:
        """The CDN public URL a switched viewer would fetch the live manifest from."""
        return self._cdn.public_url(self.manifest_key)

    def sync(self) -> str | None:
        """Push the current rolling window to the CDN; return the CDN manifest URL.

        One pass: upload new segments first, the manifest last, then evict from
        the CDN any segment the local window has since dropped. Returns ``None``
        when the sink has not written a manifest yet (nothing to publish).
        """
        manifest = self._live_dir / _MANIFEST_NAME
        if not manifest.is_file():
            return None

        current = sorted(p for p in self._live_dir.glob("seg*.ts") if p.is_file())
        current_names = {p.name for p in current}

        # New segments first, so the manifest uploaded next never references a
        # segment that is not yet on the CDN.
        for seg in current:
            if seg.name not in self._uploaded:
                self._cdn.upload_file(seg, f"{self._prefix}/{seg.name}")
                self._uploaded.add(seg.name)

        manifest_url = self._cdn.upload_file(manifest, self.manifest_key)

        # Evict from the CDN any segment the local window has dropped. The
        # freshly-uploaded manifest no longer references them, so it is safe.
        for gone in sorted(self._uploaded - current_names):
            self._cdn.delete_file(f"{self._prefix}/{gone}")
            self._uploaded.discard(gone)

        return manifest_url

    def evict_all(self) -> None:
        """Delete everything this publisher pushed to the CDN (idle cleanup).

        The manifest is deleted first, so no viewer is handed a manifest whose
        segments are being removed, then every uploaded segment. Idempotent;
        called when a channel switches back to local so CDN storage cost really
        goes to zero at idle.
        """
        self._cdn.delete_file(self.manifest_key)
        for name in sorted(self._uploaded):
            self._cdn.delete_file(f"{self._prefix}/{name}")
        self._uploaded.clear()
