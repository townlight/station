# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Ollama summary adapter and validation seam for v1.1 release proof."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

import psutil

from civiccast.ai_models.catalog import catalog_tiers_for
from civiccast.ai_runtime.evidence import RuntimeEvidence
from civiccast.ai_runtime.ollama_client import (
    DEFAULT_OLLAMA_BASE_URL,
    OllamaRuntimeUnavailableError,
    generate_with_ollama,
    get_ollama_model_manifest,
)
from civiccast.captions import CaptionCue

_SUMMARY_MODEL_TAG = "gemma4:e4b"
# Keep this reserve fixed so a selected summary tier cannot consume the station's
# memory budget for live broadcast work.
_BROADCAST_HEADROOM_GB = 8
_GIB = 1024**3


class SummaryResourceDeferredError(OllamaRuntimeUnavailableError):
    """Raised before inference when the selected tier would consume broadcast RAM."""


def _require_summary_headroom(model_tag: str) -> None:
    """Keep the selected tier's catalog RAM floor available alongside broadcast reserve."""

    tiers = catalog_tiers_for("summary")
    tier = next((candidate for candidate in tiers if candidate.model_id == model_tag), None)
    model_floor_gb = tier.min_ram_gb if tier is not None else 8
    required_available_gb = model_floor_gb + _BROADCAST_HEADROOM_GB
    available_bytes = psutil.virtual_memory().available
    if available_bytes < required_available_gb * _GIB:
        available_gb = available_bytes / _GIB
        smaller_tier = next(
            (candidate for candidate in tiers if candidate.min_ram_gb < model_floor_gb), None
        )
        recovery_action = "Wait for station memory to recover before retrying."
        if smaller_tier is not None:
            recovery_action = (
                "Wait for station memory to recover or select the smaller model "
                f"({smaller_tier.model_id})."
            )
        raise SummaryResourceDeferredError(
            f"Summary generation was deferred before loading {model_tag}: "
            f"{available_gb:.1f} GiB is available, while this tier needs "
            f"{model_floor_gb} GiB plus {_BROADCAST_HEADROOM_GB} GiB reserved for broadcast. "
            + recovery_action
        )


@dataclass(frozen=True)
class OllamaSummaryProvenance:
    """Live Ollama model provenance captured before summary generation."""

    digest: str
    ollama_version: str
    manifest_source: str
    evidence_line: str


@dataclass(frozen=True)
class OllamaSummaryModel:
    """Release summary adapter descriptor."""

    model_tag: str
    provenance: OllamaSummaryProvenance
    base_url: str = DEFAULT_OLLAMA_BASE_URL

    @classmethod
    def for_release(
        cls,
        *,
        model_tag: str = _SUMMARY_MODEL_TAG,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
    ) -> OllamaSummaryModel:
        manifest = get_ollama_model_manifest(model_tag, base_url=base_url)
        evidence = RuntimeEvidence(
            runtime="ollama",
            model=manifest.model,
            compute=None,
            digest=manifest.digest,
            runtime_version=manifest.runtime_version,
            manifest_source=manifest.manifest_source,
        )
        return cls(
            model_tag=manifest.model,
            provenance=OllamaSummaryProvenance(
                digest=manifest.digest,
                ollama_version=manifest.runtime_version,
                manifest_source=manifest.manifest_source,
                evidence_line=evidence.to_machine_line(),
            ),
            base_url=base_url,
        )

    def generate(
        self,
        *,
        meeting_id: str,
        cues: list[CaptionCue],
        prompt_version: str,
    ) -> dict[str, Any]:
        """Generate structured summary JSON with the live local Ollama model."""

        _require_summary_headroom(self.model_tag)
        prompt = _summary_prompt(
            meeting_id=meeting_id,
            cues=cues,
            prompt_version=prompt_version,
            evidence_line=self.provenance.evidence_line,
        )
        response = generate_with_ollama(model=self.model_tag, prompt=prompt, base_url=self.base_url)
        return _parse_model_json(response)


@dataclass(frozen=True)
class SummaryValidationResult:
    """Closed validation result for model output."""

    status: Literal["ok", "refused"]
    operator_action: str


def validate_ollama_summary_output(
    *,
    transcript_text: str,
    model_output: str,
    citations: list[str],
) -> SummaryValidationResult:
    """Refuse summary text containing claims unsupported by transcript citations."""

    supported_text = "\n".join([transcript_text, *citations]).casefold()
    unsupported = [
        sentence
        for sentence in _sentences(model_output)
        if sentence.casefold() not in supported_text
    ]
    if unsupported:
        return SummaryValidationResult(
            status="refused",
            operator_action=(
                "Ollama summary refused because an unsupported claim was not tied "
                "to transcript evidence. Remove or cite: " + unsupported[0]
            ),
        )
    return SummaryValidationResult(
        status="ok",
        operator_action="Summary output is tied to supplied transcript citations.",
    )


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in text.replace("?", ".").replace("!", ".").split(".")]
    return [part + "." for part in parts if part]


def _summary_prompt(
    *,
    meeting_id: str,
    cues: list[CaptionCue],
    prompt_version: str,
    evidence_line: str,
) -> str:
    transcript = "\n".join(
        f"- {cue.cue_id} [{cue.start_seconds:.2f}-{cue.end_seconds:.2f}]: {cue.text}"
        for cue in cues
    )
    return (
        "You are CivicCast's local summary model. Return only JSON with keys "
        "`narrative` and `sourced_claims`. Every sourced_claim must include "
        "`claim_id`, `text`, `claim_type`, and `transcript_ranges`. The only "
        "allowed claim_type values are `narrative` and `quantitative`; every "
        "transcript range must cite one cue_id from the transcript with its "
        "start_seconds and end_seconds. Refuse by returning an empty narrative "
        "and empty sourced_claims if the transcript does not support a claim.\n\n"
        f"meeting_id: {meeting_id}\n"
        f"prompt_version: {prompt_version}\n"
        f"runtime_evidence: {evidence_line}\n"
        f"transcript:\n{transcript}\n"
    )


def _parse_model_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            return {"narrative": "", "sourced_claims": []}
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return {"narrative": "", "sourced_claims": []}
    if not isinstance(parsed, dict):
        return {"narrative": "", "sourced_claims": []}
    return parsed
