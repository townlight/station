# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Ollama recorded translation adapter and legacy BLEU wiring helper."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from civiccast.ai_runtime.evidence import RuntimeEvidence
from civiccast.ai_runtime.ollama_client import (
    DEFAULT_OLLAMA_BASE_URL,
    generate_with_ollama,
    get_ollama_model_manifest,
)
from civiccast.recorded_languages import RECORDED_TRANSLATION_LANGUAGES

_TRANSLATION_MODEL_TAG = "translategemma:4b"


@dataclass(frozen=True)
class OllamaSpanishTranslator:
    """Recorded translation adapter; legacy class name preserves callers."""

    model_tag: str
    runtime_evidence: RuntimeEvidence
    base_url: str = DEFAULT_OLLAMA_BASE_URL

    @classmethod
    def for_release(
        cls,
        *,
        model_tag: str = _TRANSLATION_MODEL_TAG,
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
    ) -> OllamaSpanishTranslator:
        manifest = get_ollama_model_manifest(model_tag, base_url=base_url)
        return cls(
            model_tag=manifest.model,
            runtime_evidence=RuntimeEvidence(
                runtime="ollama",
                model=manifest.model,
                compute=None,
                digest=manifest.digest,
                runtime_version=manifest.runtime_version,
                manifest_source=manifest.manifest_source,
            ),
            base_url=base_url,
        )

    def translate(self, text: str) -> str:
        """Translate English civic text to Spanish with local Ollama."""

        return self.translate_text(text, source_language="en", target_language="es")

    def translate_text(
        self,
        text: str,
        *,
        source_language: str,
        target_language: str,
        glossary: Mapping[str, str] | None = None,
    ) -> str:
        """TranslationProvider-compatible boundary for caption translation."""

        if source_language != "en" or target_language not in RECORDED_TRANSLATION_LANGUAGES:
            raise ValueError("Recorded translation supports English to es, zh-Hans, fil, vi or ar.")
        target = RECORDED_TRANSLATION_LANGUAGES[target_language]
        terminology = ""
        if glossary:
            terminology = "\nUse these approved terms: " + "; ".join(
                f"{source} = {translated}" for source, translated in glossary.items()
            )
        prompt = (
            f"Translate the following English municipal meeting text to {target}. "
            f"Return only the {target} translation, no commentary."
            f"{terminology}\n\n{text}"
        )
        result = generate_with_ollama(model=self.model_tag, prompt=prompt, base_url=self.base_url)
        if not result.strip():
            raise ValueError(
                f"The translation model returned no {target} text. Retry or repair the local model."
            )
        return result.strip()


@dataclass(frozen=True)
class TranslationReleaseGateResult:
    """Result of a v1.1 translation floor check."""

    status: Literal["ok", "failed"]
    bleu: float
    evidence_line: str
    operator_action: str


def evaluate_translation_release_gate(
    *,
    candidate: str,
    reference: str,
    evidence_line: str,
) -> TranslationReleaseGateResult:
    """Score a small exact-match BLEU floor for release-gate wiring."""

    try:
        from sacrebleu import sentence_bleu
    except ImportError:
        bleu = 100.0 if candidate.strip().casefold() == reference.strip().casefold() else 0.0
    else:
        bleu = float(sentence_bleu(candidate, [reference]).score)
    has_signal = "runtime=ollama" in evidence_line and "digest=sha256:" in evidence_line
    if bleu >= 5 and has_signal:
        return TranslationReleaseGateResult(
            status="ok",
            bleu=bleu,
            evidence_line=evidence_line,
            operator_action="Translation BLEU floor and Ollama runtime signal are present.",
        )
    return TranslationReleaseGateResult(
        status="failed",
        bleu=bleu,
        evidence_line=evidence_line,
        operator_action="Rerun the translation proof with the live Ollama translation model.",
    )
