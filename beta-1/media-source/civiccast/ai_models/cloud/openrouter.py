# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""OpenRouter adapter (S13, D13) — mid-tier frontier, functional + default OFF.

OpenRouter is OpenAI-compatible: POST ``/api/v1/chat/completions`` with
``{model, messages, temperature}`` and parse ``choices[0].message.content`` plus
``usage.{prompt_tokens,completion_tokens}``. Same urllib pattern + off-box cloud
guard as :mod:`civiccast.ai_models.cloud.ollama_cloud`; the catalog ``model_id``
is the OpenRouter route slug (e.g. ``google/gemini-2.5-flash``).

Default OFF / opt-in: refuses to construct without recorded cloud consent (TOS
checkbox) and refuses to run without a resolvable provider credential (resolved
at call time via an injected resolver, never imported by the adapter).
"""

from __future__ import annotations

import json

from civiccast.ai_models.cloud import egress
from civiccast.ai_models.cloud.cost import estimate_cost_usd
from civiccast.ai_models.cloud.ollama_cloud import (
    CloudGenerationResult,
    SecretResolver,
    _resolve_tier,
)
from civiccast.ai_models.models import AiFeature


class OpenRouterAdapter:
    """Functional, opt-in OpenRouter adapter for the mid-tier frontier route."""

    BASE_URL = "https://openrouter.ai"
    ALLOWED_HOSTS = frozenset({"openrouter.ai"})
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
                "OpenRouter is a hosted, per-token frontier tier (default OFF). The "
                "operator must accept the cloud TOS before it can be used."
            )
        self._model_key = model_key
        self._feature = feature
        self._route_slug = _resolve_tier(model_key, feature).model_id
        self._credential_ref = credential_ref
        self._resolve_secret = resolve_secret

    def generate(self, *, prompt: str) -> CloudGenerationResult:
        """Generate a non-streaming completion from OpenRouter (real HTTP)."""

        token = self._resolve_secret(self._credential_ref)
        if not token:
            raise egress.CloudCredentialError(
                f"No OpenRouter credential resolved for handle {self._credential_ref!r}. "
                "Store the provider API key before enabling the frontier tier."
            )

        url = f"{self.BASE_URL}/api/v1/chat/completions"
        egress.require_cloud_https(url, allowed_hosts=self.ALLOWED_HOSTS)
        body = json.dumps(
            {
                "model": self._route_slug,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
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
                f"OpenRouter request failed for {url}. Check network and credentials."
            ) from exc

        text = _extract_message_content(payload, url)
        usage = payload.get("usage") if isinstance(payload, dict) else None
        prompt_tokens = _as_token_count((usage or {}).get("prompt_tokens"))
        completion_tokens = _as_token_count((usage or {}).get("completion_tokens"))
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


def _extract_message_content(payload: object, url: str) -> str:
    if not isinstance(payload, dict):
        raise egress.CloudRuntimeUnavailableError(f"OpenRouter returned non-object JSON for {url}.")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise egress.CloudRuntimeUnavailableError(
            "OpenRouter /api/v1/chat/completions returned no choices."
        )
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise egress.CloudRuntimeUnavailableError(
            "OpenRouter /api/v1/chat/completions returned no message content."
        )
    return content


def _as_token_count(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


__all__ = ["OpenRouterAdapter"]
