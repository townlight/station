# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Release artifact identity checks for veraPDF proof evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VeraPdfReleaseIdentityResult:
    """veraPDF release identity proof result."""

    status: str
    release_version: str
    artifact_sha256: str
    operator_action: str


def verify_verapdf_release_identity(
    *,
    proof_path: Path,
    release_version: str,
    expected_artifact_sha256: str,
) -> VeraPdfReleaseIdentityResult:
    """Verify that veraPDF proof names v1.1 release artifact hashes.

    ``expected_artifact_sha256`` must be a hash the caller actually computed
    from the real built artifact (or looked up from a trusted build record).
    A hash-shaped string merely appearing in the proof document is not
    proof of anything on its own — it must match that real digest, or a
    stale hash left over from a prior edit (or a hand-typed typo) would
    otherwise pass silently.
    """

    text = proof_path.read_text(encoding="utf-8") if proof_path.exists() else ""
    match = re.search(r"sha256:([0-9a-f]{64})", text)
    if release_version in text and match and match.group(1) == expected_artifact_sha256:
        return VeraPdfReleaseIdentityResult(
            status="ok",
            release_version=release_version,
            artifact_sha256=match.group(1),
            operator_action="veraPDF proof is tied to the v1.1 artifact identity.",
        )
    return VeraPdfReleaseIdentityResult(
        status="failed",
        release_version=release_version,
        artifact_sha256="",
        operator_action="Record veraPDF proof with release version and SHA-256 artifact identity.",
    )
