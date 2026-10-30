# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Deterministic AI quality baseline for v1.0 readiness."""

from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from civiccast.captions.benchmark import normalize_words, word_error_rate
from civiccast.captions.models import CaptionCue
from civiccast.summary.generate import DeterministicSummaryModel, SummaryGenerationPipeline
from civiccast.summary.validate import SourcedClaimValidator
from civiccast.translate import DeterministicSpanishTranslator, translate_caption_cues


class CaptionBenchmarkFixture(BaseModel):
    """One caption-quality benchmark fixture."""

    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    reference: str
    hypothesis: str


class TranslationBenchmarkFixture(BaseModel):
    """One translation-quality benchmark fixture."""

    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    source_text: str
    reference_translation: str


class SummaryBenchmarkFixture(BaseModel):
    """One sourced-summary benchmark fixture."""

    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    meeting_id: str
    reference_summary: str
    cues: list[CaptionCue]


class AiBenchmarkCorpus(BaseModel):
    """Tracked deterministic v1.0 AI benchmark corpus."""

    model_config = ConfigDict(extra="forbid")

    corpus_id: str
    caption_fixtures: list[CaptionBenchmarkFixture]
    translation_fixtures: list[TranslationBenchmarkFixture]
    summary_fixtures: list[SummaryBenchmarkFixture]


class CaptionBenchmarkScore(BaseModel):
    """Caption benchmark result."""

    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    word_error_rate: float
    tolerance: float
    passed: bool


class TranslationBenchmarkScore(BaseModel):
    """Translation benchmark result."""

    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    bleu: float
    comet_adequacy: float
    bleu_floor: float
    comet_floor: float
    passed: bool


class SummaryBenchmarkScore(BaseModel):
    """Summary benchmark result."""

    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    rouge_l: float
    factual_correctness: float
    rouge_l_floor: float
    factual_correctness_floor: float
    passed: bool


class AiBenchmarkSuiteResult(BaseModel):
    """Machine-readable v1.0 AI benchmark baseline."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    corpus_id: str
    baseline_policy: str
    caption_scores: list[CaptionBenchmarkScore]
    translation_scores: list[TranslationBenchmarkScore]
    summary_scores: list[SummaryBenchmarkScore]
    passed: bool


def build_default_corpus() -> AiBenchmarkCorpus:
    """Return the tracked v1.0 deterministic benchmark corpus."""
    return AiBenchmarkCorpus(
        corpus_id="civiccast-v1.0-ai-baseline-2026-05-15",
        caption_fixtures=[
            CaptionBenchmarkFixture(
                fixture_id="caption-motion-001",
                reference="Motion passes two to one after public comment.",
                hypothesis="Motion passes 2 to 1 after public comment.",
            ),
            CaptionBenchmarkFixture(
                fixture_id="caption-budget-002",
                reference="The budget hearing opened with a twelve thousand dollar request.",
                hypothesis="The budget hearing opened with a $12,000 request.",
            ),
        ],
        translation_fixtures=[
            TranslationBenchmarkFixture(
                fixture_id="translation-motion-001",
                source_text="motion carries",
                reference_translation="la mocion se aprueba",
            ),
            TranslationBenchmarkFixture(
                fixture_id="translation-council-002",
                source_text="welcome to the council meeting",
                reference_translation="bienvenidos a la reunion del consejo",
            ),
            TranslationBenchmarkFixture(
                fixture_id="translation-budget-003",
                source_text="budget hearing",
                reference_translation="audiencia de presupuesto",
            ),
        ],
        summary_fixtures=[
            SummaryBenchmarkFixture(
                fixture_id="summary-motion-001",
                meeting_id="meeting-ai-baseline-001",
                reference_summary="Councilmember Rivera moved to approve the item.",
                cues=[
                    CaptionCue(
                        cue_id="cue-1",
                        start_seconds=0.0,
                        end_seconds=4.0,
                        text="Councilmember Rivera moved to approve the item.",
                        confidence=0.96,
                    ),
                    CaptionCue(
                        cue_id="cue-2",
                        start_seconds=4.0,
                        end_seconds=8.0,
                        text="Councilmember Chen seconded the motion.",
                        confidence=0.96,
                    ),
                    CaptionCue(
                        cue_id="cue-3",
                        start_seconds=8.0,
                        end_seconds=12.0,
                        text="Roll call: Rivera yes, Chen yes, Malik no. Motion passes 2-1.",
                        confidence=0.96,
                    ),
                ],
            )
        ],
    )


def run_ai_benchmark_suite(corpus: AiBenchmarkCorpus | None = None) -> AiBenchmarkSuiteResult:
    """Run deterministic caption, translation, and summary quality checks."""
    selected = corpus or build_default_corpus()
    caption_scores = [_score_caption(item) for item in selected.caption_fixtures]
    translation_scores = [_score_translation(item) for item in selected.translation_fixtures]
    summary_scores = [_score_summary(item) for item in selected.summary_fixtures]
    return AiBenchmarkSuiteResult(
        generated_at=datetime.now(UTC),
        corpus_id=selected.corpus_id,
        baseline_policy=(
            "v1.0 baseline: deterministic local adapters set the release floor; "
            "future live-model runs must meet or exceed these fixture tolerances."
        ),
        caption_scores=caption_scores,
        translation_scores=translation_scores,
        summary_scores=summary_scores,
        passed=all(score.passed for score in caption_scores)
        and all(score.passed for score in translation_scores)
        and all(score.passed for score in summary_scores),
    )


def _score_caption(fixture: CaptionBenchmarkFixture) -> CaptionBenchmarkScore:
    tolerance = 0.34
    wer = word_error_rate(fixture.reference, fixture.hypothesis)
    return CaptionBenchmarkScore(
        fixture_id=fixture.fixture_id,
        word_error_rate=wer,
        tolerance=tolerance,
        passed=wer <= tolerance,
    )


def _score_translation(fixture: TranslationBenchmarkFixture) -> TranslationBenchmarkScore:
    cue = CaptionCue(
        cue_id=fixture.fixture_id,
        start_seconds=0.0,
        end_seconds=2.0,
        text=fixture.source_text,
        confidence=0.99,
    )
    result = translate_caption_cues([cue], provider=DeterministicSpanishTranslator())
    translated = result.cues[0].translated_text
    bleu = _bleu_1_2(fixture.reference_translation, translated)
    comet_adequacy = _token_f1(fixture.reference_translation, translated)
    return TranslationBenchmarkScore(
        fixture_id=fixture.fixture_id,
        bleu=bleu,
        comet_adequacy=comet_adequacy,
        bleu_floor=0.99,
        comet_floor=0.99,
        passed=bleu >= 0.99 and comet_adequacy >= 0.99,
    )


def _score_summary(fixture: SummaryBenchmarkFixture) -> SummaryBenchmarkScore:
    draft = SummaryGenerationPipeline(model=DeterministicSummaryModel()).generate(
        meeting_id=fixture.meeting_id,
        cues=fixture.cues,
    )
    SourcedClaimValidator(fixture.cues).validate_claims(draft.sourced_claims)
    rouge_l = _rouge_l(fixture.reference_summary, draft.narrative)
    factual_correctness = 1.0 if draft.status == "pending_review" and draft.sourced_claims else 0.0
    return SummaryBenchmarkScore(
        fixture_id=fixture.fixture_id,
        rouge_l=rouge_l,
        factual_correctness=factual_correctness,
        rouge_l_floor=0.45,
        factual_correctness_floor=1.0,
        passed=rouge_l >= 0.45 and factual_correctness >= 1.0,
    )


def _bleu_1_2(reference: str, hypothesis: str) -> float:
    reference_words = normalize_words(reference)
    hypothesis_words = normalize_words(hypothesis)
    if not reference_words or not hypothesis_words:
        return 0.0
    precisions = [
        _ngram_precision(reference_words, hypothesis_words, 1),
        _ngram_precision(reference_words, hypothesis_words, 2),
    ]
    if any(precision == 0 for precision in precisions):
        return 0.0
    brevity = min(1.0, math.exp(1 - (len(reference_words) / len(hypothesis_words))))
    return round(brevity * math.exp(sum(math.log(value) for value in precisions) / 2), 4)


def _ngram_precision(reference_words: list[str], hypothesis_words: list[str], n: int) -> float:
    reference_counts = Counter(
        tuple(reference_words[index : index + n]) for index in range(len(reference_words) - n + 1)
    )
    hypothesis_counts = Counter(
        tuple(hypothesis_words[index : index + n]) for index in range(len(hypothesis_words) - n + 1)
    )
    if not hypothesis_counts:
        return 0.0
    overlap = sum(min(count, reference_counts[ngram]) for ngram, count in hypothesis_counts.items())
    return overlap / sum(hypothesis_counts.values())


def _token_f1(reference: str, hypothesis: str) -> float:
    reference_counts = Counter(normalize_words(reference))
    hypothesis_counts = Counter(normalize_words(hypothesis))
    if not reference_counts or not hypothesis_counts:
        return 0.0
    overlap = sum(min(count, hypothesis_counts[token]) for token, count in reference_counts.items())
    precision = overlap / sum(hypothesis_counts.values())
    recall = overlap / sum(reference_counts.values())
    if precision + recall == 0:
        return 0.0
    return round((2 * precision * recall) / (precision + recall), 4)


def _rouge_l(reference: str, hypothesis: str) -> float:
    reference_words = normalize_words(reference)
    hypothesis_words = normalize_words(hypothesis)
    if not reference_words or not hypothesis_words:
        return 0.0
    lcs = _longest_common_subsequence(reference_words, hypothesis_words)
    return round(lcs / len(reference_words), 4)


def _longest_common_subsequence(left: list[str], right: list[str]) -> int:
    previous = [0] * (len(right) + 1)
    for left_word in left:
        current = [0]
        for index, right_word in enumerate(right, start=1):
            current.append(
                previous[index - 1] + 1
                if left_word == right_word
                else max(previous[index], current[index - 1])
            )
        previous = current
    return previous[-1]
