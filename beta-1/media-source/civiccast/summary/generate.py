# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""v0.6 summary generation pipeline with deterministic local fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError

from civiccast.captions import CaptionCue
from civiccast.summary.extract import extract_quantitative_facts
from civiccast.summary.fingerprint import sha256_fingerprint
from civiccast.summary.minutes import transcript_fingerprint
from civiccast.summary.models import ModelProvenance, SourcedClaim, SummaryDraft
from civiccast.summary.ollama import OllamaSummaryModel
from civiccast.summary.validate import SourcedClaimValidator, UnsupportedSummaryClaimError

PROMPT_VERSION = "summary-v0.6"
RETRY_PROMPT_VERSION = "summary-v0.6-retry-sourced-claims"
EXTRACTION_VERSION = "summary-extract-v0.6"


class SummaryModel(Protocol):
    """Protocol for a local or external summary model adapter."""

    def generate(
        self,
        *,
        meeting_id: str,
        cues: list[CaptionCue],
        prompt_version: str,
    ) -> dict[str, Any]:
        """Return structured model output."""


class DeterministicSummaryModel:
    """Test fixture summary model; defaults to source-backed local output."""

    def __init__(self, outputs: list[dict[str, Any]] | None = None) -> None:
        self._outputs = list(outputs or [])
        self.prompt_versions: list[str] = []

    def generate(
        self,
        *,
        meeting_id: str,
        cues: list[CaptionCue],
        prompt_version: str,
    ) -> dict[str, Any]:
        self.prompt_versions.append(prompt_version)
        if self._outputs:
            return self._outputs.pop(0)
        facts = extract_quantitative_facts(cues)
        if facts:
            fact = facts[0]
            return {
                "narrative": fact.text,
                "sourced_claims": [
                    {
                        "claim_id": "claim-1",
                        "text": fact.text,
                        "claim_type": "quantitative",
                        "transcript_ranges": [fact.source_range.model_dump()],
                    }
                ],
            }
        if cues:
            cue = cues[0]
            return {
                "narrative": cue.text,
                "sourced_claims": [
                    {
                        "claim_id": "claim-1",
                        "text": cue.text,
                        "claim_type": "narrative",
                        "transcript_ranges": [
                            {
                                "cue_id": cue.cue_id,
                                "start_seconds": cue.start_seconds,
                                "end_seconds": cue.end_seconds,
                            }
                        ],
                    }
                ],
            }
        return {"narrative": "", "sourced_claims": []}


class SummaryGenerationPipeline:
    """Generate, validate, retry once, and fail closed on unsupported claims."""

    def __init__(self, model: SummaryModel | None = None) -> None:
        self._model = model or OllamaSummaryModel.for_release()

    def generate(self, *, meeting_id: str, cues: list[CaptionCue]) -> SummaryDraft:
        errors: list[str] = []
        for prompt_version in (PROMPT_VERSION, RETRY_PROMPT_VERSION):
            output = self._model.generate(
                meeting_id=meeting_id,
                cues=cues,
                prompt_version=prompt_version,
            )
            try:
                claims = [SourcedClaim.model_validate(raw) for raw in output["sourced_claims"]]
                facts = extract_quantitative_facts(cues)
                has_quantitative_claim = any(claim.claim_type == "quantitative" for claim in claims)
                if facts and not has_quantitative_claim:
                    raise UnsupportedSummaryClaimError(
                        "quantitative transcript facts require sourced timestamp evidence"
                    )
                SourcedClaimValidator(cues).validate_claims(claims)
                return self._draft(
                    meeting_id=meeting_id,
                    cues=cues,
                    status="pending_review",
                    narrative=str(output["narrative"]),
                    sourced_claims=claims,
                    prompt_version=prompt_version,
                    operator_message=None,
                )
            except (KeyError, TypeError, ValidationError, UnsupportedSummaryClaimError) as exc:
                errors.append(str(exc))

        return self._draft(
            meeting_id=meeting_id,
            cues=cues,
            status="refused",
            narrative="",
            sourced_claims=[],
            prompt_version=RETRY_PROMPT_VERSION,
            operator_message=(
                "Summary refused because the model output could not be tied to "
                "committed transcript timestamp evidence. Review the transcript "
                "and regenerate after correcting missing or ambiguous cues."
            ),
            errors=errors,
        )

    def _draft(
        self,
        *,
        meeting_id: str,
        cues: list[CaptionCue],
        status: str,
        narrative: str,
        sourced_claims: list[SourcedClaim],
        prompt_version: str,
        operator_message: str | None,
        errors: list[str] | None = None,
    ) -> SummaryDraft:
        # Record the provenance of the model that actually ran. Real adapters
        # (OllamaSummaryModel, CloudSummaryModel) expose ``model_tag`` and, for
        # the local path, a ``provenance`` with the live manifest digest and
        # runtime version; the pipeline previously discarded all of it and
        # stamped every persisted, records-bound draft with a fixture tag, so
        # the durable audit fingerprint misstated which model produced an
        # operator-approved summary. Only a genuine test fixture (which exposes
        # no ``model_tag``) falls back to the fixture label.
        model = self._model
        model_provenance = getattr(model, "provenance", None)
        provenance = ModelProvenance(
            model_tag=getattr(model, "model_tag", None) or "fixture-summary-no-real-model",
            model_digest=getattr(model_provenance, "digest", None),
            ollama_version=getattr(model_provenance, "ollama_version", None),
            prompt_version=prompt_version,
            extraction_version=EXTRACTION_VERSION,
            runtime_parameters={
                "temperature": 0,
                "source_transcript_fingerprint": transcript_fingerprint(cues),
            },
            generated_at=datetime.now(UTC),
        )
        summary_id = f"summary-{uuid4().hex}"
        fingerprint = sha256_fingerprint(
            {
                "meeting_id": meeting_id,
                "status": status,
                "narrative": narrative,
                "sourced_claims": [claim.model_dump(mode="json") for claim in sourced_claims],
                "provenance": provenance.model_dump(mode="json"),
                "errors": errors or [],
            }
        )
        return SummaryDraft(
            summary_id=summary_id,
            meeting_id=meeting_id,
            status=status,  # type: ignore[arg-type]
            narrative=narrative,
            sourced_claims=sourced_claims,
            provenance=provenance,
            audit_fingerprint=fingerprint,
            operator_message=operator_message,
        )
