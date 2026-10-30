# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S13 operator AI-model selection staff API.

All endpoints under ``/api/staff/ai-models``, gated by the real product roles
(``auth/roles.py``) via ``require_any_role`` per the S13 §4.1 table:

* ``setup_admin`` *or* ``meeting_operator`` may READ a registry / the config,
* only ``setup_admin`` may WRITE a selection (commissioning act).

The single DI seam (``get_ai_model_service``) returns ``None`` at import so the
module opens no database; the app factory overrides it once durable storage is
ready (one edit, via the shared ``_wire_durable_stores``). An unknown model key
for the feature is a 400; an unknown feature is a 404; an unwired service is a
503 — never a silent 200 against storage that is not there.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from civiccast.ai_models.models import (
    AiModelAvailability,
    AiModelConfiguration,
    FeatureModelRegistry,
    FirstRunOverrideRequest,
    ModelSelectionRequest,
    ProviderKeyRequest,
    ProviderKeyStatus,
)
from civiccast.ai_models.secrets import ProviderSecretStoreError
from civiccast.ai_models.service import (
    AiModelService,
    ConsentRequiredError,
    FirstRunNotSeededError,
    InvalidProviderKeyError,
    UnknownFeatureError,
    UnknownModelError,
    UnknownProviderError,
)
from civiccast.auth.models import OperatorIdentity
from civiccast.auth.roles import require_any_role

_DB_NOT_READY = "Durable storage is not ready yet."
_UNKNOWN_FEATURE = "Unknown AI feature (no such captions/summary/translation registry)."
_UNKNOWN_MODEL = "Unknown model key for this feature, or cloud consent not accepted."
_NOT_SEEDED = "First-run adaptive default is not seeded yet; finish commissioning first."
_UNKNOWN_PROVIDER = "Unknown cloud provider (expected ollama-cloud or openrouter)."
_INVALID_KEY = "The provider API key must be a non-empty single-line secret."
_KEYRING_UNAVAILABLE = "The OS credential store is unavailable; provider keys cannot be saved here."

_READ = ("setup_admin", "meeting_operator")
_WRITE = ("setup_admin",)

# Surfaced into the generated OpenAPI (``x-required-roles``) so the published API
# contract and ``docs/API-REFERENCE.md`` show the §4.1 differential authorization
# (READ = setup_admin OR meeting_operator; WRITE = setup_admin only). Kept beside
# the ``require_any_role`` calls below so the documented role cannot drift from the
# enforced one (the doc-lint + 403 API test guard the pair).
_READ_ROLES_EXTRA = {"x-required-roles": list(_READ)}
_WRITE_ROLES_EXTRA = {"x-required-roles": list(_WRITE)}

staff_router = APIRouter(prefix="/api/staff/ai-models", tags=["staff", "ai-models"])
_LOG = logging.getLogger(__name__)


# --- DI seam (overridden by the app factory) --------------------------------


def get_ai_model_service() -> AiModelService | None:
    return None


def _require_service(svc: AiModelService | None) -> AiModelService:
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return svc


# --- endpoints ---------------------------------------------------------------


@staff_router.get(
    "",
    response_model=AiModelConfiguration,
    summary="List the station-wide AI-model configuration (every feature)",
    dependencies=[Depends(require_any_role(*_READ))],
    openapi_extra=_READ_ROLES_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def list_all_models(
    svc: AiModelService | None = Depends(get_ai_model_service),
) -> AiModelConfiguration:
    """The station-wide configuration: every feature's assembled registry."""
    return _require_service(svc).get_configuration()


@staff_router.get(
    "/availability",
    response_model=AiModelAvailability,
    summary="Per-feature availability of the effective AI model",
    dependencies=[Depends(require_any_role(*_READ))],
    openapi_extra=_READ_ROLES_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def get_model_availability(
    request: Request,
    svc: AiModelService | None = Depends(get_ai_model_service),
) -> AiModelAvailability:
    """Per-feature availability of the effective model (present/absent + reachability).

    A lightweight, role-gated read the operator console consumes to render a §6.3
    "AI runtime unavailable / model not installed — this feature will defer" hint
    (Q2/U4). Routed BEFORE ``/{feature}`` so the literal path is not captured as a
    feature name.
    """
    availability = _require_service(svc).get_availability()
    captions = availability.features.get("captions")
    runtime_key = getattr(request.app.state, "caption_live_model_key", None)
    if captions is not None and runtime_key:
        captions.live_runtime_model_key = runtime_key
        captions.live_runtime_model_loaded_at_startup = getattr(
            request.app.state, "caption_live_runtime_model_loaded_at_startup", None
        )
    translation = availability.features.get("translation")
    store_bundle = getattr(request.app.state, "store_bundle", None)
    review_store_factory = getattr(store_bundle, "caption_review_store", None)
    if translation is not None and callable(review_store_factory):
        try:
            spanish_rows = review_store_factory().list(language="es")
        except Exception:
            # Activity is an optional status hint. A review-store outage must not
            # hide otherwise-available model configuration from the operator.
            _LOG.warning("Could not read saved Spanish caption activity for AI Models.")
        else:
            translation.translation_generated_cue_count = len(spanish_rows)
            translation.translation_last_generated_at = max(
                (row.created_at for row in spanish_rows), default=None
            )
    return availability


@staff_router.put(
    "/credentials/{provider}",
    response_model=ProviderKeyStatus,
    summary="Store a cloud-provider API key (write-only; never returned)",
    dependencies=[Depends(require_any_role(*_WRITE))],
    openapi_extra=_WRITE_ROLES_EXTRA,
    responses={
        400: {"description": _INVALID_KEY},
        404: {"description": _UNKNOWN_PROVIDER},
        503: {"description": f"{_DB_NOT_READY} {_KEYRING_UNAVAILABLE}"},
    },
)
def save_provider_key(
    provider: str,
    payload: ProviderKeyRequest,
    svc: AiModelService | None = Depends(get_ai_model_service),
) -> ProviderKeyStatus:
    """Store the cloud-provider API key in the OS keyring (DONE-10 / D13).

    Write-only: the key is persisted under the provider's opaque handle and is NEVER
    echoed — the response is only a stored/not-stored signal. ``setup_admin`` gated; an
    unknown provider is a 404, a blank/multiline key a 400. This is the operator path
    that makes a hosted tier actually work end-to-end once selected (with consent).
    """
    service = _require_service(svc)
    try:
        service.save_provider_credential(provider, payload.api_key)
    except UnknownProviderError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidProviderKeyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ProviderSecretStoreError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail=_KEYRING_UNAVAILABLE
        ) from exc
    return ProviderKeyStatus(provider=provider, stored=True)  # type: ignore[arg-type]


@staff_router.get(
    "/credentials/{provider}",
    response_model=ProviderKeyStatus,
    summary="Whether a cloud-provider API key is stored (boolean only, never the key)",
    dependencies=[Depends(require_any_role(*_READ))],
    openapi_extra=_READ_ROLES_EXTRA,
    responses={
        404: {"description": _UNKNOWN_PROVIDER},
        503: {"description": f"{_DB_NOT_READY} {_KEYRING_UNAVAILABLE}"},
    },
)
def get_provider_key_status(
    provider: str,
    svc: AiModelService | None = Depends(get_ai_model_service),
) -> ProviderKeyStatus:
    """Report ONLY whether a key is stored for ``provider`` (the UI's save/edit signal)."""
    service = _require_service(svc)
    try:
        stored = service.provider_credential_stored(provider)
    except UnknownProviderError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ProviderSecretStoreError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail=_KEYRING_UNAVAILABLE
        ) from exc
    return ProviderKeyStatus(provider=provider, stored=stored)  # type: ignore[arg-type]


@staff_router.delete(
    "/credentials/{provider}",
    response_model=ProviderKeyStatus,
    summary="Clear a stored cloud-provider API key",
    dependencies=[Depends(require_any_role(*_WRITE))],
    openapi_extra=_WRITE_ROLES_EXTRA,
    responses={
        404: {"description": _UNKNOWN_PROVIDER},
        503: {"description": f"{_DB_NOT_READY} {_KEYRING_UNAVAILABLE}"},
    },
)
def delete_provider_key(
    provider: str,
    svc: AiModelService | None = Depends(get_ai_model_service),
) -> ProviderKeyStatus:
    """Remove the stored key for ``provider`` (idempotent); reports stored=False after."""
    service = _require_service(svc)
    try:
        service.delete_provider_credential(provider)
    except UnknownProviderError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ProviderSecretStoreError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail=_KEYRING_UNAVAILABLE
        ) from exc
    return ProviderKeyStatus(provider=provider, stored=False)  # type: ignore[arg-type]


@staff_router.get(
    "/{feature}",
    response_model=FeatureModelRegistry,
    summary="Catalog tiers + the operator selection for one feature",
    dependencies=[Depends(require_any_role(*_READ))],
    openapi_extra=_READ_ROLES_EXTRA,
    responses={
        404: {"description": _UNKNOWN_FEATURE},
        503: {"description": _DB_NOT_READY},
    },
)
def get_feature_model_registry(
    feature: str,
    svc: AiModelService | None = Depends(get_ai_model_service),
) -> FeatureModelRegistry:
    """The catalog tiers + the operator's selection for one feature."""
    service = _require_service(svc)
    try:
        return service.get_registry(feature)
    except UnknownFeatureError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@staff_router.post(
    "/{feature}/select",
    response_model=FeatureModelRegistry,
    summary="Record the operator AI-model selection for one feature",
    dependencies=[Depends(require_any_role(*_WRITE))],
    openapi_extra=_WRITE_ROLES_EXTRA,
    responses={
        400: {"description": _UNKNOWN_MODEL},
        404: {"description": _UNKNOWN_FEATURE},
        503: {"description": _DB_NOT_READY},
    },
)
def select_feature_model(
    feature: str,
    payload: ModelSelectionRequest,
    request: Request,
    svc: AiModelService | None = Depends(get_ai_model_service),
) -> FeatureModelRegistry:
    """Record the operator's model selection for ``feature`` (catalog-validated).

    A cloud/frontier tier requires ``consent_accepted`` (the TOS checkbox); absent
    consent is a 400. The authenticated operator identity is recorded as the consent
    actor (falling back to a caller-supplied ``consent_actor`` for non-HTTP callers).
    """
    service = _require_service(svc)
    actor = _consent_actor(request, payload)
    try:
        return service.select_model(
            feature,
            payload.model_key,
            consent_accepted=payload.consent_accepted,
            consent_actor=actor,
        )
    except UnknownFeatureError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (UnknownModelError, ConsentRequiredError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@staff_router.put(
    "/{feature}/first-run-override",
    response_model=FeatureModelRegistry,
    summary="Set/clear the commissioning-wizard first-run model override for one feature",
    dependencies=[Depends(require_any_role(*_WRITE))],
    openapi_extra=_WRITE_ROLES_EXTRA,
    responses={
        400: {"description": _UNKNOWN_MODEL},
        404: {"description": _UNKNOWN_FEATURE},
        409: {"description": _NOT_SEEDED},
        503: {"description": _DB_NOT_READY},
    },
)
def set_feature_first_run_override(
    feature: str,
    payload: FirstRunOverrideRequest,
    svc: AiModelService | None = Depends(get_ai_model_service),
) -> FeatureModelRegistry:
    """Record (or clear) the first-run override the runtime honors before any DB ``/select``.

    The commissioning wizard (S3 first-run, §5.3) uses this so the operator's adaptive
    default override is the effective model until a durable selection is made. A hosted
    tier is refused (400) because a first-run override cannot carry TOS consent; a stale
    seed (commissioning not yet run) is a 409.
    """
    service = _require_service(svc)
    try:
        return service.set_first_run_override(feature, payload.model_key)
    except UnknownFeatureError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (UnknownModelError, ConsentRequiredError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except FirstRunNotSeededError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _consent_actor(request: Request, payload: ModelSelectionRequest) -> str | None:
    """The authenticated operator id (preferred) or the caller-supplied actor."""
    identity = getattr(request.state, "operator_identity", None)
    if isinstance(identity, OperatorIdentity):
        return identity.operator_id
    return payload.consent_actor


__all__ = [
    "get_ai_model_service",
    "staff_router",
]
