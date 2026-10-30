# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Live-caption stabilization."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import floor, isfinite
from string import punctuation
from uuid import uuid4

from civiccast.captions.models import CaptionCue, CaptionHypothesis, CaptionWord


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


@dataclass
class _PendingCue:
    hypothesis: CaptionHypothesis
    bucket: int
    ordinal: int
    stable_count: int = 1


@dataclass
class CaptionStabilizer:
    """Publish live recognition immediately; stabilize offline observations."""

    window_seconds: float = 4.0
    stable_windows: int = 2
    low_confidence_threshold: float = 0.75
    # Live broadcasts publish a single recognition. Offline confirmation remains
    # available for batch processing; it never gates the live caption track.
    live: bool = False
    # Continuous live delivery keeps recent cues only. Recording/offline
    # callers retain their complete track unless they explicitly opt in.
    history_seconds: float | None = None
    max_history_cues: int | None = None
    _pending: list[_PendingCue] = field(default_factory=list, init=False)
    _committed: list[CaptionCue] = field(default_factory=list, init=False)
    _expired_unconfirmed: list[CaptionCue] = field(default_factory=list, init=False)
    _bucket_ordinals: dict[int, int] = field(default_factory=dict, init=False)
    _latest_observed_end_seconds: float = field(default=0.0, init=False)
    _last_word_window: tuple[float, float] | None = field(default=None, init=False)
    _last_live_words: list[CaptionWord] = field(default_factory=list, init=False)
    _last_live_text: str = field(default="", init=False)
    _live_cue_sequence: int = field(default=0, init=False)
    _live_epoch: str = field(default_factory=lambda: uuid4().hex, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be greater than zero")
        if self.stable_windows < 1:
            raise ValueError("stable_windows must be at least 1")
        if not 0 <= self.low_confidence_threshold <= 1:
            raise ValueError("low_confidence_threshold must be between 0 and 1")
        if self.history_seconds is not None and (
            not isfinite(self.history_seconds) or self.history_seconds <= 0
        ):
            raise ValueError("history_seconds must be finite and greater than zero")
        if self.max_history_cues is not None and (
            isinstance(self.max_history_cues, bool)
            or not isinstance(self.max_history_cues, int)
            or self.max_history_cues < 1
        ):
            raise ValueError("max_history_cues must be a positive integer")
        if self._bounded_live_history and not self.live:
            raise ValueError("bounded history is available only for live caption delivery")

    @property
    def _bounded_live_history(self) -> bool:
        return self.history_seconds is not None or self.max_history_cues is not None

    def _prune_live_history(self, end_seconds: float) -> None:
        if self.history_seconds is not None:
            cutoff = end_seconds - self.history_seconds
            self._committed = [cue for cue in self._committed if cue.end_seconds >= cutoff]
        if self.max_history_cues is not None:
            self._committed = self._committed[-self.max_history_cues :]

    def observe(self, hypothesis: CaptionHypothesis) -> list[CaptionCue]:
        """Observe one runtime hypothesis and return newly committed cues."""

        if self.live:
            return self._observe_live(hypothesis)
        self._latest_observed_end_seconds = max(
            self._latest_observed_end_seconds,
            hypothesis.end_seconds,
        )
        self._expire_stale_pending()
        bucket = self._bucket_for(hypothesis.start_seconds)
        if self._overlaps_committed(hypothesis):
            return []

        pending = self._matching_pending(hypothesis)
        if pending is not None:
            pending.stable_count += 1
            pending.hypothesis = hypothesis
            if pending.stable_count >= self.stable_windows:
                return [self._commit(pending)]
            return []

        revision = self._revision_candidate(hypothesis)
        if revision is not None:
            revision.hypothesis = hypothesis
            revision.stable_count = 1
            return []

        ordinal = self._bucket_ordinals.get(bucket, 0) + 1
        self._bucket_ordinals[bucket] = ordinal
        pending = _PendingCue(
            hypothesis=hypothesis,
            bucket=bucket,
            ordinal=ordinal,
        )
        self._pending.append(pending)
        if self.stable_windows == 1:
            return [self._commit(pending)]
        return []

    def _observe_live(self, hypothesis: CaptionHypothesis) -> list[CaptionCue]:
        """Air the first reading and discard audio already covered by a window.

        Timed words select the new audio tail. Without usable word timing, a
        suffix/prefix match removes only text already aired; it is not a vote
        or a prerequisite for publication. Zero-duration words use coarse audio
        bounds instead of disappearing from the live track.
        """
        start = hypothesis.audio_window_start_seconds
        end = hypothesis.audio_window_end_seconds
        if start is None or end is None:
            start, end = hypothesis.start_seconds, hypothesis.end_seconds
        previous = self._last_word_window
        if previous is not None and end <= previous[1]:
            return []
        self._last_word_window = (start, end)
        self._prune_live_history(end)
        if not hypothesis.text.strip():
            # Silence still advances the audio watermark, but it is not a
            # text overlap with the last spoken phrase. Keeping that phrase
            # here would suppress genuinely repeated speech after silence.
            self._last_live_text = ""
            return []
        boundary = max(start, previous[1]) if previous is not None else start
        words = hypothesis.words or []
        timed = any(w.end_seconds > w.start_seconds for w in words)
        if timed:
            words = [
                w
                for w in words
                if (w.end_seconds > boundary or w.start_seconds == w.end_seconds == boundary)
                and w.start_seconds < end
                and w.text.strip()
            ]
            if previous is not None and self._last_live_words:
                old = self._last_live_words
                for count in range(min(len(words), len(old)), 0, -1):
                    if (
                        all(w.start_seconds < boundary for w in words[:count])
                        and [w.text.casefold().strip(punctuation + " ") for w in old[-count:]]
                        == [w.text.casefold().strip(punctuation + " ") for w in words[:count]]
                        and all(
                            min(a.end_seconds, b.end_seconds)
                            > max(a.start_seconds, b.start_seconds)
                            for a, b in zip(old[-count:], words[:count], strict=True)
                        )
                    ):
                        words = words[count:]
                        break
            if not words:
                return []
            text = " ".join(w.text.strip() for w in words)
            cue_start = max(boundary, min(w.start_seconds for w in words))
            cue_end = min(end, max(w.end_seconds for w in words))
            confidence = min(hypothesis.confidence, *(w.confidence for w in words))
        else:
            tokens = hypothesis.text.split()
            if previous is not None and start < previous[1] and self._last_live_text:
                old_text_tokens = self._last_live_text.split()

                def norm(xs: list[str]) -> list[str]:
                    return [x.casefold().strip(punctuation) for x in xs]

                for count in range(min(len(tokens), len(old_text_tokens)), 0, -1):
                    if norm(old_text_tokens[-count:]) == norm(tokens[:count]):
                        tokens = tokens[count:]
                        break
            if not tokens:
                return []
            text = " ".join(tokens)
            cue_start = max(boundary, hypothesis.start_seconds)
            cue_end = min(end, hypothesis.end_seconds)
            confidence = hypothesis.confidence
        if cue_end <= cue_start:
            cue_start, cue_end = boundary, end
        if cue_end <= cue_start:
            return []
        ready = hypothesis.model_copy(
            update={
                "text": text,
                "start_seconds": cue_start,
                "end_seconds": cue_end,
                "confidence": confidence,
                "words": words,
            }
        )
        self._last_live_words = words if timed else []
        self._last_live_text = text
        if self._bounded_live_history:
            # The epoch distinguishes replacement workers while the channel
            # remains ON_AIR and downstream delivery caches remain alive.
            # A scalar sequence keeps tracking independent of session length.
            self._live_cue_sequence += 1
            cue = CaptionCue(
                cue_id=f"cue-live-{self._live_epoch}-{self._live_cue_sequence:012d}",
                start_seconds=ready.start_seconds,
                end_seconds=ready.end_seconds,
                text=ready.text,
                confidence=ready.confidence,
                low_confidence=ready.confidence < self.low_confidence_threshold,
            )
            self._committed.append(cue)
            self._prune_live_history(end)
            return [cue]
        return [self._commit(self._new_pending(ready))]

    def _new_pending(self, hypothesis: CaptionHypothesis, *, stable_count: int = 1) -> _PendingCue:
        bucket = self._bucket_for(hypothesis.start_seconds)
        ordinal = self._bucket_ordinals.get(bucket, 0) + 1
        self._bucket_ordinals[bucket] = ordinal
        pending = _PendingCue(hypothesis, bucket, ordinal, stable_count)
        self._pending.append(pending)
        return pending

    def committed(self) -> list[CaptionCue]:
        """Return retained committed cues in playback order.

        Bounded live sessions expose a rolling window; recording pipelines
        retain their complete track.
        """

        return sorted(
            self._committed,
            key=lambda cue: (cue.start_seconds, cue.end_seconds, cue.cue_id),
        )

    def flush(self) -> list[CaptionCue]:
        """Flush offline pending cues; live observations already aired."""

        ordered = sorted(
            self._pending,
            key=lambda pending: (
                pending.hypothesis.start_seconds,
                pending.hypothesis.end_seconds,
                pending.bucket,
                pending.ordinal,
            ),
        )
        flushed: list[CaptionCue] = []
        for pending in ordered:
            earned_confirmation = (
                pending.stable_count >= self.stable_windows
                and pending.hypothesis.confidence >= self.low_confidence_threshold
            )
            flushed.append(self._commit(pending, low_confidence=not earned_confirmation))
        return flushed

    def expired_unconfirmed(self) -> list[CaptionCue]:
        """Return pending cues dropped by :meth:`_expire_stale_pending`, in expiry order.

        These never earned re-confirmation and were never committed -- they
        must never be treated as active/on-air -- but they are counted and
        returned here instead of being silently deleted, so a drop is always
        observable.
        """

        return list(self._expired_unconfirmed)

    @property
    def expired_unconfirmed_count(self) -> int:
        """Total number of pending cues expired without re-confirmation."""

        return len(self._expired_unconfirmed)

    def _bucket_for(self, start_seconds: float) -> int:
        return floor(start_seconds / self.window_seconds)

    def _matching_pending(self, hypothesis: CaptionHypothesis) -> _PendingCue | None:
        normalized = _normalize(hypothesis.text)
        matches = [
            pending
            for pending in self._pending
            if _normalize(pending.hypothesis.text) == normalized
            and abs(pending.hypothesis.start_seconds - hypothesis.start_seconds)
            <= self.window_seconds
        ]
        if not matches:
            return None
        return min(
            matches,
            key=lambda pending: abs(pending.hypothesis.start_seconds - hypothesis.start_seconds),
        )

    def _revision_candidate(self, hypothesis: CaptionHypothesis) -> _PendingCue | None:
        candidates = [
            pending
            for pending in self._pending
            if _substantially_overlaps(pending.hypothesis, hypothesis)
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda pending: _overlap_seconds(pending.hypothesis, hypothesis),
        )

    def _overlaps_committed(self, hypothesis: CaptionHypothesis) -> bool:
        normalized = _normalize(hypothesis.text)
        return any(
            _substantially_overlaps(cue, hypothesis)
            or (
                _normalize(cue.text) == normalized
                and abs(cue.start_seconds - hypothesis.start_seconds) <= self.window_seconds
            )
            for cue in self._committed
        )

    def _expire_stale_pending(self) -> None:
        oldest_relevant_end = self._latest_observed_end_seconds - (2 * self.window_seconds)
        survivors: list[_PendingCue] = []
        for pending in self._pending:
            if pending.hypothesis.end_seconds >= oldest_relevant_end:
                survivors.append(pending)
            else:
                self._expired_unconfirmed.append(self._build_cue(pending, low_confidence=True))
        self._pending = survivors

    def _build_cue(self, pending: _PendingCue, *, low_confidence: bool | None = None) -> CaptionCue:
        cue_id = f"cue-{pending.bucket:06d}"
        if pending.ordinal > 1:
            cue_id = f"{cue_id}-{pending.ordinal:02d}"
        resolved_low_confidence = (
            pending.hypothesis.confidence < self.low_confidence_threshold
            if low_confidence is None
            else low_confidence
        )
        return CaptionCue(
            cue_id=cue_id,
            start_seconds=pending.hypothesis.start_seconds,
            end_seconds=pending.hypothesis.end_seconds,
            text=pending.hypothesis.text,
            confidence=pending.hypothesis.confidence,
            low_confidence=resolved_low_confidence,
        )

    def _commit(self, pending: _PendingCue, *, low_confidence: bool | None = None) -> CaptionCue:
        self._pending.remove(pending)
        cue = self._build_cue(pending, low_confidence=low_confidence)
        self._committed.append(cue)
        return cue


def _overlap_seconds(
    first: CaptionCue | CaptionHypothesis,
    second: CaptionHypothesis,
) -> float:
    return max(
        0.0,
        min(first.end_seconds, second.end_seconds) - max(first.start_seconds, second.start_seconds),
    )


def _substantially_overlaps(
    first: CaptionCue | CaptionHypothesis,
    second: CaptionHypothesis,
) -> bool:
    shorter = min(
        first.end_seconds - first.start_seconds,
        second.end_seconds - second.start_seconds,
    )
    return shorter > 0 and _overlap_seconds(first, second) / shorter >= 0.5
