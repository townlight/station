# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Channel egress contracts and helpers."""

from civiccast.egress.branding import (
    BRANDING_PROOF_BOUNDARY,
    EgressBrandingPlan,
    EgressOverlayRegion,
    build_branding_filter_plan,
)
from civiccast.egress.caption_embed import (
    CAPTION_EMBED_PROOF_BOUNDARY,
    EgressCaptionDecodeBackProof,
    EgressCaptionEmbeddingPlan,
    PassThroughCaptionEmbedder,
    SidecarCaptionEmbedder,
    evaluate_caption_decode_back,
    load_caption_cues_from_timed_text,
    parse_caption_cues_from_timed_text,
)
from civiccast.egress.cg_bridge import (
    CG_EGRESS_PROOF_BOUNDARY,
    EgressCgOverlayClearProof,
    EgressCgOverlayProof,
    build_cg_overlay_clear_egress_proof,
    build_cg_overlay_egress_proof,
)
from civiccast.egress.continuity import (
    CONTINUITY_PROOF_BOUNDARY,
    EgressContinuityBoundary,
    EgressContinuityProof,
    build_boundary_events,
    run_filesink_continuity_proof,
    run_srt_receiver_continuity_proof,
    split_srt_receiver_options,
)
from civiccast.egress.daemon import EgressDaemon
from civiccast.egress.encoder_strategy import (
    ConcatEncoderStrategy,
    EncoderStartRequest,
    EncoderStartResult,
)
from civiccast.egress.health import (
    EgressEncoderMetrics,
    build_default_sink_health,
    encoder_has_progress,
    parse_ffmpeg_encoder_metrics_line,
    read_latest_ffmpeg_encoder_metrics,
    worker_produced_output,
    worker_reached_playing,
)
from civiccast.egress.live_takeover import build_live_takeover_source_plan
from civiccast.egress.models import (
    CanonicalProfile,
    EgressCommand,
    EgressCommandDb,
    EgressConfig,
    EgressConfigDb,
    EgressHealthSample,
    EgressHealthSampleDb,
    EgressProofEvent,
    EgressProofEventDb,
    EgressSinkDb,
    EgressSinkSpec,
    EgressSourcePlan,
    EgressSourceSegment,
    EgressStateDb,
    EgressStateRow,
)
from civiccast.egress.preparer import (
    PreparedSegmentRecord,
    SourcePreparationReport,
    SourcePreparer,
    build_conform_source_args,
)
from civiccast.egress.resolver import (
    ScheduleSourcePlanProvider,
    SlateSourceGenerator,
    build_slate_source_args,
    build_source_plan_from_schedule,
)
from civiccast.egress.runtime import build_persistent_encoder_args, write_concat_plan
from civiccast.egress.service import EgressService, EgressServiceReport
from civiccast.egress.store import InMemoryEgressStore, PostgresEgressStore
from civiccast.egress.supervisor import LookAheadSourcePlanProvider, PlayoutSupervisor

__all__ = [
    "BRANDING_PROOF_BOUNDARY",
    "CAPTION_EMBED_PROOF_BOUNDARY",
    "CG_EGRESS_PROOF_BOUNDARY",
    "CONTINUITY_PROOF_BOUNDARY",
    "CanonicalProfile",
    "ConcatEncoderStrategy",
    "EgressBrandingPlan",
    "EgressCaptionDecodeBackProof",
    "EgressCaptionEmbeddingPlan",
    "EgressCgOverlayClearProof",
    "EgressCgOverlayProof",
    "EgressCommand",
    "EgressCommandDb",
    "EgressConfig",
    "EgressConfigDb",
    "EgressContinuityBoundary",
    "EgressContinuityProof",
    "EgressDaemon",
    "EgressEncoderMetrics",
    "EgressHealthSample",
    "EgressHealthSampleDb",
    "EgressOverlayRegion",
    "EgressProofEvent",
    "EgressProofEventDb",
    "EgressService",
    "EgressServiceReport",
    "EgressSinkDb",
    "EgressSinkSpec",
    "EgressSourcePlan",
    "EgressSourceSegment",
    "EgressStateDb",
    "EgressStateRow",
    "EncoderStartRequest",
    "EncoderStartResult",
    "InMemoryEgressStore",
    "LookAheadSourcePlanProvider",
    "PassThroughCaptionEmbedder",
    "PlayoutSupervisor",
    "PostgresEgressStore",
    "PreparedSegmentRecord",
    "ScheduleSourcePlanProvider",
    "SidecarCaptionEmbedder",
    "SlateSourceGenerator",
    "SourcePreparationReport",
    "SourcePreparer",
    "build_boundary_events",
    "build_branding_filter_plan",
    "build_cg_overlay_clear_egress_proof",
    "build_cg_overlay_egress_proof",
    "build_conform_source_args",
    "build_default_sink_health",
    "build_live_takeover_source_plan",
    "build_persistent_encoder_args",
    "build_slate_source_args",
    "build_source_plan_from_schedule",
    "encoder_has_progress",
    "evaluate_caption_decode_back",
    "load_caption_cues_from_timed_text",
    "parse_caption_cues_from_timed_text",
    "parse_ffmpeg_encoder_metrics_line",
    "read_latest_ffmpeg_encoder_metrics",
    "run_filesink_continuity_proof",
    "run_srt_receiver_continuity_proof",
    "split_srt_receiver_options",
    "worker_produced_output",
    "worker_reached_playing",
    "write_concat_plan",
]
