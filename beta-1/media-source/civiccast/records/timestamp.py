# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""RFC 3161 timestamp proof protocol and deterministic local authority."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

from civiccast.records.models import Rfc3161TimestampProof


class TimestampVerificationError(ValueError):
    """Raised when a timestamp proof does not bind to the payload."""


class DeterministicTimestampAuthority:
    """Local deterministic timestamp authority for v0.6 tests and dev runs."""

    def __init__(
        self,
        *,
        nonce: str = "deterministic-nonce",
        timestamp_iso: str = "2026-05-14T12:00:00+00:00",
    ) -> None:
        self._nonce = nonce
        self._timestamp_iso = timestamp_iso

    def timestamp(self, payload: bytes) -> Rfc3161TimestampProof:
        digest = _digest(payload)
        token = base64.b64encode(f"{digest}:{self._nonce}:{self._timestamp_iso}".encode()).decode()
        return Rfc3161TimestampProof(
            algorithm="sha256",
            artifact_digest=digest,
            token_der_b64=token,
            tsa_policy_oid="1.3.6.1.4.1.57264.0.6",
            nonce=self._nonce,
            timestamped_at=datetime.fromisoformat(self._timestamp_iso).astimezone(UTC),
            certificate_fingerprint=_digest(f"civiccast-tsa:{self._nonce}".encode()),
        )


def verify_timestamp_proof(payload: bytes, proof: Rfc3161TimestampProof) -> bool:
    """Verify that ``proof`` binds to ``payload``."""

    digest = _digest(payload)
    if digest != proof.artifact_digest:
        raise TimestampVerificationError(
            "timestamp proof digest does not match the supplied record artifact"
        )
    return True


def verify_timestamp_proof_structure(proof: Rfc3161TimestampProof) -> bool:
    """Structurally re-check a *stored* deterministic-authority proof.

    The deterministic token is a base64 blob of
    ``f"{digest}:{nonce}:{timestamp_iso}"`` rather than a real ASN.1
    ``TimeStampToken`` (see ``civiccast.records.rfc3161`` for the real one),
    so there is no DER structure to parse. The archived PDF/A-3 record only
    keeps the token, not the original pre-embed bytes it was computed from
    (see ``civiccast.records.pdfa.embed_timestamp_token``), so the strongest
    honest check available here is that the decoded token still carries the
    proof's own recorded ``artifact_digest`` -- catching a swapped-in-garbage
    or corrupted token. It cannot independently re-derive the digest from a
    live artifact the way ``verify_timestamp_proof`` does.
    """
    import base64
    import binascii

    try:
        decoded = base64.b64decode(proof.token_der_b64, validate=True).decode("ascii")
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise TimestampVerificationError(
            f"token_der_b64 is not a valid deterministic timestamp token: {exc}"
        ) from exc
    if not decoded.startswith(f"{proof.artifact_digest}:"):
        raise TimestampVerificationError(
            "Deterministic timestamp token does not carry the proof's recorded artifact_digest"
        )
    return True


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()
