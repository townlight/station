# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""External caption appliance ingest contracts."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from civiccast.captions.models import CaptionCue
from civiccast.captions.review import (
    CaptionReviewItemCreate,
    CaptionReviewItemResponse,
    CaptionReviewStore,
)

ExternalCaptionProtocol = Literal["cea-608-708", "srt", "webvtt"]

PROOF_BOUNDARY = "external-caption-appliance-to-review-queue-no-hardware-control"
# Hours are unbounded: an appliance streaming for more than 100 hours emits a
# three-digit hours field, and bounding it at two digits rejects the whole
# payload (see U46, 2026-09-26, for the same defect on the egress sidecar side).
_TIMING_RE = re.compile(
    r"(?P<start>\d+:\d{2}(?::\d{2})?[\.,]\d{3})\s*-->\s*"
    r"(?P<end>\d+:\d{2}(?::\d{2})?[\.,]\d{3})"
)
_CEA_TIMING_RE = re.compile(
    r"^\s*(?:\[)?(?P<start>\d+:\d{2}(?::\d{2})?[\.,]\d{3})\s*"
    r"(?:-->|-|\|)\s*"
    r"(?P<end>\d+:\d{2}(?::\d{2})?[\.,]\d{3})(?:\])?\s*"
    r"(?:\||:)?\s*(?P<text>.+?)\s*$"
)


class ExternalCaptionIngestRequest(BaseModel):
    """Caption payload received from an external caption appliance or bridge."""

    model_config = ConfigDict(extra="forbid")

    request_id: Annotated[str, Field(min_length=1, max_length=80)]
    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    appliance_id: Annotated[str, Field(min_length=1, max_length=120)]
    source_label: Annotated[str, Field(min_length=1, max_length=160)]
    protocol: ExternalCaptionProtocol
    payload: Annotated[str, Field(min_length=1, max_length=250_000)]
    default_confidence: Annotated[float, Field(ge=0, le=1)] = 0.86
    received_at: datetime | None = None

    @field_validator("request_id", "asset_id", "appliance_id", "source_label")
    @classmethod
    def _clean_identifier(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("payload")
    @classmethod
    def _normalize_payload(cls, value: str) -> str:
        return value.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n").strip()

    @model_validator(mode="after")
    def _received_at_utc(self) -> ExternalCaptionIngestRequest:
        if self.received_at is None:
            self.received_at = datetime.now(UTC)
        elif self.received_at.tzinfo is None:
            self.received_at = self.received_at.replace(tzinfo=UTC)
        return self


class ExternalCaptionIngestResult(BaseModel):
    """Operator-facing result after appliance captions enter the review queue."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    asset_id: str
    appliance_id: str
    protocol: ExternalCaptionProtocol
    cue_count: int
    review_items: list[CaptionReviewItemResponse]
    proof_boundary: str = PROOF_BOUNDARY
    operator_action: str = "Review external caption appliance output in the normal caption review queue before publish."


class ExternalCaptionParseError(ValueError):
    """Raised when an external caption payload cannot produce reviewable cues."""


def parse_external_caption_payload(payload: ExternalCaptionIngestRequest) -> list[CaptionCue]:
    """Parse external appliance output into stable caption cues."""

    if payload.protocol in {"srt", "webvtt"}:
        cue_texts = _parse_timed_text(payload.payload)
    else:
        cue_texts = _parse_decoded_cea_text(payload.payload)
    if not cue_texts:
        raise ExternalCaptionParseError("External caption payload did not contain any timed cues.")
    return [
        CaptionCue(
            cue_id=f"external-{_safe_id_fragment(payload.request_id)}-{index:06d}",
            start_seconds=start,
            end_seconds=end,
            text=text,
            confidence=payload.default_confidence,
            low_confidence=payload.default_confidence < 0.8,
        )
        for index, (start, end, text) in enumerate(cue_texts, start=1)
    ]


def ingest_external_caption_review_items(
    payload: ExternalCaptionIngestRequest,
    store: CaptionReviewStore,
) -> ExternalCaptionIngestResult:
    """Parse appliance captions and write them to the shared review queue."""

    cues = parse_external_caption_payload(payload)
    received_at = payload.received_at or datetime.now(UTC)
    note = (
        f"External caption appliance {payload.source_label} "
        f"({payload.protocol}) received {received_at.isoformat()}."
    )
    review_items = [
        store.create(
            CaptionReviewItemCreate(
                review_item_id=f"{cue.cue_id}-review",
                asset_id=payload.asset_id,
                cue=cue,
                reviewer_note=note,
            )
        )
        for cue in cues
    ]
    return ExternalCaptionIngestResult(
        request_id=payload.request_id,
        asset_id=payload.asset_id,
        appliance_id=payload.appliance_id,
        protocol=payload.protocol,
        cue_count=len(review_items),
        review_items=review_items,
    )


def _parse_timed_text(payload: str) -> list[tuple[float, float, str]]:
    lines = payload.splitlines()
    cues: list[tuple[float, float, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        timing = _TIMING_RE.search(line)
        if not timing:
            index += 1
            continue
        text_lines: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip():
            candidate = lines[index].strip()
            if not candidate.isdigit():
                text_lines.append(candidate)
            index += 1
        text = _clean_caption_text(" ".join(text_lines))
        if text:
            cues.append(
                (
                    _parse_timestamp(timing.group("start")),
                    _parse_timestamp(timing.group("end")),
                    text,
                )
            )
        index += 1
    return cues


def _parse_decoded_cea_text(payload: str) -> list[tuple[float, float, str]]:
    cues: list[tuple[float, float, str]] = []
    for line in payload.splitlines():
        if not line.strip():
            continue
        match = _CEA_TIMING_RE.match(line)
        if match is None:
            raise ExternalCaptionParseError(
                "Decoded CEA caption lines must include start time, end time, and text."
            )
        text = _clean_caption_text(match.group("text"))
        if text:
            cues.append(
                (
                    _parse_timestamp(match.group("start")),
                    _parse_timestamp(match.group("end")),
                    text,
                )
            )
    return cues


def _parse_timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2])
    hours = int(parts[-3]) if len(parts) == 3 else 0
    return hours * 3600 + minutes * 60 + seconds


def _clean_caption_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("<br>", " ").replace("<br/>", " ")).strip()


def _safe_id_fragment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return safe or "caption-request"
