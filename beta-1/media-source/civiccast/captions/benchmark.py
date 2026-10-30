# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Caption runtime benchmark helpers for Sprint 0.5 evidence."""

from __future__ import annotations

import json
import re
import time
import wave
from contextlib import suppress
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from civiccast.captions.models import AudioChunk, CaptionHypothesis, CustomVocabulary
from civiccast.captions.runtime import CaptionRuntime


@dataclass(frozen=True)
class CaptionBenchmarkGpuSample:
    """Point-in-time GPU memory sample."""

    name: str
    used_mb: float
    free_mb: float
    total_mb: float


@dataclass(frozen=True)
class CaptionBenchmarkResult:
    """Machine-readable caption benchmark result."""

    model: str
    device: str
    compute_type: str
    audio_path: str
    audio_duration_seconds: float
    chunk_count: int
    elapsed_seconds: float
    mean_chunk_seconds: float
    p95_chunk_seconds: float
    transcript: str
    word_error_rate: float | None
    gpu_before: CaptionBenchmarkGpuSample | None
    gpu_after: CaptionBenchmarkGpuSample | None

    def to_json(self) -> str:
        """Serialize the result for release evidence."""
        return json.dumps(_as_plain_data(self), indent=2, sort_keys=True)


def load_wav_chunks(
    audio_path: Path,
    *,
    chunk_seconds: float = 4.0,
    max_chunks: int | None = None,
) -> list[AudioChunk]:
    """Load mono 16-bit PCM WAV audio into caption runtime chunks."""

    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be positive")

    with wave.open(str(audio_path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frame_count = wav_file.getnframes()
        if channels != 1:
            raise ValueError("benchmark audio must be mono WAV")
        if sample_width != 2:
            raise ValueError("benchmark audio must be signed 16-bit PCM WAV")

        frames_per_chunk = max(1, int(sample_rate * chunk_seconds))
        chunks: list[AudioChunk] = []
        chunk_index = 0
        while True:
            if max_chunks is not None and chunk_index >= max_chunks:
                break
            frames = wav_file.readframes(frames_per_chunk)
            if not frames:
                break
            start_frame = chunk_index * frames_per_chunk
            actual_frames = len(frames) // sample_width
            end_frame = min(start_frame + actual_frames, frame_count)
            chunks.append(
                AudioChunk(
                    chunk_id=f"{audio_path.stem}-{chunk_index:05d}",
                    start_seconds=start_frame / sample_rate,
                    end_seconds=end_frame / sample_rate,
                    sample_rate_hz=sample_rate,
                    pcm_s16le=frames,
                )
            )
            chunk_index += 1
    return chunks


def run_caption_benchmark(
    runtime: CaptionRuntime,
    chunks: list[AudioChunk],
    *,
    audio_path: Path,
    model: str,
    device: str,
    compute_type: str,
    vocabulary: CustomVocabulary | None = None,
    ground_truth: str | None = None,
) -> CaptionBenchmarkResult:
    """Run one caption runtime benchmark over prepared chunks."""

    gpu_before = sample_gpu()
    started = time.perf_counter()
    chunk_latencies: list[float] = []
    hypotheses: list[CaptionHypothesis] = []

    for chunk in chunks:
        chunk_started = time.perf_counter()
        hypotheses.extend(runtime.transcribe([chunk], vocabulary=vocabulary))
        chunk_latencies.append(time.perf_counter() - chunk_started)

    elapsed = time.perf_counter() - started
    gpu_after = sample_gpu()
    transcript = " ".join(hypothesis.text for hypothesis in sorted(hypotheses, key=_hypothesis_key))
    duration = chunks[-1].end_seconds - chunks[0].start_seconds if chunks else 0.0

    return CaptionBenchmarkResult(
        model=model,
        device=device,
        compute_type=compute_type,
        audio_path=str(audio_path),
        audio_duration_seconds=round(duration, 3),
        chunk_count=len(chunks),
        elapsed_seconds=round(elapsed, 3),
        mean_chunk_seconds=round(mean(chunk_latencies), 3) if chunk_latencies else 0.0,
        p95_chunk_seconds=round(percentile(chunk_latencies, 0.95), 3) if chunk_latencies else 0.0,
        transcript=transcript,
        word_error_rate=word_error_rate(ground_truth, transcript)
        if ground_truth is not None
        else None,
        gpu_before=gpu_before,
        gpu_after=gpu_after,
    )


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Compute WER as edit distance over normalized word tokens."""

    reference_words = normalize_words(reference)
    hypothesis_words = normalize_words(hypothesis)
    if not reference_words:
        return 0.0 if not hypothesis_words else 1.0
    return round(_edit_distance(reference_words, hypothesis_words) / len(reference_words), 4)


def normalize_words(text: str) -> list[str]:
    """Normalize transcript text into word tokens for WER."""

    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text.casefold())


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile for small benchmark samples."""

    if not values:
        raise ValueError("values must not be empty")
    if not 0 <= fraction <= 1:
        raise ValueError("fraction must be between 0 and 1")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def sample_gpu() -> CaptionBenchmarkGpuSample | None:
    """Return a best-effort NVIDIA GPU memory sample."""

    try:
        import pynvml  # type: ignore[import-untyped]
    except ImportError:
        return None

    try:
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
        return CaptionBenchmarkGpuSample(
            name=str(name),
            used_mb=round(memory.used / 1024**2, 1),
            free_mb=round(memory.free / 1024**2, 1),
            total_mb=round(memory.total / 1024**2, 1),
        )
    except Exception:
        return None
    finally:
        with suppress(Exception):
            pynvml.nvmlShutdown()


def _hypothesis_key(hypothesis: CaptionHypothesis) -> tuple[float, float, str]:
    return (hypothesis.start_seconds, hypothesis.end_seconds, hypothesis.source_id)


def _edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row_index, reference_word in enumerate(reference, start=1):
        current = [row_index]
        for col_index, hypothesis_word in enumerate(hypothesis, start=1):
            substitution = previous[col_index - 1] + int(reference_word != hypothesis_word)
            insertion = current[col_index - 1] + 1
            deletion = previous[col_index] + 1
            current.append(min(substitution, insertion, deletion))
        previous = current
    return previous[-1]


def _as_plain_data(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _as_plain_data(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, list):
        return [_as_plain_data(item) for item in value]
    return value
