# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Recording-target URI resolution shared by the store and the worker.

Extracted from the finalization worker (Beta sprint B1) so ``go_on_air`` can
stamp the resolved recording target onto the session with the exact same
resolution rules the worker uses — provenance by construction instead of two
implementations drifting apart.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

# The installer's System Health rehearsal plants this target on fresh
# installs; real session recordings must never resolve into it (audit
# ENG-005).
REHEARSAL_RECORDING_TARGET_ID = "local-rehearsal-recordings"

# The installer-managed first-run path creates this local target for real
# scheduled/live recordings. The rehearsal target above stays isolated so
# proof artifacts cannot become production captures by accident.
DEFAULT_RECORDING_TARGET_DIR_NAME = "recordings"
DEFAULT_RECORDING_TARGET_ID = "local-recordings"
DEFAULT_RECORDING_TARGET_NAME = "Local recordings"

__all__ = [
    "DEFAULT_RECORDING_TARGET_DIR_NAME",
    "DEFAULT_RECORDING_TARGET_ID",
    "DEFAULT_RECORDING_TARGET_NAME",
    "REHEARSAL_RECORDING_TARGET_ID",
    "local_recording_path",
]


def local_recording_path(recording_uri: str) -> Path | None:
    """Resolve a recording-target URI to a local path, or None if unusable.

    Accepted shapes (QA-003/ENG-013): ``file://`` URIs, plain Windows drive
    paths (``C:\\recordings`` / ``C:/recordings`` — ``urlparse`` reads the
    drive letter as a one-letter scheme), and absolute POSIX paths. Relative
    paths are rejected rather than resolved against an arbitrary process CWD;
    non-file schemes (http, s3, …) are not local recordings.
    """

    parsed = urlparse(recording_uri)
    if len(parsed.scheme) == 1 and recording_uri[1:2] == ":":
        return Path(recording_uri)
    if parsed.scheme == "":
        path = Path(recording_uri)
        return path if path.anchor else None
    if parsed.scheme != "file":
        return None
    if parsed.netloc not in {"", "localhost"}:
        return None
    raw_path = unquote(parsed.path)
    if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    return Path(raw_path)
