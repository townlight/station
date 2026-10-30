# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S13 runtime wiring — feed the operator-selected model into the feature adapters.

Slice 4. The three feature adapters (summary / translation / captions) used to
hard-code a single runtime tag. This module is the single seam where the operator's
effective selection (resolved by :class:`AiModelService`) becomes the runtime tag each
adapter loads:

* :func:`resolve_runtime_tag` turns a feature's *effective registry slug* into the
  *runtime tag* the adapter consumes (§3.1.1). The slug->tag mapping lives here, at
  the wiring boundary, NOT inside the adapters. The captions/external provider carries
  the catalog id ``whisper-large-v3`` but faster-whisper loads ``large-v3``, so the
  ``whisper-`` prefix is stripped here.
* :func:`build_summary_model` / :func:`build_translator` / :func:`build_caption_runtime`
  construct each adapter with the resolved tag.

Behavior-preserving by construction: when the operator has made no selection, the
service returns the catalog *default* whose ``model_id`` equals each feature's current
catalog tag (``gemma4:e4b`` / ``gemma4:12b`` adaptive, ``translategemma:4b``,
and -- as of OWNER-DECISION-caption-adaptive-tier.md, 2026-07-30, BINDING --
``whisper-medium`` -> ``medium``, the caption FLOOR tier), so an unconfigured station
always resolves to whatever the catalog currently names as each feature's default.
"""

from __future__ import annotations

import logging
import os

from civiccast.ai_models.service import AiModelService
from civiccast.captions.runtime import CaptionRuntime
from civiccast.native.caption_tiers import FLOOR_TIER_ID, LARGE_V3_TIER_ID
from civiccast.summary.generate import SummaryModel
from civiccast.translate.service import TranslationProvider

logger = logging.getLogger(__name__)

# faster-whisper loads the bare size id (``large-v3``), but the catalog model_id is
# namespaced ``whisper-large-v3`` so the registry/UI tag is unambiguous. Strip the
# provider prefix at this boundary; the prefix never reaches the runtime adapter.
_WHISPER_TAG_PREFIX = "whisper-"


def resolve_runtime_tag(service: AiModelService, feature: str) -> str:
    """Resolve ``feature``'s effective model to the runtime tag its adapter loads.

    Delegates the slug -> catalog ``model_id`` resolution to the service (which honors
    the operator selection, falling back to the adaptive/local default), then applies
    the per-feature runtime normalization (captions strips the ``whisper-`` prefix).
    """
    tag = service.effective_model_tag(feature)
    if feature == "captions" and tag.startswith(_WHISPER_TAG_PREFIX):
        return tag[len(_WHISPER_TAG_PREFIX) :]
    return tag


def build_local_summary_model(
    service: AiModelService, *, base_url: str | None = None
) -> SummaryModel:
    """Construct the LOCAL Ollama summary adapter bound to the effective model tag.

    The local branch of the dispatch seam; never called for a cloud/frontier selection.
    """
    from civiccast.summary.ollama import OllamaSummaryModel

    model_tag = resolve_runtime_tag(service, "summary")
    if base_url is None:
        return OllamaSummaryModel.for_release(model_tag=model_tag)
    return OllamaSummaryModel.for_release(model_tag=model_tag, base_url=base_url)


def build_local_translator(
    service: AiModelService, *, base_url: str | None = None
) -> TranslationProvider:
    """Construct the LOCAL Ollama translation adapter bound to the effective model tag."""
    from civiccast.translate.ollama import OllamaSpanishTranslator

    model_tag = resolve_runtime_tag(service, "translation")
    if base_url is None:
        return OllamaSpanishTranslator.for_release(model_tag=model_tag)
    return OllamaSpanishTranslator.for_release(model_tag=model_tag, base_url=base_url)


def build_summary_model(service: AiModelService, *, base_url: str | None = None) -> SummaryModel:
    """Build the summary adapter for the operator's effective tier (local OR cloud).

    Tier-aware: a local selection builds the loopback Ollama adapter; a cloud/frontier
    selection builds a cloud-backed shim (E1/T1). Delegates the branch to the dispatch
    seam so the local default path is unchanged.
    """
    from civiccast.ai_models import dispatch

    return dispatch.build_summary_model(service, base_url=base_url)


def build_translator(
    service: AiModelService, *, base_url: str | None = None
) -> TranslationProvider:
    """Build the translation adapter for the operator's effective tier (local OR cloud)."""
    from civiccast.ai_models import dispatch

    return dispatch.build_translator(service, base_url=base_url)


def build_caption_runtime(service: AiModelService, *, live: bool = False) -> CaptionRuntime:
    """Construct the faster-whisper runtime bound to the operator's effective model id.

    ``live=True`` builds the runtime for the LIVE caption tap, which shares the
    box with playout and is therefore sized conservatively (a core-count-aware,
    capped CTranslate2 intra-thread count, greedy decoding on CPU -- see
    :func:`civiccast.captions.runtime.default_live_tap_cpu_threads`). The
    default, ``live=False``, is the batch/VOD sizing: a finalization pass is
    allowed to use the machine.

    This flag exists here, at the seam the running service actually calls,
    because the app pre-builds the caption runtime and INJECTS it into
    ``build_tap_worker`` -- so conservative values applied inside
    ``build_tap_worker``'s own construction branch were dead code in the
    native service and the live tap kept running with every core and beam 5.
    """
    from civiccast.captions.runtime import FasterWhisperRuntime

    model_tag = resolve_runtime_tag(service, "captions")
    runtime_model: dict[str, str] = {}
    if os.environ.get("CIVICCAST_NATIVE_STATION", "").strip() == "1":
        from pathlib import Path

        from civiccast.native.station_runtime import resolve_caption_model_for_tier

        desired_tier = {"medium": FLOOR_TIER_ID, "large-v3": LARGE_V3_TIER_ID}.get(model_tag)
        if desired_tier is None:
            raise ValueError(f"Unsupported configured native caption model: {model_tag!r}")
        active_tier = os.environ.get("CIVICCAST_CAPTION_TIER", "").strip()
        active_path = os.environ.get("CIVICCAST_WHISPER_MODEL_PATH", "").strip()
        if active_tier == desired_tier and active_path:
            # station_runtime already verified this exact path while loading
            # the signed activation configuration.
            selected_path = Path(active_path)
        else:
            station_root = os.environ.get("CIVICCAST_NATIVE_STATION_ROOT", "").strip()
            if not station_root:
                raise RuntimeError("Native station did not provide its resolved installation root")
            program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "CivicCast"
            selected_path, _receipt = resolve_caption_model_for_tier(
                station_root, desired_tier, acquisition_root=program_data
            )
            logger.info(
                "Configured caption tier %s overrides activation default %s; loaded the verified configured model",
                desired_tier,
                active_tier or "unspecified",
            )
        runtime_model = {
            "packaged_model_path": str(selected_path),
            "packaged_model_tier": desired_tier,
        }

    fallback = FasterWhisperRuntime(
        model_size_or_path=model_tag,
        live=live,
        packaged_model_path=runtime_model.get("packaged_model_path"),
        packaged_model_tier=runtime_model.get("packaged_model_tier"),
    )
    if live:
        from pathlib import Path

        backend = os.environ.get("CIVICCAST_LIVE_CAPTION_ENGINE", "").strip().lower()
        if not backend:
            backend = "whistle" if os.environ.get("CIVICCAST_NATIVE_STATION") == "1" else "whisper"
        if backend == "whistle":
            from civiccast.captions.whistle import MixedCaptionRuntime, WhistleRuntime

            root = os.environ.get("CIVICCAST_WHISTLE_ROOT", "").strip()
            if root:
                assets = Path(root)
            else:
                model = Path(fallback.model_size_or_path)
                assets = (
                    model.parents[2] / "captions-whistle"
                    if len(model.parents) >= 3
                    else Path("packs/captions-whistle")
                )
            config = {
                name: getattr(fallback, name)
                for name in (
                    "model_size_or_path",
                    "device",
                    "compute_type",
                    "cpu_threads",
                    "num_workers",
                    "beam_size",
                    "language",
                    "task",
                    "vad_filter",
                )
            }
            config["live"] = True
            # The isolated fallback inherits activation environment variables.
            # Preserve the verified operator selection across that boundary.
            config.update(runtime_model)
            config["beam_size"] = 1  # The measured real-time fallback profile, on CPU and CUDA.
            config["num_workers"] = 1  # Isolated fallback requests are serialized.
            primary = WhistleRuntime(
                weights=assets / "whistle.cact",
                library=assets / "libneedle.dll",
                fallback_config=config,
            )
            channels = os.environ.get("CIVICCAST_WHISTLE_CHANNELS", "").strip()
            if channels:
                return MixedCaptionRuntime(
                    primary,
                    fallback,
                    {channel.strip() for channel in channels.split(",") if channel.strip()},
                )
            return primary
        if backend != "whisper":
            raise ValueError("CIVICCAST_LIVE_CAPTION_ENGINE must be whistle or whisper")
    return fallback


__all__ = [
    "build_caption_runtime",
    "build_local_summary_model",
    "build_local_translator",
    "build_summary_model",
    "build_translator",
    "resolve_runtime_tag",
]
