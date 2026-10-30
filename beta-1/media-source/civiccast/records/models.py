# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Closed data contracts for signed-record export."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecordExportRequest(BaseModel):
    """Request to export an operator-approved summary as a signed record."""

    model_config = ConfigDict(extra="forbid")

    summary_id: Annotated[str, Field(min_length=1, max_length=160)]
    summary_status: Literal["approved"]


class PdfARecordMetadata(BaseModel):
    """PDF/A-3B export metadata."""

    model_config = ConfigDict(extra="forbid")

    conformance: Literal["PDF/A-3B"]
    file_name: Annotated[str, Field(min_length=1, max_length=240)]
    media_type: Literal["application/pdf"]
    byte_size: Annotated[int, Field(gt=0)]
    embedded_metadata_names: list[str] = Field(default_factory=list)


class Rfc3161TimestampProof(BaseModel):
    """Timestamp proof metadata for a signed-record artifact.

    ``tsa_url`` and ``serial_number`` are populated by the real HTTP TSA
    client (``civiccast.records.rfc3161.Rfc3161HttpAuthority``) and stay
    ``None`` for the deterministic placeholder authority. They let the
    operator UI surface "who signed this and which serial?" — important
    for archival audit trails. The deterministic authority remains the
    default for tests + the unit-test path; production opt-in via
    ``CIVICCAST_TSA_URL`` (or explicit DI override).
    """

    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["sha256"]
    artifact_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    token_der_b64: Annotated[str, Field(min_length=1)]
    timestamped_at: datetime
    tsa_policy_oid: str | None = None
    nonce: str | None = None
    certificate_fingerprint: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None = None
    tsa_url: Annotated[str | None, Field(default=None, max_length=2000)] = None
    serial_number: Annotated[str | None, Field(default=None, max_length=200)] = None


class RecordExportResponse(BaseModel):
    """Response returned by signed-record export and verification."""

    model_config = ConfigDict(extra="forbid")

    record_id: Annotated[str, Field(min_length=1, max_length=160)]
    summary_id: Annotated[str, Field(min_length=1, max_length=160)]
    status: Literal["verified", "failed"]
    audit_fingerprint: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}(?:[:][0-9a-f]{64})?$")]
    pdfa: PdfARecordMetadata
    timestamp_proof: Rfc3161TimestampProof
    artifact_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None = None
    pdf_bytes: bytes = Field(default=b"", exclude=True)

    @model_validator(mode="after")
    def _default_artifact_digest(self) -> RecordExportResponse:
        if self.artifact_digest is None:
            self.artifact_digest = self.timestamp_proof.artifact_digest
        return self
