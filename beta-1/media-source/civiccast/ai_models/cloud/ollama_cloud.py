# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Ollama Cloud adapter (S13, D13) — gemma4:31b-cloud, functional + default OFF.

Ollama Cloud speaks the same ``/api/generate`` wire shape as the local Ollama
daemon; the *only* deltas vs. the local client are the host (off-box), ``https``,
and an ``Authorization: Bearer <key>`` header. So we mirror the request PATTERN
of :func:`civiccast.ai_runtime.ollama_client.generate_with_ollama` (POST JSON,
``stream:false``, ``options.temperature:0``, parse ``response``) WITHOUT importing
its loopback-only ``_request_json`` — egress here goes through the off-box cloud
guard instead.

Default OFF / opt-in: the adapter refuses to construct unless cloud consent (the
TOS checkbox) is recorded, and refuses to run unless its provider credential is
resolvable. The credential is resolved at call time via an injected resolver
(never imported by the adapter), mirroring ``eas/workers.build_http_fetcher``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from civiccast.ai_models.catalog import catalog_tier, catalog_tier_for_feature
from civiccast.ai_models.cloud import egress
from civiccast.ai_models.cloud.cost import estimate_cost_usd
from civiccast.ai_models.models import AiFeature, ModelTier

SecretResolver = Callable[[str], str | None]


def _resolve_tier(model_key: str, feature: AiFeature | None) -> ModelTier:
    """Resolve ``model_key`` to its catalog tier, feature-scoped when ``feature`` is set.

    Feature-scoping (``catalog_tier_for_feature``) ensures the runtime tag binds to the
    selected feature's copy of a shared key, never another feature's first-match (M6).
    """
    if feature is not None:
        tier = catalog_tier_for_feature(feature, model_key)
        if tier is None:
            raise KeyError(f"Unknown model key {model_key!r} for feature {feature!r}")
        return tier
    return catalog_tier(model_key)


@dataclass(frozen=True)
class CloudGenerationResult:
    """A completed cloud generation plus realized token counts and cost estimate."""

    text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal


class OllamaCloudAdapter:
    """Functional, opt-in Ollama Cloud adapter for the ``gemma4:31b-cloud`` tier."""

    BASE_URL = "https://ollama.com"
    ALLOWED_HOSTS = frozenset({"ollama.com"})
    _TIMEOUT_SECONDS = 120

    def __init__(
        self,
        *,
        model_key: str,
        credential_ref: str,
        resolve_secret: SecretResolver,
        consent_accepted: bool,
        feature: AiFeature | None = None,
    ) -> None:
        if not consent_accepted:
            raise egress.CloudConsentRequiredError(
                "Ollama Cloud is a hosted, per-token tier (default OFF). The operator "
                "must accept the cloud TOS before it can be used."
            )
        self._model_key = model_key
        self._feature = feature
        self._model_tag = _resolve_tier(model_key, feature).model_id
        self._credential_ref = credential_ref
        self._resolve_secret = resolve_secret

    def generate(self, *, prompt: str) -> CloudGenerationResult:
        """Generate a non-streaming completion from Ollama Cloud (real HTTP)."""

        token = self._resolve_secret(self._credential_ref)
        if not token:
            raise egress.CloudCredentialError(
                f"No Ollama Cloud credential resolved for handle {self._credential_ref!r}. "
                "Store the provider API key before enabling the hosted tier."
            )

        url = f"{self.BASE_URL}/api/generate"
        egress.require_cloud_https(url, allowed_hosts=self.ALLOWED_HOSTS)
        body = json.dumps(
            {
                "model": self._model_tag,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0},
            }
        ).encode("utf-8")
        # Egress is host-allowlisted + https-enforced by require_cloud_https above.
        request = egress.urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with egress.urlopen(request, timeout=self._TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise egress.CloudRuntimeUnavailableError(
                f"Ollama Cloud request failed for {url}. Check network and credentials."
            ) from exc

        if not isinstance(payload, dict):
            raise egress.CloudRuntimeUnavailableError(
                f"Ollama Cloud returned non-object JSON for {url}."
            )
        text = payload.get("response")
        if not isinstance(text, str):
            raise egress.CloudRuntimeUnavailableError(
                "Ollama Cloud /api/generate returned no response text."
            )
        prompt_tokens = _as_token_count(payload.get("prompt_eval_count"))
        completion_tokens = _as_token_count(payload.get("eval_count"))
        return CloudGenerationResult(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=estimate_cost_usd(
                self._model_key,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                feature=self._feature,
            ),
        )


def _as_token_count(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


__all__ = ["CloudGenerationResult", "OllamaCloudAdapter", "SecretResolver"]
