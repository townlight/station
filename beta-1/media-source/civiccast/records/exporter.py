# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Signed-record export orchestration for approved summaries."""

from __future__ import annotations

import base64
import hashlib
from typing import Protocol
from uuid import uuid4

from civiccast.auth.models import OperatorIdentity
from civiccast.records.models import PdfARecordMetadata, RecordExportResponse, Rfc3161TimestampProof
from civiccast.records.pdfa import embed_timestamp_token, render_pdfa_record, validate_pdfa3_shape
from civiccast.records.timestamp import DeterministicTimestampAuthority
from civiccast.summary.store import SummaryStore


def _default_timestamp_authority() -> TimestampAuthority:
    """Return the timestamp authority dictated by environment configuration.

    Production opt-in: setting ``CIVICCAST_TSA_URL`` (or, equivalently, any
    truthy ``CIVICCAST_TSA_ENABLE``) selects the real RFC 3161 HTTP authority
    pointed at that URL. Without those, the deterministic placeholder runs —
    that is the test + unit-test default.

    Lazy-imports the HTTP authority so the test path never pays the
    ``asn1crypto``/``httpx`` import cost when the placeholder is in use.
    """
    import os

    tsa_url = os.environ.get("CIVICCAST_TSA_URL")
    enable_flag = (os.environ.get("CIVICCAST_TSA_ENABLE") or "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if tsa_url or enable_flag:
        from civiccast.records.rfc3161 import Rfc3161HttpAuthority

        return Rfc3161HttpAuthority(tsa_url=tsa_url)
    return DeterministicTimestampAuthority()


class RecordExportError(RuntimeError):
    """Raised when a summary cannot be exported as a legal record."""


class TimestampAuthority(Protocol):
    def timestamp(self, payload: bytes) -> Rfc3161TimestampProof: ...


class SignedRecordExporter:
    """Export approved summary data as a signed PDF/A-3 record."""

    def __init__(
        self,
        *,
        summary_store: SummaryStore,
        timestamp_authority: TimestampAuthority | None = None,
    ) -> None:
        self._summary_store = summary_store
        self._timestamp_authority = timestamp_authority or _default_timestamp_authority()

    def export(
        self,
        *,
        summary_id: str,
        operator_identity: OperatorIdentity,
    ) -> RecordExportResponse:
        summary = self._summary_store.get_summary(summary_id)
        if summary is None:
            raise RecordExportError(
                f"Summary {summary_id!r} was not found; generate and approve a summary first."
            )
        if summary.status != "approved":
            raise RecordExportError(
                f"Summary {summary_id!r} must be approved before exporting a signed record."
            )
        approval = self._summary_store.get_approval(summary_id)
        identity = operator_identity

        pdf_bytes = render_pdfa_record(summary, approval=approval)
        proof_messages = validate_pdfa3_shape(pdf_bytes)
        failures = [message for message in proof_messages if message.startswith("FAIL")]
        if failures:
            raise RecordExportError("PDF/A-3 shape validation failed: " + "; ".join(failures))
        proof = self._timestamp_authority.timestamp(pdf_bytes)
        # The timestamp authority can only be called on the rendered PDF, so
        # the token it returns cannot already be inside that PDF -- embed the
        # real token now, replacing the placeholder render_pdfa_record() had
        # to use. The final artifact digest covers these post-embed bytes
        # (the ones actually stored/served); it necessarily differs from
        # proof.artifact_digest, which is the digest the timestamp authority
        # signed (the pre-embed render).
        final_pdf_bytes = embed_timestamp_token(pdf_bytes, base64.b64decode(proof.token_der_b64))
        pdfa = PdfARecordMetadata(
            conformance="PDF/A-3B",
            file_name=f"{summary.meeting_id}-record.pdf",
            media_type="application/pdf",
            byte_size=len(final_pdf_bytes),
            embedded_metadata_names=[
                "sourced-claims.json",
                "provenance.json",
                "approval.json",
                "timestamp-token.der",
                *(["agenda-minutes.json"] if summary.agenda_minutes else []),
            ],
        )
        record_id = f"record-{uuid4().hex}"
        export_fingerprint_material = (
            f"{summary.audit_fingerprint}:{record_id}:{identity.operator_id}:"
            f"{identity.operator_id}:"
            f"{proof.artifact_digest.removeprefix('sha256:')}"
        )
        export_fingerprint = hashlib.sha256(export_fingerprint_material.encode("utf-8")).hexdigest()
        return RecordExportResponse(
            record_id=record_id,
            summary_id=summary_id,
            status="verified",
            audit_fingerprint=f"{summary.audit_fingerprint}:{export_fingerprint}",
            pdfa=pdfa,
            timestamp_proof=proof,
            artifact_digest="sha256:" + hashlib.sha256(final_pdf_bytes).hexdigest(),
            pdf_bytes=final_pdf_bytes,
        )
