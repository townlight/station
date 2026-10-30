# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S13 tier-aware dispatch seam (E1/T1) — route a selection to local OR cloud.

The runtime wiring (:mod:`civiccast.ai_models.runtime`) used to build ONLY the local
loopback adapters, so selecting a hosted tier (``ollama-cloud`` / ``openrouter``) sent
a cloud tag to the local Ollama daemon and failed — the cloud adapters were orphaned
(audit E1/T1). This module is the single seam that branches on the effective tier's
``provider``:

* ``ollama`` / ``external`` -> the local adapter (today's behavior, unchanged).
* ``ollama-cloud`` / ``openrouter`` -> a cloud-backed shim that delegates ``generate``
  to :class:`OllamaCloudAdapter` / :class:`OpenRouterAdapter`, supplying the
  keyring-resolved credential (via the injected ``resolve_secret``) and the PERSISTED
  consent flag, then adapts the :class:`CloudGenerationResult` to the summary /
  translate protocol the feature pipelines consume.

The cloud shims honor the same defense-in-depth as the adapters: they construct with
the persisted ``consent_accepted`` (a missing consent surfaces ``CloudConsentRequiredError``)
and the adapter refuses to run without a resolvable credential (``CloudCredentialError``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from civiccast.ai_models.cloud.ollama_cloud import OllamaCloudAdapter, SecretResolver
from civiccast.ai_models.cloud.openrouter import OpenRouterAdapter
from civiccast.ai_models.models import ModelProvider, ModelTier
from civiccast.ai_models.secrets import credential_ref_for_provider, load_provider_secret
from civiccast.ai_models.service import AiModelService, EffectiveSelection
from civiccast.ai_runtime.evidence import RuntimeEvidence
from civiccast.summary.generate import SummaryModel
from civiccast.translate.service import TranslationProvider

# The cloud providers the dispatch seam knows how to back with a cloud adapter.
_CLOUD_ADAPTERS: dict[ModelProvider, type[OllamaCloudAdapter] | type[OpenRouterAdapter]] = {
    "ollama-cloud": OllamaCloudAdapter,
    "openrouter": OpenRouterAdapter,
}


def _is_cloud(provider: ModelProvider) -> bool:
    return provider in _CLOUD_ADAPTERS


def _build_cloud_adapter(
    selection: EffectiveSelection,
    *,
    resolve_secret: SecretResolver,
) -> OllamaCloudAdapter | OpenRouterAdapter:
    """Construct the cloud adapter for an effective cloud/frontier selection.

    Passes the persisted consent flag (so the adapter's ``CloudConsentRequiredError``
    guard is satisfied from durable state, not transient UI state) and the keyring
    handle for the provider; the credential itself is resolved at call time by
    ``resolve_secret`` (never imported by the adapter).
    """
    tier = selection.tier
    adapter_cls = _CLOUD_ADAPTERS[tier.provider]
    return adapter_cls(
        model_key=tier.key,
        credential_ref=credential_ref_for_provider(tier.provider),
        resolve_secret=resolve_secret,
        consent_accepted=selection.consent_accepted,
        feature=selection.feature,
    )


def _cloud_evidence_line(tier: ModelTier) -> str:
    """A hosted runtime-evidence line for the de-pinned release gate (attributable)."""
    return RuntimeEvidence(
        runtime=tier.provider,  # type: ignore[arg-type]  # ollama-cloud / openrouter are RuntimeKind
        model=tier.model_id,
        compute=None,
        digest=None,
        runtime_version=f"{tier.provider}-hosted",
        manifest_source=f"https://{_provider_host(tier.provider)}",
    ).to_machine_line()


def _provider_host(provider: ModelProvider) -> str:
    if provider == "ollama-cloud":
        return OllamaCloudAdapter.BASE_URL.removeprefix("https://")
    return OpenRouterAdapter.BASE_URL.removeprefix("https://")


class CloudSummaryModel:
    """Cloud-backed :class:`~civiccast.summary.generate.SummaryModel` shim.

    Adapts a cloud completion to the summary protocol: it builds the SAME prompt the
    local summary adapter builds and parses the model's JSON the SAME way, so the
    downstream :class:`SummaryGenerationPipeline` validation contract is unchanged.
    """

    def __init__(
        self,
        adapter: OllamaCloudAdapter | OpenRouterAdapter,
        *,
        model_tag: str,
        evidence_line: str,
    ) -> None:
        self._adapter = adapter
        self.model_tag = model_tag
        self._evidence_line = evidence_line

    def generate(
        self,
        *,
        meeting_id: str,
        cues: list[Any],
        prompt_version: str,
    ) -> dict[str, Any]:
        from civiccast.summary.ollama import _parse_model_json, _summary_prompt

        prompt = _summary_prompt(
            meeting_id=meeting_id,
            cues=cues,
            prompt_version=prompt_version,
            evidence_line=self._evidence_line,
        )
        result = self._adapter.generate(prompt=prompt)
        return _parse_model_json(result.text)


class CloudTranslator:
    """Cloud-backed :class:`~civiccast.translate.service.TranslationProvider` shim.

    Adapts a cloud completion to ``translate_text``; preserves protected glossary
    placeholders by leaving the prompt/response text untouched (the caption pipeline
    re-checks placeholder integrity around this call).
    """

    def __init__(
        self,
        adapter: OllamaCloudAdapter | OpenRouterAdapter,
        *,
        model_tag: str,
    ) -> None:
        self._adapter = adapter
        self.model_tag = model_tag

    def translate_text(
        self,
        text: str,
        *,
        source_language: str,
        target_language: str,
        glossary: Mapping[str, str] | None = None,
    ) -> str:
        prompt = (
            f"Translate the following text from {source_language} to {target_language}. "
            "Return only the translation, no commentary. Keep any tokens like "
            "§§0001§§ unchanged.\n\n"
            f"{text}"
        )
        result = self._adapter.generate(prompt=prompt)
        return result.text


def build_summary_model(
    service: AiModelService,
    *,
    base_url: str | None = None,
    resolve_secret: SecretResolver = load_provider_secret,
) -> SummaryModel:
    """Build the summary adapter for the operator's effective tier (local OR cloud)."""
    selection = service.effective_selection("summary")
    if _is_cloud(selection.tier.provider):
        adapter = _build_cloud_adapter(selection, resolve_secret=resolve_secret)
        return CloudSummaryModel(
            adapter,
            model_tag=selection.tier.model_id,
            evidence_line=_cloud_evidence_line(selection.tier),
        )
    from civiccast.ai_models.runtime import build_local_summary_model

    return build_local_summary_model(service, base_url=base_url)


def build_translator(
    service: AiModelService,
    *,
    base_url: str | None = None,
    resolve_secret: SecretResolver = load_provider_secret,
) -> TranslationProvider:
    """Build the translation adapter for the operator's effective tier (local OR cloud)."""
    selection = service.effective_selection("translation")
    if _is_cloud(selection.tier.provider):
        adapter = _build_cloud_adapter(selection, resolve_secret=resolve_secret)
        return CloudTranslator(adapter, model_tag=selection.tier.model_id)
    from civiccast.ai_models.runtime import build_local_translator

    return build_local_translator(service, base_url=base_url)


__all__ = [
    "CloudSummaryModel",
    "CloudTranslator",
    "build_summary_model",
    "build_translator",
]
