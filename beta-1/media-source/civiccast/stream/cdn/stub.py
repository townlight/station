# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Stub CDN adapter for testing.

Copies files to a local directory and returns ``file://`` URLs.
The broken-media regression suite and all stream unit tests use this adapter
so CDN credentials are not required in CI (per ADR 0006 compliance section).
"""

from __future__ import annotations

import shutil
from pathlib import Path

__all__ = ["StubCDNAdapter"]


class StubCDNAdapter:
    """CDN adapter that writes to a local directory with file:// URLs.

    Args:
        root: Local directory that acts as the CDN storage root.
              Created on first ``upload_file`` call if it does not exist.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def upload_file(self, local_path: Path, remote_key: str) -> str:
        dest = self._root / remote_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)
        return self.public_url(remote_key)

    def delete_file(self, remote_key: str) -> None:
        target = self._root / remote_key
        if target.exists():
            target.unlink()

    def public_url(self, remote_key: str) -> str:
        return (self._root / remote_key).as_uri()

    def health_check(self) -> bool:
        """The local stub is 'reachable' whenever its root can be created."""
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        return True
