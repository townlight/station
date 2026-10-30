# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S13 AiModelService — the seam between catalog, store, and the adaptive default.

Assembles a :class:`FeatureModelRegistry` per feature from three inputs:

1. the hard-coded catalog (decision A) — the available tiers + the local default,
2. the durable operator selection (:class:`AiModelStore`),
3. the adaptive summary default (RAM-driven).

It validates a selection against the catalog (an unknown / not-offered model key is
refused, which the router maps to 400), records the computed tier *band*
(``local`` / ``cloud`` / ``frontier``) alongside the selection, and resolves the
effective registry slug to the runtime *tag* the adapters load (§3.1.1).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from civiccast.ai_models.catalog import (
    CATALOG_FEATURES,
    build_feature_registry,
    catalog_tier,
    catalog_tier_for_feature,
)
from civiccast.ai_models.models import (
    AiFeature,
    AiModelAvailability,
    AiModelConfiguration,
    CloudProvider,
    FeatureModelAvailability,
    FeatureModelRegistry,
    ModelProvider,
    ModelTier,
    ModelTierBand,
)
from civiccast.ai_models.secrets import ProviderSecretStoreError, credential_ref_for_provider
from civiccast.ai_models.store import AiModelStore

# Injectable probes for the availability read (kept testable / offline).
LocalModelLister = Callable[[], "set[str] | None"]
SecretResolver = Callable[[str], "str | None"]
# The first-run seed reader/writer (station-state JSON). Injected so the service is
# testable offline and never hard-couples to the installer module at import time.
FirstRunOverrideReader = Callable[[str], "str | None"]
FirstRunOverrideWriter = Callable[[str, "str | None"], None]
# Injectable keyring write surface (cloud-provider API keys). Injected so the credential
# endpoints/CLI are testable without a real OS keyring; defaults bind the real keyring.
ProviderSecretSaver = Callable[[str, str], None]
ProviderSecretDeleter = Callable[[str], None]


class _Unset:
    """Sentinel so the local-model probe runs at most once per availability read."""


_UNSET = _Unset()


def _local_detail(*, reachable: bool, present: bool | None, tag: str) -> str:
    if not reachable:
        return "AI runtime unavailable — this feature will defer. Start Ollama and retry."
    if present is False:
        return f"Model {tag!r} is not installed — this feature will defer. Run 'ollama pull {tag}'."
    return f"Model {tag!r} is installed and the AI runtime is reachable."


# Provider -> the tier band stored with a selection. ``external`` (faster-whisper)
# runs on-box, so it is a LOCAL band like ``ollama``.
_PROVIDER_BAND: dict[ModelProvider, ModelTierBand] = {
    "ollama": "local",
    "external": "local",
    "ollama-cloud": "cloud",
    "openrouter": "frontier",
}


class AiModelServiceError(RuntimeError):
    """Base error for AI-model service failures."""


class UnknownFeatureError(AiModelServiceError):
    """Raised when a feature is not one of the catalog features."""


class UnknownModelError(AiModelServiceError):
    """Raised when a model key is not offered for the requested feature."""


class ConsentRequiredError(AiModelServiceError):
    """Raised when a cloud/frontier selection is made without recorded TOS consent.

    A billable, content-egressing tier requires the operator to accept the cloud TOS
    (decision A). The router maps this to a 400 so the UI cannot persist a hosted
    selection with no auditable acceptance.
    """


def _default_first_run_override(feature: str) -> str | None:
    """Read the commissioning-wizard first-run override for ``feature`` (S13 §5.3/§6.1).

    The wizard records an override into the station-state JSON seed before any DB
    selection exists; the runtime honors it as the effective key until the operator
    makes a durable ``/select``. Only ``summary`` carries an override slot today
    (the only adaptive default); other features return ``None``. A missing/absent
    seed (no commissioning yet) is ``None``.
    """
    if feature != "summary":
        return None
    from civiccast.installer.station_state import read_ai_model_seed

    seed = read_ai_model_seed()
    if seed is None:
        return None
    return seed.summary.operator_override_key


def _default_set_first_run_override(feature: str, model_key: str | None) -> None:
    """Write the commissioning-wizard first-run override into the station-state seed."""
    from civiccast.installer.station_state import set_ai_model_override

    set_ai_model_override(feature, model_key)


def _default_save_provider_secret(credential_ref: str, secret: str) -> None:
    from civiccast.ai_models.secrets import save_provider_secret

    save_provider_secret(credential_ref, secret)


def _default_delete_provider_secret(credential_ref: str) -> None:
    from civiccast.ai_models.secrets import delete_provider_secret

    delete_provider_secret(credential_ref)


def _default_load_provider_secret(credential_ref: str) -> str | None:
    from civiccast.ai_models.secrets import load_provider_secret

    return load_provider_secret(credential_ref)


# The cloud providers an operator can store a key for (the credential write surface).
_CLOUD_PROVIDERS: frozenset[ModelProvider] = frozenset({"ollama-cloud", "openrouter"})


class UnknownProviderError(AiModelServiceError):
    """Raised when a credential op names a provider with no cloud key handle.

    Only the hosted providers (``ollama-cloud`` / ``openrouter``) have a credential;
    the router maps this to a 404 so a typo / local provider cannot create a handle.
    """


class InvalidProviderKeyError(AiModelServiceError):
    """Raised when a submitted provider API key is empty or malformed (control chars)."""


class FirstRunNotSeededError(AiModelServiceError):
    """Raised when a first-run override is set before commissioning seeded the default.

    The station-state adaptive-default seed must exist (commissioning ran) before the
    wizard can record an override; the router maps this to a 409 so the UI prompts the
    operator to finish commissioning first.
    """


class AiModelService:
    """Validate + assemble per-feature model registries over the durable store."""

    def __init__(
        self,
        store: AiModelStore,
        *,
        system_ram_total_gb: int = 8,
        has_gpu: bool = False,
        read_first_run_override: FirstRunOverrideReader = _default_first_run_override,
        write_first_run_override: FirstRunOverrideWriter = _default_set_first_run_override,
        save_provider_secret: ProviderSecretSaver = _default_save_provider_secret,
        delete_provider_secret: ProviderSecretDeleter = _default_delete_provider_secret,
        load_provider_secret: SecretResolver = _default_load_provider_secret,
    ) -> None:
        self._store = store
        self._ram_gb = system_ram_total_gb
        # Gates the summary adaptive default (detect_summary_model_default):
        # a CPU-only box gets gemma4-e4b regardless of RAM -- see catalog.py
        # and models.py for the field evidence a RAM-only rule missed.
        self._has_gpu = has_gpu
        self._read_first_run_override = read_first_run_override
        self._write_first_run_override = write_first_run_override
        self._save_provider_secret = save_provider_secret
        self._delete_provider_secret = delete_provider_secret
        self._load_provider_secret = load_provider_secret

    def _require_feature(self, feature: str) -> AiFeature:
        if feature not in CATALOG_FEATURES:
            raise UnknownFeatureError(f"Unknown AI feature: {feature!r}.")
        return feature

    def _first_run_override(self, feature: AiFeature) -> str | None:
        """A VALID first-run override for ``feature`` (ignored if not a catalog tier).

        The override is honored only when no durable DB selection exists and only when
        it names a tier actually offered for the feature — a stale/invalid seed value
        can never force an unknown model onto the runtime.
        """
        key = self._read_first_run_override(feature)
        if key is None:
            return None
        return key if catalog_tier_for_feature(feature, key) is not None else None

    def get_registry(self, feature: str) -> FeatureModelRegistry:
        """The registry for ``feature`` — catalog tiers + the effective selection.

        Precedence (§6.1): a durable operator selection wins; absent that, the
        commissioning-wizard first-run override (station-state seed) is honored; absent
        both, the (adaptive) local default. Surfacing the override as
        ``operator_selected_key`` means ``effective_model_key`` and the console reflect
        the wizard choice before any DB ``/select``.
        """
        resolved = self._require_feature(feature)
        selection = self._store.get_selection(resolved)
        if selection is None:
            selection = self._first_run_override(resolved)
        return build_feature_registry(
            resolved,
            system_ram_total_gb=self._ram_gb,
            has_gpu=self._has_gpu,
            operator_selected_key=selection,
        )

    def select_model(
        self,
        feature: str,
        model_key: str,
        *,
        consent_accepted: bool = False,
        consent_actor: str | None = None,
    ) -> FeatureModelRegistry:
        """Record the operator's selection for ``feature`` after catalog validation.

        A cloud/frontier band selection requires ``consent_accepted`` (the TOS
        checkbox, decision A); without it this raises :class:`ConsentRequiredError`
        (router -> 400). The consent flag + actor are persisted on the selection row so
        the choice is auditable and the dispatch seam can construct the cloud adapter
        from durable state.
        """
        resolved = self._require_feature(feature)
        tier = catalog_tier_for_feature(resolved, model_key)
        if tier is None:
            raise UnknownModelError(f"Model {model_key!r} is not offered for feature {resolved!r}.")
        band = _PROVIDER_BAND[tier.provider]
        if band in ("cloud", "frontier") and not consent_accepted:
            raise ConsentRequiredError(
                f"Model {model_key!r} is a {band} tier that sends content to a third-party "
                "provider and bills per token; the operator must accept the cloud terms of "
                "service before it can be selected."
            )
        self._store.set_selection(
            resolved,
            model_key=model_key,
            tier=band,
            consent_accepted=consent_accepted,
            consent_actor=consent_actor,
        )
        return self.get_registry(resolved)

    def clear_selection(self, feature: str) -> FeatureModelRegistry:
        """Revert ``feature`` to its (adaptive) local default."""
        resolved = self._require_feature(feature)
        self._store.clear_selection(resolved)
        return self.get_registry(resolved)

    def set_first_run_override(self, feature: str, model_key: str | None) -> FeatureModelRegistry:
        """Record (or clear) the commissioning-wizard first-run override for ``feature``.

        The wizard (S3 first-run, §5.3) calls this to honor the operator's choice before
        any durable DB ``/select`` exists; it writes the station-state seed override that
        :meth:`get_registry` reads. ``model_key=None`` clears the override (back to the
        adaptive default). A non-None key is catalog-validated for the feature (unknown ->
        :class:`UnknownModelError`); a cloud/frontier override is refused here because it
        cannot carry the TOS consent a billable tier requires — the operator must use the
        consent-bearing durable ``/select`` for a hosted tier. Raises
        :class:`FirstRunNotSeededError` if commissioning has not seeded the default yet.
        """
        resolved = self._require_feature(feature)
        if model_key is not None:
            tier = catalog_tier_for_feature(resolved, model_key)
            if tier is None:
                raise UnknownModelError(
                    f"Model {model_key!r} is not offered for feature {resolved!r}."
                )
            if _PROVIDER_BAND[tier.provider] in ("cloud", "frontier"):
                raise ConsentRequiredError(
                    f"Model {model_key!r} is a hosted tier that bills per token and egresses "
                    "content; a first-run override cannot record the required TOS consent. "
                    "Use the consent-bearing model selection to enable a hosted tier."
                )
        try:
            self._write_first_run_override(resolved, model_key)
        except ValueError as exc:  # seed not present yet (commissioning not run)
            raise FirstRunNotSeededError(str(exc)) from exc
        return self.get_registry(resolved)

    # --- cloud provider credentials (DONE-10 / D13) ----------------------

    def _require_cloud_provider(self, provider: str) -> CloudProvider:
        if provider not in _CLOUD_PROVIDERS:
            raise UnknownProviderError(
                f"Provider {provider!r} has no cloud credential handle; "
                f"expected one of {sorted(_CLOUD_PROVIDERS)}."
            )
        return provider  # type: ignore[return-value]

    def save_provider_credential(self, provider: str, api_key: str) -> None:
        """Persist a cloud-provider API key to the keyring (write-only; never returned).

        Maps the hosted ``provider`` to its opaque ``credential_ref`` and writes the key
        under that handle. The key is validated as a non-empty, single-line secret; it is
        NEVER logged or echoed (the caller gets only a stored/not-stored signal). This is
        the operator-facing surface that makes a hosted tier actually usable end-to-end
        (DONE-10): after ``/select`` (with consent) the dispatch seam can resolve the key.
        """
        resolved = self._require_cloud_provider(provider)
        key = api_key.strip()
        if not key or any(ord(ch) < 32 for ch in key):
            raise InvalidProviderKeyError(
                "The provider API key must be a non-empty single-line secret."
            )
        ref = credential_ref_for_provider(resolved)
        self._save_provider_secret(ref, key)

    def provider_credential_stored(self, provider: str) -> bool:
        """Whether a key is stored for ``provider`` (boolean only — never the key)."""
        resolved = self._require_cloud_provider(provider)
        ref = credential_ref_for_provider(resolved)
        try:
            return bool(self._load_provider_secret(ref))
        except ProviderSecretStoreError:
            return False

    def delete_provider_credential(self, provider: str) -> None:
        """Remove the stored key for ``provider`` (idempotent: no-op if already absent)."""
        resolved = self._require_cloud_provider(provider)
        ref = credential_ref_for_provider(resolved)
        if not self._load_provider_secret(ref):
            return
        self._delete_provider_secret(ref)

    def get_configuration(self) -> AiModelConfiguration:
        """The station-wide configuration: every feature's assembled registry."""
        config = self._store.get_or_create_configuration()
        config.features = {feature: self.get_registry(feature) for feature in CATALOG_FEATURES}
        return config

    def effective_model_tag(self, feature: str) -> str:
        """Resolve ``feature``'s effective registry slug to its runtime tag (§3.1.1)."""
        registry = self.get_registry(feature)
        return catalog_tier(registry.effective_model_key).model_id

    def get_availability(
        self,
        *,
        list_local_models: LocalModelLister | None = None,
        resolve_secret: SecretResolver | None = None,
    ) -> AiModelAvailability:
        """Per-feature availability of the EFFECTIVE model (S13 §6.3 / Q2/U4).

        A lightweight read for the operator console: for each feature it reports the
        effective model, its band, and whether it is usable right now. Local tiers are
        probed against the (loopback) AI runtime via ``list_local_models`` (None means
        the runtime is unreachable, a set tests model presence). Cloud/frontier tiers
        report whether a provider credential is stored (the off-box endpoint is not
        pre-flight pinged). Probes are injectable so the read is testable offline.
        """
        from civiccast.ai_runtime.ollama_client import list_local_model_names

        list_models = list_local_models or list_local_model_names
        load_secret = resolve_secret or self._load_provider_secret
        local_models: set[str] | _Unset | None = _UNSET  # probed lazily, at most once

        features: dict[str, FeatureModelAvailability] = {}
        for feature in CATALOG_FEATURES:
            selection = self.effective_selection(feature)
            tier = selection.tier
            band = _PROVIDER_BAND[tier.provider]
            if tier.provider in ("ollama-cloud", "openrouter"):
                ref = credential_ref_for_provider(tier.provider)
                try:
                    has_credential = bool(load_secret(ref))
                except ProviderSecretStoreError:
                    has_credential = False
                features[feature] = FeatureModelAvailability(
                    feature=feature,
                    effective_model_key=tier.key,
                    band=band,
                    requires_network=tier.requires_network,
                    runtime_reachable=has_credential,
                    model_present=None,
                    detail=(
                        "Hosted tier ready (provider credential stored)."
                        if has_credential
                        else "Hosted tier selected but no provider credential is stored — "
                        "this feature will defer until a key is saved."
                    ),
                )
                continue
            if tier.provider == "external":
                # faster-whisper runs on-box but is not an Ollama model; presence is not
                # probeable here, so report band/network only (no false RED/GREEN).
                features[feature] = FeatureModelAvailability(
                    feature=feature,
                    effective_model_key=tier.key,
                    band=band,
                    requires_network=tier.requires_network,
                    runtime_reachable=None,
                    model_present=None,
                    detail="On-box faster-whisper runtime (presence not probed here).",
                )
                continue
            # Local Ollama tier: probe the runtime once and test the effective tag.
            if local_models is _UNSET:
                local_models = list_models()
            reachable = local_models is not None
            present = tier.model_id in local_models if isinstance(local_models, set) else None
            features[feature] = FeatureModelAvailability(
                feature=feature,
                effective_model_key=tier.key,
                band=band,
                requires_network=tier.requires_network,
                runtime_reachable=reachable,
                model_present=present,
                detail=_local_detail(reachable=reachable, present=present, tag=tier.model_id),
            )
        return AiModelAvailability(features=features)

    def effective_selection(self, feature: str) -> EffectiveSelection:
        """The effective tier for ``feature`` plus the persisted cloud-consent flag.

        The dispatch seam (:mod:`civiccast.ai_models.dispatch`) keys on the resolved
        :class:`ModelTier.provider` to decide local-vs-cloud, and needs the durable
        consent flag to construct a cloud adapter. When the operator has made no
        selection the effective tier is the (adaptive) local default and consent is
        ``False`` (a local default never egresses, so no consent is needed).
        """
        resolved = self._require_feature(feature)
        registry = self.get_registry(resolved)
        tier = catalog_tier(registry.effective_model_key)
        row = self._store.get_selection_row(resolved)
        consent = bool(row.consent_accepted) if row is not None else False
        return EffectiveSelection(feature=resolved, tier=tier, consent_accepted=consent)


@dataclass(frozen=True)
class EffectiveSelection:
    """The resolved effective tier for a feature + its persisted consent flag."""

    feature: AiFeature
    tier: ModelTier
    consent_accepted: bool


__all__ = [
    "AiModelService",
    "AiModelServiceError",
    "ConsentRequiredError",
    "EffectiveSelection",
    "FirstRunNotSeededError",
    "InvalidProviderKeyError",
    "UnknownFeatureError",
    "UnknownModelError",
    "UnknownProviderError",
]
