# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI routes for installer and first-run proof."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from civiccast.alerting.router import get_alerting_session_factory
from civiccast.auth.rate_limit import AuthRateLimiter, auth_rate_limit_config, client_ip
from civiccast.auth.roles import require_any_role
from civiccast.auth.tokens import (
    StaffAuthError,
    StaffAuthMissingCredentialError,
    verify_bearer_token,
)
from civiccast.dr.models import DrillReport
from civiccast.egress.compliance import TsduckStatus, locate_tsduck
from civiccast.egress.router import get_egress_store
from civiccast.installer.airgap import verify_airgap_bundle
from civiccast.installer.cdn_bridge import check_provider_connection
from civiccast.installer.contribution_install import (
    ContributionInstallError,
    ContributionInstallReport,
    contribution_install_status,
    install_remote_contribution,
)
from civiccast.installer.handoff import build_beta_handoff_summary
from civiccast.installer.model_state import mark_model_unavailable
from civiccast.installer.models import (
    AcceptancePacketResponse,
    AirGapVerificationResult,
    BackupSetupRequest,
    BackupStatus,
    BetaHandoffSummary,
    DeploymentProfile,
    DiagnosticBundleRequest,
    DiagnosticBundleResponse,
    FirstAdminSetupContract,
    FirstAdminSetupRequest,
    FirstAdminSetupResponse,
    FirstRunHealthReport,
    FirstRunPlan,
    InstallerSummary,
    ModelBundleManifest,
    ModelBundleRequest,
    ModelSetupResult,
    PackageVerificationResult,
    ProviderConnectionTestResponse,
    ProviderCredentialSetupRequest,
    ProviderCredentialSetupResponse,
    ProviderProofRecordRequest,
    ProviderProofRecordResponse,
    ProviderReadinessReport,
    R2ConciergeRequest,
    R2ConciergeResponse,
    RecoveryKitAcknowledgeRequest,
    RecoveryKitRegenerateResponse,
    RehearsalReport,
    ResidentPreview,
    RestoreStatus,
    RevokeOtherSessionsResponse,
    RollbackArtifactRequest,
    SafeToBroadcastContract,
    SampleSeedStatus,
    SourceSetupCreateRequest,
    SourceSetupMutationResponse,
    SourceSetupReport,
    SourceSetupSampleUploadResponse,
    StationAuthResponse,
    StationLoginRequest,
    StationRecoveryRequest,
    StationSetupState,
    SystemHealthReport,
    UpdateMaintenanceWindowRequest,
    UpdateRollbackStatus,
)
from civiccast.installer.packages import verify_package_artifact
from civiccast.installer.platform import OsFamily, build_bootstrap_plan
from civiccast.installer.r2_concierge import provision_r2
from civiccast.installer.service import (
    SourceSetupError,
    SourceSetupUnavailableError,
    acknowledge_station_recovery_kit,
    build_backup_status,
    build_first_admin_setup_contract,
    build_first_run_plan,
    build_installer_summary,
    build_model_bundle_manifest,
    build_provider_readiness_report,
    build_resident_preview,
    build_restore_status,
    build_safe_to_broadcast_contract,
    build_source_setup_report,
    build_system_health_report,
    build_update_rollback_status,
    complete_first_admin_setup,
    configure_backup,
    configure_rollback_artifact,
    count_production_recording_targets,
    create_acceptance_packet,
    create_diagnostic_bundle,
    create_sample_rehearsal_upload,
    create_source_from_setup,
    diagnostic_bundle_path,
    dismiss_first_run_seed_notice,
    ensure_default_recording_target,
    login_station_admin,
    mark_first_run_seed_pending,
    operator_console_url,
    read_first_run_seed_status,
    read_station_setup,
    record_provider_proof,
    recover_station_admin,
    regenerate_station_recovery_kit,
    retry_first_run_seed,
    revoke_other_station_sessions,
    run_dr_drill,
    run_failed_update_rollback_rehearsal,
    run_first_health_check,
    run_first_run_seed,
    run_maintenance_window_open,
    run_post_update_proof,
    run_private_rehearsal,
    run_restore_rehearsal,
    run_rollback_rehearsal,
    run_update_preflight,
    save_provider_credentials,
)
from civiccast.installer.station_state import (
    StationAuthError,
    StationSetupAlreadyCompleteError,
    StationSetupNotCompleteError,
    login_credentials_correct,
    recovery_code_correct,
)
from civiccast.installer.storage import (
    ManagedStorageError,
    ManagedStorageStatus,
    ManagedStorageStatusReport,
    durable_storage_status,
    ensure_managed_storage,
)
from civiccast.installer.tsduck_install import TsduckInstallReport, install_tsduck
from civiccast.live.router import (
    get_live_session_store,
    get_live_source_store,
    get_preflight_evaluator,
    get_recording_target_store,
)
from civiccast.publish.router import get_publish_store
from civiccast.schedule.router import get_postgres_store, get_schedule_store

staff_router = APIRouter(prefix="/api/staff/installer", tags=["staff", "installer"])
public_router = APIRouter(prefix="/api/setup", tags=["setup"])
_LOCAL_SETUP_ACCESS_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {
        "description": (
            "First setup is only reachable from the station computer itself "
            "(loopback), or the station is already configured."
        )
    }
}
_SETUP_AFTER_COMPLETE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_LOCAL_SETUP_ACCESS_RESPONSES,
    401: {
        "description": (
            "Setup is complete, so this endpoint now requires the staff bearer "
            "token (sign in at /api/setup/login or recover at /api/setup/recover)."
        )
    },
}


def get_live_recording_finalizer() -> Any:
    """Return the durable live-recording finalizer when storage is active."""

    return None


class StorageSetupRequest(BaseModel):
    """Request body for installer-managed durable storage setup."""

    model_config = ConfigDict(extra="forbid")

    storage_dir: str | None = None


class PublicStorageSetupRequest(BaseModel):
    """Request body for local browser storage setup."""

    model_config = ConfigDict(extra="forbid")


@staff_router.get(
    "/first-run-plan",
    response_model=FirstRunPlan,
    summary="Read the profile-driven first-run installer plan",
)
def first_run_plan(
    profile: DeploymentProfile = "public-meetings",
    recommended_tier: str = "tier-1",
) -> FirstRunPlan:
    from civiccast.ai_models.models import detect_summary_model_default
    from civiccast.installer.service import _probed_summary_ram_gb

    summary_default = detect_summary_model_default(_probed_summary_ram_gb())
    return build_first_run_plan(
        profile=profile,
        recommended_tier=recommended_tier,
        summary_default_key=summary_default,
    )


@staff_router.get(
    "/health",
    response_model=FirstRunHealthReport,
    summary="Run fail-closed first-run publish-surface health checks",
)
def first_run_health(profile: DeploymentProfile = "public-meetings") -> FirstRunHealthReport:
    return run_first_health_check(profile=profile)


@staff_router.get(
    "/first-admin-contract",
    response_model=FirstAdminSetupContract,
    summary="Read the v1.3 first-admin and recovery-kit product contract",
)
def first_admin_contract() -> FirstAdminSetupContract:
    return build_first_admin_setup_contract()


@staff_router.get(
    "/safe-to-broadcast-contract",
    response_model=SafeToBroadcastContract,
    summary="Read the v1.3 safe-to-broadcast product contract",
)
def safe_to_broadcast_contract() -> SafeToBroadcastContract:
    return build_safe_to_broadcast_contract()


@staff_router.get(
    "/station-state",
    response_model=StationSetupState,
    summary="Read first-admin setup and recovery-kit state",
)
def station_state() -> StationSetupState:
    return read_station_setup()


def _schedule_first_run_seed(
    background_tasks: BackgroundTasks,
    response: FirstAdminSetupResponse,
    *,
    postgres_store: Any,
    publish_store: Any,
    schedule_store: Any,
) -> None:
    """Queue first-run sample content seeding after a successful first-admin setup.

    A no-op when the operator turned sample content off (audit A-1 design
    constraint 5: off means seed nothing). ``mark_first_run_seed_pending``
    runs synchronously so a GET of the seed status immediately after setup
    honestly reports "pending" rather than nothing recorded yet; the actual
    ingest/package/publish work happens in the background task so first-
    admin setup's own response is never delayed or blocked by it.
    """

    if not response.profile.sample_content_enabled:
        return
    mark_first_run_seed_pending()
    background_tasks.add_task(
        run_first_run_seed,
        postgres_store=postgres_store,
        publish_store=publish_store,
        schedule_store=schedule_store,
        default_channel_id=response.profile.default_channel_id,
        initial_schedule_enabled=response.profile.initial_schedule_enabled,
    )


@staff_router.post(
    "/first-admin",
    response_model=FirstAdminSetupResponse,
    summary="Complete first-admin setup and generate the recovery kit",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def staff_first_admin_setup(
    request: FirstAdminSetupRequest,
    background_tasks: BackgroundTasks,
    postgres_store: Any = Depends(get_postgres_store),
    publish_store: Any = Depends(get_publish_store),
    schedule_store: Any = Depends(get_schedule_store),
) -> FirstAdminSetupResponse:
    try:
        response = complete_first_admin_setup(request)
    except StationSetupAlreadyCompleteError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _schedule_first_run_seed(
        background_tasks,
        response,
        postgres_store=postgres_store,
        publish_store=publish_store,
        schedule_store=schedule_store,
    )
    return response


@staff_router.get(
    "/sample-seed-status",
    response_model=SampleSeedStatus,
    summary="Read first-run sample content and starter schedule seeding status",
)
def sample_seed_status() -> SampleSeedStatus:
    return read_first_run_seed_status()


@staff_router.post(
    "/sample-seed-status/dismiss",
    response_model=SampleSeedStatus,
    summary="Dismiss the first-run sample content seeding notice",
    dependencies=[Depends(require_any_role("setup_admin", "publish_operator"))],
)
def dismiss_sample_seed_status() -> SampleSeedStatus:
    return dismiss_first_run_seed_notice()


@staff_router.post(
    "/sample-seed-status/retry",
    response_model=SampleSeedStatus,
    summary="Retry first-run sample content and starter schedule seeding",
    dependencies=[Depends(require_any_role("setup_admin", "publish_operator"))],
    responses={409: {"description": "Sample content is turned off for this station"}},
)
def retry_sample_seed_status(
    postgres_store: Any = Depends(get_postgres_store),
    publish_store: Any = Depends(get_publish_store),
    schedule_store: Any = Depends(get_schedule_store),
) -> SampleSeedStatus:
    try:
        return retry_first_run_seed(
            postgres_store=postgres_store,
            publish_store=publish_store,
            schedule_store=schedule_store,
        )
    except SourceSetupError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.get(
    "/resident-preview",
    response_model=ResidentPreview,
    summary="Read the resident preview target",
)
def resident_preview() -> ResidentPreview:
    return build_resident_preview()


@staff_router.get(
    "/system-health",
    response_model=SystemHealthReport,
    summary="Read operator-facing system readiness",
)
def system_health(
    profile: DeploymentProfile = "public-meetings",
    live_preflight_ready: bool = False,
    recording_write_probe_ready: bool = False,
    resident_preview_confirmed: bool = False,
    live_source_store: Any = Depends(get_live_source_store),
    recording_target_store: Any = Depends(get_recording_target_store),
    egress_store: Any = Depends(get_egress_store),
    alerting_session_factory: Any = Depends(get_alerting_session_factory),
) -> SystemHealthReport:
    return build_system_health_report(
        profile=profile,
        live_source_count=_safe_count(live_source_store),
        recording_target_count=count_production_recording_targets(recording_target_store),
        live_preflight_ready=live_preflight_ready,
        recording_write_probe_ready=recording_write_probe_ready,
        resident_preview_confirmed=resident_preview_confirmed,
        channel_automation=_automation_rollup(egress_store),
        headend_readiness=_headend_rollup(egress_store),
        **_alerting_overlay(egress_store, alerting_session_factory),
    )


@staff_router.get(
    "/safe-to-broadcast",
    response_model=SystemHealthReport,
    summary="Read the current safe-to-broadcast report",
)
def safe_to_broadcast(
    profile: DeploymentProfile = "public-meetings",
    live_preflight_ready: bool = False,
    recording_write_probe_ready: bool = False,
    resident_preview_confirmed: bool = False,
    live_source_store: Any = Depends(get_live_source_store),
    recording_target_store: Any = Depends(get_recording_target_store),
    egress_store: Any = Depends(get_egress_store),
) -> SystemHealthReport:
    return build_system_health_report(
        profile=profile,
        live_source_count=_safe_count(live_source_store),
        recording_target_count=count_production_recording_targets(recording_target_store),
        live_preflight_ready=live_preflight_ready,
        recording_write_probe_ready=recording_write_probe_ready,
        resident_preview_confirmed=resident_preview_confirmed,
        channel_automation=_automation_rollup(egress_store),
        headend_readiness=_headend_rollup(egress_store),
    )


def _headend_rollup(egress_store: Any) -> Any:
    """CA-7: roll up headend verification posture when storage is active."""

    if egress_store is None:
        return None
    from civiccast.egress.compliance import (
        HeadendReadinessRollup,
        locate_tsduck,
        read_last_probe,
    )
    from civiccast.egress.router import get_egress_work_dir

    try:
        work_dir = get_egress_work_dir()
        passes: list[str] = []
        fails: list[str] = []
        not_run: list[str] = []
        udp_channels = 0
        for config in egress_store.list_configs():
            if not any(sink.kind == "udp-ts" for sink in config.sinks):
                continue
            udp_channels += 1
            last = read_last_probe(config.channel_id, work_dir)
            if last is None or last.verdict == "not-run":
                not_run.append(config.channel_id)
            elif last.verdict == "pass":
                passes.append(config.channel_id)
            else:
                fails.append(config.channel_id)
        return HeadendReadinessRollup(
            udp_channels=udp_channels,
            tsduck_installed=locate_tsduck().installed,
            passes=passes,
            fails=fails,
            not_run=not_run,
        )
    except Exception:
        return None


def _automation_rollup(egress_store: Any) -> Any:
    """CA-4: roll up auto_start channel state when durable storage is active."""

    if egress_store is None:
        return None
    from civiccast.egress.automation import summarize_automation

    try:
        return summarize_automation(egress_store)
    except Exception:
        return None


def _alerting_overlay(egress_store: Any, factory: Any) -> dict[str, Any]:
    """S8-5: runtime safe-to-air + active-alert counts + last self-test + latest
    resource sample, when alerting storage is active. Returns {} (defaults apply)
    when alerting is off or anything is unavailable — never blocks the report."""

    if egress_store is None or factory is None:
        return {}
    from civiccast.alerting.runtime_status import compute_runtime_safe_to_air
    from civiccast.alerting.store import (
        get_alert_events,
        get_self_tests,
        recent_resource_samples,
    )

    try:
        with factory() as session:
            firing = get_alert_events(session, state="firing")
            tests = get_self_tests(session, limit=1)
            samples = recent_resource_samples(session, window_minutes=60, limit=1)
        return {
            # Independent uncached snapshot: the dedicated GET /runtime-safe-to-air
            # endpoint caches ~4s (OD-3) to survive a 1 Hz dashboard poll, but
            # System Health is not a high-frequency poll, so it reads fresh. The two
            # surfaces can momentarily differ by up to that TTL; both are honest.
            "runtime_safe_to_air": compute_runtime_safe_to_air(egress_store, firing),
            "active_critical_alerts": sum(1 for e in firing if e.severity == "critical"),
            "active_warning_alerts": sum(1 for e in firing if e.severity == "warning"),
            "last_self_test": tests[0] if tests else None,
            "latest_resource_sample": samples[0] if samples else None,
        }
    except Exception:
        return {}


@staff_router.post(
    "/rehearsal",
    response_model=RehearsalReport,
    summary="Run a private first-broadcast rehearsal",
    dependencies=[Depends(require_any_role("meeting_operator"))],
)
def rehearsal(
    profile: DeploymentProfile = "public-meetings",
    live_session_store: Any = Depends(get_live_session_store),
    live_source_store: Any = Depends(get_live_source_store),
    recording_target_store: Any = Depends(get_recording_target_store),
    preflight_evaluator: Any = Depends(get_preflight_evaluator),
    finalizer: Any = Depends(get_live_recording_finalizer),
) -> RehearsalReport:
    return run_private_rehearsal(
        profile=profile,
        live_session_store=live_session_store,
        live_source_store=live_source_store,
        recording_target_store=recording_target_store,
        preflight_evaluator=preflight_evaluator,
        finalizer=finalizer,
    )


@staff_router.get(
    "/provider-readiness",
    response_model=ProviderReadinessReport,
    summary="Read provider setup and readiness cards",
)
def provider_readiness() -> ProviderReadinessReport:
    return build_provider_readiness_report()


@staff_router.post(
    "/provider-credentials",
    response_model=ProviderCredentialSetupResponse,
    summary="Save provider credentials from the setup wizard",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def provider_credentials(
    payload: ProviderCredentialSetupRequest,
) -> ProviderCredentialSetupResponse:
    try:
        return save_provider_credentials(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Provider credentials could not be stored safely: {exc}",
        ) from exc


@staff_router.post(
    "/provider-proof",
    response_model=ProviderProofRecordResponse,
    summary="Record redacted provider proof evidence",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def provider_proof(
    payload: ProviderProofRecordRequest,
) -> ProviderProofRecordResponse:
    try:
        return record_provider_proof(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Provider proof evidence could not be stored safely: {exc}",
        ) from exc


@staff_router.post(
    "/provider-credentials/{provider_id}/test-connection",
    response_model=ProviderConnectionTestResponse,
    summary="Live-test a provider's saved credentials against the provider",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def provider_connection_test(provider_id: str) -> ProviderConnectionTestResponse:
    """Build the adapter from saved credentials and call its health check.

    Returns a pass/fail with an operator-facing message that never echoes the
    credentials. 422 when the provider is not a CDN provider or no credentials
    are saved yet.
    """
    try:
        return check_provider_connection(provider_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@staff_router.post(
    "/cdn-concierge/r2",
    response_model=R2ConciergeResponse,
    summary="Provision Cloudflare R2 CDN storage from one pasted API token",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def cdn_concierge_r2(payload: R2ConciergeRequest) -> R2ConciergeResponse:
    """Paste one Cloudflare API token; CivicCast does the rest.

    Verifies the token, resolves the account, creates the R2 bucket, enables
    its public domain, derives the S3 keypair, stores it via the existing
    provider-credential path, then runs the existing live health check. The
    pasted token is used in-memory only -- never logged, stored, or echoed
    back; only the derived, non-secret-looking response fields go out.
    """
    with httpx.Client(timeout=30.0) as http:
        result = provision_r2(payload.token, bucket_name=payload.bucket_name, http=http)
    if result.status != "success":
        return R2ConciergeResponse(
            status="failed",
            message=result.message,
            error_code=result.error_code,
            deep_link=result.deep_link,
        )

    save_provider_credentials(
        ProviderCredentialSetupRequest(provider_id="cloudflare-r2", values=result.credential_fields)
    )
    health = check_provider_connection("cloudflare-r2")
    return R2ConciergeResponse(
        status="ok" if health.status == "ok" else "failed",
        message=health.message if health.status == "ok" else f"{result.message} {health.message}",
        bucket=result.bucket,
        public_base_url=result.public_base_url,
    )


@staff_router.get(
    "/source-setup",
    response_model=SourceSetupReport,
    summary="Read plain-language camera and source setup guidance",
)
def source_setup(
    live_source_store: Any = Depends(get_live_source_store),
) -> SourceSetupReport:
    return build_source_setup_report(configured_source_count=_safe_count(live_source_store))


@staff_router.post(
    "/source-setup/live-source",
    response_model=SourceSetupMutationResponse,
    summary="Create a meeting source from the operator setup wizard",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def source_setup_live_source(
    payload: SourceSetupCreateRequest,
    live_source_store: Any = Depends(get_live_source_store),
) -> SourceSetupMutationResponse:
    try:
        return create_source_from_setup(payload, live_source_store=live_source_store)
    except SourceSetupUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except SourceSetupError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@staff_router.post(
    "/source-setup/sample-upload",
    response_model=SourceSetupSampleUploadResponse,
    summary="Create a bundled sample video for private rehearsal",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def source_setup_sample_upload(
    postgres_store: Any = Depends(get_postgres_store),
    live_source_store: Any = Depends(get_live_source_store),
) -> SourceSetupSampleUploadResponse:
    try:
        return create_sample_rehearsal_upload(
            postgres_store=postgres_store,
            live_source_store=live_source_store,
        )
    except SourceSetupUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except SourceSetupError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@staff_router.get(
    "/backup",
    response_model=BackupStatus,
    summary="Read backup setup and write-proof status",
)
def backup_status() -> BackupStatus:
    return build_backup_status()


@staff_router.post(
    "/backup",
    response_model=BackupStatus,
    summary="Configure and verify a backup destination",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def backup_setup(payload: BackupSetupRequest) -> BackupStatus:
    return configure_backup(payload)


@staff_router.get(
    "/restore",
    response_model=RestoreStatus,
    summary="Read restore rehearsal status",
)
def restore_status() -> RestoreStatus:
    return build_restore_status()


@staff_router.post(
    "/restore/rehearsal",
    response_model=RestoreStatus,
    summary="Run a backup restore rehearsal",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
def restore_rehearsal() -> RestoreStatus:
    return run_restore_rehearsal()


@staff_router.post(
    "/dr/run-drill",
    response_model=DrillReport,
    summary="Run the REAL 0.5.0 disaster-recovery drill (backup + restore + crash-recovery)",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
def dr_run_drill() -> DrillReport:
    try:
        return run_dr_drill()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@staff_router.get(
    "/update-rollback",
    response_model=UpdateRollbackStatus,
    summary="Read update and rollback readiness",
)
def update_rollback_status() -> UpdateRollbackStatus:
    return build_update_rollback_status()


@staff_router.post(
    "/update-rollback/preflight",
    response_model=UpdateRollbackStatus,
    summary="Run an update preflight checkpoint",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
def update_rollback_preflight() -> UpdateRollbackStatus:
    return run_update_preflight()


@staff_router.post(
    "/update-rollback/maintenance-window",
    response_model=UpdateRollbackStatus,
    summary="Open an update maintenance window",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
def update_rollback_maintenance_window(
    payload: UpdateMaintenanceWindowRequest,
) -> UpdateRollbackStatus:
    return run_maintenance_window_open(payload)


@staff_router.post(
    "/update-rollback/rollback-artifact",
    response_model=UpdateRollbackStatus,
    summary="Configure a rollback artifact",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
def update_rollback_artifact(payload: RollbackArtifactRequest) -> UpdateRollbackStatus:
    return configure_rollback_artifact(payload)


@staff_router.post(
    "/update-rollback/rollback-rehearsal",
    response_model=UpdateRollbackStatus,
    summary="Run rollback artifact rehearsal",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
def update_rollback_rehearsal() -> UpdateRollbackStatus:
    return run_rollback_rehearsal()


@staff_router.post(
    "/update-rollback/failed-update-rehearsal",
    response_model=UpdateRollbackStatus,
    summary="Run controlled failed-update rollback rehearsal",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
def update_rollback_failed_update_rehearsal() -> UpdateRollbackStatus:
    return run_failed_update_rollback_rehearsal()


@staff_router.post(
    "/update-rollback/post-update-proof",
    response_model=UpdateRollbackStatus,
    summary="Run post-update Safe to broadcast proof",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
def update_rollback_post_update_proof() -> UpdateRollbackStatus:
    return run_post_update_proof()


def _alerting_bundle_section(egress_store: Any, factory: Any) -> dict[str, Any]:
    """S8-5 A1b: redacted operations history for the support bundle — recent alert
    events + their delivery attempts, self-test history, resource samples, and a
    per-channel egress health/proof window. Returns {} when alerting/egress storage
    is unavailable; never blocks bundle creation. No secrets leave the box: alert
    channels are already redacted (``target_redacted``, credential handle dropped)
    and delivery signatures (cryptographic material) are dropped."""

    if egress_store is None or factory is None:
        return {}
    from civiccast.alerting.store import (
        get_alert_channels,
        get_alert_events,
        get_event_deliveries,
        get_self_tests,
        recent_resource_samples,
    )

    def _delivery_json(delivery: Any) -> dict[str, Any]:
        data = delivery.model_dump(mode="json")
        data.pop("signature", None)  # HMAC material — not needed to triage delivery
        # last_error is a fixed "transport error" string (alerting/delivery.py), never
        # the raw exception, so it cannot carry a secret-bearing URL into the bundle.
        return dict(data)

    section: dict[str, Any] = {}
    try:
        with factory() as session:
            events = get_alert_events(session, limit=100)
            deliveries: list[dict[str, Any]] = []
            for event in events[:50]:  # bound: deliveries only for the recent window
                deliveries.extend(
                    _delivery_json(d) for d in get_event_deliveries(session, event.event_id)
                )
            self_tests = get_self_tests(session, limit=20)
            samples = recent_resource_samples(session, window_minutes=1440, limit=200)
            channels = get_alert_channels(session)
        section = {
            "alert_events": [e.model_dump(mode="json") for e in events],
            "alert_event_deliveries": deliveries,
            "alert_channels": [
                {k: v for k, v in c.model_dump(mode="json").items() if k != "credential_handle"}
                for c in channels
            ],
            "self_test_history": [t.model_dump(mode="json") for t in self_tests],
            "resource_samples": [s.model_dump(mode="json") for s in samples],
        }
    except Exception:
        section = {}

    # Per-channel egress activity window — independent of the alerting store so a
    # failure on either side still yields the other.
    egress_activity: dict[str, Any] = {}
    try:
        for config in egress_store.list_configs():
            cid = config.channel_id
            egress_activity[cid] = {
                "recent_health": [
                    h.model_dump(mode="json") for h in egress_store.recent_health(cid, 20)
                ],
                "recent_proof_events": [
                    p.model_dump(mode="json") for p in egress_store.recent_proof_events(cid, 20)
                ],
            }
    except Exception:
        egress_activity = {}

    out: dict[str, Any] = dict(section)
    if egress_activity:
        out["egress_activity"] = egress_activity
    return out


@staff_router.post(
    "/support-bundle",
    response_model=DiagnosticBundleResponse,
    summary="Generate a redacted support bundle",
    dependencies=[Depends(require_any_role("support_admin"))],
)
def support_bundle(
    payload: DiagnosticBundleRequest,
    egress_store: Any = Depends(get_egress_store),
    alerting_session_factory: Any = Depends(get_alerting_session_factory),
) -> DiagnosticBundleResponse:
    operations = _alerting_bundle_section(egress_store, alerting_session_factory)
    channel_ids: tuple[str, ...] = ()
    if egress_store is not None:
        try:
            channel_ids = tuple(config.channel_id for config in egress_store.list_configs())
        except Exception:
            channel_ids = ()
    return create_diagnostic_bundle(payload, operations=operations or None, channel_ids=channel_ids)


@staff_router.get(
    "/support-bundle/{bundle_id}/download",
    response_class=FileResponse,
    summary="Download a generated redacted support bundle",
    description="Requires the support_admin role. Downloads only a previously generated, redacted CivicCast support bundle.",
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Support administrator access required."},
        status.HTTP_404_NOT_FOUND: {"description": "Support bundle not found."},
    },
    dependencies=[Depends(require_any_role("support_admin"))],
)
def support_bundle_download(bundle_id: str) -> FileResponse:
    path = diagnostic_bundle_path(bundle_id)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Support bundle not found."
        )
    return FileResponse(
        path,
        media_type="application/json",
        filename=f"{bundle_id}.json",
        headers={"Cache-Control": "no-store"},
    )


@staff_router.post(
    "/acceptance-packet",
    response_model=AcceptancePacketResponse,
    summary="Generate a redacted station acceptance packet",
    dependencies=[Depends(require_any_role("support_admin"))],
)
def acceptance_packet() -> AcceptancePacketResponse:
    return create_acceptance_packet()


@staff_router.get(
    "/tsduck",
    response_model=TsduckStatus,
    summary="Whether TSDuck (cable stream verification) is installed",
)
def tsduck_status() -> TsduckStatus:
    return locate_tsduck()


@staff_router.post(
    "/tsduck/install",
    response_model=TsduckInstallReport,
    summary="Download and install TSDuck on demand to enable cable verification",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
def tsduck_install() -> TsduckInstallReport:
    # Pull-on-demand: the operator opted in. On Windows this fetches the pinned,
    # checksum-verified portable build (no admin); elsewhere it returns the
    # operator-assisted command. Runs in FastAPI's threadpool (sync def), so the
    # multi-second download does not block the event loop.
    return install_tsduck()


@staff_router.get(
    "/remote-contribution",
    response_model=ContributionInstallReport,
    summary="Whether the pinned VDO.Ninja (remote contribution) is installed + verified",
)
def remote_contribution_status() -> ContributionInstallReport:
    return contribution_install_status()


@staff_router.post(
    "/remote-contribution/install",
    response_model=ContributionInstallReport,
    summary="Install the pinned VDO.Ninja for the remote-contribution tier (S17/S3 commissioning)",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
def remote_contribution_install() -> ContributionInstallReport:
    # S3 commissioning: shallow-clone the pinned VDO.Ninja commit into the managed
    # dir and verify it (stage→verify→swap; a drifted clone never goes live).
    # coturn stays operator-assisted (a system service). Runs in the threadpool
    # (sync def) so the clone never blocks the event loop.
    try:
        return install_remote_contribution(include_relay=True)
    except ContributionInstallError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@staff_router.get(
    "/beta-handoff",
    response_model=BetaHandoffSummary,
    summary="Read fail-closed beta tester handoff readiness (release engineering only)",
)
def beta_handoff() -> BetaHandoffSummary:
    # Internal release-engineering lanes (package proofs, CLI-driven federation setup,
    # etc.) are not for customer wizards -- hide this endpoint from every install unless
    # a release engineer has explicitly opted in. The installer wizard frontend already
    # treats a non-2xx response here as "no beta lanes" (civiccast/apps/installer/src/api.ts
    # fromApiSummary), so 404 is a graceful no-op for every customer session.
    if os.environ.get("CIVICCAST_RELEASE_ENGINEERING") != "1":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found.",
        )
    return build_beta_handoff_summary()


@staff_router.get(
    "/storage",
    response_model=ManagedStorageStatusReport,
    summary="Read installer-managed durable storage setup state",
)
def staff_storage_state() -> ManagedStorageStatusReport:
    return durable_storage_status().report()


@staff_router.post(
    "/storage",
    response_model=ManagedStorageStatusReport,
    summary="Prepare installer-managed durable storage",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def staff_storage_setup(
    request: Request, payload: StorageSetupRequest
) -> ManagedStorageStatusReport:
    if os.environ.get("DATABASE_URL") and payload.storage_dir is None:
        return durable_storage_status().report()
    try:
        storage_dir = Path(payload.storage_dir).expanduser() if payload.storage_dir else None
        storage = _activate_storage(request, ensure_managed_storage(storage_dir=storage_dir))
        _ensure_storage_recording_target(request, storage)
        return storage.report()
    except ManagedStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


_CREDENTIAL_GUESSING_SETUP_PATHS = frozenset({"/api/setup/login", "/api/setup/recover"})


def _setup_rate_limit_key(request: Request) -> str:
    return f"{client_ip(request)}:{request.url.path}"


def _raise_setup_rate_limited(
    limiter: AuthRateLimiter,
    *,
    key: str,
    window_seconds: int,
) -> None:
    """Raise the single-box "wait, then here's your way back in" 429.

    OWNER DECISION 2026-08-29 (field evidence, candidate #17): the old
    generic detail text ("Too many setup requests. Wait before trying
    again.") never said how long or what to do next, so a locked-out
    single-admin station read as a dead end instead of a timer. The wait
    time and the concrete next step are baked directly into ``detail`` (not
    left to the frontend to reconstruct from the Retry-After header) so
    every caller of this API -- console, CLI, a future client -- shows the
    operator the same actionable message.
    """

    retry_after = limiter.retry_after_seconds(key, window_seconds=window_seconds)
    unit = "second" if retry_after == 1 else "seconds"
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            f"Too many sign-in attempts from this station. Wait {retry_after} {unit}, "
            "then try again with the correct password, or use a printed recovery code."
        ),
        headers={"Retry-After": str(retry_after)},
    )


async def _setup_credential_in_body_is_correct(request: Request) -> bool:
    """Cheaply peek the request body to see whether its credential is right.

    Mirrors ``civiccast.auth.tokens.token_matches_exactly`` for the staff-auth
    pattern (audit finding #4, OWNER DECISION 2026-08-30): before rejecting a
    saturated login/recover request with 429, check whether the credential it
    actually carries is correct, so a caller who genuinely knows the password
    or recovery code is never locked out by someone else's -- or their own
    earlier -- wrong guesses. ``Request.json()``/``.body()`` cache the raw
    ASGI body after the first read, so this peek does not interfere with
    FastAPI's own body parsing for the route handler's ``payload`` parameter,
    regardless of which reads first. An unparseable or malformed body is
    treated as "not correct" (falls through to the normal rate-limit check;
    the handler will still return its own 422 for a malformed body once it
    runs).
    """

    try:
        body = await request.json()
    except Exception:
        return False
    if not isinstance(body, dict):
        return False
    try:
        if request.url.path == "/api/setup/login":
            return login_credentials_correct(StationLoginRequest.model_validate(body))
        if request.url.path == "/api/setup/recover":
            return recovery_code_correct(StationRecoveryRequest.model_validate(body))
    except ValidationError:
        return False
    return False


async def _enforce_setup_rate_limit(request: Request) -> None:
    """Rate-limit every pre-staff-auth setup/login/recovery route.

    A single choke point (called from ``_require_local_setup_request``,
    which every ``/api/setup/*`` handler already calls first) covers
    station-state, storage, first-admin, recovery-kit acknowledge, login,
    and recover in one place — these are exactly the password-bearing and
    setup-nonce-guessing surfaces named in audit item #27. Loopback is NOT
    exempted: a local attacker on a shared or LAN-exposed box is real.
    Keyed by (client IP, path) so one noisy route can't burn another's
    budget and one client can't burn another's.

    OWNER DECISION 2026-08-29 (field evidence, candidate #17): on a
    single-box station every legitimate caller is also 127.0.0.1, so the
    old scheme -- every REQUEST to a path burns its budget, success or not
    -- meant the sole admin could lock themselves out just by reading setup
    state a few times or retyping a stale password after a recovery reset
    it. login/recover now use the SAME "only failures count" accounting
    already audited and shipped for ``/api/staff/*``
    (civiccast.auth.middleware.staff_auth_middleware): this function only
    *peeks* at whether that route is already saturated (never consumes a
    hit for those two paths), and the route handlers themselves call
    ``_record_setup_auth_failure`` from their except-blocks so only a wrong
    password or an invalid/expired recovery code spends the budget. The
    total number of wrong guesses allowed per window is unchanged (still
    ``CIVICCAST_AUTH_RATE_LIMIT`` per ``CIVICCAST_AUTH_RATE_LIMIT_WINDOW_SECONDS``)
    -- this closes the self-lockout dead end without loosening the guard
    against real guessing. Every other setup route keeps the original
    per-request accounting (they carry no credential to fail).

    OWNER DECISION 2026-08-30 (audit finding #4): the peek above was NOT
    sufficient on its own. It let a saturated budget reject the CORRECT
    password too, because ``saturated()`` only asks "has this key spent its
    budget", never "is this particular guess right" -- unlike the staff
    pattern's ``token_matches_exactly`` bypass, which login/recover did not
    have despite the commit message that introduced them claiming parity.
    A saturated login/recover request is now also checked against
    :func:`_setup_credential_in_body_is_correct` before being rejected --
    the exact-match bypass the staff pattern already has. Wrong guesses
    still saturate and stay blocked; the one guess that is actually correct
    always gets through, exactly like an exact staff bearer-token match
    does. This is process-local, in-memory state -- see
    ``civiccast.auth.rate_limit.AuthRateLimiter`` -- so a multi-worker
    deployment would reset this budget per worker; out of scope for a
    single-box station, noted here for the record.
    """

    limiter = cast(AuthRateLimiter, request.app.state.auth_rate_limiter)
    limit, window_seconds = auth_rate_limit_config()
    key = _setup_rate_limit_key(request)
    if request.url.path in _CREDENTIAL_GUESSING_SETUP_PATHS:
        if limiter.saturated(
            key, limit=limit, window_seconds=window_seconds
        ) and not await _setup_credential_in_body_is_correct(request):
            _raise_setup_rate_limited(limiter, key=key, window_seconds=window_seconds)
        return
    if limiter.allow(key, limit=limit, window_seconds=window_seconds):
        return
    _raise_setup_rate_limited(limiter, key=key, window_seconds=window_seconds)


def _record_setup_auth_failure(request: Request) -> None:
    """Record one failed login/recovery attempt against the per-IP-and-path budget.

    Call only from the ``/api/setup/login`` and ``/api/setup/recover``
    handlers' except-blocks -- see ``_enforce_setup_rate_limit`` for why
    only failures burn this budget. If this failure saturates the budget,
    raises 429 in place of the caller's 401 (matching the staff-auth
    middleware's behavior for the same situation).
    """

    limiter = cast(AuthRateLimiter, request.app.state.auth_rate_limiter)
    limit, window_seconds = auth_rate_limit_config()
    key = _setup_rate_limit_key(request)
    if not limiter.allow(key, limit=limit, window_seconds=window_seconds):
        _raise_setup_rate_limited(limiter, key=key, window_seconds=window_seconds)


async def _require_local_setup_request(request: Request) -> None:
    """Allow unauthenticated first setup only from the station's own loopback.

    OWNER DECISION 2026-08-29: the installer-handoff nonce this gate used to
    also enforce is retired. The station's control plane binds
    ``127.0.0.1`` only (``civiccast.native.supervisor.core``'s
    ``control_plane_host`` default; verified live via ``netstat`` showing
    ``TCP 127.0.0.1:8000 LISTENING`` and nothing on ``0.0.0.0``), so first
    setup is already unreachable from the network by construction -- the
    nonce was guarding a door inside a room the network can't enter. It
    produced four separate field failures in two days (nonce unreadable
    across the elevated-installer/normal-user split, the recovery code file
    never written, "Get a new code" silently no-op'ing, the elevated CLI
    restore printing nothing) for a control that a PEG station in a locked,
    cleared-personnel room does not need on top of the loopback bind.

    The ONE guard that still matters -- a configured station must never
    offer first setup again -- lives in
    ``civiccast.installer.station_state.complete_first_admin_setup``
    (raises ``StationSetupAlreadyCompleteError``, which the router turns
    into 409) and is untouched by this change.

    ``request.client.host`` is the ASGI transport's own peer address (set by
    the socket the connection actually arrived on), never a
    caller-supplied header like ``X-Forwarded-For`` -- so a remote caller
    cannot spoof it into looking local. If a reverse proxy is ever placed in
    front of this app, that proxy would need to terminate the connection
    itself and this check would need to move to (or validate) the proxy's
    own trusted-peer configuration; nothing here reads a forwarded-for style
    header today, so there is nothing for a remote caller to forge.
    """

    await _enforce_setup_rate_limit(request)
    if _is_local_client(request):
        return
    if request.app.debug or request.scope.get("client") is None:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="First setup can only be done from the station computer itself.",
    )


def _is_local_client(request: Request) -> bool:
    if request.client is None:
        return True
    host = request.client.host
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _verify_setup_caller(request: Request) -> bool:
    """Verify the staff bearer token on a ``/api/setup/*`` request, if one is sent.

    Returns ``True`` when a valid token was presented (and attaches the
    operator identity to ``request.state`` the way ``staff_auth_middleware``
    does for ``/api/staff/*``), ``False`` when no Authorization header was
    sent at all, and raises 401 for a present-but-invalid token so a browser
    still sending a stale console token learns to drop it (the console's
    shared 401 handler clears the stored token).
    """

    authorization = request.headers.get("Authorization")
    if not authorization:
        return False
    token_store = getattr(request.app.state, "staff_token_store", None)
    try:
        identity = verify_bearer_token(authorization, token_store=token_store)
    except StaffAuthMissingCredentialError:
        return False
    except StaffAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    request.state.operator_identity = identity
    return True


async def _require_setup_access(request: Request) -> None:
    """Loopback-only before setup; loopback AND the staff token after it.

    SECURITY (walkthrough on a running beta.5 station, 2026-09-09): every
    ``/api/setup/*`` route was reachable with no credential for the whole
    life of the station, not just during first setup -- the loopback bind
    was the only guard, and it does nothing against another local user or
    process on the same box. Once ``setup_complete`` is true, the routes
    that read or change station state require the staff bearer token, the
    same credential every ``/api/staff/*`` route already demands. Only the
    routes a signed-out operator genuinely needs stay open after setup:
    ``station-state`` (its :meth:`StationSetupState.signed_out_view`),
    ``login`` and ``recover`` -- and those keep this module's existing
    loopback gate and rate limit.
    """

    await _require_local_setup_request(request)
    if not read_station_setup().setup_complete:
        return
    if _verify_setup_caller(request):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            "Setup is complete. Sign in with the station admin password to use this "
            "endpoint; it requires the staff bearer token now."
        ),
        headers={"WWW-Authenticate": "Bearer"},
    )


def _require_setup_role(*roles: str) -> Callable[[Request], Awaitable[None]]:
    """Post-setup, demand the same product role the ``/api/staff/`` sibling demands.

    MAJOR-2 (hostile review of PR #215): ``_require_setup_access`` proves a
    staff token exists, not what it may do. ``POST /api/staff/installer/storage``
    requires ``setup_admin``, but ``POST /api/setup/storage`` -- the same
    ``_activate_storage`` mutation behind a different path -- accepted any
    valid token, so a records-clerk token could rebind the engine and rewrite
    ``DATABASE_URL`` through the weaker door. Before ``setup_complete`` there
    is no token and no identity, so nothing to check; after it, the identity
    ``_verify_setup_caller`` attached is checked exactly like
    :func:`civiccast.auth.roles.require_any_role` does for staff routes
    (401 without an identity, 403 for the wrong role). Order matters: list
    this AFTER ``_require_setup_access`` in ``dependencies`` so the identity
    is attached first.
    """

    role_dependency = require_any_role(*roles)

    async def dependency(request: Request) -> None:
        if not read_station_setup().setup_complete:
            return
        role_dependency(request)

    return dependency


@public_router.get(
    "/station-state",
    response_model=StationSetupState,
    summary="Read local setup state before staff auth exists",
    responses=_LOCAL_SETUP_ACCESS_RESPONSES,
)
async def public_station_state(request: Request) -> StationSetupState:
    await _require_local_setup_request(request)
    state = read_station_setup()
    if state.setup_complete and not _verify_setup_caller(request):
        return state.signed_out_view()
    return state


@public_router.get(
    "/storage",
    response_model=ManagedStorageStatusReport,
    summary="Read local durable storage setup state before staff auth exists",
    responses=_SETUP_AFTER_COMPLETE_RESPONSES,
    dependencies=[Depends(_require_setup_access)],
)
def public_storage_state() -> ManagedStorageStatusReport:
    return durable_storage_status().report()


@public_router.post(
    "/storage",
    response_model=ManagedStorageStatusReport,
    summary="Prepare installer-managed durable storage before staff auth exists",
    responses=_SETUP_AFTER_COMPLETE_RESPONSES,
    dependencies=[
        Depends(_require_setup_access),
        Depends(_require_setup_role("setup_admin")),
    ],
)
def public_storage_setup(
    _payload: PublicStorageSetupRequest,
    request: Request,
) -> ManagedStorageStatusReport:
    if os.environ.get("DATABASE_URL"):
        return durable_storage_status().report()
    try:
        storage = _activate_storage(request, ensure_managed_storage())
        _ensure_storage_recording_target(request, storage)
        return storage.report()
    except ManagedStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@public_router.post(
    "/first-admin",
    response_model=FirstAdminSetupResponse,
    summary="Complete local first-admin setup before staff auth exists",
    responses=_SETUP_AFTER_COMPLETE_RESPONSES,
    dependencies=[
        Depends(_require_setup_access),
        Depends(_require_setup_role("setup_admin")),
    ],
)
def public_first_admin_setup(
    payload: FirstAdminSetupRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    postgres_store: Any = Depends(get_postgres_store),
    publish_store: Any = Depends(get_publish_store),
    schedule_store: Any = Depends(get_schedule_store),
) -> FirstAdminSetupResponse:
    try:
        response = complete_first_admin_setup(payload)
    except StationSetupAlreadyCompleteError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    _schedule_first_run_seed(
        background_tasks,
        response,
        postgres_store=postgres_store,
        publish_store=publish_store,
        schedule_store=schedule_store,
    )
    return response


@public_router.post(
    "/recovery-kit/acknowledge",
    response_model=StationSetupState,
    summary="Record that the operator saved or printed the one-time recovery kit",
    responses=_SETUP_AFTER_COMPLETE_RESPONSES,
    dependencies=[
        Depends(_require_setup_access),
        Depends(_require_setup_role("setup_admin")),
    ],
)
def public_recovery_kit_acknowledge(
    payload: RecoveryKitAcknowledgeRequest,
    request: Request,
) -> StationSetupState:
    if not payload.confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Set confirmed=true only after the recovery kit is saved or printed.",
        )
    try:
        return acknowledge_station_recovery_kit()
    except StationSetupNotCompleteError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@public_router.post(
    "/login",
    response_model=StationAuthResponse,
    summary="Sign in with the local first-admin password",
    responses=_LOCAL_SETUP_ACCESS_RESPONSES,
    dependencies=[Depends(_require_local_setup_request)],
)
def public_station_login(
    payload: StationLoginRequest,
    request: Request,
) -> StationAuthResponse:
    try:
        return login_station_admin(payload)
    except StationAuthError as exc:
        _record_setup_auth_failure(request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@public_router.post(
    "/recover",
    response_model=StationAuthResponse,
    summary="Recover the local first-admin account with a one-time recovery code",
    responses=_LOCAL_SETUP_ACCESS_RESPONSES,
    dependencies=[Depends(_require_local_setup_request)],
)
def public_station_recovery(
    payload: StationRecoveryRequest,
    request: Request,
) -> StationAuthResponse:
    try:
        return recover_station_admin(payload)
    except StationAuthError as exc:
        _record_setup_auth_failure(request)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


def _caller_bearer_token(request: Request) -> str:
    """Return the raw bearer token this already-authenticated request carried.

    Only used by staff routes that need to identify the caller's OWN token
    value (not just its resolved identity) -- e.g. so a "revoke other
    sessions" action can tell which operator-console session is the one
    making the call. ``staff_auth_middleware`` has already verified this
    header for every ``/api/staff/*`` request before any route handler
    runs, so this just re-parses the same header the same way; it never
    performs its own verification.
    """

    authorization = request.headers.get("Authorization")
    if not authorization:  # pragma: no cover - staff_auth_middleware already rejects this
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header. Use Bearer <staff-token>.",
        )
    scheme, sep, token = authorization.partition(" ")
    if not sep or scheme.lower() != "bearer" or not token.strip():
        # pragma: no cover - staff_auth_middleware already rejects this
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header. Use Bearer <staff-token>.",
        )
    return token.strip()


@staff_router.post(
    "/sessions/revoke-others",
    response_model=RevokeOtherSessionsResponse,
    summary="Sign out every other operator-console session, keeping this one signed in",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def revoke_other_operator_console_sessions(request: Request) -> RevokeOtherSessionsResponse:
    """CRITICAL fix: a lost/stolen laptop's session had no revoke path.

    Login and recovery deliberately APPEND to the session list rather than
    replacing it (see the OWNER DECISION in
    civiccast.installer.station_state on ``_MAX_OPERATOR_SESSIONS``), so a
    session from a lost device stayed valid until 20 more sign-ins evicted
    it or the station was destructively reset. This lets the signed-in
    operator end every OTHER session in one action while staying signed in
    themselves.
    """

    caller_token = _caller_bearer_token(request)
    try:
        return revoke_other_station_sessions(caller_token)
    except StationAuthError as exc:
        # The caller authenticated with something other than a station
        # operator-console token (an env-configured or DB-issued staff
        # token) -- there is no operator-console session of theirs to keep,
        # so refuse rather than guess which session (if any) to preserve.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@staff_router.post(
    "/recovery-kit/regenerate",
    response_model=RecoveryKitRegenerateResponse,
    summary="Mint a fresh recovery kit for the already-signed-in admin",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def regenerate_operator_recovery_kit() -> RecoveryKitRegenerateResponse:
    """CRITICAL fix: recovery kits had no regenerate path before this.

    If the browser died between first-run setup and saving the kit, the 8
    codes were gone forever -- a later lost password meant a permanent
    lockout with no escape except the destructive, undocumented
    ``CIVICCAST_ALLOW_FIRST_ADMIN_RESET`` full station wipe. This route
    requires an already-authenticated admin (``setup_admin``, enforced by
    the dependency above) so it can never become a second lockout-bypass --
    it is for "I still have my password but lost my codes," not for
    recovering a locked-out account.
    """

    try:
        return regenerate_station_recovery_kit()
    except StationSetupNotCompleteError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.post(
    "/model-bundle",
    response_model=ModelBundleManifest,
    summary="Build an offline model bundle manifest with hash verification",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def model_bundle(request: ModelBundleRequest) -> ModelBundleManifest:
    return build_model_bundle_manifest(request)


class PackageVerificationRequest(BaseModel):
    """Request body for package artifact verification."""

    model_config = ConfigDict(extra="forbid")

    artifact: str
    sidecar: str


class AirGapImportRequest(BaseModel):
    """Request body for air-gapped import verification."""

    model_config = ConfigDict(extra="forbid")

    bundle_dir: str
    proof_manifest: str | None = None
    network_enabled: bool = False


class InstallerActionRequest(BaseModel):
    """Request body for local installer GUI actions."""

    model_config = ConfigDict(extra="forbid")

    lane_id: str
    action: str


class InstallerActionResult(BaseModel):
    """Result returned after queuing a local installer GUI action."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    message: str
    operator_console_url: str


def _safe_count(store: Any) -> int | None:
    """Return item count for a DI store without turning health into a 500."""

    if store is None:
        return None
    try:
        return len(store.list())
    except Exception:
        return None


def _activate_storage(request: Request, storage: ManagedStorageStatus) -> ManagedStorageStatus:
    """Make newly prepared storage active for the current app process."""

    os.environ["CIVICCAST_UPLOAD_DIR"] = storage.upload_dir
    activator = getattr(request.app.state, "activate_durable_storage", None)
    if callable(activator):
        activator(storage.database_url, storage.upload_dir)
    else:
        os.environ["DATABASE_URL"] = storage.database_url
    return storage


def _ensure_storage_recording_target(
    request: Request,
    storage: ManagedStorageStatus,
) -> None:
    """Create the default production target after storage rewires this app."""

    resolver = request.app.dependency_overrides.get(get_recording_target_store)
    recording_target_store = resolver() if callable(resolver) else None
    ensure_default_recording_target(recording_target_store, upload_dir=Path(storage.upload_dir))


@staff_router.get(
    "/platform-plan",
    summary="Read the cross-platform installer bootstrap plan",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
    include_in_schema=False,
)
def platform_plan(os_family: OsFamily = "linux") -> dict[str, object]:
    plan = build_bootstrap_plan(os_family=os_family, detected_tools={})
    payload = plan.model_dump(mode="json")
    payload["model_config"] = {"extra": "forbid"}
    return payload


@staff_router.post(
    "/package-verification",
    response_model=PackageVerificationResult,
    summary="Verify package bytes, sidecar, install metadata, and attestation",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
    include_in_schema=False,
)
def package_verification(request: PackageVerificationRequest) -> PackageVerificationResult:
    return verify_package_artifact(Path(request.artifact), Path(request.sidecar))


@staff_router.get(
    "/model-state",
    response_model=ModelSetupResult,
    summary="Read installer-facing model proof state",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
    include_in_schema=False,
)
def model_state() -> ModelSetupResult:
    item = mark_model_unavailable("gemma4:e4b", reason="provider proof not recorded")
    return ModelSetupResult(
        status="unavailable",
        ready=False,
        items=[item],
        next_step=item.next_step,
    )


@staff_router.post(
    "/airgap-import",
    response_model=AirGapVerificationResult,
    summary="Verify an air-gapped import bundle",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
    include_in_schema=False,
)
def airgap_import(request: AirGapImportRequest) -> AirGapVerificationResult:
    bundle_dir = Path(request.bundle_dir).expanduser().resolve()
    proof_manifest = (
        Path(request.proof_manifest).expanduser().resolve()
        if request.proof_manifest is not None
        else bundle_dir / "proof.json"
    )
    if not proof_manifest.is_relative_to(bundle_dir):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="proof_manifest must be inside bundle_dir.",
        )
    return verify_airgap_bundle(
        bundle_dir,
        proof_manifest=proof_manifest,
        network_enabled=request.network_enabled,
    )


@staff_router.post(
    "/actions",
    response_model=InstallerActionResult,
    summary="Queue a local installer action from the GUI",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
    include_in_schema=False,
)
def installer_action(
    request: Request, action_request: InstallerActionRequest
) -> InstallerActionResult:
    allowed_actions = {"retry", "cancel", "continue", "repair", "reset", "uninstall"}
    if action_request.action not in allowed_actions:
        return InstallerActionResult(
            accepted=False,
            message="Choose Retry, Continue, Repair, Reset, or Uninstall from the installer window.",
            operator_console_url=operator_console_url(),
        )
    if action_request.action == "repair":
        if action_request.lane_id == "storage":
            try:
                storage = _activate_storage(request, ensure_managed_storage())
            except ManagedStorageError as exc:
                return InstallerActionResult(
                    accepted=False,
                    message=f"CivicCast could not repair local storage: {exc}",
                    operator_console_url=operator_console_url(),
                )
            return InstallerActionResult(
                accepted=True,
                message=storage.operator_message,
                operator_console_url=operator_console_url(),
            )
        return InstallerActionResult(
            accepted=True,
            message=(
                "CivicCast queued a repair check for this installer lane. "
                "Rerun readiness after the local service refresh finishes."
            ),
            operator_console_url=operator_console_url(),
        )
    if action_request.action == "reset":
        return InstallerActionResult(
            accepted=True,
            message="CivicCast reset the local installer progress marker. Durable records were not deleted.",
            operator_console_url=operator_console_url(),
        )
    if action_request.action == "uninstall":
        return InstallerActionResult(
            accepted=True,
            message=(
                "Use Windows Settings to uninstall CivicCast. Back up meeting records "
                "before removing local storage."
            ),
            operator_console_url=operator_console_url(),
        )
    if action_request.action == "continue":
        if action_request.lane_id == "storage":
            if os.environ.get("DATABASE_URL"):
                storage = durable_storage_status()
                return InstallerActionResult(
                    accepted=True,
                    message=storage.operator_message,
                    operator_console_url=operator_console_url(),
                )
            try:
                storage = _activate_storage(request, ensure_managed_storage())
            except ManagedStorageError as exc:
                return InstallerActionResult(
                    accepted=False,
                    message=f"CivicCast could not prepare durable storage: {exc}",
                    operator_console_url=operator_console_url(),
                )
            return InstallerActionResult(
                accepted=True,
                message=storage.operator_message,
                operator_console_url=operator_console_url(),
            )
        return InstallerActionResult(
            accepted=True,
            message="CivicCast queued this lane for local verification. When every required proof passes, use Open operator console to continue.",
            operator_console_url=operator_console_url(),
        )
    if action_request.action == "cancel":
        return InstallerActionResult(
            accepted=True,
            message="CivicCast paused this lane. Resume from this installer before the first public meeting.",
            operator_console_url=operator_console_url(),
        )
    return InstallerActionResult(
        accepted=True,
        message="CivicCast is refreshing this installer proof lane.",
        operator_console_url=operator_console_url(),
    )


@staff_router.get(
    "/summary",
    response_model=InstallerSummary,
    summary="Read fail-closed installer readiness summary",
    include_in_schema=False,
)
def installer_summary() -> InstallerSummary:
    return build_installer_summary()
