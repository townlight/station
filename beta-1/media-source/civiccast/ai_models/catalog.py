# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Hard-coded model catalog (S13, decision A).

CivicCast does not fetch the model list from a provider API (decision A: hard-coded).
This module is the single source of truth for the §3.1.1 ``key -> model_id (runtime
tag) -> provider`` mapping plus the cost / latency / privacy / network flags that the
operator console renders and the service enforces.

The DEFAULT for every feature is the LOCAL (zero cloud fee, on-device, private) tier.
Summary's default is adaptive (12B QAT when a real GPU is present AND RAM >=16GB;
e4b otherwise, including every CPU-only box regardless of RAM -- see
``detect_summary_model_default``, which field evidence on a 32GB CPU-only reference
station retired the old RAM-only rule for: 12B took 366s+ to complete a summary there
and then failed twice more under realistic memory pressure, while e4b completed every
attempt in 94-128s). The CLOUD tiers (Ollama Cloud ``gemma4:31b-cloud`` + the
OpenRouter mid-tier) ship FUNCTIONAL (D13): they appear as real, selectable options,
are priced per token, require network, and are off until the operator selects one and
accepts the cost — they are not stubs.

Latency figures below (``latency_p95_ms``) are measured, not estimated, for every
on-box (``ollama``/``external``) tier that has been benchmarked on the CPU-only 32GB
reference station -- see each tier's ``notes`` for the measurement. The operator
console (``tierLatencyLabel`` in ``apps/portal-operator/src/screens/ai-models-format
.ts``) renders local CPU-bound tiers as a range with the CPU-only caveat rather than a
single misleadingly-precise number, because real CPU inference latency varies heavily
with transcript/recording length and concurrent load on the box -- a single p95
number was previously wrong by ~30x for summary and ~70x for captions on real
hardware. Cloud/frontier tiers keep a plain single-number label; their latency is
network-bound, not CPU-bound, and does not have that same variance.
"""

from __future__ import annotations

from civiccast.ai_models.models import (
    AiFeature,
    FeatureModelRegistry,
    ModelTier,
    detect_summary_model_default,
)

# Every feature the catalog covers (the operator-controllable AI surfaces).
CATALOG_FEATURES: tuple[AiFeature, ...] = ("captions", "summary", "translation")

_GEMMA4_LICENSE = "https://ai.google.dev/gemma/terms"
_WHISPER_LICENSE = "https://github.com/openai/whisper/blob/main/LICENSE"
_OPENROUTER_LICENSE = "https://openrouter.ai/terms"

# The hosted Ollama Cloud tier is offered by BOTH summary and translation. It is
# defined ONCE here (single source of truth for its cost / tag / privacy flags) and
# referenced by both feature lists below, so its cost rate and runtime tag can never
# drift between the two features or bind to the "wrong" copy (prior M6). Note: the
# ``notes`` are feature-neutral because one object is shared across features.
_GEMMA4_31B_CLOUD = ModelTier(
    key="gemma4-31b-cloud",
    provider="ollama-cloud",
    model_id="gemma4:31b-cloud",
    cost_per_token_usd=1e-7,
    latency_p95_ms=1800,
    private=False,
    requires_network=True,
    min_ram_gb=8,
    license_url=_GEMMA4_LICENSE,
    notes="Ollama Cloud Gemma 4 31B — hosted, metered per token. Default OFF.",
)

# Per-feature tier lists. ``key`` is the stable registry slug the operator/API
# references; ``model_id`` is the runtime tag the adapter loads (§3.1.1).
_CATALOG: dict[AiFeature, list[ModelTier]] = {
    "summary": [
        ModelTier(
            key="gemma4-12b-ollama",
            provider="ollama",
            model_id="gemma4:12b",
            cost_per_token_usd=0.0,
            # MEASURED 2026-08-29, CPU-only 32GB reference station (Ryzen-class,
            # 16c/32t, gpu=None): 366s wall to complete one summary generation
            # cold, then two more attempts failed outright under realistic
            # memory pressure (CPU buffer allocation failure; a crashed
            # llama-server subprocess). Not just slow -- unreliable CPU-only.
            # The number here is the one completed measurement; the operator
            # console never renders it as a plain seconds figure for an
            # on-box tier (see ai-models-format.ts tierLatencyLabel) because
            # a single number cannot honestly represent "sometimes crashes."
            latency_p95_ms=366_000,
            private=True,
            requires_network=False,
            min_ram_gb=16,
            license_url=_GEMMA4_LICENSE,
            notes=(
                "Local Gemma 4 12B QAT — long-context summary option. Selectable on any "
                "box, but the adaptive DEFAULT only offers it with a real GPU present "
                "(detect_summary_model_default): measured CPU-only on the 32GB reference "
                "station it took 366s to complete once and then failed twice more "
                "(memory allocation failure; a crashed llama-server process). A GPU is "
                "recommended before selecting this tier manually."
            ),
        ),
        ModelTier(
            key="gemma4-e4b-ollama",
            provider="ollama",
            model_id="gemma4:e4b",
            cost_per_token_usd=0.0,
            # MEASURED 2026-08-29, same CPU-only 32GB reference station: 128s
            # cold, 94s warm, both completed successfully every attempt.
            latency_p95_ms=128_000,
            private=True,
            requires_network=False,
            min_ram_gb=8,
            license_url=_GEMMA4_LICENSE,
            notes=(
                "Local Gemma 4 e4b — the summary DEFAULT on any box without a real GPU, "
                "regardless of system RAM (32GB CPU-only is the reference target, not an "
                "edge case). Measured 94-128s per summary, CPU-only, and completed every "
                "attempt where 12B did not."
            ),
        ),
        _GEMMA4_31B_CLOUD,
        ModelTier(
            key="gemini-2.5-flash-openrouter",
            provider="openrouter",
            model_id="google/gemini-2.5-flash",
            cost_per_token_usd=3e-7,
            latency_p95_ms=1500,
            private=False,
            requires_network=True,
            min_ram_gb=8,
            license_url=_OPENROUTER_LICENSE,
            notes="OpenRouter mid-tier frontier (Gemini 2.5 Flash) — hosted, per-token. Default OFF.",
        ),
    ],
    "translation": [
        ModelTier(
            key="translategemma-4b-ollama",
            provider="ollama",
            model_id="translategemma:4b",
            cost_per_token_usd=0.0,
            latency_p95_ms=1900,
            private=True,
            requires_network=False,
            min_ram_gb=8,
            license_url=_GEMMA4_LICENSE,
            notes=(
                "Local TranslateGemma 4B — Spanish translation default (never gemma4:4b). "
                "Used for Spanish caption cues on published recordings; generated cues "
                "enter the caption review queue for their own approval."
            ),
        ),
        _GEMMA4_31B_CLOUD,
    ],
    "captions": [
        # BOUND 2026-07-30 per OWNER-DECISION-caption-adaptive-tier.md: the caption
        # FLOOR tier is `medium` (Systran/faster-whisper-medium, pinned in
        # civiccast.native.caption_tiers.CAPTION_TIER_REGISTRY) and is the mandatory
        # CPU-only baseline -- see `_DEFAULT_KEY["captions"]` below. large-v3 remains
        # the optional QUALITY tier, still offered here and auto-selected only when
        # measured hardware allows.
        ModelTier(
            key="whisper-medium-faster",
            provider="external",
            model_id="whisper-medium",
            cost_per_token_usd=0.0,
            # MEASURED 2026-08-29, CPU-only 32GB reference station: ~37s to
            # transcribe 11s of audio, ~3.3x real time. Transcription time
            # scales with recording length, so a fixed ms figure cannot
            # honestly stand for "typical latency" the way it can for a
            # bounded LLM request; the console renders this tier as a
            # realtime multiple, not a raw ms count (tierLatencyLabel,
            # ai-models-format.ts). This field is kept as a rough same-order
            # reference for any non-UI caller, not a promise to the operator.
            latency_p95_ms=3_300,
            private=True,
            requires_network=False,
            min_ram_gb=4,
            license_url=_WHISPER_LICENSE,
            notes=(
                "faster-whisper medium (int8) on-box — CPU-only caption FLOOR tier "
                "(mandatory baseline), captions default. Measured on the 32GB CPU-only "
                "reference station: ~3.3x real time (37s to transcribe 11s of audio) — "
                "not a fixed per-request latency, since it scales with recording length."
            ),
        ),
        ModelTier(
            key="whisper-large-v3-faster",
            provider="external",
            model_id="whisper-large-v3",
            cost_per_token_usd=0.0,
            # NOT independently measured (only the floor tier above was
            # benchmarked in the field); kept conservatively above the
            # measured floor-tier multiplier rather than the old flat 900ms,
            # which was wrong for the same reason the floor tier's 500ms was.
            latency_p95_ms=5_000,
            private=True,
            requires_network=False,
            min_ram_gb=8,
            license_url=_WHISPER_LICENSE,
            notes=(
                "faster-whisper large-v3 (int8) on-box — captions QUALITY tier, "
                "auto-selected only when measured hardware allows. Not independently "
                "measured; expect slower than the medium floor tier's measured ~3.3x "
                "real time, CPU-only."
            ),
        ),
    ],
}

# The local default slug per feature (summary is resolved adaptively at build time).
_DEFAULT_KEY: dict[AiFeature, str] = {
    # OWNER-DECISION-caption-adaptive-tier.md (2026-07-30, BINDING): the caption FLOOR
    # tier (medium) is the mandatory CPU-only baseline and the default here. large-v3
    # is the optional quality tier (see caption_tier_selection.select_caption_tier /
    # the native adaptive-tier packaging system for the auto-upgrade policy).
    "captions": "whisper-medium-faster",
    "summary": "gemma4-12b-ollama",  # placeholder; overridden by detect_summary_model_default
    "translation": "translategemma-4b-ollama",
}


def catalog_tiers_for(feature: AiFeature) -> list[ModelTier]:
    """Every selectable tier for ``feature`` (fresh copies; immutable to callers)."""
    return [tier.model_copy(deep=True) for tier in _CATALOG[feature]]


def catalog_tier(key: str) -> ModelTier:
    """Resolve a registry slug to its catalog :class:`ModelTier` (first match wins)."""
    for tiers in _CATALOG.values():
        for tier in tiers:
            if tier.key == key:
                return tier.model_copy(deep=True)
    raise KeyError(f"Unknown model key: {key!r}")


def catalog_tier_for_feature(feature: AiFeature, key: str) -> ModelTier | None:
    """Resolve ``key`` only within ``feature``'s tiers (None if not offered there)."""
    for tier in _CATALOG[feature]:
        if tier.key == key:
            return tier.model_copy(deep=True)
    return None


def default_key_for(
    feature: AiFeature, *, system_ram_total_gb: int = 8, has_gpu: bool = False
) -> str:
    """The (possibly adaptive) local default slug for ``feature``.

    ``has_gpu`` matters only for ``summary`` (see
    :func:`~civiccast.ai_models.models.detect_summary_model_default`): CPU-only
    boxes -- the common case at the 32GB reference target -- get the model
    that actually completes there regardless of RAM headroom.
    """
    if feature == "summary":
        return detect_summary_model_default(system_ram_total_gb, has_gpu=has_gpu)
    return _DEFAULT_KEY[feature]


def build_feature_registry(
    feature: AiFeature,
    *,
    system_ram_total_gb: int = 8,
    has_gpu: bool = False,
    operator_selected_key: str | None = None,
) -> FeatureModelRegistry:
    """Assemble a :class:`FeatureModelRegistry` from the catalog + an optional selection."""
    return FeatureModelRegistry(
        feature=feature,
        default_key=default_key_for(
            feature, system_ram_total_gb=system_ram_total_gb, has_gpu=has_gpu
        ),
        adaptive_default=(feature == "summary"),
        available_tiers=catalog_tiers_for(feature),
        operator_selected_key=operator_selected_key,
    )


__all__ = [
    "CATALOG_FEATURES",
    "build_feature_registry",
    "catalog_tier",
    "catalog_tier_for_feature",
    "catalog_tiers_for",
    "default_key_for",
]
