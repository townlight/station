# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Operator-facing AI model-selection registry (S13).

CivicCast restores operator agency over the AI stack: each feature (captions,
summary, translation) exposes a registry of model tiers (local Ollama -> Ollama
Cloud -> OpenRouter), the operator always chooses, and the default stays local
(zero cloud fee).
"""

from __future__ import annotations

from civiccast.ai_models.catalog import (
    CATALOG_FEATURES,
    build_feature_registry,
    catalog_tier,
    catalog_tiers_for,
    default_key_for,
)
from civiccast.ai_models.cloud import (
    CloudConsentRequiredError,
    CloudCredentialError,
    CloudEgressError,
    CloudGenerationResult,
    CloudRuntimeUnavailableError,
    OllamaCloudAdapter,
    OpenRouterAdapter,
    estimate_cost_usd,
    require_cloud_https,
)
from civiccast.ai_models.models import (
    AiFeature,
    AiModelAvailability,
    AiModelConfiguration,
    AiModelConfigurationDb,
    CloudProvider,
    FeatureModelAvailability,
    FeatureModelRegistry,
    FeatureModelRegistryDb,
    FirstRunOverrideRequest,
    ModelProvider,
    ModelSelectionRequest,
    ModelTier,
    ModelTierBand,
    ProviderKeyRequest,
    ProviderKeyStatus,
    detect_summary_model_default,
)
from civiccast.ai_models.secrets import (
    ProviderSecretStoreError,
    credential_ref_for_provider,
    delete_provider_secret,
    load_provider_secret,
    save_provider_secret,
)
from civiccast.ai_models.service import (
    AiModelService,
    AiModelServiceError,
    ConsentRequiredError,
    EffectiveSelection,
    FirstRunNotSeededError,
    InvalidProviderKeyError,
    UnknownFeatureError,
    UnknownModelError,
    UnknownProviderError,
)
from civiccast.ai_models.store import (
    AiModelStore,
    AiModelStoreError,
    FeatureNotFoundError,
)

__all__ = [
    "CATALOG_FEATURES",
    "AiFeature",
    "AiModelAvailability",
    "AiModelConfiguration",
    "AiModelConfigurationDb",
    "AiModelService",
    "AiModelServiceError",
    "AiModelStore",
    "AiModelStoreError",
    "CloudConsentRequiredError",
    "CloudCredentialError",
    "CloudEgressError",
    "CloudGenerationResult",
    "CloudProvider",
    "CloudRuntimeUnavailableError",
    "ConsentRequiredError",
    "EffectiveSelection",
    "FeatureModelAvailability",
    "FeatureModelRegistry",
    "FeatureModelRegistryDb",
    "FeatureNotFoundError",
    "FirstRunNotSeededError",
    "FirstRunOverrideRequest",
    "InvalidProviderKeyError",
    "ModelProvider",
    "ModelSelectionRequest",
    "ModelTier",
    "ModelTierBand",
    "OllamaCloudAdapter",
    "OpenRouterAdapter",
    "ProviderKeyRequest",
    "ProviderKeyStatus",
    "ProviderSecretStoreError",
    "UnknownFeatureError",
    "UnknownModelError",
    "UnknownProviderError",
    "build_feature_registry",
    "catalog_tier",
    "catalog_tiers_for",
    "credential_ref_for_provider",
    "default_key_for",
    "delete_provider_secret",
    "detect_summary_model_default",
    "estimate_cost_usd",
    "load_provider_secret",
    "require_cloud_https",
    "save_provider_secret",
]
