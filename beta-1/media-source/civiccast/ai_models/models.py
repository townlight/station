# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Model-selection registry entities (S13).

Pure data + selection logic: a per-feature registry of model tiers, the
operator's selection, and the adaptive local default. Storage, API, and UI land
in later slices. ``model_id`` / ``model_key`` start with pydantic's protected
``model_`` namespace, so the relevant configs disable that protection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field
from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String, false, text
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

AiFeature = Literal["captions", "summary", "translation"]
ModelProvider = Literal["ollama", "ollama-cloud", "openrouter", "external"]
# Computed tier band stored alongside a selection (local on-box, hosted Ollama
# Cloud, or an OpenRouter frontier route). NULL until the operator selects.
ModelTierBand = Literal["local", "cloud", "frontier"]


def _now() -> datetime:
    return datetime.now(UTC)


class ModelTier(BaseModel):
    """One selectable model with its cost / latency / privacy characteristics."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    key: Annotated[str, Field(min_length=1, max_length=120)]
    provider: ModelProvider
    model_id: Annotated[str, Field(min_length=1, max_length=120)]
    cost_per_token_usd: Annotated[float, Field(ge=0.0)] = 0.0
    latency_p95_ms: Annotated[int, Field(ge=0)] = 0
    private: bool = True
    requires_network: bool = False
    min_ram_gb: Annotated[int, Field(ge=1)] = 8
    license_url: Annotated[str | None, Field(max_length=200)] = None
    notes: Annotated[str, Field(max_length=400)] = ""


class FeatureModelRegistry(BaseModel):
    """The tiers available for one feature plus the operator's selection."""

    model_config = ConfigDict(extra="forbid")

    feature: AiFeature
    default_key: Annotated[str, Field(min_length=1, max_length=120)]
    adaptive_default: bool = False
    available_tiers: list[ModelTier] = Field(default_factory=list)
    operator_selected_key: Annotated[str | None, Field(max_length=120)] = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_model_key(self) -> str:
        """The operator's choice if set, else the (possibly adaptive) default.

        Exposed as a computed field so the wire payload carries the resolved key
        and the operator console does not re-implement the override-else-default
        rule (S13 §5.1).
        """
        return self.operator_selected_key or self.default_key


class AiModelConfiguration(BaseModel):
    """The station-wide configuration: every feature's registry."""

    model_config = ConfigDict(extra="forbid")

    created_at: datetime
    updated_at: datetime
    features: dict[str, FeatureModelRegistry] = Field(default_factory=dict)


class FeatureModelAvailability(BaseModel):
    """Whether one feature's EFFECTIVE model is usable right now (S13 §6.3 / Q2/U4).

    The UI renders this as a per-card availability hint so an operator can see that a
    feature will defer (not silently fail) before a meeting. For a local tier:
    ``model_present`` reflects whether the effective tag is installed and
    ``runtime_reachable`` whether the local AI runtime answered. For a cloud/frontier
    tier: ``runtime_reachable`` reflects whether a provider credential is stored
    (the off-box endpoint is not pre-flight pinged) and ``model_present`` is ``None``
    (a hosted model is not locally installable).
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    feature: AiFeature
    effective_model_key: Annotated[str, Field(min_length=1, max_length=120)]
    band: ModelTierBand
    requires_network: bool
    runtime_reachable: bool | None = None
    model_present: bool | None = None
    live_runtime_model_key: Annotated[str | None, Field(max_length=120)] = None
    live_runtime_model_loaded_at_startup: bool | None = None
    translation_generated_cue_count: int | None = None
    translation_last_generated_at: datetime | None = None
    detail: Annotated[str, Field(max_length=400)] = ""


class AiModelAvailability(BaseModel):
    """Per-feature availability for the whole station (the availability read payload)."""

    model_config = ConfigDict(extra="forbid")

    features: dict[str, FeatureModelAvailability] = Field(default_factory=dict)


class ModelSelectionRequest(BaseModel):
    """Operator request to select a model for a feature.

    ``consent_accepted`` records the operator's acceptance of the cloud TOS
    (decision A) for a billable, content-egressing choice. It is required by the
    service for cloud/frontier tiers and ignored (defaulting to ``False``) for the
    free, on-box local tiers. ``consent_actor`` is optional caller-supplied context;
    the router prefers the authenticated operator identity when present.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_key: Annotated[str, Field(min_length=1, max_length=120)]
    consent_accepted: bool = False
    consent_actor: Annotated[str | None, Field(max_length=120)] = None


CloudProvider = Literal["ollama-cloud", "openrouter"]


class ProviderKeyRequest(BaseModel):
    """Operator request to store a cloud-provider API key (write-only).

    The key is persisted to the OS keyring under the provider's opaque credential
    handle and is NEVER echoed back. A blank/oversized key is rejected by the field
    constraints; the value is validated as a single-line secret by the service.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    api_key: Annotated[str, Field(min_length=1, max_length=4000)]


class ProviderKeyStatus(BaseModel):
    """Whether a cloud provider's API key is stored — the boolean the UI consumes.

    Deliberately carries NO key material (not even a redaction/prefix): the response
    is a pure stored/not-stored signal so the AI Models card can render "key saved"
    vs "save a key" without any secret ever leaving the keyring.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    provider: CloudProvider
    stored: bool


class FirstRunOverrideRequest(BaseModel):
    """Commissioning-wizard request to set/clear the first-run model override (S13 §5.3).

    ``model_key=None`` clears the override (back to the adaptive default). A hosted tier
    cannot be set here (it needs the TOS consent the durable selection captures); the
    service refuses one with a 400.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_key: Annotated[str | None, Field(max_length=120)] = None


def detect_summary_model_default(system_ram_total_gb: int, *, has_gpu: bool = False) -> str:
    """Adaptive summary default: gated on GPU presence, not RAM size alone.

    Field evidence (candidate #17, AMD Ryzen 7 8745HS / 32 GB RAM / CPU-only
    inference, Windows 11 -- the reference target hardware class, spec §7.7
    ``tier-0``): the RAM-only rule below picked ``gemma4-12b-ollama`` on this
    32GB box because it satisfied ``>=16GB``, but 12B is not viable CPU-only
    there. Measured on that same hardware class (a second CPU-only 32GB
    32-logical-core station, gpu=None): ``gemma4:12b`` took 366s to complete
    one summary generation cold and then failed outright twice more under
    realistic memory pressure (CPU buffer allocation failure; a crashed
    ``llama-server`` subprocess) -- not just slow, unreliable. ``gemma4:e4b``
    completed every attempt (cold 128s, warm 94s) on the same box. RAM
    headroom does not predict CPU token-generation throughput; a discrete
    GPU does, because CPU-only inference throughput is bound by memory
    bandwidth and core count, not capacity. So a real GPU (any VRAM, i.e.
    tier-1 and above per ``platform.hardware._tier_for``) is now required
    before 12B is offered as the default; a CPU-only box (``has_gpu=False``,
    ``platform.hardware`` recommends ``tier-0`` for it) gets e4b regardless
    of how much system RAM it reports, because 32GB CPU-only boxes are a
    real, common, and explicitly supported target configuration -- not an
    edge case to optimize away from.
    """
    if has_gpu and system_ram_total_gb >= 16:
        return "gemma4-12b-ollama"
    return "gemma4-e4b-ollama"


# ---------------------------------------------------------------------------
# SQLAlchemy ORM twins (schema applied by migration 0053, not here)
# ---------------------------------------------------------------------------


class AiModelConfigurationDb(Base):
    """The station-wide AI-model config singleton (created_at/updated_at only)."""

    __tablename__ = "ai_model_configuration"

    config_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class FeatureModelRegistryDb(Base):
    """A per-feature operator selection (soft-delete aware; one live row per feature).

    A surrogate ``registry_id`` PK lets a feature's selection be cleared (soft-deleted)
    and re-created — history rows recur with the same ``feature`` and a non-null
    ``deleted_at``. The partial-unique index enforces at most one LIVE row per feature.
    """

    __tablename__ = "feature_model_registry"
    __table_args__ = (
        CheckConstraint(
            "feature IN ('captions', 'summary', 'translation')",
            name="feature_model_registry_feature_check",
        ),
        CheckConstraint(
            "tier IS NULL OR tier IN ('local', 'cloud', 'frontier')",
            name="feature_model_registry_tier_check",
        ),
        Index(
            "feature_model_registry_feature_unique",
            "feature",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    registry_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    feature: Mapped[str] = mapped_column(String(20), nullable=False)
    model_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    tier: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Cloud-TOS consent audit (S13 E4/Q3): recorded for a billable, content-egressing
    # cloud/frontier selection so who/when accepted is durable. NULL/False for local.
    consent_accepted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    consent_actor: Mapped[str | None] = mapped_column(String(120), nullable=True)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
