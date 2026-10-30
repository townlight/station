# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Shared on-disk location rules for uploaded assets and their VOD packages.

Three call sites need the same two answers -- "where does CivicCast keep
uploaded media?" and "where does the packager write asset ``X``'s HLS
tree?": the staff packaging endpoint (:mod:`civiccast.schedule.router`),
the public media route that serves the package back
(:mod:`civiccast.stream.media_router`), and the offline caption job that
attaches a reviewed WebVTT track to that same package
(:mod:`civiccast.captions.vod_job`). Each had -- or would have had -- its
own copy of the ``CIVICCAST_UPLOAD_DIR`` / ``CIVICCAST_VOD_PACKAGE_DIR``
arithmetic, which is exactly how a writer and a reader drift onto two
different directories.

These helpers are deliberately *pure path arithmetic with no policy*: they
never raise on an unconfigured environment (they return ``None``) and never
check containment. Each caller keeps its own containment guard and its own
error surface, because the right answer differs -- the staff endpoint owes
the operator a 503 with setup instructions, the public route owes a
resident a 404, and the caption worker owes the job row a recorded reason.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "PACKAGE_DIR_ENV_VAR",
    "UPLOAD_DIR_ENV_VAR",
    "VOD_PACKAGE_DIR_NAME",
    "resolve_upload_root",
    "resolve_vod_package_dir",
    "resolve_vod_package_root",
]

UPLOAD_DIR_ENV_VAR = "CIVICCAST_UPLOAD_DIR"
PACKAGE_DIR_ENV_VAR = "CIVICCAST_VOD_PACKAGE_DIR"
#: Default package root, relative to the upload root. Dot-prefixed so it
#: does not show up as a stray "folder" in an operator's media directory.
VOD_PACKAGE_DIR_NAME = ".civiccast-packages"


def resolve_upload_root() -> Path | None:
    """Return the configured upload storage root, or ``None`` when unset."""

    raw = os.environ.get(UPLOAD_DIR_ENV_VAR, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def resolve_vod_package_root(upload_root: Path) -> Path:
    """Return the root under which per-asset HLS packages are written."""

    raw = os.environ.get(PACKAGE_DIR_ENV_VAR, "").strip()
    return Path(raw or (upload_root / VOD_PACKAGE_DIR_NAME)).expanduser().resolve()


def resolve_vod_package_dir(asset_id: str) -> Path | None:
    """Return asset ``asset_id``'s package directory, or ``None`` when unset.

    Returns the path whether or not anything has been written there yet --
    callers that need "does a package exist" must test for the manifest
    themselves, the same way :mod:`civiccast.stream.media_router` does.
    """

    upload_root = resolve_upload_root()
    if upload_root is None:
        return None
    return (resolve_vod_package_root(upload_root) / asset_id).resolve()
