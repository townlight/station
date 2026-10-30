# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API for PDF/A-3B signed-record export and verification."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict

from civiccast.auth.roles import require_any_role
from civiccast.platform.stores import resolve_app_store
from civiccast.records.models import PdfARecordMetadata, RecordExportResponse, Rfc3161TimestampProof
from civiccast.records.store import RecordStore
from civiccast.schedule.retention_worker import DispositionReviewResponse
from civiccast.summary.router import get_summary_store
from civiccast.summary.store import SummaryStore

staff_router = APIRouter(prefix="/api/staff/records", tags=["staff", "records"])


def get_record_store(request: Request) -> RecordStore:
    """FastAPI dependency for the active signed-record store."""

    return cast(
        RecordStore, resolve_app_store(request, "record_store", surface="Signed-record store")
    )


def get_disposition_review_reader() -> object | None:
    """DI seam for the retention disposition queue; wired with durable storage."""

    return None


@staff_router.get(
    "",
    response_model=list[RecordExportResponse],
    summary="List recent signed-record metadata",
    dependencies=[Depends(require_any_role("records_clerk"))],
    responses={
        200: {"description": "Metadata only; PDF artifact bytes are never included."},
    },
)
def list_records(
    summary_id: str | None = Query(default=None, min_length=1, max_length=160),
    limit: int = Query(default=50, ge=1, le=100),
    store: RecordStore = Depends(get_record_store),
) -> list[RecordExportResponse]:
    """List a bounded record window, optionally for one approved summary."""

    return store.list_records(summary_id=summary_id, limit=limit)


@staff_router.get(
    "/disposition-queue",
    response_model=list[DispositionReviewResponse],
    summary="List assets flagged for retention disposition review",
    responses={
        503: {"description": "Durable storage is not ready; the disposition queue requires it."}
    },
)
def list_disposition_queue(
    reader: object | None = Depends(get_disposition_review_reader),
) -> list[DispositionReviewResponse]:
    """Assets whose retention schedule expired, flagged by the retention worker.

    The worker never deletes anything: each row is a records-clerk review
    item. Disposition (purge/extend/hold) is a manual decision per the
    station's records policy; an action surface is a tracked follow-up.
    """

    if reader is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Durable storage is not ready, so the retention disposition queue is unavailable."
            ),
        )
    rows: list[DispositionReviewResponse] = reader.list_disposition_reviews()  # type: ignore[attr-defined]
    return rows


class RecordExportApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_id: str
    summary_status: str | None = None


@staff_router.post(
    "",
    response_model=RecordExportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a PDF/A-3B signed-record export",
    dependencies=[Depends(require_any_role("records_clerk"))],
    responses={409: {"description": "Summary is not approved"}},
)
def export_record(
    request: Request,
    payload: RecordExportApiRequest,
    store: RecordStore = Depends(get_record_store),
    summary_store: SummaryStore = Depends(get_summary_store),
) -> RecordExportResponse:
    if payload.summary_status not in (None, "approved"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Approve the sourced summary before exporting a signed record. "
                "Open the summary review item, verify timestamp evidence, then approve."
            ),
        )
    summary = summary_store.get_summary(payload.summary_id)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Summary not found: {payload.summary_id}. Generate the sourced summary, "
                "review its timestamp evidence, then approve it before exporting."
            ),
        )
    if summary.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Approve the sourced summary before exporting a signed record. "
                "Open the summary review item, verify timestamp evidence, then approve."
            ),
        )
    try:
        from civiccast.records.exporter import RecordExportError, SignedRecordExporter

        operator_identity = request.state.operator_identity
        record = SignedRecordExporter(summary_store=summary_store).export(
            summary_id=payload.summary_id,
            operator_identity=operator_identity,
        )
    except ModuleNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "PDF/A export dependencies are not installed. Install the CivicCast release "
                "environment, then rerun signed-record export."
            ),
        ) from exc
    except RecordExportError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return store.create_record(record, artifact_bytes=record.pdf_bytes)


@staff_router.get(
    "/{record_id}/download",
    summary="Download a PDF/A-3B signed-record artifact",
    dependencies=[Depends(require_any_role("records_clerk"))],
    responses={404: {"description": "Record not found"}},
)
def download_record(
    record_id: str,
    store: RecordStore = Depends(get_record_store),
) -> Response:
    artifact = store.get_artifact(record_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Signed record not found: {record_id}. Export the approved summary first.",
        )
    return Response(content=artifact, media_type="application/pdf")


@staff_router.get(
    "/{record_id}/verify",
    response_model=RecordExportResponse,
    summary="Verify timestamp and audit metadata for a signed record",
    dependencies=[Depends(require_any_role("records_clerk"))],
)
def verify_record(
    record_id: str,
    store: RecordStore = Depends(get_record_store),
) -> RecordExportResponse:
    record = store.get_record(record_id)
    if record is None:
        return _failed_verification(record_id=record_id)

    # Lazy-imported so the module-import path never pays the asn1crypto
    # DER-parsing cost when nothing is being verified (matches the lazy
    # import of the exporter above and of Rfc3161HttpAuthority in
    # exporter.py).
    from civiccast.records import rfc3161, timestamp

    artifact_bytes = store.get_artifact(record_id)
    if artifact_bytes is None:
        return record.model_copy(update={"status": "failed"})

    fresh_digest = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
    if record.artifact_digest is not None and fresh_digest != record.artifact_digest:
        # The stored artifact no longer matches the digest recorded at
        # export time -- corruption after export (bad migration, direct DB
        # edit, replication bit-rot, a store bug).
        return record.model_copy(update={"status": "failed"})

    try:
        if record.timestamp_proof.tsa_url is not None:
            rfc3161.verify_rfc3161_proof_structure(record.timestamp_proof)
        else:
            timestamp.verify_timestamp_proof_structure(record.timestamp_proof)
    except (rfc3161.Rfc3161VerificationError, timestamp.TimestampVerificationError):
        return record.model_copy(update={"status": "failed"})
    return record


def _failed_verification(*, record_id: str) -> RecordExportResponse:
    digest = "sha256:" + ("0" * 64)
    proof = Rfc3161TimestampProof(
        algorithm="sha256",
        artifact_digest=digest,
        token_der_b64=_fixture_timestamp_token(),
        nonce="deterministic-nonce",
        timestamped_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
    )
    return RecordExportResponse(
        record_id=record_id,
        summary_id="unknown",
        status="failed",
        audit_fingerprint="sha256:" + ("0" * 64),
        pdfa=PdfARecordMetadata(
            conformance="PDF/A-3B",
            file_name=f"{record_id}-missing.pdf",
            media_type="application/pdf",
            byte_size=1,
            embedded_metadata_names=[],
        ),
        timestamp_proof=proof,
        artifact_digest=proof.artifact_digest,
    )


def _fixture_timestamp_token() -> str:
    return "MII="
