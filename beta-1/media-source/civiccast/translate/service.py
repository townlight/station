# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Deterministic caption translation services for v0.9."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Protocol

from civiccast.captions.hls import CaptionHlsTrack
from civiccast.captions.models import CaptionCue
from civiccast.translate.models import (
    TranslationBatchResult,
    TranslationCue,
    TranslationModelRegistration,
    TranslationTarget,
)

PLACEHOLDER_RE = re.compile(r"§§\d{4}§§")


class PlaceholderIntegrityError(ValueError):
    """Raised when translation drops or mutates protected glossary placeholders."""


class TranslationProvider(Protocol):
    """Runtime boundary for caption translation backends."""

    def translate_text(
        self,
        text: str,
        *,
        source_language: str,
        target_language: str,
        glossary: Mapping[str, str] | None = None,
    ) -> str:
        """Translate text while preserving protected placeholder tokens."""


class DeterministicSpanishTranslator:
    """Local Spanish adapter used by tests and CI.

    Production can swap this for the Ollama TranslateGemma boundary without
    changing the caption/HLS contracts. The deterministic adapter intentionally
    covers civic-meeting phrases used in fixtures and leaves unknown text
    readable instead of fabricating opaque output.
    """

    _phrasebook: Mapping[str, str] = {
        "motion carries": "la mocion se aprueba",
        "motion carries.": "la mocion se aprueba.",
        "welcome to the meeting": "bienvenidos a la reunion",
        "welcome to the council meeting": "bienvenidos a la reunion del consejo",
        "council meeting": "reunion del consejo",
        "public comment": "comentario publico",
        "budget hearing": "audiencia de presupuesto",
        "call to order": "inicio de la sesion",
    }

    def translate_text(
        self,
        text: str,
        *,
        source_language: str,
        target_language: str,
        glossary: Mapping[str, str] | None = None,
    ) -> str:
        if source_language != "en" or target_language != "es":
            raise ValueError("DeterministicSpanishTranslator supports en -> es only.")

        protected = _extract_placeholders(text)
        translated = self._phrasebook.get(text.casefold(), f"[es] {text}")
        for source, replacement in (glossary or {}).items():
            translated = re.sub(re.escape(source), replacement, translated, flags=re.IGNORECASE)
        _assert_placeholders_preserved(protected, translated)
        return translated


def available_translation_models() -> list[TranslationModelRegistration]:
    """Return the v0.9 model registry."""

    return [
        TranslationModelRegistration(
            key="translate-gemma-4b-ollama",
            provider="ollama",
            model_id="translate-gemma:4b",
            role="primary",
            notes="Primary production contract for v0.9 live caption translation.",
        ),
        TranslationModelRegistration(
            key="madlad-400",
            provider="external",
            model_id="MADLAD-400",
            role="alternate",
            notes="Registered alternate multilingual translation model.",
        ),
        TranslationModelRegistration(
            key="deterministic-es-ci",
            provider="local-deterministic",
            model_id="civiccast-deterministic-es",
            role="ci-proof",
            notes="Deterministic local adapter for CI and release proofs.",
        ),
    ]


def translated_cue_id(
    source_cue_id: str,
    target_language: str,
    *,
    source_text: str | None = None,
    source_window: tuple[float, float] | None = None,
) -> str:
    """Return the cue id a translation of ``source_cue_id`` carries.

    One definition of the rule, because callers on both sides depend on it
    and a silent disagreement between them is a data-loss bug rather than a
    cosmetic one: the recorded-Spanish review path *mints* the id in
    :func:`civiccast.captions.vod.queue_translated_captions` and *predicts*
    it -- without translating -- in
    :meth:`civiccast.captions.vod_job.OfflineCaptionJobWorker
    ._resolve_spanish_review`, to work out which approved English cues
    already have a translated review row. If prediction and minting ever
    diverged, every cue would look missing (endless re-translation) or none
    would (a short track published as complete).

    ``source_text`` binds the id to the **exact source wording** it was
    translated from, by appending a short digest of that text. Without it,
    the id says only *which cue* a translation came from, not *which
    version* of it -- so an operator who corrects an English cue after the
    Spanish pass has already run leaves a Spanish row that still says the
    old thing, and an id-only match accepts it as present and publishes it.
    That is not a hypothetical: "the motion FAILS", corrected in English
    after translation, shipped as "la moción se aprueba" in Spanish with the
    job green. With the digest, an edited source produces a *different*
    expected id, so its translation is correctly seen as missing and
    re-queued, and the row carrying the superseded wording is no longer
    among the ids the publisher will attach.

    Recorded review may also bind ``source_window`` so a timing correction
    requires review of the replacement translation interval. Live callers omit it.

    Omitting ``source_text`` yields the plain ``<cue>:<lang>`` form, which is
    what the live translated-track path (:func:`translate_caption_cues`)
    uses -- live cues are rendered straight to a track and never matched
    back to a stored review row, so they need no version identity.
    """

    base = f"{source_cue_id}:{target_language}"
    if source_text is None:
        return base
    if source_window is not None:
        source_text = (
            f"{source_text}\0{float(source_window[0]).hex()}:{float(source_window[1]).hex()}"
        )
    digest = sha256(source_text.encode("utf-8")).hexdigest()[:8]
    return f"{base}:{digest}"


def translate_caption_cues(
    cues: Sequence[CaptionCue],
    *,
    provider: TranslationProvider,
    target: TranslationTarget | None = None,
    glossary: Mapping[str, str] | None = None,
    latency_budget_ms: float = 800.0,
) -> TranslationBatchResult:
    """Translate stable caption cues and compute p95 latency."""

    translation_target = target or TranslationTarget()
    translated: list[TranslationCue] = []
    latencies: list[float] = []
    for cue in cues:
        protected = _extract_placeholders(cue.text)
        started = time.perf_counter()
        text = provider.translate_text(
            cue.text,
            source_language=translation_target.source_language,
            target_language=translation_target.target_language,
            glossary=glossary,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        _assert_placeholders_preserved(protected, text)
        latencies.append(latency_ms)
        translated.append(
            TranslationCue(
                cue_id=translated_cue_id(cue.cue_id, translation_target.target_language),
                start_seconds=cue.start_seconds,
                end_seconds=cue.end_seconds,
                source_language=translation_target.source_language,
                target_language=translation_target.target_language,
                source_text=cue.text,
                translated_text=text,
                confidence=cue.confidence,
                latency_ms=latency_ms,
            )
        )

    return TranslationBatchResult(
        source_language=translation_target.source_language,
        target_language=translation_target.target_language,
        target_name=translation_target.target_name,
        cues=translated,
        p95_latency_ms=_p95(latencies),
        latency_budget_ms=latency_budget_ms,
    )


def translated_hls_track(result: TranslationBatchResult) -> CaptionHlsTrack:
    """Convert translated cues into an HLS caption track."""

    return CaptionHlsTrack(
        cues=[
            CaptionCue(
                cue_id=cue.cue_id,
                start_seconds=cue.start_seconds,
                end_seconds=cue.end_seconds,
                text=cue.translated_text,
                confidence=cue.confidence,
            )
            for cue in result.cues
        ],
        language=result.target_language,
        name=result.target_name,
        default=False,
        autoselect=True,
    )


def _extract_placeholders(text: str) -> list[str]:
    return PLACEHOLDER_RE.findall(text)


def _assert_placeholders_preserved(expected: Sequence[str], translated: str) -> None:
    actual = _extract_placeholders(translated)
    if list(expected) != actual:
        raise PlaceholderIntegrityError(
            "Translation changed protected glossary placeholders. "
            "Keep tokens like §§0001§§ unchanged."
        )


def _p95(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) * 0.95) + 0.5) - 1))
    return ordered[index]
