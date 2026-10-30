# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Caption data contracts for the 0.5 captions core."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Seconds = Annotated[float, Field(ge=0)]


class CustomVocabulary(BaseModel):
    """Station-specific captioning hints passed to the runtime adapter."""

    model_config = ConfigDict(extra="forbid")

    terms: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list, max_length=100
    )
    initial_prompt: Annotated[str | None, Field(default=None, max_length=1200)] = None

    @field_validator("terms")
    @classmethod
    def _dedupe_terms(cls, terms: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for term in terms:
            normalized = " ".join(term.split())
            key = normalized.casefold()
            if key and key not in seen:
                seen.add(key)
                cleaned.append(normalized)
        return cleaned


class AudioChunk(BaseModel):
    """A small audio buffer passed into a caption runtime."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: Annotated[str, Field(min_length=1, max_length=120)]
    start_seconds: Seconds
    end_seconds: Seconds
    sample_rate_hz: Annotated[int, Field(gt=0)]
    pcm_s16le: bytes = Field(description="Mono PCM signed 16-bit little-endian audio.")

    @model_validator(mode="after")
    def _end_after_start(self) -> AudioChunk:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class CaptionWord(BaseModel):
    """An actual ASR word observation, including zero-duration observations."""

    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    start_seconds: Seconds
    end_seconds: Seconds
    confidence: Annotated[float, Field(ge=0, le=1)] = 1.0

    @model_validator(mode="after")
    def _forward_time(self) -> CaptionWord:
        if self.end_seconds < self.start_seconds:
            raise ValueError("word end_seconds cannot precede start_seconds")
        return self


class CaptionHypothesis(BaseModel):
    """A runtime transcript candidate before stabilization commits it."""

    model_config = ConfigDict(extra="forbid")

    source_id: Annotated[str, Field(min_length=1, max_length=120)]
    start_seconds: Seconds
    end_seconds: Seconds
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    confidence: Annotated[float, Field(ge=0, le=1)] = 1.0
    # Audio provenance is independent of ASR's arbitrary speech segmentation.
    # Optional for existing runtimes/offline records; never word timestamps.
    audio_window_start_seconds: Seconds | None = None
    audio_window_end_seconds: Seconds | None = None
    words: list[CaptionWord] | None = None

    @model_validator(mode="after")
    def _end_after_start(self) -> CaptionHypothesis:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        start, end = self.audio_window_start_seconds, self.audio_window_end_seconds
        if (start is None) != (end is None):
            raise ValueError("audio window timestamps must be provided together")
        if start is not None and end is not None and end <= start:
            raise ValueError("audio_window_end_seconds must be greater than start")
        return self

    @field_validator("text")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        return " ".join(value.split())


class CaptionCue(BaseModel):
    """A stable caption cue ready for WebVTT/HLS/review queue consumers."""

    model_config = ConfigDict(extra="forbid")

    cue_id: Annotated[str, Field(min_length=1, max_length=160)]
    start_seconds: Seconds
    end_seconds: Seconds
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    confidence: Annotated[float, Field(ge=0, le=1)]
    low_confidence: bool = False

    @model_validator(mode="after")
    def _end_after_start(self) -> CaptionCue:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self
