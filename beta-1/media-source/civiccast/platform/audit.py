# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Hash-chain audit verification for `civiccast doctor audit`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuditVerificationResult:
    """Hash-chain verification result."""

    status: str
    operator_action: str
    command: str


def verify_hash_chain(records: list[dict[str, Any]]) -> AuditVerificationResult:
    """Verify adjacent hash links and report actionable repair guidance."""

    command = "civiccast doctor audit"
    known_hashes: set[str] = set()
    previous_hash: str | None = None
    for index, record in enumerate(records):
        current_hash = str(record.get("hash") or "")
        claimed_previous = record.get("previous_hash")
        if index == 0 and claimed_previous is not None and claimed_previous not in known_hashes:
            return AuditVerificationResult(
                status="failed",
                operator_action=(
                    "Audit hash chain is missing an earlier link; restore the missing record "
                    "from backup and rerun civiccast doctor audit."
                ),
                command=command,
            )
        if index > 0 and claimed_previous != previous_hash:
            return AuditVerificationResult(
                status="failed",
                operator_action=(
                    "Audit hash chain tamper evidence detected; stop release proof, "
                    "restore the expected audit log, and rerun civiccast doctor audit."
                ),
                command=command,
            )
        known_hashes.add(current_hash)
        previous_hash = current_hash
    return AuditVerificationResult(
        status="ok",
        operator_action="Audit hash chain is intact.",
        command=command,
    )
