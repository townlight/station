# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Data contracts for caption translation."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class TranslationTarget(BaseModel):
    """One translated-caption target language."""

    model_config = ConfigDict(extra="forbid")

    source_language: Annotated[str, Field(min_length=2, max_length=12)] = "en"
    target_language: Annotated[str, Field(min_length=2, max_length=12)] = "es"
    target_name: Annotated[str, Field(min_length=1, max_length=80)] = "Spanish"


class TranslationCue(BaseModel):
    """A caption cue translated into a target language."""

    model_config = ConfigDict(extra="forbid")

    cue_id: Annotated[str, Field(min_length=1, max_length=180)]
    start_seconds: Annotated[float, Field(ge=0)]
    end_seconds: Annotated[float, Field(ge=0)]
    source_language: Annotated[str, Field(min_length=2, max_length=12)]
    target_language: Annotated[str, Field(min_length=2, max_length=12)]
    source_text: Annotated[str, Field(min_length=1, max_length=2000)]
    translated_text: Annotated[str, Field(min_length=1, max_length=2000)]
    confidence: Annotated[float, Field(ge=0, le=1)]
    latency_ms: Annotated[float, Field(ge=0)]


class TranslationBatchResult(BaseModel):
    """Result of translating one stable caption-cue batch."""

    model_config = ConfigDict(extra="forbid")

    source_language: Annotated[str, Field(min_length=2, max_length=12)]
    target_language: Annotated[str, Field(min_length=2, max_length=12)]
    target_name: Annotated[str, Field(min_length=1, max_length=80)]
    cues: list[TranslationCue]
    p95_latency_ms: Annotated[float, Field(ge=0)]
    latency_budget_ms: Annotated[float, Field(gt=0)] = 800.0

    @property
    def within_latency_budget(self) -> bool:
        """Return whether p95 latency satisfies the v0.9 budget."""

        return self.p95_latency_ms < self.latency_budget_ms


class TranslationModelRegistration(BaseModel):
    """Registered translation backend shape for runtime selection."""

    model_config = ConfigDict(extra="forbid")

    key: Annotated[str, Field(min_length=1, max_length=80)]
    provider: Literal["ollama", "local-deterministic", "external"]
    model_id: Annotated[str, Field(min_length=1, max_length=120)]
    role: Literal["primary", "alternate", "ci-proof"]
    notes: Annotated[str, Field(min_length=1, max_length=400)]
