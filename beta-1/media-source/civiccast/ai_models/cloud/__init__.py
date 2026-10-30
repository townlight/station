# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Functional cloud AI adapters (S13, D13).

Two real, opt-in cloud adapters that ship FUNCTIONAL but default OFF:

* :class:`~civiccast.ai_models.cloud.ollama_cloud.OllamaCloudAdapter` — Ollama
  Cloud ``gemma4:31b-cloud`` over ``https://ollama.com/api/generate``.
* :class:`~civiccast.ai_models.cloud.openrouter.OpenRouterAdapter` — the
  OpenRouter mid-tier frontier route over the OpenAI-compatible chat endpoint.

This subpackage is the *directory boundary* for network egress: nothing here
imports the local loopback-only client's ``_request_json``, and nothing outside
here performs cloud egress. Every request is gated by
:func:`~civiccast.ai_models.cloud.egress.require_cloud_https` (https-only, NOT
loopback, host on a hard-coded allowlist) and only runs after the operator
records cloud consent (the TOS checkbox) and a credential is resolvable.
"""

from __future__ import annotations

from civiccast.ai_models.cloud.cost import estimate_cost_usd
from civiccast.ai_models.cloud.egress import (
    CloudConsentRequiredError,
    CloudCredentialError,
    CloudEgressError,
    CloudRuntimeUnavailableError,
    require_cloud_https,
)
from civiccast.ai_models.cloud.ollama_cloud import (
    CloudGenerationResult,
    OllamaCloudAdapter,
)
from civiccast.ai_models.cloud.openrouter import OpenRouterAdapter

__all__ = [
    "CloudConsentRequiredError",
    "CloudCredentialError",
    "CloudEgressError",
    "CloudGenerationResult",
    "CloudRuntimeUnavailableError",
    "OllamaCloudAdapter",
    "OpenRouterAdapter",
    "estimate_cost_usd",
    "require_cloud_https",
]
