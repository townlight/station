# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Publish an on-disk HLS package tree through a :class:`CDNAdapter`.

One helper, one invariant: **the multivariant manifest uploads last.**

A CDN-fronted player fetches the manifest first and then everything the
manifest names. If the manifest reaches the edge before the objects it
declares, a resident gets a playback error against a package that is
perfectly fine on disk. Uploading every other file first and the manifest
last makes that window impossible.

This was :meth:`civiccast.live.finalization_worker.LiveFinalizationWorker
._upload_package`'s private body. It is module-level now because a second
caller needs the same invariant: after caption review completes,
:mod:`civiccast.captions.cdn_republish` re-uploads the *rewritten* manifest
plus the new caption-track files, so a CDN viewer sees the caption tracks
the local package just gained. That caller uploads a subset of the tree
(nothing else changed), which is why ``files`` is a parameter rather than a
tree walk inside this function.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from civiccast.stream.cdn import CDNAdapter

__all__ = ["CdnPackageTarget", "upload_package_files"]


@dataclass(frozen=True)
class CdnPackageTarget:
    """Where one asset's HLS package was published on the CDN, if it was.

    Neutral ground between the module that *records* a CDN publish
    (:mod:`civiccast.live.cdn_targets`, reading the finalization job rows)
    and the module that needs to *re-publish* part of it
    (:mod:`civiccast.captions.cdn_republish`), so neither imports the other.

    ``recorded_manifest_url`` is the URL CivicCast stored for that package's
    manifest at publish time. It is the proof that the package really went
    to a CDN rather than being served locally: the finalization worker
    stores the adapter's returned URL only after a successful upload, and
    stores a *local* portal URL when no CDN adapter was configured. A
    republisher compares it against the adapter's own ``public_url`` for the
    manifest key before uploading anything, so a station that switched CDN
    providers (or turned a CDN on after the fact) never has caption files
    pushed to a prefix whose media segments were never uploaded.
    """

    #: Remote key prefix the package's files live under on the CDN.
    prefix: str
    #: The manifest URL CivicCast recorded when the package was published.
    recorded_manifest_url: str | None


def upload_package_files(
    adapter: CDNAdapter,
    *,
    package_dir: Path,
    prefix: str,
    files: Iterable[Path],
    manifest_path: Path,
) -> str:
    """Upload ``files`` under ``prefix``, then ``manifest_path`` last.

    ``files`` may contain ``manifest_path``; it is skipped there and only
    uploaded at the end. Remote keys are ``<prefix>/<path relative to
    package_dir>`` with forward slashes on every OS, per the
    :class:`CDNAdapter` contract.

    Returns the public URL the adapter reports for the uploaded manifest --
    the adapter only returns it after the upload succeeded, which is what
    keeps a stored ``manifest_url`` honest.
    """

    for path in sorted(set(files)):
        if path == manifest_path:
            continue
        adapter.upload_file(path, f"{prefix}/{path.relative_to(package_dir).as_posix()}")
    manifest_key = f"{prefix}/{manifest_path.relative_to(package_dir).as_posix()}"
    return adapter.upload_file(manifest_path, manifest_key)
