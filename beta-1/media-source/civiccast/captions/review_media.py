# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Bounded local-media evidence for the operator caption review queue."""

from __future__ import annotations

import hashlib
import os
import tempfile
import wave
from collections.abc import Callable
from pathlib import Path

from civiccast.captions.models import AudioChunk, CaptionCue
from civiccast.captions.review import CaptionReviewAudioEvidence
from civiccast.live.recording_paths import local_recording_path
from civiccast.stream._ffmpeg import FfmpegResult, run_ffmpeg

_PRE_ROLL_SECONDS = 2.0
_POST_ROLL_SECONDS = 2.0
_MAX_CLIP_SECONDS = 120.0
_FFMPEG_TIMEOUT_SECONDS = 30.0

FfmpegRunner = Callable[..., FfmpegResult]
CaptionReviewClipBuilder = Callable[[Path, CaptionCue], Path]


class CaptionReviewClipError(RuntimeError):
    """Raised when local media cannot produce a playable review clip."""


def write_caption_review_audio_evidence(
    chunk: AudioChunk,
    output_path: Path,
) -> CaptionReviewAudioEvidence:
    """Atomically retain the exact PCM window used to create a live caption cue."""

    destination = output_path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with wave.open(str(temporary), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(chunk.sample_rate_hz)
            wav_file.writeframes(chunk.pcm_s16le)
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return CaptionReviewAudioEvidence(
        source_path=str(destination),
        source_start_seconds=chunk.start_seconds,
        source_sha256=_sha256(destination),
        source_bytes=destination.stat().st_size,
    )


def verify_caption_review_audio_evidence(
    evidence: CaptionReviewAudioEvidence,
) -> Path:
    """Fail closed when retained review audio is missing or no longer identical."""

    source = Path(evidence.source_path).expanduser().resolve()
    if not source.is_file():
        raise CaptionReviewClipError(
            "The retained live-caption audio evidence is missing. "
            "Restore the evidence file before reviewing this cue."
        )
    if source.stat().st_size != evidence.source_bytes:
        raise CaptionReviewClipError(
            "The retained live-caption audio evidence size changed; review is blocked."
        )
    if _sha256(source) != evidence.source_sha256:
        raise CaptionReviewClipError(
            "The retained live-caption audio evidence hash changed; review is blocked."
        )
    return source


def verify_caption_review_audio_evidence_for_cue(
    evidence: CaptionReviewAudioEvidence,
    cue: CaptionCue,
) -> Path:
    """Verify identity, WAV readability, and full cue-window coverage."""
    source = verify_caption_review_audio_evidence(evidence)
    try:
        with wave.open(str(source), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frame_count = audio.getnframes()
    except (OSError, EOFError, wave.Error) as error:
        raise CaptionReviewClipError(
            "The retained live-caption audio evidence is not a readable WAV file."
        ) from error
    if channels != 1 or sample_width != 2 or sample_rate <= 0 or frame_count <= 0:
        raise CaptionReviewClipError(
            "The retained live-caption audio evidence is not valid mono signed 16-bit PCM."
        )

    audio_duration = frame_count / sample_rate
    relative_start = cue.start_seconds - evidence.source_start_seconds
    relative_end = cue.end_seconds - evidence.source_start_seconds
    frame_tolerance = 1.0 / sample_rate
    if (
        relative_start < -frame_tolerance
        or relative_end <= relative_start
        or relative_end > audio_duration + frame_tolerance
    ):
        raise CaptionReviewClipError(
            "The retained caption audio does not cover the whole cue "
            f"({cue.start_seconds:.3f} to {cue.end_seconds:.3f}s); available audio covers "
            f"{evidence.source_start_seconds:.3f} to "
            f"{evidence.source_start_seconds + audio_duration:.3f}s. "
            "Regenerate this recording's captions from its local source media. "
            "If the source is unavailable, this cue cannot be verified."
        )
    return source


def cue_relative_to_audio_evidence(
    cue: CaptionCue,
    evidence: CaptionReviewAudioEvidence,
) -> CaptionCue:
    """Translate a channel-timeline cue into its retained audio-window timeline."""

    start_seconds = max(0.0, cue.start_seconds - evidence.source_start_seconds)
    end_seconds = cue.end_seconds - evidence.source_start_seconds
    if end_seconds <= start_seconds:
        raise CaptionReviewClipError(
            "The retained live-caption audio window does not cover this cue."
        )
    return cue.model_copy(
        update={
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
        }
    )


def resolve_caption_review_source(stored_path: str) -> Path | None:
    """Resolve an asset's stored path or file URI without accepting remote media."""

    return local_recording_path(stored_path)


def build_caption_review_clip(
    source: Path,
    cue: CaptionCue,
    *,
    ffmpeg_runner: FfmpegRunner = run_ffmpeg,
) -> Path:
    """Decode a short mono WAV around one caption cue.

    The returned temporary file is owned by the caller and must be removed
    after the HTTP response finishes. The clip is intentionally capped so a
    review request cannot become an unbounded transcode or media download.
    """

    cue_duration = cue.end_seconds - cue.start_seconds
    if cue_duration > _MAX_CLIP_SECONDS:
        raise CaptionReviewClipError(
            "This cue exceeds the 120 seconds review limit. Split it into shorter cues."
        )
    # Spend the bounded window on the entire cue before adding context.
    pre_roll = min(_PRE_ROLL_SECONDS, _MAX_CLIP_SECONDS - cue_duration)
    clip_start = max(0.0, cue.start_seconds - pre_roll)
    clip_end = min(cue.end_seconds + _POST_ROLL_SECONDS, clip_start + _MAX_CLIP_SECONDS)
    clip_duration = min(_MAX_CLIP_SECONDS, clip_end - clip_start)
    with tempfile.NamedTemporaryFile(
        prefix="civiccast-caption-review-",
        suffix=".wav",
        delete=False,
    ) as handle:
        output = Path(handle.name)

    try:
        result = ffmpeg_runner(
            [
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{clip_start:.3f}",
                "-i",
                str(source.resolve()),
                "-t",
                f"{clip_duration:.3f}",
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                str(output),
            ],
            timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
        if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise CaptionReviewClipError(
                "CivicCast could not decode the asset audio for caption review."
            )
        try:
            with wave.open(str(output), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
                tolerance = 1 / audio.getframerate()
        except (OSError, EOFError, wave.Error, ZeroDivisionError) as exc:
            raise CaptionReviewClipError(
                "The decoded caption review audio is not readable."
            ) from exc
        if duration + tolerance < cue.end_seconds - clip_start:
            raise CaptionReviewClipError(
                "The available source audio ends before the selected cue finishes "
                f"(audio ends at {clip_start + duration:.3f}s; cue ends at {cue.end_seconds:.3f}s). "
                "Relink the complete recording or regenerate captions from the available media."
            )
        return output
    except Exception:
        output.unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


__all__ = [
    "CaptionReviewClipBuilder",
    "CaptionReviewClipError",
    "build_caption_review_clip",
    "cue_relative_to_audio_evidence",
    "resolve_caption_review_source",
    "verify_caption_review_audio_evidence",
    "verify_caption_review_audio_evidence_for_cue",
    "write_caption_review_audio_evidence",
]
