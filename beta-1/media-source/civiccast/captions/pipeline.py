# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Caption processing pipeline from runtime hypotheses to review items."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext, suppress
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING

from civiccast.captions.hls import (
    CaptionHlsTrack,
    CaptionHlsTrackOutput,
    attach_caption_tracks_to_package,
)
from civiccast.captions.models import AudioChunk, CaptionCue, CaptionHypothesis, CustomVocabulary
from civiccast.captions.review import CaptionReviewItemCreate
from civiccast.captions.runtime import CaptionRuntime
from civiccast.captions.stabilize import CaptionStabilizer
from civiccast.stream.config import HLS_SEGMENT_DURATION
from civiccast.stream.packager import SlateOnlyResult, VodPackageResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from civiccast.captions.phase_timing import NullPhaseTimingCollector, PhaseTimingCollector
    from civiccast.translate import TranslationProvider, TranslationTarget


@dataclass(frozen=True)
class CaptionPipelineResult:
    """Artifacts produced by one caption pipeline pass."""

    hypotheses: list[CaptionHypothesis]
    committed_cues: list[CaptionCue]
    review_items: list[CaptionReviewItemCreate]
    expired_unconfirmed_cues: list[CaptionCue] = field(default_factory=list)


@dataclass(frozen=True)
class CaptionHlsPipelineResult:
    """Caption processing result plus any HLS files written."""

    caption_result: CaptionPipelineResult
    hls_outputs: list[CaptionHlsTrackOutput]


class CaptionPipeline:
    """Run recognition and stabilization, optionally preparing review records.

    Continuous live workers disable review construction. Recording/editing
    workflows preserve their review preparation by default.
    """

    def __init__(
        self,
        runtime: CaptionRuntime,
        *,
        stabilizer: CaptionStabilizer | None = None,
        phase_timing: PhaseTimingCollector | NullPhaseTimingCollector | None = None,
        phase_timing_channel: str | None = None,
        prepare_review_items: bool = True,
    ) -> None:
        self._runtime = runtime
        self._stabilizer = stabilizer or CaptionStabilizer()
        self._phase_timing = phase_timing
        self._phase_timing_channel = phase_timing_channel
        self._prepare_review_items = prepare_review_items

    def _phase(self, name: str) -> AbstractContextManager[None]:
        """Return an opt-in timing context without affecting pipeline work."""

        timing = self._phase_timing
        if timing is None:
            return nullcontext()
        with suppress(Exception):
            return timing.phase(name, channel=self._phase_timing_channel)
        return nullcontext()

    def process(
        self,
        chunks: list[AudioChunk],
        *,
        asset_id: str,
        vocabulary: CustomVocabulary | None = None,
        reviewer_note: str | None = None,
    ) -> CaptionPipelineResult:
        """Transcribe chunks and prepare stable cue review rows.

        The pipeline keeps the stabilizer instance alive across calls, so a
        live worker publishes each recognition and deduplicates overlapping audio.
        Offline pipelines retain their configured confirmation policy.
        """
        with self._phase("runtime_transcribe"):
            hypotheses = list(self._runtime.transcribe(chunks, vocabulary=vocabulary))
        with self._phase("caption_stabilize"):
            committed_cues: list[CaptionCue] = []
            expired_before = self._stabilizer.expired_unconfirmed_count
            for hypothesis in hypotheses:
                committed_cues.extend(self._stabilizer.observe(hypothesis))
            expired_unconfirmed_cues = self._stabilizer.expired_unconfirmed()[expired_before:]

        # Expired-unconfirmed cues never air (never enter committed_cues /
        # the active track) but still land in the same review queue as any
        # other low-confidence commit, so a drop is never silent.
        review_items = [
            CaptionReviewItemCreate(
                review_item_id=_review_item_id(asset_id, cue),
                asset_id=asset_id,
                cue=cue,
                reviewer_note=reviewer_note,
            )
            for cue in (*committed_cues, *expired_unconfirmed_cues)
            if self._prepare_review_items
        ]
        return CaptionPipelineResult(
            hypotheses=hypotheses,
            committed_cues=committed_cues,
            review_items=review_items,
            expired_unconfirmed_cues=expired_unconfirmed_cues,
        )

    def process_and_publish_hls(
        self,
        chunks: list[AudioChunk],
        *,
        asset_id: str,
        package: VodPackageResult | SlateOnlyResult,
        vocabulary: CustomVocabulary | None = None,
        reviewer_note: str | None = None,
        language: str = "en",
        name: str = "English",
        translation_provider: TranslationProvider | None = None,
        translation_targets: list[TranslationTarget] | None = None,
        translation_glossary: Mapping[str, str] | None = None,
        segment_duration: int = HLS_SEGMENT_DURATION,
    ) -> CaptionHlsPipelineResult:
        """Process chunks and attach committed captions to an HLS package.

        If this pass does not commit any new cues, no HLS files are rewritten.
        Once a cue commits, the package is rewritten with every committed cue
        known to this pipeline instance so repeated live-worker calls keep the
        caption track complete.
        """
        caption_result = self.process(
            chunks,
            asset_id=asset_id,
            vocabulary=vocabulary,
            reviewer_note=reviewer_note,
        )
        if not caption_result.committed_cues:
            return CaptionHlsPipelineResult(caption_result=caption_result, hls_outputs=[])

        tracks = [
            CaptionHlsTrack(
                cues=self.committed(),
                language=language,
                name=name,
            )
        ]
        if translation_provider is not None:
            from civiccast.translate import translate_caption_cues, translated_hls_track

            for target in translation_targets or []:
                result = translate_caption_cues(
                    self.committed(),
                    provider=translation_provider,
                    target=target,
                    glossary=translation_glossary,
                )
                tracks.append(translated_hls_track(result))

        with self._phase("hls_publish"):
            hls_outputs = attach_caption_tracks_to_package(
                package,
                tracks,
                segment_duration=segment_duration,
            )
        return CaptionHlsPipelineResult(caption_result=caption_result, hls_outputs=hls_outputs)

    def flush(
        self,
        *,
        asset_id: str,
        reviewer_note: str | None = None,
    ) -> CaptionPipelineResult:
        """Finish pending captions at end-of-stream/channel-stop.

        Timed live words without confirmation are persisted for review only;
        they never enter the active track. Legacy/offline pending hypotheses
        retain the low-confidence commit behavior described below.

        There is no second transcription pass once audio has ended, so any
        hypothesis that has not yet earned full re-confirmation must be
        committed now or lost forever. Flushed cues flow through the exact
        same downstream construction as a normal commit -- they land in
        ``committed_cues`` (and therefore :meth:`committed` / the active
        caption track) and get review rows -- they are just flagged
        low-confidence when they never earned re-confirmation. Safe to call
        repeatedly: once pending is empty, later calls return an empty
        result.
        """

        expired_before = self._stabilizer.expired_unconfirmed_count
        committed_cues = self._stabilizer.flush()
        expired_unconfirmed_cues = self._stabilizer.expired_unconfirmed()[expired_before:]
        review_items = [
            CaptionReviewItemCreate(
                review_item_id=_review_item_id(asset_id, cue),
                asset_id=asset_id,
                cue=cue,
                reviewer_note=reviewer_note,
            )
            for cue in (*committed_cues, *expired_unconfirmed_cues)
            if self._prepare_review_items
        ]
        return CaptionPipelineResult(
            hypotheses=[],
            committed_cues=committed_cues,
            review_items=review_items,
            expired_unconfirmed_cues=expired_unconfirmed_cues,
        )

    def flush_and_publish_hls(
        self,
        *,
        asset_id: str,
        package: VodPackageResult | SlateOnlyResult,
        reviewer_note: str | None = None,
        language: str = "en",
        name: str = "English",
        translation_provider: TranslationProvider | None = None,
        translation_targets: list[TranslationTarget] | None = None,
        translation_glossary: Mapping[str, str] | None = None,
        segment_duration: int = HLS_SEGMENT_DURATION,
    ) -> CaptionHlsPipelineResult:
        """Flush end-of-stream cues and republish the HLS caption track.

        Mirrors :meth:`process_and_publish_hls`: if flush produces no new
        cues, the package is left untouched.
        """

        caption_result = self.flush(asset_id=asset_id, reviewer_note=reviewer_note)
        if not caption_result.committed_cues:
            return CaptionHlsPipelineResult(caption_result=caption_result, hls_outputs=[])

        tracks = [
            CaptionHlsTrack(
                cues=self.committed(),
                language=language,
                name=name,
            )
        ]
        if translation_provider is not None:
            from civiccast.translate import translate_caption_cues, translated_hls_track

            for target in translation_targets or []:
                result = translate_caption_cues(
                    self.committed(),
                    provider=translation_provider,
                    target=target,
                    glossary=translation_glossary,
                )
                tracks.append(translated_hls_track(result))

        hls_outputs = attach_caption_tracks_to_package(
            package,
            tracks,
            segment_duration=segment_duration,
        )
        return CaptionHlsPipelineResult(caption_result=caption_result, hls_outputs=hls_outputs)

    def committed(self) -> list[CaptionCue]:
        """Return stable cues retained by this pipeline's history policy."""
        return self._stabilizer.committed()

    def expired_unconfirmed(self) -> list[CaptionCue]:
        """Return every pending cue this pipeline's stabilizer expired.

        These never entered :meth:`committed` and never air; exposed so a
        caller (or an operator dashboard) can see the drop is never silent.
        """
        return self._stabilizer.expired_unconfirmed()


def review_item_id_for_cue(asset_id: str, cue: CaptionCue) -> str:
    """Derive the stable, collision-safe review-item id for ``(asset, cue)``.

    Public seam over the private helper so other producers of review rows --
    notably the recorded-Spanish translation queue in
    :mod:`civiccast.captions.vod` -- derive ids the exact same way the
    transcription pipeline does, keeping re-runs idempotent. Spanish cue ids
    carry a ``:es`` suffix (see
    :func:`civiccast.translate.service.translate_caption_cues`), so a Spanish
    row's id never collides with its English source row's id.
    """

    natural_id = f"{asset_id}:{cue.cue_id}"
    if len(natural_id) <= 160:
        return natural_id

    digest = sha256(natural_id.encode("utf-8")).hexdigest()[:12]
    asset_prefix = asset_id[: max(1, 160 - len(cue.cue_id) - len(digest) - 3)]
    return f"{asset_prefix}:{cue.cue_id}:{digest}"


#: Backwards-compatible private alias -- this module's internal call sites
#: predate the public name above.
_review_item_id = review_item_id_for_cue
