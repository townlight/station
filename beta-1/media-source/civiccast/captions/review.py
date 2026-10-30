# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Caption review queue contracts and in-memory store."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from civiccast.captions.models import CaptionCue

CaptionReviewStatus = Literal["pending", "approved", "edited", "rejected"]

#: Default review-queue language. Offline English transcription and every
#: pre-language-dimension review row are ``en``; the recorded-Spanish path
#: (:func:`civiccast.captions.vod.queue_translated_captions`) queues ``es``
#: rows so the two review passes stay cleanly separated by language.
DEFAULT_CAPTION_REVIEW_LANGUAGE = "en"


class CaptionReviewAudioEvidence(BaseModel):
    """Private local-audio identity retained for one live caption review cue."""

    model_config = ConfigDict(extra="forbid")

    source_path: Annotated[str, Field(min_length=1, max_length=1000)]
    source_start_seconds: Annotated[float, Field(ge=0)]
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source_bytes: Annotated[int, Field(gt=0)]

    @field_validator("source_path")
    @classmethod
    def _absolute_source_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("caption review audio evidence path must be absolute")
        return str(path)


class CaptionReviewItemRequest(BaseModel):
    """Public request body for adding a cue to the operator review queue."""

    model_config = ConfigDict(extra="forbid")

    review_item_id: Annotated[str, Field(min_length=1, max_length=160)]
    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    cue: CaptionCue
    #: BCP-47-ish language tag for the queue this row belongs to. English
    #: transcription rows are ``en``; recorded-Spanish translation rows are
    #: ``es``. Defaulted so every existing caller (and every pre-language
    #: row) is unambiguously English without a code change.
    language: Annotated[str, Field(min_length=2, max_length=12)] = DEFAULT_CAPTION_REVIEW_LANGUAGE
    reviewer_note: Annotated[str | None, Field(default=None, max_length=1000)] = None


class CaptionReviewItemCreate(CaptionReviewItemRequest):
    """Internal create contract, optionally carrying private live-audio evidence."""

    audio_evidence: CaptionReviewAudioEvidence | None = None


class CaptionReviewEdit(BaseModel):
    """Request body for editing a caption review item."""

    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=2000)]
    start_seconds: Annotated[float | None, Field(default=None, ge=0, allow_inf_nan=False)] = None
    end_seconds: Annotated[float | None, Field(default=None, ge=0, allow_inf_nan=False)] = None
    reviewer_note: Annotated[str | None, Field(default=None, max_length=1000)] = None

    @model_validator(mode="after")
    def validate_timing(self) -> CaptionReviewEdit:
        if (self.start_seconds is None) != (self.end_seconds is None):
            raise ValueError("Supply both start_seconds and end_seconds.")
        if self.start_seconds is not None:
            assert self.end_seconds is not None
            if self.end_seconds <= self.start_seconds:
                raise ValueError("end_seconds must be greater than start_seconds.")
        return self

    @field_validator("text")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        return " ".join(value.split())


def legacy_translation_source_window(
    cue: CaptionCue,
    language: str,
    sources: list[CaptionCue],
) -> tuple[float, float] | None:
    """Capture source provenance before an operator changes legacy target timing."""
    from civiccast.translate.service import translated_cue_id

    for source in sources:
        if translated_cue_id(source.cue_id, language, source_text=source.text) == cue.cue_id:
            return source.start_seconds, source.end_seconds
    return None


def edited_caption_cue(
    cue: CaptionCue,
    payload: CaptionReviewEdit,
    evidence: CaptionReviewAudioEvidence | None,
    review_item_id: str,
) -> CaptionCue:
    if payload.start_seconds is None or (
        payload.start_seconds == cue.start_seconds and payload.end_seconds == cue.end_seconds
    ):
        return cue
    changed = cue.model_copy(
        update={
            "start_seconds": payload.start_seconds,
            "end_seconds": payload.end_seconds,
        }
    )
    if evidence is not None:
        from civiccast.captions.review_media import (
            CaptionReviewClipError,
            verify_caption_review_audio_evidence_for_cue,
        )

        try:
            verify_caption_review_audio_evidence_for_cue(evidence, changed)
        except CaptionReviewClipError as exc:
            raise CaptionReviewAudioEvidenceRequiredError(review_item_id, str(exc)) from exc
    return changed


class CaptionReviewDecision(BaseModel):
    """Request body for approve/reject review decisions."""

    model_config = ConfigDict(extra="forbid")

    reviewer_note: Annotated[str | None, Field(default=None, max_length=1000)] = None
    low_confidence_acknowledged: bool = False


class CaptionReviewItemResponse(BaseModel):
    """Operator-facing caption review queue row."""

    model_config = ConfigDict(extra="forbid")

    review_item_id: str
    asset_id: str
    cue: CaptionCue
    translation_source_start_seconds: float | None = None
    translation_source_end_seconds: float | None = None
    language: str = DEFAULT_CAPTION_REVIEW_LANGUAGE
    status: CaptionReviewStatus
    original_text: str
    reviewed_text: str | None = None
    low_confidence: bool
    audio_evidence_available: bool = False
    reviewer_note: str | None = None
    created_at: datetime
    updated_at: datetime


class CaptionReviewItemAlreadyExistsError(Exception):
    """Raised when a review item id already exists."""

    def __init__(self, review_item_id: str) -> None:
        self.review_item_id = review_item_id
        super().__init__(f"Caption review item {review_item_id!r} already exists")


class CaptionReviewItemNotFoundError(Exception):
    """Raised when a review item id cannot be found."""

    def __init__(self, review_item_id: str) -> None:
        self.review_item_id = review_item_id
        super().__init__(f"Caption review item {review_item_id!r} not found")


class CaptionReviewLowConfidenceAcknowledgementRequiredError(Exception):
    """Raised when a low-confidence cue is approved without explicit review."""

    def __init__(self, review_item_id: str) -> None:
        self.review_item_id = review_item_id
        super().__init__(
            f"Caption review item {review_item_id!r} is low-confidence and requires "
            "explicit acknowledgement before approval"
        )


class CaptionReviewAudioEvidenceRequiredError(Exception):
    """Raised when low-confidence approval lacks verified covering audio."""

    def __init__(self, review_item_id: str, reason: str) -> None:
        self.review_item_id = review_item_id
        self.reason = reason
        super().__init__(
            f"Caption review item {review_item_id!r} requires valid audio evidence "
            f"that covers the cue before approval: {reason}"
        )


def require_low_confidence_approval_evidence(
    *,
    review_item_id: str,
    cue: CaptionCue,
    evidence: CaptionReviewAudioEvidence | None,
    decision: CaptionReviewDecision,
) -> None:
    """Fail closed unless a low-confidence decision has verified cue audio."""
    if not cue.low_confidence:
        return
    if not decision.low_confidence_acknowledged:
        raise CaptionReviewLowConfidenceAcknowledgementRequiredError(review_item_id)
    if evidence is None:
        raise CaptionReviewAudioEvidenceRequiredError(
            review_item_id,
            "no retained audio evidence is attached",
        )

    # Lazy import avoids a module cycle: review_media owns filesystem/WAV
    # validation and imports this module's evidence contract.
    from civiccast.captions.review_media import (
        CaptionReviewClipError,
        verify_caption_review_audio_evidence_for_cue,
    )

    try:
        verify_caption_review_audio_evidence_for_cue(evidence, cue)
    except CaptionReviewClipError as error:
        raise CaptionReviewAudioEvidenceRequiredError(
            review_item_id,
            str(error),
        ) from error


class CaptionReviewStore(Protocol):
    """Storage contract used by the staff caption review API."""

    def create(self, payload: CaptionReviewItemCreate) -> CaptionReviewItemResponse:
        """Persist a new review item."""

    def get(self, review_item_id: str) -> CaptionReviewItemResponse | None:
        """Return one review item when present."""

    def get_audio_evidence(
        self,
        review_item_id: str,
    ) -> CaptionReviewAudioEvidence | None:
        """Return private live-audio evidence metadata when present."""

    def list(
        self,
        *,
        asset_id: str | None = None,
        status: CaptionReviewStatus | None = None,
        language: str | None = None,
    ) -> list[CaptionReviewItemResponse]:
        """Return review items filtered for operator queue views.

        ``language`` scopes the queue to one review pass (``en`` transcription
        vs ``es`` translation); the recorded-Spanish path relies on it to keep
        the two passes separate on a shared ``asset_id``.
        """

    def approve(
        self,
        review_item_id: str,
        payload: CaptionReviewDecision,
    ) -> CaptionReviewItemResponse:
        """Approve a review item."""

    def edit(self, review_item_id: str, payload: CaptionReviewEdit) -> CaptionReviewItemResponse:
        """Edit a review item."""

    def reject(
        self,
        review_item_id: str,
        payload: CaptionReviewDecision,
    ) -> CaptionReviewItemResponse:
        """Reject a review item."""


class InMemoryCaptionReviewStore:
    """In-memory review queue store for tests and non-DB development."""

    def __init__(self) -> None:
        self._items: dict[str, CaptionReviewItemResponse] = {}
        self._audio_evidence: dict[str, CaptionReviewAudioEvidence] = {}
        # Insertion order, independent of ``created_at``. Two rows created in
        # the same tight loop (e.g. queueing every cue of a job in one pass)
        # can get the *identical* microsecond from ``datetime.now(UTC)`` --
        # timer resolution on some hosts is coarser than the loop -- and
        # ``created_at`` alone can no longer tell them apart. ``list()``
        # falls back to ``review_item_id`` on such ties, which sorts
        # lexically rather than by creation order (e.g. cue id
        # ``...:cue-000000-02:es:...`` sorts before ``...:cue-000000:es:...``
        # because ``-`` (0x2D) < ``:`` (0x3A)) and silently reorders rows a
        # caller reads by list index. A per-store monotonic counter, recorded
        # on each row at create time, gives a total order that always
        # matches creation order, tie or no tie.
        self._sequence: dict[str, int] = {}
        self._next_sequence = 0

    def create(self, payload: CaptionReviewItemCreate) -> CaptionReviewItemResponse:
        if payload.review_item_id in self._items:
            raise CaptionReviewItemAlreadyExistsError(payload.review_item_id)
        now = datetime.now(UTC)
        item = CaptionReviewItemResponse(
            review_item_id=payload.review_item_id,
            asset_id=payload.asset_id,
            cue=payload.cue,
            language=payload.language,
            status="pending",
            original_text=payload.cue.text,
            reviewed_text=None,
            low_confidence=payload.cue.low_confidence,
            audio_evidence_available=payload.audio_evidence is not None,
            reviewer_note=payload.reviewer_note,
            created_at=now,
            updated_at=now,
        )
        self._items[item.review_item_id] = item
        self._sequence[item.review_item_id] = self._next_sequence
        self._next_sequence += 1
        if payload.audio_evidence is not None:
            self._audio_evidence[item.review_item_id] = payload.audio_evidence
        return deepcopy(item)

    def get(self, review_item_id: str) -> CaptionReviewItemResponse | None:
        item = self._items.get(review_item_id)
        return deepcopy(item) if item is not None else None

    def get_audio_evidence(
        self,
        review_item_id: str,
    ) -> CaptionReviewAudioEvidence | None:
        evidence = self._audio_evidence.get(review_item_id)
        return evidence.model_copy(deep=True) if evidence is not None else None

    def list_with_audio_evidence(
        self,
    ) -> list[tuple[CaptionReviewItemResponse, CaptionReviewAudioEvidence | None]]:
        """Return review rows and private evidence without changing the public API."""

        return [(item, self.get_audio_evidence(item.review_item_id)) for item in self.list()]

    def list(
        self,
        *,
        asset_id: str | None = None,
        status: CaptionReviewStatus | None = None,
        language: str | None = None,
    ) -> list[CaptionReviewItemResponse]:
        rows = list(self._items.values())
        if asset_id is not None:
            rows = [row for row in rows if row.asset_id == asset_id]
        if status is not None:
            rows = [row for row in rows if row.status == status]
        if language is not None:
            rows = [row for row in rows if row.language == language]
        return [
            deepcopy(row)
            for row in sorted(
                rows,
                key=lambda item: (
                    item.created_at,
                    self._sequence[item.review_item_id],
                    item.review_item_id,
                ),
            )
        ]

    def approve(
        self,
        review_item_id: str,
        payload: CaptionReviewDecision,
    ) -> CaptionReviewItemResponse:
        item = self._require_item(review_item_id)
        require_low_confidence_approval_evidence(
            review_item_id=review_item_id,
            cue=item.cue,
            evidence=self._audio_evidence.get(review_item_id),
            decision=payload,
        )
        reviewed_text = item.reviewed_text or item.original_text
        return self._replace(
            item,
            status="approved",
            reviewed_text=reviewed_text,
            reviewer_note=payload.reviewer_note,
        )

    def edit(self, review_item_id: str, payload: CaptionReviewEdit) -> CaptionReviewItemResponse:
        item = self._require_item(review_item_id)
        if (
            payload.start_seconds is not None
            and item.language != "en"
            and item.translation_source_start_seconds is None
        ):
            sources = [
                row.cue.model_copy(update={"text": row.reviewed_text or row.cue.text})
                for row in self.list(asset_id=item.asset_id, language="en")
            ]
            window = legacy_translation_source_window(item.cue, item.language, sources)
            if window is not None:
                item = item.model_copy(
                    update={
                        "translation_source_start_seconds": window[0],
                        "translation_source_end_seconds": window[1],
                    }
                )
        item = item.model_copy(
            update={
                "cue": edited_caption_cue(
                    item.cue,
                    payload,
                    self._audio_evidence.get(review_item_id),
                    review_item_id,
                )
            }
        )
        return self._replace(
            item,
            status="edited",
            reviewed_text=payload.text,
            reviewer_note=payload.reviewer_note,
        )

    def reject(
        self,
        review_item_id: str,
        payload: CaptionReviewDecision,
    ) -> CaptionReviewItemResponse:
        item = self._require_item(review_item_id)
        return self._replace(
            item,
            status="rejected",
            reviewed_text=None,
            reviewer_note=payload.reviewer_note,
        )

    def _require_item(self, review_item_id: str) -> CaptionReviewItemResponse:
        item = self._items.get(review_item_id)
        if item is None:
            raise CaptionReviewItemNotFoundError(review_item_id)
        return item

    def _replace(
        self,
        item: CaptionReviewItemResponse,
        *,
        status: CaptionReviewStatus,
        reviewed_text: str | None,
        reviewer_note: str | None,
    ) -> CaptionReviewItemResponse:
        updated = item.model_copy(
            update={
                "status": status,
                "reviewed_text": reviewed_text,
                "reviewer_note": reviewer_note,
                "updated_at": datetime.now(UTC),
            }
        )
        self._items[item.review_item_id] = updated
        return deepcopy(updated)
