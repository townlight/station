# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Production recording-storage probe for the live pre-flight.

Bug B3 (field evidence, native beta candidate #17): same root cause as
``civiccast.live.network_probe`` -- the "Recording storage" pre-flight
check required a caller-supplied ``storage_free_bytes``, no real caller
ever supplied one, and there was no probe button to run instead. The
check reported ``storage.not_probed`` forever.

This module answers the question the check is actually asking: how much
free space is on the drive CivicCast will record onto. It uses the same
``shutil.disk_usage`` primitive as
:func:`civiccast.platform.hardware._probe_disk` and the same
``CIVICCAST_UPLOAD_DIR`` convention every other storage-aware module in
this tree already reads (e.g. ``civiccast.installer.service``,
``civiccast.schedule.paths``) -- the station's real data volume, not an
arbitrary path. Falls back to the home directory's volume (matching
``civiccast.platform.hardware.probe``'s own default) only when
``CIVICCAST_UPLOAD_DIR`` is unset, e.g. before first-run storage setup
has completed.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

__all__ = [
    "StorageProbeFn",
    "build_storage_probe",
    "probe_storage_free_bytes",
]

StorageProbeFn = Callable[[], "tuple[int | None, str | None]"]


def _default_storage_path() -> Path:
    raw = os.environ.get("CIVICCAST_UPLOAD_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home()


def probe_storage_free_bytes(*, path: Path | None = None) -> tuple[int | None, str | None]:
    """Return free bytes on ``path``'s volume, or ``(None, message)`` on failure.

    Never raises for probe-outcome reasons -- matching
    :data:`civiccast.live.preflight.StorageProbe`.
    """

    target = path or _default_storage_path()
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        return None, (
            f"CivicCast could not check free space at {target}: {exc}. Confirm the "
            "recording drive is connected, then run pre-flight again."
        )
    return usage.free, None


def build_storage_probe(*, path: Path | None = None) -> StorageProbeFn:
    """Build the callable ``PreflightEvaluator`` expects for its storage check.

    ``civiccast.app._resolve_preflight_evaluator`` calls this with no
    arguments so every real station probes its own recording volume
    (``CIVICCAST_UPLOAD_DIR``) at pre-flight time.
    """

    def _probe() -> tuple[int | None, str | None]:
        return probe_storage_free_bytes(path=path)

    return _probe
