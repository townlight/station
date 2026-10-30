# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Closed data contracts for installer and first-run proof."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from civiccast.alerting.models import (
    RuntimeSafeToAirStatus,
    SystemResourceSample,
    SystemSelfTest,
)

DeploymentProfile = Literal["public-meetings", "streaming-only", "peg-cable"]
InstallerStepStatus = Literal["ready", "needs_input", "blocked", "complete"]
FirstAdminSetupStatus = Literal["contract_defined"]
FirstAdminIdentityMethod = Literal["local_admin_password"]
RecoveryKitMedium = Literal["printable", "downloadable_text_file", "offline_copy"]
SafeToBroadcastColor = Literal["green", "yellow", "red"]
SafeToBroadcastCheckKind = Literal["required", "optional", "advanced"]
StationSetupStatus = Literal["not_started", "complete"]
StationOperationMode = Literal["test", "on_air"]
StationDashboardReadyState = Literal["ready", "not_ready"]
SystemHealthCheckState = Literal["ready", "needs_attention", "needs_it_help", "not_set_up"]
RehearsalStatus = Literal["ready", "needs_attention", "blocked"]
# What the rehearsal run itself did, independent of the broadcast gate:
# "passed" = a private session ran, passed preflight and finalized a recording;
# "failed" = it started and stopped short; "not_run" = a precondition
# (storage, sample media, durable services) kept it from starting.
RehearsalResult = Literal["passed", "failed", "not_run"]
ResidentPreviewStatus = Literal["available", "not_configured"]
TesterReadinessState = Literal["ready", "needs_attention", "not_set_up", "needs_it_help"]
ProviderReadinessStatus = Literal["ready", "not_set_up", "needs_live_proof", "needs_it_help"]
ProviderProofStatus = Literal[
    "not_configured",
    "needs_live_proof",
    "proof_passed",
    "proof_failed_redaction",
    "skipped_optional",
]
SourceSetupKind = Literal["usb-hdmi", "phone-app", "encoder", "ndi", "sample-upload"]
SourceSetupLiveKind = Literal["usb-hdmi", "phone-app", "encoder", "ndi"]
BackupState = Literal["not_set_up", "ready", "needs_attention"]
RestoreProofState = Literal["not_tested", "passed", "needs_attention"]
RestoreProofItemState = Literal["pending", "passed", "needs_attention", "excluded"]
UpdateRollbackState = Literal["current", "update_available", "needs_attention"]
RollbackProofState = Literal["not_configured", "not_tested", "passed", "needs_attention"]
PostUpdateProofState = Literal["not_run", "passed", "needs_attention"]
MaintenanceWindowState = Literal["closed", "open", "expired"]
FailedUpdateRollbackProofState = Literal["not_run", "passed", "needs_attention"]
HealthState = Literal[
    "ok",
    "warning",
    "error",
    "credential_or_secret_required",
    "hardware_required",
    "failed",
]
InstallerLaneStatus = Literal[
    "ok",
    "ready",
    "planned",
    "running",
    "progress",
    "complete",
    "blocked",
    "cancelled",
    "skipped",
    "unavailable",
]
ProofState = Literal["hash_verified", "proof_unavailable", "attestation_verified"]
PackageVerificationStatus = Literal["ok", "blocked"]
BetaHandoffStatus = Literal[
    "passed",
    "blocked",
    "credential_or_secret_required",
    "hardware_required",
]
BetaHandoffLaneId = Literal[
    "package-acquisition",
    "clean-windows-install-proof",
    "dependencies",
    "models",
    "mtls",
    "activitypub",
    "external-providers",
]
PackageVerificationReason = Literal[
    "verified",
    "missing_artifact",
    "missing_sidecar",
    "invalid_sidecar",
    "hash_mismatch",
    "unsigned_artifact",
    # Legacy values kept for API/schema compatibility; no longer emitted. This
    # repo's release chain carries no cosign/Sigstore step (Azure Trusted
    # Signing/Authenticode is the only signing mechanism -- see
    # CODE_SIGNING_POLICY.md), so verification no longer looks for a
    # Sigstore attestation bundle at all. "unsigned_artifact" is the current
    # reason for a signed=true claim that has no real Authenticode evidence.
    "missing_attestation",
    "invalid_attestation",
    "attestation_mismatch",
    "unsigned_install_manifest",
]
AirGapReason = Literal[
    "verified",
    "network_enabled",
    "missing_proof_metadata",
    "missing_operator_guide",
    "missing_artifact",
    "hash_mismatch",
    "credential_or_secret_required",
]


class InstallerStep(BaseModel):
    """One operator-visible step in the profile-driven installer wizard."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    title: Annotated[str, Field(min_length=1, max_length=120)]
    status: InstallerStepStatus
    required: bool = True
    summary: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class FirstRunPlan(BaseModel):
    """Deterministic first-run plan for the selected deployment profile."""

    model_config = ConfigDict(extra="forbid")

    profile: DeploymentProfile
    recommended_tier: Annotated[str, Field(min_length=1)]
    time_to_first_broadcast_minutes: Annotated[int, Field(gt=0, le=480)]
    cloud_fallback_default: Literal["off"] = "off"
    steps: list[InstallerStep]

    @property
    def ready(self) -> bool:
        return all(step.status in {"ready", "complete"} for step in self.steps)


class HealthCheckItem(BaseModel):
    """One first-run health check result."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    state: HealthState
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class FirstRunHealthReport(BaseModel):
    """Installer health report proving the publish path is ready."""

    model_config = ConfigDict(extra="forbid")

    profile: DeploymentProfile
    ready: bool
    checks: list[HealthCheckItem]


class FirstAdminRequiredField(BaseModel):
    """One field the first-admin setup screen must collect."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    help_text: Annotated[str, Field(min_length=1)]
    secret: bool = False


class RecoveryKitContract(BaseModel):
    """Operator-safe recovery-kit contract without secret values."""

    model_config = ConfigDict(extra="forbid")

    generated_during: Literal["first-admin-setup"]
    media: list[RecoveryKitMedium]
    contains: list[Annotated[str, Field(min_length=1)]]
    excludes: list[Annotated[str, Field(min_length=1)]]
    operator_action: Annotated[str, Field(min_length=1)]
    rotation_path: Annotated[str, Field(min_length=1)]


class FirstAdminSetupContract(BaseModel):
    """v1.3 first-admin and recovery-kit product contract."""

    model_config = ConfigDict(extra="forbid")

    status: FirstAdminSetupStatus
    identity_method: FirstAdminIdentityMethod
    required_fields: list[FirstAdminRequiredField]
    recovery_kit: RecoveryKitContract
    supported_clients: list[Annotated[str, Field(min_length=1)]]
    non_goals: list[Annotated[str, Field(min_length=1)]]
    next_step: Annotated[str, Field(min_length=1)]


class SafeToBroadcastStateDefinition(BaseModel):
    """Plain-language definition of one safe-to-broadcast state."""

    model_config = ConfigDict(extra="forbid")

    color: SafeToBroadcastColor
    label: Annotated[str, Field(min_length=1, max_length=80)]
    meaning: Annotated[str, Field(min_length=1)]
    operator_copy: Annotated[str, Field(min_length=1)]


class SafeToBroadcastCheckContract(BaseModel):
    """One check category used by the safe-to-broadcast product contract."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    kind: SafeToBroadcastCheckKind
    failure_state: SafeToBroadcastColor
    operator_message: Annotated[str, Field(min_length=1)]
    admin_message: Annotated[str, Field(min_length=1)]


class SafeToBroadcastContract(BaseModel):
    """v1.3 product contract for the night-of-meeting readiness signal."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["contract_defined"]
    default_state: SafeToBroadcastColor
    states: list[SafeToBroadcastStateDefinition]
    required_checks: list[SafeToBroadcastCheckContract]
    optional_checks: list[SafeToBroadcastCheckContract]
    five_minutes_before_meeting: list[Annotated[str, Field(min_length=1)]]
    non_goals: list[Annotated[str, Field(min_length=1)]]
    next_step: Annotated[str, Field(min_length=1)]


class StationStorageLocations(BaseModel):
    """Local station storage paths created or proposed during first-run setup."""

    model_config = ConfigDict(extra="forbid")

    media_library: Annotated[str, Field(min_length=1, max_length=260)]
    recordings: Annotated[str, Field(min_length=1, max_length=260)]
    backups: Annotated[str, Field(min_length=1, max_length=260)]


class StationRuntimePaths(BaseModel):
    """Resolved paths used by the installed service, reported read-only."""

    model_config = ConfigDict(extra="forbid")

    installation_root: Annotated[str, Field(min_length=1, max_length=1024)]
    database_data: Annotated[str, Field(min_length=1, max_length=1024)]
    media_uploads: Annotated[str, Field(min_length=1, max_length=1024)]


class StationChannelProfile(BaseModel):
    """Generated local channel profile for first-run commissioning."""

    model_config = ConfigDict(extra="forbid")

    channel_id: Annotated[str, Field(min_length=1, max_length=80)]
    display_name: Annotated[str, Field(min_length=1, max_length=120)]
    purpose: Annotated[str, Field(min_length=1, max_length=160)]


#: Fresh-install default for ``StationProfile.live_captions_enabled`` and the
#: value ``resolve_live_captions_enabled`` (``station_state.py``) reports when
#: the key is absent. ``False`` for beta.5 (temporary, owner decision
#: 2026-09-09): the live caption embed leg held video for 25-30 s and then
#: burst-released it every 1-2 minutes on every channel in the sandbox soak.
#: One constant so the model default, the resolver's absent-key default and
#: the profile rehydration default cannot drift apart.
LIVE_CAPTIONS_DEFAULT = False


class StationProfile(BaseModel):
    """Operator-facing station identity created during first-admin setup."""

    model_config = ConfigDict(extra="forbid")

    station_name: Annotated[str, Field(min_length=1, max_length=120)]
    admin_display_name: Annotated[str, Field(min_length=1, max_length=120)]
    admin_username: Annotated[str, Field(min_length=1, max_length=80)]
    default_channel_id: Annotated[str, Field(min_length=1, max_length=80)] = "government"
    public_base_url: Annotated[str, Field(min_length=1, max_length=240)] | None = None
    station_timezone: Annotated[str, Field(min_length=1, max_length=120)] = "local"
    storage_locations: StationStorageLocations
    runtime_paths: StationRuntimePaths | None = None
    channel_count: Annotated[int, Field(ge=1, le=12)] = 3
    channel_profiles: list[StationChannelProfile] = Field(default_factory=list)
    sample_content_enabled: bool = True
    initial_schedule_enabled: bool = True
    live_captions_enabled: bool = Field(
        default=LIVE_CAPTIONS_DEFAULT,
        description=(
            "Operator switch for LIVE captions: the real-time ASR tap on the "
            "broadcast audio of every ON_AIR channel AND the CEA-708 caption "
            "embed leg on the video path. OFF by default in beta.5 "
            "(temporary): with the embed leg built, the 2026-09-09 sandbox "
            "soak measured a recurring 25-30 s video hold followed by a burst "
            "of frames every 1-2 minutes on every channel, and twice the 10 s "
            "stall watchdog restarted a channel. Turn it ON from the operator "
            "console (Setup > Station Profile > Show live captions on air) on "
            "a station that can keep up. Turning it off does NOT affect "
            "captions on published recordings (the offline caption job), "
            "which is the legal requirement; this is the live one."
        ),
    )
    default_roles: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        default_factory=lambda: [
            "setup_admin",
            "publish_operator",
            "support_admin",
            "viewer",
        ]
    )
    operation_mode: StationOperationMode = "test"
    dashboard_ready_state: StationDashboardReadyState = "not_ready"
    recovery_kit_id: Annotated[str, Field(min_length=1, max_length=80)]
    recovery_kit_generated_at: datetime


class FirstAdminSetupRequest(BaseModel):
    """First-run setup payload from the installer or operator console."""

    model_config = ConfigDict(extra="forbid")

    station_name: Annotated[str, Field(min_length=1, max_length=120)]
    admin_display_name: Annotated[str, Field(min_length=1, max_length=120)]
    admin_username: Annotated[str, Field(min_length=1, max_length=80)]
    admin_password: Annotated[str, Field(min_length=12, max_length=256)]
    recovery_kit_destination: Annotated[
        str,
        Field(
            min_length=1,
            max_length=240,
            description=(
                "Advisory free-text note describing where the operator will keep "
                "the printed/saved recovery kit (e.g. 'printed and stored in the "
                "clerk safe'). This is NOT a filesystem path: the server does not "
                "write a file here. The kit is delivered in the response and via "
                "the operator console's download action; the separate Backup "
                "destination control is the real filesystem-path field."
            ),
        ),
    ]
    default_channel_id: Annotated[str, Field(min_length=1, max_length=80)] = "government"
    public_base_url: Annotated[str, Field(min_length=1, max_length=240)] | None = None
    station_timezone: Annotated[str, Field(min_length=1, max_length=120)] = "local"
    storage_locations: StationStorageLocations | None = None
    channel_count: Annotated[int, Field(ge=1, le=12)] = 3
    sample_content_enabled: bool = True
    initial_schedule_enabled: bool = True
    operation_mode: StationOperationMode = "test"


class RecoveryKit(BaseModel):
    """One-time recovery kit returned during first-admin setup."""

    model_config = ConfigDict(extra="forbid")

    kit_id: Annotated[str, Field(min_length=1, max_length=80)]
    generated_at: datetime
    station_name: Annotated[str, Field(min_length=1, max_length=120)]
    admin_username: Annotated[str, Field(min_length=1, max_length=80)]
    recovery_codes: list[Annotated[str, Field(min_length=8, max_length=80)]]
    instructions: list[Annotated[str, Field(min_length=1)]]
    excludes: list[Annotated[str, Field(min_length=1)]]


class FirstAdminSetupResponse(BaseModel):
    """Completed first-admin setup response with one-time operator handoff."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["complete"]
    profile: StationProfile
    recovery_kit: RecoveryKit
    operator_console_url: Annotated[str, Field(min_length=1)]
    operator_console_token: Annotated[str, Field(min_length=24)]
    next_step: Annotated[str, Field(min_length=1)]


class StationLoginRequest(BaseModel):
    """Local first-admin password sign-in request."""

    model_config = ConfigDict(extra="forbid")

    admin_username: Annotated[str, Field(min_length=1, max_length=80)]
    admin_password: Annotated[str, Field(min_length=1, max_length=256)]


class StationRecoveryRequest(BaseModel):
    """One-code local first-admin recovery request."""

    model_config = ConfigDict(extra="forbid")

    admin_username: Annotated[str, Field(min_length=1, max_length=80)]
    recovery_code: Annotated[str, Field(min_length=8, max_length=80)]
    new_admin_password: Annotated[str, Field(min_length=12, max_length=256)]


class StationAuthResponse(BaseModel):
    """Operator console auth response with a hidden API bearer token."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["authenticated", "recovered"]
    profile: StationProfile
    operator_console_url: Annotated[str, Field(min_length=1)]
    operator_console_token: Annotated[str, Field(min_length=24)]
    next_step: Annotated[str, Field(min_length=1)]


class StationSetupState(BaseModel):
    """Current station setup state without secret values.

    Two views exist once setup is complete. A caller holding a valid staff
    bearer token gets the full state. A caller WITHOUT one -- the signed-out
    operator console loading its sign-in screen, or any other process on the
    station's loopback -- gets :meth:`signed_out_view`: ``setup_complete``,
    ``station_name`` (public branding) and ``next_step`` only. The profile
    (admin user name, display name, channel profiles, storage locations),
    the recovery-kit id and the recovery-kit acknowledgement are not
    disclosed to an unauthenticated caller (walkthrough on a running beta.5
    station, 2026-09-09).
    """

    model_config = ConfigDict(extra="forbid")

    status: StationSetupStatus
    setup_complete: bool
    #: Public station branding; the one profile field the signed-out console
    #: still needs ("<station> already has a first admin -- sign in").
    station_name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    profile: StationProfile | None = None
    recovery_kit_created: bool = False
    recovery_kit_id: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    #: ``None`` means "not disclosed": the caller is not signed in, so whether
    #: the one-time kit was ever confirmed is withheld along with the kit id.
    recovery_kit_acknowledged: bool | None = False
    operator_console_url: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]

    def signed_out_view(self) -> StationSetupState:
        """The state an unauthenticated caller may see after setup completes."""

        if not self.setup_complete:
            return self
        return StationSetupState(
            status=self.status,
            setup_complete=True,
            station_name=self.station_name,
            profile=None,
            recovery_kit_created=False,
            recovery_kit_id=None,
            recovery_kit_acknowledged=None,
            operator_console_url=self.operator_console_url,
            next_step="Sign in with the local admin password to continue.",
        )


class RevokeOtherSessionsResponse(BaseModel):
    """Result of revoking every operator-console session except the caller's."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["revoked"]
    revoked_count: Annotated[int, Field(ge=0)]
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class RecoveryKitRegenerateResponse(BaseModel):
    """One-time regenerated recovery kit for an already-authenticated admin."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["regenerated"]
    recovery_kit: RecoveryKit
    next_step: Annotated[str, Field(min_length=1)]


class RecoveryKitAcknowledgeRequest(BaseModel):
    """Operator confirmation that the one-time recovery kit is saved or printed."""

    model_config = ConfigDict(extra="forbid")

    confirmed: bool


SampleSeedStatusValue = Literal["not_applicable", "pending", "succeeded", "failed"]
SampleSeedStepValue = Literal["ingest", "package", "publish", "schedule"]


class SampleSeedStatus(BaseModel):
    """First-run sample content + starter schedule seeding state (audit A-1).

    ``sample_content_enabled`` / ``initial_schedule_enabled`` come from the
    setup toggles as recorded on the station profile (not from whether
    seeding happened to succeed), so the operator console can always show
    *why* nothing was seeded when the toggles were off. A failure is never
    swallowed (K3-1): ``status="failed"`` persists until the operator either
    retries successfully or dismisses the notice, and ``failed_step`` +
    ``error_message`` name exactly what broke.
    """

    model_config = ConfigDict(extra="forbid")

    status: SampleSeedStatusValue
    sample_content_enabled: bool
    initial_schedule_enabled: bool
    asset_id: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    schedule_item_id: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    failed_step: SampleSeedStepValue | None = None
    error_message: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    dismissed: bool = False
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class ResidentPreview(BaseModel):
    """Operator-safe resident preview target."""

    model_config = ConfigDict(extra="forbid")

    status: ResidentPreviewStatus
    public_url: Annotated[str, Field(min_length=1)]
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class SystemHealthCheck(BaseModel):
    """One operator-facing readiness check."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    kind: SafeToBroadcastCheckKind
    required: bool
    state: SystemHealthCheckState
    color: SafeToBroadcastColor
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class SystemHealthReport(BaseModel):
    """System Health report used by the operator console."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    safe_to_broadcast: SafeToBroadcastColor
    label: Annotated[str, Field(min_length=1, max_length=80)]
    operator_message: Annotated[str, Field(min_length=1)]
    setup: StationSetupState
    resident_preview: ResidentPreview
    checks: list[SystemHealthCheck]
    # S8-5 operational-alerting overlay. All default empty so existing callers and
    # the ephemeral/storage-off path stay valid; populated when alerting is active.
    runtime_safe_to_air: RuntimeSafeToAirStatus | None = None
    active_critical_alerts: Annotated[int, Field(ge=0)] = 0
    active_warning_alerts: Annotated[int, Field(ge=0)] = 0
    last_self_test: SystemSelfTest | None = None
    latest_resource_sample: SystemResourceSample | None = None


class BroadcastGateItem(BaseModel):
    """One required System Health check the broadcast gate is waiting on."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    color: SafeToBroadcastColor
    next_step: Annotated[str, Field(min_length=1)]


class BroadcastGate(BaseModel):
    """The required-items gate, reported separately from the rehearsal run.

    beta.5 walkthrough F-21: the headline used to say "rehearsal is blocked"
    while the detail lines said the rehearsal ran and passed. The gate is a
    property of the station's required checks, not of the run -- so it is its
    own object, naming the items that block (red) or need attention (yellow).
    """

    model_config = ConfigDict(extra="forbid")

    color: SafeToBroadcastColor
    blocking: list[BroadcastGateItem] = Field(default_factory=list)
    attention: list[BroadcastGateItem] = Field(default_factory=list)
    summary: Annotated[str, Field(min_length=1)]


class RehearsalReport(BaseModel):
    """Private first-broadcast rehearsal result."""

    model_config = ConfigDict(extra="forbid")

    rehearsal_id: Annotated[str, Field(min_length=1, max_length=80)]
    started_at: datetime
    status: RehearsalStatus
    safe_to_broadcast: SafeToBroadcastColor
    rehearsal_result: RehearsalResult
    gate: BroadcastGate
    message: Annotated[str, Field(min_length=1)]
    resident_preview: ResidentPreview
    checks: list[SystemHealthCheck]
    private_session_id: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    recording_asset_id: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    recording_uri: Annotated[str, Field(min_length=1)] | None = None
    resident_preview_proof: Annotated[str, Field(min_length=1)] | None = None
    evidence: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    next_step: Annotated[str, Field(min_length=1)]


class ProviderReadinessItem(BaseModel):
    """One provider setup/readiness card for operator-facing setup."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    required: bool
    status: ProviderReadinessStatus
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]
    advanced: bool = False
    what_you_need: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    setup_steps: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    setup_url: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    proof_requirement: Annotated[str, Field(min_length=1)] | None = None
    proof_status: ProviderProofStatus | None = None
    evidence_reference: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    proof_recorded_at: datetime | None = None
    redaction_reviewed: bool = False
    credential_fields: list[ProviderCredentialField] = Field(default_factory=list)
    credential_handle: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    manual_section: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    """Anchor id (matching an id in civiccast/docsite/manual.json's table of
    contents) for this card's "Read more in the manual" link -- e.g.
    "provider-cloudflare-r2" -> /help#provider-cloudflare-r2 in the operator
    console. None for a card that doesn't have a dedicated manual section."""


class ProviderReadinessReport(BaseModel):
    """Provider setup/readiness report without secret values."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    items: list[ProviderReadinessItem]
    next_step: Annotated[str, Field(min_length=1)]


class ProviderCredentialField(BaseModel):
    """One write-only provider credential/configuration field."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    help_text: Annotated[str, Field(min_length=1)]
    secret: bool = True
    required: bool = True


class ProviderCredentialSetupRequest(BaseModel):
    """Write-only provider credential setup request."""

    model_config = ConfigDict(extra="forbid")

    provider_id: Annotated[str, Field(min_length=1, max_length=80)]
    values: dict[Annotated[str, Field(min_length=1, max_length=80)], str]


class ProviderCredentialSetupResponse(BaseModel):
    """Redacted result of saving provider credentials."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["stored"]
    provider_id: Annotated[str, Field(min_length=1, max_length=80)]
    credential_handle: Annotated[str, Field(min_length=1, max_length=160)]
    configured_fields: list[Annotated[str, Field(min_length=1, max_length=80)]]
    redacted_fields: list[Annotated[str, Field(min_length=1, max_length=80)]]
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class ProviderProofRecordRequest(BaseModel):
    """Operator request to record a redacted live provider proof reference."""

    model_config = ConfigDict(extra="forbid")

    provider_id: Annotated[str, Field(min_length=1, max_length=80)]
    evidence_reference: Annotated[str, Field(min_length=1, max_length=500)]
    redaction_reviewed: Literal[True]
    reviewer_note: Annotated[str, Field(max_length=500)] | None = None


class ProviderProofRecordResponse(BaseModel):
    """Redacted result of recording live provider proof evidence."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["stored"]
    provider_id: Annotated[str, Field(min_length=1, max_length=80)]
    proof_status: ProviderProofStatus
    evidence_reference: Annotated[str, Field(min_length=1, max_length=500)]
    recorded_at: datetime
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class ProviderConnectionTestResponse(BaseModel):
    """Result of a live 'Test connection' against a provider's saved credentials.

    Carries no secret values: only a pass/fail status and an operator-facing
    message that never echoes the credentials or a raw provider error.
    """

    model_config = ConfigDict(extra="forbid")

    provider_id: Annotated[str, Field(min_length=1, max_length=80)]
    status: Literal["ok", "failed"]
    message: Annotated[str, Field(min_length=1)]


R2ConciergeErrorCode = Literal[
    "invalid_token",
    "r2_not_enabled",
    "no_account",
    "bucket_error",
    "domain_error",
]


class R2ConciergeRequest(BaseModel):
    """Request to provision Cloudflare R2 from one pasted API token.

    The token is used in-memory only to call the Cloudflare API and is never
    persisted or echoed back -- only the derived credentials are stored.
    """

    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, Field(min_length=1, max_length=4000)]
    bucket_name: Annotated[
        str, Field(min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
    ] = "civiccast-media"


class R2ConciergeResponse(BaseModel):
    """Redacted result of Cloudflare R2 concierge provisioning.

    Never carries the pasted token or the derived secret access key -- only
    what an operator needs to see: whether it worked, where media will live,
    and (on the R2-not-enabled path) the one-time dashboard link to fix it.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "failed"]
    message: Annotated[str, Field(min_length=1)]
    error_code: R2ConciergeErrorCode | None = None
    deep_link: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    bucket: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    public_base_url: Annotated[str, Field(min_length=1, max_length=500)] | None = None


class SourceSetupOption(BaseModel):
    """Plain-language source setup option for non-technical operators."""

    model_config = ConfigDict(extra="forbid")

    id: SourceSetupKind
    label: Annotated[str, Field(min_length=1, max_length=120)]
    best_for: Annotated[str, Field(min_length=1)]
    source_type: Annotated[str, Field(min_length=1, max_length=16)] | None = None
    operator_steps: list[Annotated[str, Field(min_length=1)]]
    needs_it_help: bool = False


class SourceSetupReport(BaseModel):
    """Source setup guidance and current readiness."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    status: TesterReadinessState
    configured_source_count: Annotated[int, Field(ge=0)]
    options: list[SourceSetupOption]
    next_step: Annotated[str, Field(min_length=1)]


class SourceSetupCreateRequest(BaseModel):
    """Operator-console request to create a live meeting source."""

    model_config = ConfigDict(extra="forbid")

    kind: SourceSetupLiveKind
    label: Annotated[str, Field(min_length=1, max_length=120)]
    endpoint: Annotated[str, Field(min_length=1, max_length=500)]
    channel_id: Annotated[str, Field(min_length=1, max_length=80)] = "government"


class SourceSetupMutationResponse(BaseModel):
    """Result of a source setup mutation."""

    model_config = ConfigDict(extra="forbid")

    status: TesterReadinessState
    live_source_id: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    source_type: Annotated[str, Field(min_length=1, max_length=16)] | None = None
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class SourceSetupSampleUploadResponse(BaseModel):
    """Result of creating the bundled sample rehearsal upload."""

    model_config = ConfigDict(extra="forbid")

    status: TesterReadinessState
    asset_id: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    file_path: Annotated[str, Field(min_length=1)] | None = None
    live_source_id: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    source_type: Annotated[str, Field(min_length=1, max_length=16)] | None = None
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class BackupSetupRequest(BaseModel):
    """Operator backup destination setup request."""

    model_config = ConfigDict(extra="forbid")

    destination: Annotated[str, Field(min_length=1, max_length=500)]


class RollbackArtifactRequest(BaseModel):
    """Operator rollback artifact setup request."""

    model_config = ConfigDict(extra="forbid")

    artifact_path: Annotated[str, Field(min_length=1, max_length=500)]


class UpdateMaintenanceWindowRequest(BaseModel):
    """Operator request to open a visible update maintenance window."""

    model_config = ConfigDict(extra="forbid")

    duration_minutes: Annotated[int, Field(ge=5, le=240)] = 60


class BackupStatus(BaseModel):
    """Backup setup and write-proof state."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    status: BackupState
    destination: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    last_probe_at: datetime | None = None
    last_backup_at: datetime | None = None
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class RestoreProofItem(BaseModel):
    """One required or excluded surface in the restore proof checklist."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    required: bool
    state: RestoreProofItemState
    message: Annotated[str, Field(min_length=1)]


class RestoreStatus(BaseModel):
    """Restore rehearsal status for the configured backup destination."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    status: RestoreProofState
    target_profile: Annotated[str, Field(min_length=1)] = "isolated-station-profile"
    last_restore_test_at: datetime | None = None
    proof_summary: Annotated[str, Field(min_length=1)] | None = None
    proof_items: list[RestoreProofItem] = Field(default_factory=list)
    excluded_items: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    plan_steps: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]
    # 0.5.0: the last REAL disaster-recovery drill result (civiccast.dr —
    # a real database backup/restore/crash-recovery drill, not the proof-token
    # round-trip above). None until an operator has run one via
    # `civiccast dr run-drill` or POST /api/staff/installer/dr/run-drill.
    real_drill_summary: Annotated[str, Field(min_length=1)] | None = None


class UpdateRollbackStatus(BaseModel):
    """Operator-facing update and rollback status."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    current_version: Annotated[str, Field(min_length=1)]
    available_version: Annotated[str, Field(min_length=1)] | None = None
    status: UpdateRollbackState
    migration_state: Annotated[str, Field(min_length=1)]
    rollback_available: bool
    rollback_artifact: Annotated[str, Field(min_length=1)] | None = None
    rollback_artifact_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    rollback_proof_state: RollbackProofState = "not_configured"
    last_rollback_test_at: datetime | None = None
    rollback_proof_summary: Annotated[str, Field(min_length=1)] | None = None
    post_update_proof_state: PostUpdateProofState = "not_run"
    last_post_update_proof_at: datetime | None = None
    post_update_proof_summary: Annotated[str, Field(min_length=1)] | None = None
    maintenance_window_state: MaintenanceWindowState = "closed"
    maintenance_window_expires_at: datetime | None = None
    maintenance_window_summary: Annotated[str, Field(min_length=1)] | None = None
    failed_update_rollback_state: FailedUpdateRollbackProofState = "not_run"
    last_failed_update_rollback_at: datetime | None = None
    failed_update_rollback_summary: Annotated[str, Field(min_length=1)] | None = None
    safe_to_apply: bool = False
    last_preflight_at: datetime | None = None
    checkpoint_summary: Annotated[str, Field(min_length=1)] | None = None
    plan_steps: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class DiagnosticBundleRequest(BaseModel):
    """Request for a redacted support bundle."""

    model_config = ConfigDict(extra="forbid")

    operator_note: Annotated[str, Field(max_length=1000)] | None = None


class DiagnosticBundleResponse(BaseModel):
    """Generated redacted support bundle metadata."""

    model_config = ConfigDict(extra="forbid")

    bundle_id: Annotated[str, Field(min_length=1, max_length=80)]
    generated_at: datetime
    path: Annotated[str, Field(min_length=1)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    redacted: Literal[True]
    contains: list[Annotated[str, Field(min_length=1)]]
    excludes: list[Annotated[str, Field(min_length=1)]]
    next_step: Annotated[str, Field(min_length=1)]


class AcceptancePacketResponse(BaseModel):
    """Generated station acceptance packet metadata (item #26).

    The packet is a redacted, hashed JSON snapshot of the same already-computed
    readiness sections the support bundle draws from (setup, backup/restore/
    update, provider readiness, source setup, system health) plus the station's
    as-run/proof-of-performance and EPG export configuration counts, framed for
    handing to a franchise authority or reviewer as evidence a station stood up
    a working CivicCast install. It asserts nothing that was not already
    computed by a real status builder elsewhere in the app."""

    model_config = ConfigDict(extra="forbid")

    packet_id: Annotated[str, Field(min_length=1, max_length=80)]
    generated_at: datetime
    path: Annotated[str, Field(min_length=1)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    redacted: Literal[True]
    safe_to_broadcast: SafeToBroadcastColor
    contains: list[Annotated[str, Field(min_length=1)]]
    next_step: Annotated[str, Field(min_length=1)]


class ModelBundleRequest(BaseModel):
    """Request for an air-gapped model bundle manifest."""

    model_config = ConfigDict(extra="forbid")

    profile: DeploymentProfile = "public-meetings"
    include_translation: bool = True
    include_summary: bool = True
    include_captions: bool = True


class ModelBundleItem(BaseModel):
    """One model artifact in the offline bundle."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    filename: Annotated[str, Field(min_length=1)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    required: bool = True


class ModelBundleManifest(BaseModel):
    """Hash-verified offline model bundle plan."""

    model_config = ConfigDict(extra="forbid")

    profile: DeploymentProfile
    bundle_name: Annotated[str, Field(min_length=1)]
    estimated_size_gb: Annotated[float, Field(gt=0)]
    items: list[ModelBundleItem]


class InstallerLane(BaseModel):
    """One readiness lane in the cross-platform installer summary."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=80)]
    label: Annotated[str, Field(min_length=1, max_length=120)]
    status: InstallerLaneStatus
    ready: bool
    next_step: Annotated[str, Field(min_length=1)]


class InstallerSummary(BaseModel):
    """Fail-closed summary for local tester setup readiness."""

    model_config = ConfigDict(extra="forbid")

    ready: bool
    #: ``"windows-native"`` is the native Windows station (the supervisor's own
    #: control plane) -- every Windows control plane running today's code is
    #: this. ``"windows-wsl2"`` named the retired WSL2 deployment; kept in the
    #: type only so a pre-native build's cached progress or on-disk state
    #: still type-checks on the frontend (``apps/installer/src/api.ts``'s
    #: ``withHonestNativePlatform``) -- this backend never produces it. See
    #: ``build_installer_summary``.
    platform: Literal["linux", "macos", "windows-native", "windows-wsl2"]
    operator_console_url: Annotated[str, Field(min_length=1)]
    lanes: list[InstallerLane]


class BetaHandoffArtifact(BaseModel):
    """One release-candidate artifact used by the beta tester handoff."""

    model_config = ConfigDict(extra="forbid")

    filename: Annotated[str, Field(min_length=1)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    required: bool = True


class BetaHandoffLane(BaseModel):
    """One fail-closed beta handoff readiness lane."""

    model_config = ConfigDict(extra="forbid")

    id: BetaHandoffLaneId
    label: Annotated[str, Field(min_length=1, max_length=120)]
    status: BetaHandoffStatus
    ready: bool
    message: Annotated[str, Field(min_length=1)]
    operator_action: Annotated[str, Field(min_length=1)]
    evidence_target: Annotated[str, Field(min_length=1)]


class BetaHandoffSummary(BaseModel):
    """Operator-facing beta handoff state without secret values or soft success."""

    model_config = ConfigDict(extra="forbid")

    ready: bool
    version: Annotated[str, Field(min_length=1)]
    acquisition_manifest: str | None = None
    install_command: Annotated[str, Field(min_length=1)] | None = None
    artifacts: list[BetaHandoffArtifact]
    lanes: list[BetaHandoffLane]


class ServiceMetadata(BaseModel):
    """Service manager metadata carried by installer artifacts and plans."""

    model_config = ConfigDict(extra="forbid")

    manager: Annotated[str, Field(min_length=1)]
    name: Annotated[str, Field(min_length=1)] = "civiccast"
    service_name: Annotated[str, Field(min_length=1)] = "civiccast"
    host_service: bool = False
    restart_policy: str | None = None
    recovery_window_seconds: int | None = Field(default=None, ge=1)


class BootstrapMetadata(BaseModel):
    """Package/bootstrap metadata for one platform family."""

    model_config = ConfigDict(extra="forbid")

    package_kind: Annotated[str, Field(min_length=1)]
    package_manager: str | None = None


class PackageVerificationResult(BaseModel):
    """Result of byte-hash and sidecar validation for one installer artifact."""

    model_config = ConfigDict(extra="forbid")

    status: PackageVerificationStatus
    ready: bool
    reason: PackageVerificationReason
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    service_metadata: ServiceMetadata | None = None
    additional_services: list[ServiceMetadata] = Field(default_factory=list)
    bootstrap_metadata: BootstrapMetadata | None = None
    attestation: str | None = None
    next_step: Annotated[str, Field(min_length=1)]


class ModelSetupItem(BaseModel):
    """One installer-facing model setup state."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1)]
    status: InstallerLaneStatus
    proof_state: ProofState
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    next_step: Annotated[str, Field(min_length=1)]


class ModelSetupResult(BaseModel):
    """Aggregate state for an online or offline model setup action."""

    model_config = ConfigDict(extra="forbid")

    status: InstallerLaneStatus
    ready: bool
    items: list[ModelSetupItem]
    next_step: Annotated[str, Field(min_length=1)]


class AirGapArtifactProof(BaseModel):
    """One artifact entry from an air-gapped proof manifest."""

    model_config = ConfigDict(extra="forbid")

    filename: Annotated[str, Field(min_length=1)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class AirGapProofMetadata(BaseModel):
    """Closed proof metadata emitted with an air-gapped bundle."""

    model_config = ConfigDict(extra="forbid")

    artifacts: list[AirGapArtifactProof]
    network_required: bool = False


class AirGapVerificationResult(BaseModel):
    """Verification result for an air-gapped installer bundle."""

    model_config = ConfigDict(extra="forbid")

    status: PackageVerificationStatus
    ready: bool
    reason: AirGapReason
    operator_guide: str | None = None
    proof_metadata: AirGapProofMetadata | None = None
    next_step: Annotated[str, Field(min_length=1)]
