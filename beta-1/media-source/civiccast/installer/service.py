# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Installer and first-run verification services."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from civiccast import __version__
from civiccast.installer.models import (
    AcceptancePacketResponse,
    BackupSetupRequest,
    BackupStatus,
    BroadcastGate,
    BroadcastGateItem,
    DeploymentProfile,
    DiagnosticBundleRequest,
    DiagnosticBundleResponse,
    FailedUpdateRollbackProofState,
    FirstAdminRequiredField,
    FirstAdminSetupContract,
    FirstAdminSetupRequest,
    FirstAdminSetupResponse,
    FirstRunHealthReport,
    FirstRunPlan,
    HealthCheckItem,
    InstallerLane,
    InstallerStep,
    InstallerSummary,
    MaintenanceWindowState,
    ModelBundleItem,
    ModelBundleManifest,
    ModelBundleRequest,
    PostUpdateProofState,
    ProviderCredentialField,
    ProviderCredentialSetupRequest,
    ProviderCredentialSetupResponse,
    ProviderProofRecordRequest,
    ProviderProofRecordResponse,
    ProviderReadinessItem,
    ProviderReadinessReport,
    ProviderReadinessStatus,
    RecoveryKitContract,
    RecoveryKitRegenerateResponse,
    RehearsalReport,
    RehearsalResult,
    ResidentPreview,
    RestoreProofItem,
    RestoreStatus,
    RevokeOtherSessionsResponse,
    RollbackArtifactRequest,
    RollbackProofState,
    SafeToBroadcastCheckContract,
    SafeToBroadcastColor,
    SafeToBroadcastContract,
    SafeToBroadcastStateDefinition,
    SampleSeedStatus,
    SampleSeedStepValue,
    SourceSetupCreateRequest,
    SourceSetupMutationResponse,
    SourceSetupOption,
    SourceSetupReport,
    SourceSetupSampleUploadResponse,
    StationAuthResponse,
    StationLoginRequest,
    StationRecoveryRequest,
    StationSetupState,
    SystemHealthCheck,
    SystemHealthReport,
    UpdateMaintenanceWindowRequest,
    UpdateRollbackStatus,
)
from civiccast.installer.platform import OsFamily, PlatformBootstrapPlan, build_bootstrap_plan
from civiccast.installer.station_state import (
    acknowledge_recovery_kit as persist_recovery_kit_acknowledgement,
)
from civiccast.installer.station_state import (
    complete_first_admin_setup as persist_first_admin_setup,
)
from civiccast.installer.station_state import (
    login_station_admin as persist_station_login,
)
from civiccast.installer.station_state import (
    read_station_setup_state,
    seed_ai_model_default,
    station_state_path,
)
from civiccast.installer.station_state import (
    recover_station_admin as persist_station_recovery,
)
from civiccast.installer.station_state import (
    regenerate_recovery_kit as persist_recovery_kit_regeneration,
)
from civiccast.installer.station_state import (
    revoke_other_operator_sessions as persist_other_session_revocation,
)
from civiccast.installer.storage import (
    EXTERNAL_DATABASE_NOT_READY_STATUSES,
    durable_storage_status,
    load_managed_upload_dir,
)
from civiccast.live.finalization import LiveRecordingAssetCollisionError, LiveRecordingFinalizer
from civiccast.live.models import (
    LiveSessionCreate,
    LiveSourceCreate,
    LiveSourceTypeValue,
    RecordingTargetCreate,
)
from civiccast.live.preflight import PreflightInputs
from civiccast.live.recording_paths import (
    DEFAULT_RECORDING_TARGET_DIR_NAME,
    DEFAULT_RECORDING_TARGET_ID,
    DEFAULT_RECORDING_TARGET_NAME,
    REHEARSAL_RECORDING_TARGET_ID,
    local_recording_path,
)
from civiccast.live.store import (
    LiveSessionAlreadyExistsError,
    LiveSessionNotFoundError,
    LiveSessionStateError,
    LiveSourceAlreadyExistsError,
    RecordingTargetAlreadyExistsError,
)
from civiccast.publish.models import PublishApprovalRequest
from civiccast.publish.proof import build_provider_proof_plan
from civiccast.publish.service import approve_publish
from civiccast.schedule.ingest import (
    FfprobeError,
    FfprobeNotFoundError,
    UnsupportedFormatError,
    hash_file,
    run_ffprobe,
    validate_ingest,
)
from civiccast.schedule.models import ScheduleItemCreate
from civiccast.schedule.paths import resolve_upload_root, resolve_vod_package_root
from civiccast.stream._ffmpeg import FfmpegNotFoundError, run_ffmpeg
from civiccast.stream.packager import PackagingError, pack_vod_asset
from civiccast.vod.store import AssetAlreadyExistsError

if TYPE_CHECKING:
    from civiccast.dr.models import DrillReport

_WINDOWS_DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:(?![\\/])")

# Field evidence (candidate #17): a backup destination showed READY for
# C:\mnt\c\CivicCastBackups, a WSL-style mounted-drive path that does not
# exist on this native-Windows product's own control plane (which always
# runs directly on Windows, never inside a WSL2 guest -- see
# _backup_destination_path's docstring). Nothing in this codebase sets that
# path as a default; it can only reach here as operator-typed or
# environment-configured input carried over from the retired WSL product
# line. Reject it outright rather than silently accepting a path that would
# make "backup verified" a lie.
_WSL_STYLE_PATH_RE = re.compile(r"(?i)^(?:[a-z]:)?[\\/]mnt[\\/][a-z](?:[\\/]|$)")

_PROFILE_LABELS: dict[DeploymentProfile, str] = {
    "public-meetings": "Public Meetings",
    "streaming-only": "Streaming Only",
    "peg-cable": "PEG Cable",
}

_MODEL_BUNDLE_HASHES = {
    "faster-whisper-large-v3": "06c6e8790caa15e3908847f20e0468f4a051d1dd7987c7481c22a647561eae54",
    # Both summary tags ship in the air-gap bundle so the adaptive 12B/e4b default is
    # present offline regardless of detected RAM (S13 E2/T2/Q1).
    "gemma-4-12b-summary": "b01817c585804c34319cc3b56ba6438ca464521f75fdc865e82887bbc01edbd9",
    "gemma-4e4b-summary": "1154934d4d329307676bd58e6528dffa1b2ddfd89d4340f0405ff320dda96ed5",
    "translate-gemma-4b": "2d76f332a5a70693677a31d277069ee203e8ca601b05fc80357a5e697e03890e",
}
_OPS_STATE_SCHEMA_VERSION = 1
_OPS_STATE_LOCK_NAME = ".tester-ops-state.lock"
_OPS_STATE_LOCK_TIMEOUT_SECONDS = 5.0
_PROVIDER_CREDENTIALS_SCHEMA_VERSION = 1
_PROVIDER_PROOF_SCHEMA_VERSION = 1
_SECRET_ENV_MARKERS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PRIVATE",
    "KEY",
    "CREDENTIAL",
    "DATABASE_URL",
)
_SUPPORT_ENV_KEYS = (
    "CIVICCAST_MANAGED_STORAGE_DIR",
    "CIVICCAST_UPLOAD_DIR",
    "CIVICCAST_BACKUP_DIR",
    "CIVICCAST_RESIDENT_PORTAL_URL",
    "CIVICCAST_NAS_ARCHIVE_PATH",
    "CIVICCAST_INTERNET_ARCHIVE_ACCESS_KEY",
    "CIVICCAST_YOUTUBE_CLIENT_ID",
    "CIVICCAST_YOUTUBE_CLIENT_SECRET",
    "CIVICCAST_SUBSCRIBER_WEBHOOK_SECRET",
    "CIVICCAST_R2_ACCOUNT_ID",
    "CIVICCAST_R2_ACCESS_KEY_ID",
    "CIVICCAST_R2_SECRET_ACCESS_KEY",
    "CIVICCAST_R2_BUCKET",
    "CIVICCAST_R2_PUBLIC_BASE_URL",
    "DATABASE_URL",
)


_PROVIDER_CREDENTIAL_FIELDS: dict[str, tuple[ProviderCredentialField, ...]] = {
    "internet-archive": (
        ProviderCredentialField(
            id="access_key",
            label="Access key",
            help_text="Paste the station Internet Archive S3 access key.",
            secret=True,
        ),
        ProviderCredentialField(
            id="secret_key",
            label="Secret key",
            help_text="Paste the matching Internet Archive S3 secret key.",
            secret=True,
        ),
    ),
    "youtube": (
        ProviderCredentialField(
            id="client_id",
            label="Client ID",
            help_text="Paste the Google OAuth client ID for the station channel.",
            secret=False,
        ),
        ProviderCredentialField(
            id="client_secret",
            label="Client secret",
            help_text="Paste the Google OAuth client secret. CivicCast stores it locally and never prints it back.",
            secret=True,
        ),
        ProviderCredentialField(
            id="refresh_token",
            label="OAuth refresh token",
            secret=True,
            help_text="Paste the refresh token authorized for the station's YouTube channel.",
        ),
    ),
    "subscriber-notifications": (
        ProviderCredentialField(
            id="smtp_host",
            required=False,
            help_text="Enter the station SMTP relay setting; optional fields use the displayed defaults.",
            label="SMTP host",
            secret=False,
        ),
        ProviderCredentialField(
            id="smtp_from",
            required=False,
            help_text="Enter the station SMTP relay setting; optional fields use the displayed defaults.",
            label="Sender email",
            secret=False,
        ),
        ProviderCredentialField(
            id="smtp_port",
            help_text="Enter the station SMTP relay setting; optional fields use the displayed defaults.",
            label="SMTP port (default 587)",
            secret=False,
            required=False,
        ),
        ProviderCredentialField(
            id="smtp_username",
            help_text="Enter the station SMTP relay setting; optional fields use the displayed defaults.",
            label="SMTP username",
            secret=False,
            required=False,
        ),
        ProviderCredentialField(
            id="smtp_password",
            help_text="Enter the station SMTP relay setting; optional fields use the displayed defaults.",
            label="SMTP password",
            secret=True,
            required=False,
        ),
        ProviderCredentialField(
            id="smtp_starttls",
            help_text="Enter the station SMTP relay setting; optional fields use the displayed defaults.",
            label="STARTTLS (1=yes, 0=no)",
            secret=False,
            required=False,
        ),
        ProviderCredentialField(
            id="webhook_secret",
            required=False,
            label="Webhook secret",
            help_text="Paste the shared secret for signed subscriber notification webhooks.",
            secret=True,
        ),
    ),
    "local-nas": (
        ProviderCredentialField(
            id="archive_path",
            label="Archive folder",
            help_text="Enter the mounted archive folder or NAS path CivicCast should verify.",
            secret=False,
        ),
    ),
    "cloudflare-r2": (
        ProviderCredentialField(
            id="account_id",
            label="Account ID",
            help_text="Paste the Cloudflare account ID.",
            secret=False,
        ),
        ProviderCredentialField(
            id="access_key_id",
            label="Access key ID",
            help_text="Paste the R2 access key ID scoped to the station bucket.",
            secret=True,
        ),
        ProviderCredentialField(
            id="secret_access_key",
            label="Secret access key",
            help_text="Paste the R2 secret access key. CivicCast stores it locally and never prints it back.",
            secret=True,
        ),
        ProviderCredentialField(
            id="bucket",
            label="Bucket",
            help_text="Enter the R2 bucket name for meeting media.",
            secret=False,
        ),
        ProviderCredentialField(
            id="public_base_url",
            label="Public media URL",
            help_text="Enter the public base URL residents will use for R2-hosted media.",
            secret=False,
        ),
    ),
    "bunny": (
        ProviderCredentialField(
            id="storage_zone_name",
            label="Storage zone name",
            help_text="Enter the BunnyCDN storage zone name.",
            secret=False,
        ),
        ProviderCredentialField(
            id="access_key",
            label="Storage Zone API key",
            help_text=(
                "Paste the BunnyCDN Storage Zone API key (not the account key). "
                "CivicCast stores it locally and never prints it back."
            ),
            secret=True,
        ),
        ProviderCredentialField(
            id="cdn_hostname",
            label="Pull-zone hostname",
            help_text="Enter the pull-zone hostname, e.g. your-zone.b-cdn.net.",
            secret=False,
        ),
    ),
    "fastly": (
        ProviderCredentialField(
            id="region",
            label="Region",
            help_text="Enter the Fastly Object Storage region, e.g. us-east.",
            secret=False,
        ),
        ProviderCredentialField(
            id="access_key_id",
            label="Access key ID",
            help_text="Paste the Fastly Object Storage access key ID.",
            secret=True,
        ),
        ProviderCredentialField(
            id="secret_access_key",
            label="Secret access key",
            help_text=(
                "Paste the Fastly Object Storage secret key. CivicCast stores it "
                "locally and never prints it back."
            ),
            secret=True,
        ),
        ProviderCredentialField(
            id="bucket",
            label="Bucket",
            help_text="Enter the Fastly Object Storage bucket name for meeting media.",
            secret=False,
        ),
        ProviderCredentialField(
            id="public_base_url",
            label="Public media URL",
            help_text="Enter the public base URL residents will use for Fastly-hosted media.",
            secret=False,
        ),
    ),
    "akamai": (
        ProviderCredentialField(
            id="region",
            label="Region",
            help_text="Enter the Akamai/Linode Object Storage region, e.g. us-east-1.",
            secret=False,
        ),
        ProviderCredentialField(
            id="access_key_id",
            label="Access key ID",
            help_text="Paste the Akamai/Linode Object Storage access key ID.",
            secret=True,
        ),
        ProviderCredentialField(
            id="secret_access_key",
            label="Secret access key",
            help_text=(
                "Paste the Akamai/Linode Object Storage secret key. CivicCast stores "
                "it locally and never prints it back."
            ),
            secret=True,
        ),
        ProviderCredentialField(
            id="bucket",
            label="Bucket",
            help_text="Enter the Akamai/Linode Object Storage bucket name for meeting media.",
            secret=False,
        ),
        ProviderCredentialField(
            id="public_base_url",
            label="Public media URL",
            help_text="Enter the public base URL residents will use for Akamai-hosted media.",
            secret=False,
        ),
    ),
}
_INSTALLER_PROVIDER_PROOF_CREDENTIALS: dict[str, tuple[str, ...]] = {
    "internet-archive": ("internet-archive",),
    "youtube": ("youtube-live", "youtube-vod"),
    "subscriber-notifications": ("subscriber-notifications",),
    "local-nas": ("local-nas-rsync", "local-nas-zfs"),
}
_INSTALLER_PROVIDER_ENV_NAMES: dict[str, tuple[str, ...]] = {
    # rc17 D4: these must match what the real adapter's own `.from_env()`
    # reads (docs/ops/cdn-and-providers.md is the canonical list) -- not a
    # hand-maintained guess. A name here that the real adapter never consumes
    # lets an operator "configure" a provider readiness never actually uses.
    "internet-archive": ("CIVICCAST_IA_ACCESS_KEY", "CIVICCAST_IA_SECRET_KEY"),
    "youtube": (
        "CIVICCAST_YOUTUBE_CLIENT_ID",
        "CIVICCAST_YOUTUBE_CLIENT_SECRET",
        "CIVICCAST_YOUTUBE_REFRESH_TOKEN",
    ),
    "subscriber-notifications": ("CIVICCAST_SUBSCRIBER_WEBHOOK_SECRET",),
    "local-nas": ("CIVICCAST_NAS_ARCHIVE_PATH",),
    "cloudflare-r2": (
        "CIVICCAST_R2_ACCOUNT_ID",
        "CIVICCAST_R2_ACCESS_KEY_ID",
        "CIVICCAST_R2_SECRET_ACCESS_KEY",
        "CIVICCAST_R2_BUCKET",
        "CIVICCAST_R2_PUBLIC_BASE_URL",
    ),
    "bunny": (
        "CIVICCAST_BUNNY_STORAGE_ZONE",
        "CIVICCAST_BUNNY_ACCESS_KEY",
        "CIVICCAST_BUNNY_CDN_HOSTNAME",
    ),
    "fastly": (
        "CIVICCAST_FASTLY_REGION",
        "CIVICCAST_FASTLY_ACCESS_KEY_ID",
        "CIVICCAST_FASTLY_SECRET_ACCESS_KEY",
        "CIVICCAST_FASTLY_BUCKET",
        "CIVICCAST_FASTLY_PUBLIC_BASE_URL",
    ),
    "akamai": (
        "CIVICCAST_AKAMAI_REGION",
        "CIVICCAST_AKAMAI_ACCESS_KEY_ID",
        "CIVICCAST_AKAMAI_SECRET_ACCESS_KEY",
        "CIVICCAST_AKAMAI_BUCKET",
        "CIVICCAST_AKAMAI_PUBLIC_BASE_URL",
    ),
}
_INSTALLER_PROVIDER_PROOF_PROVIDERS: dict[str, tuple[str, ...]] = {
    "internet-archive": ("internet_archive",),
    "youtube": ("youtube_live", "youtube_vod"),
    "subscriber-notifications": ("email_double_opt_in",),
    "local-nas": ("nas_rsync", "nas_zfs"),
}
_INSTALLER_PROVIDER_BY_PROOF: dict[str, str] = {
    proof_provider: provider_id
    for provider_id, proof_providers in _INSTALLER_PROVIDER_PROOF_PROVIDERS.items()
    for proof_provider in proof_providers
}
# rc17 D4: providers whose "configured" claim is checked by actually
# constructing the real adapter's own settings object (a genuine positive
# validation), instead of trusting presence of a hand-maintained env-var
# name list that can silently drift away from what the real adapter reads.
# "subscriber-notifications" joined round 2 (CC-RC17-001): its proof-provider
# mapping (`email_double_opt_in`) resolves to the real mail adapter
# (`civiccast.platform.providers._real_mail`), which builds
# `civiccast.subscribe.smtp.SmtpSettings.from_env()` -- not the webhook
# secret this provider's Setup UI collects. The SMTP loader is the truth.
_PROVIDERS_WITH_REAL_SETTINGS_VALIDATION = frozenset(
    {"internet-archive", "youtube", "local-nas", "subscriber-notifications"}
)


def _real_provider_settings_error(provider_id: str) -> str | None:
    """Construct the real provider's own settings from saved setup or environment.

    Returns the real settings loader's own actionable ``ValueError`` message
    when the saved settings/environment don't satisfy it, or ``None`` when it validates.
    This is the exact code path :mod:`civiccast.platform.providers` uses to
    build the real adapter -- never a hand-invented message, never a network
    call. ``local-nas`` additionally confirms the archive directory exists.
    """

    loader: Callable[[], object]
    if provider_id == "internet-archive":
        from civiccast.archive.internet_archive import InternetArchiveSettings

        loader = InternetArchiveSettings.from_env
    elif provider_id == "youtube":
        from civiccast.syndicate.youtube import YouTubeSettings

        loader = YouTubeSettings.from_env
    elif provider_id == "local-nas":
        from civiccast.archive.local_nas import LocalNasSettings

        loader = LocalNasSettings.from_env
    elif provider_id == "subscriber-notifications":
        # Same loader `civiccast.platform.providers._real_mail()` uses to
        # build the real adapter for the `email_double_opt_in` proof lane.
        from civiccast.subscribe.smtp import SmtpSettings

        loader = SmtpSettings.from_env
    else:
        return None
    try:
        loader()
    except ValueError as exc:
        return str(exc)
    return None


def _provider_credentials_are_valid(provider_id: str) -> bool:
    """Single source of truth for "this provider's credentials are usable".

    Shared by the readiness report's "ready" gate and by proof recording's
    guard, so the two can never drift out of sync again (rc17 D4: proof used
    to be recordable off a mere ANY-stored-field or ANY-configured-env-var
    check that had no relationship to what the real adapter required).

    The real settings loader consumes protected Setup values first, with
    environment fallback. Field names alone never establish readiness: the
    resulting adapter configuration must validate, and delivery still needs
    separate destination proof. No values are copied into os.environ.
    """

    if provider_id in _PROVIDERS_WITH_REAL_SETTINGS_VALIDATION:
        return _real_provider_settings_error(provider_id) is None
    fields = _PROVIDER_CREDENTIAL_FIELDS.get(provider_id, ())
    required_field_ids = {field.id for field in fields if field.required}
    stored_fields = _stored_provider_fields(provider_id)
    if required_field_ids and required_field_ids.issubset(stored_fields):
        return True
    env_names = _INSTALLER_PROVIDER_ENV_NAMES.get(provider_id, ())
    return bool(env_names) and all(os.getenv(name) for name in env_names)


def build_first_run_plan(
    *,
    profile: DeploymentProfile = "public-meetings",
    recommended_tier: str = "tier-1",
    summary_default_key: str | None = None,
) -> FirstRunPlan:
    """Build the profile-driven installer wizard plan.

    ``summary_default_key`` is the S13 adaptive summary default computed from detected
    RAM (e.g. ``gemma4-12b-ollama`` on a >=16GB box). The "models" step names it and
    offers an override (S13 §5.3 / §6.1). When omitted, the step names the conservative
    e4b fallback so it never advertises a 12B default a smaller box cannot run.
    """

    profile_label = _PROFILE_LABELS[profile]
    summary_default = summary_default_key or "gemma4-e4b-ollama"
    steps = [
        InstallerStep(
            id="profile",
            title="Choose deployment profile",
            status="complete",
            summary=f"{profile_label} profile selected.",
            next_step="Confirm the station identity and proceed to hardware detection.",
        ),
        InstallerStep(
            id="hardware",
            title="Hardware probe and tier recommendation",
            status="ready",
            summary=f"Hardware probe recommends {recommended_tier}.",
            next_step="Review CPU, RAM, disk, GPU, and VRAM before continuing.",
        ),
        InstallerStep(
            id="storage",
            title="Storage configuration",
            status="needs_input",
            summary="Choose the media volume and local NAS archive target.",
            next_step="Select a writable media path and a separate archive path.",
        ),
        InstallerStep(
            id="operator-account",
            title="Operator account creation",
            status="needs_input",
            summary="Create the first operator account for local administration.",
            next_step="Enter a named operator account before first broadcast.",
        ),
        InstallerStep(
            id="publish-targets",
            title="Profile-aware publish targets",
            status="needs_input",
            summary=(
                "Configure portal, Internet Archive, YouTube, local NAS, podcast, "
                "signed transcript, and subscriber notification checks."
            ),
            next_step="Use local proof adapters unless live credentials are approved.",
        ),
        InstallerStep(
            id="models",
            title="Model download with hash verification",
            status="ready",
            summary=(
                "Caption, summary, and translation model bundle hashes are available. "
                f"Commissioning provisions the adaptive summary default for this hardware "
                f"({summary_default}) plus the gemma4-e4b-ollama fallback, so the named "
                "default is present after install."
            ),
            next_step=(
                "Download the online bundle or use an offline bundle manifest. "
                "Accept the adaptive summary default or override it in AI Models."
            ),
        ),
        InstallerStep(
            id="health",
            title="First-run health check",
            status="ready",
            summary="Health check can verify all publish surfaces before broadcast.",
            next_step='Run the health check and wait for "You are streaming" confirmation.',
        ),
    ]
    return FirstRunPlan(
        profile=profile,
        recommended_tier=recommended_tier,
        time_to_first_broadcast_minutes=360,
        steps=steps,
    )


def run_first_health_check(profile: DeploymentProfile = "public-meetings") -> FirstRunHealthReport:
    """Return fail-closed first-run health checks for the v1.2 proof path."""

    portal_token = os.getenv("CIVICCAST_PORTAL_TOKEN")
    ia_key = os.getenv("CIVICCAST_INTERNET_ARCHIVE_ACCESS_KEY")
    youtube_secret = os.getenv("CIVICCAST_YOUTUBE_CLIENT_SECRET")
    nas_path = os.getenv("CIVICCAST_NAS_ARCHIVE_PATH")
    subscriber_secret = os.getenv("CIVICCAST_SUBSCRIBER_WEBHOOK_SECRET")

    checks = [
        _mtls_local_ca_check(),
        _external_target_check(
            check_id="portal",
            label="Resident portal",
            env_name="CIVICCAST_PORTAL_TOKEN",
            credential=portal_token,
            configured_message=(
                "Resident portal credential is configured, but live portal verification has not "
                "run in this first-run check."
            ),
            missing_message=(
                "Resident portal verification is blocked until a portal credential is available."
            ),
            configured_next_step=(
                "Run the live resident portal verification command and record redacted evidence "
                "before this lane can report ok."
            ),
        ),
        _external_target_check(
            check_id="internet-archive",
            label="Internet Archive",
            env_name="CIVICCAST_INTERNET_ARCHIVE_ACCESS_KEY",
            credential=ia_key,
            configured_message=(
                "Internet Archive credential is configured, but a live item upload has not been "
                "verified in this first-run check."
            ),
            missing_message=(
                "Internet Archive verification is blocked until live credentials are provided."
            ),
            configured_next_step=(
                "Run the live Internet Archive verification command, retain the item URL and "
                "hash, then rerun first-run verification."
            ),
        ),
        _external_target_check(
            check_id="youtube",
            label="YouTube",
            env_name="CIVICCAST_YOUTUBE_CLIENT_SECRET",
            credential=youtube_secret,
            configured_message=(
                "YouTube credential is configured, but live ingest or VOD verification has not "
                "run in this first-run check."
            ),
            missing_message="YouTube verification is blocked until OAuth credentials are provided.",
            configured_next_step=(
                "Run the live YouTube verification command and retain the unlisted proof URL "
                "before this lane can report ok."
            ),
        ),
        _local_nas_check(nas_path),
        HealthCheckItem(
            id="podcast",
            label="Podcast",
            state="ok",
            message="Podcast feed generation can be checked locally without external credentials.",
            next_step="Review podcast metadata before the first public publish.",
        ),
        HealthCheckItem(
            id="signed-transcript",
            label="Signed transcript",
            state="ok",
            message="Signed transcript verification can produce a local verification record.",
            next_step="Keep signing keys in the configured credential store.",
        ),
        _external_target_check(
            check_id="subscriber-notifications",
            label="Subscriber notifications",
            env_name="CIVICCAST_SUBSCRIBER_WEBHOOK_SECRET",
            credential=subscriber_secret,
            configured_message=(
                "Subscriber notification credential is configured, but double opt-in and delivery "
                "verification have not run in this first-run check."
            ),
            missing_message=(
                "Subscriber notification verification is blocked until delivery credentials exist."
            ),
            configured_next_step=(
                "Run the subscriber double opt-in and notification delivery verification, then "
                "record redacted evidence before this lane can report ok."
            ),
        ),
        _activitypub_health_check(),
    ]
    return FirstRunHealthReport(
        profile=profile,
        ready=all(check.state == "ok" for check in checks),
        checks=checks,
    )


def build_first_admin_setup_contract() -> FirstAdminSetupContract:
    """Return the v1.3 first-admin contract without generating credentials."""

    return FirstAdminSetupContract(
        status="contract_defined",
        identity_method="local_admin_password",
        required_fields=[
            FirstAdminRequiredField(
                id="station-name",
                label="Station name",
                help_text="The public name residents and staff recognize.",
            ),
            FirstAdminRequiredField(
                id="admin-display-name",
                label="Admin display name",
                help_text="The person responsible for setup and recovery.",
            ),
            FirstAdminRequiredField(
                id="admin-username",
                label="Admin username",
                help_text="The local sign-in name for the first admin account.",
            ),
            FirstAdminRequiredField(
                id="admin-password",
                label="Admin password",
                help_text="A local password or passkey secret created during setup.",
                secret=True,
            ),
            FirstAdminRequiredField(
                id="recovery-kit-destination",
                label="Where will you keep the recovery kit?",
                help_text=(
                    "A note for the station record of where the kit will be kept. "
                    "CivicCast does not save the kit to that location; save or print "
                    "the kit from the setup screen."
                ),
            ),
        ],
        recovery_kit=RecoveryKitContract(
            generated_during="first-admin-setup",
            media=["printable", "downloadable_text_file", "offline_copy"],
            contains=[
                "station identity",
                "admin account identifier",
                "one-time recovery codes",
                "recovery-code instructions",
                "credential-rotation instructions",
            ],
            excludes=[
                "bearer token values",
                "provider secret values",
                "private keys",
                "resident email addresses",
                "database passwords",
            ],
            operator_action=("Save or print the recovery kit before the station is marked ready."),
            rotation_path=(
                "If the kit is exposed, rotate admin credentials and provider secrets "
                "from the System Health recovery screen."
            ),
        ),
        supported_clients=[
            "operator console setup screen",
            "installer handoff screen",
            "civiccast CLI JSON for support automation",
        ],
        non_goals=[
            "full RBAC or SSO enforcement",
            "magic-link sign-in requirement",
            "external provider account creation",
        ],
        next_step=(
            "Use the first-admin setup endpoint or operator Setup screen to create "
            "the local admin, recovery kit, and one-time console handoff token."
        ),
    )


def build_safe_to_broadcast_contract() -> SafeToBroadcastContract:
    """Return the v1.3 safe-to-broadcast product contract."""

    return SafeToBroadcastContract(
        status="contract_defined",
        default_state="red",
        states=[
            SafeToBroadcastStateDefinition(
                color="green",
                label="Ready",
                meaning="Required checks passed for the selected meeting workflow.",
                operator_copy="You are ready to broadcast this meeting.",
            ),
            SafeToBroadcastStateDefinition(
                color="yellow",
                label="Check before meeting",
                meaning=(
                    "Required broadcast checks passed, but optional or recoverable "
                    "items need attention."
                ),
                operator_copy=(
                    "You can probably broadcast, but review the yellow items before "
                    "the meeting starts."
                ),
            ),
            SafeToBroadcastStateDefinition(
                color="red",
                label="Do not broadcast yet",
                meaning="A required broadcast check failed or has not run.",
                operator_copy=(
                    "Do not start the public broadcast until the required item is fixed."
                ),
            ),
        ],
        required_checks=[
            SafeToBroadcastCheckContract(
                id="source-preflight",
                label="Camera or meeting source",
                kind="required",
                failure_state="red",
                operator_message="The camera or source must show video and audio.",
                admin_message="Verify the live source contract, encoder URL, and preflight result.",
            ),
            SafeToBroadcastCheckContract(
                id="recording-path",
                label="Local recording",
                kind="required",
                failure_state="red",
                operator_message="CivicCast must be able to save the meeting.",
                admin_message="Verify durable media storage and write/read/delete proof.",
            ),
            SafeToBroadcastCheckContract(
                id="resident-portal",
                label="Resident portal",
                kind="required",
                failure_state="red",
                operator_message="Residents need a working place to watch the meeting.",
                admin_message="Verify portal publication and public playback health.",
            ),
            SafeToBroadcastCheckContract(
                id="station-policy",
                label="Station policy",
                kind="required",
                failure_state="red",
                operator_message="Required archive and caption policy checks must pass.",
                admin_message="Bind station policy to required archive, caption, and publish gates.",
            ),
        ],
        optional_checks=[
            SafeToBroadcastCheckContract(
                id="youtube",
                label="YouTube",
                kind="optional",
                failure_state="yellow",
                operator_message="YouTube is not set up yet; required portal broadcast can continue.",
                admin_message="Run the provider setup and live proof before marking YouTube ready.",
            ),
            SafeToBroadcastCheckContract(
                id="subscriber-notifications",
                label="Subscriber notifications",
                kind="optional",
                failure_state="yellow",
                operator_message="Subscriber notices can be sent after the record is published.",
                admin_message="Verify email or webhook delivery with redacted evidence.",
            ),
            SafeToBroadcastCheckContract(
                id="activitypub",
                label="ActivityPub federation",
                kind="optional",
                failure_state="yellow",
                operator_message="Federation is optional and off unless the station enables it.",
                admin_message="Use approval-only mode with authorized fetch before publicizing the actor.",
            ),
            SafeToBroadcastCheckContract(
                id="advanced-services",
                label="Advanced internal services",
                kind="advanced",
                failure_state="red",
                operator_message="This item needs IT help before the station can rely on it.",
                admin_message="Inspect mTLS, model runtime, and certificate readiness.",
            ),
        ],
        five_minutes_before_meeting=[
            "If the state is green, proceed with the normal start-broadcast flow.",
            "If the state is yellow, ignore optional reach surfaces unless station policy requires them.",
            "If the state is red, do not start the public broadcast; keep or start a local backup recording and call the admin.",
        ],
        non_goals=[
            "live external-provider proof without operator credentials",
            "full RBAC or SSO enforcement",
        ],
        next_step=(
            "Use System Health, provider readiness, source setup, backup, and "
            "update status to keep this report tied to real operator surfaces."
        ),
    )


def read_station_setup(*, console_url: str | None = None) -> StationSetupState:
    """Return current first-admin setup state without secret values."""

    return read_station_setup_state(operator_console_url=console_url or operator_console_url())


def acknowledge_station_recovery_kit(*, console_url: str | None = None) -> StationSetupState:
    """Record the operator's recovery-kit save/print confirmation."""

    return persist_recovery_kit_acknowledgement(
        operator_console_url=console_url or operator_console_url(),
    )


def _probed_summary_ram_gb() -> int:
    """Detected total RAM as an int (floor of the probed float), or 8 if probing fails.

    Coerced DOWN so a 15.9 GB box rounds to 15 -> e4b (never a 12B it cannot run). A
    failed probe falls back to 8 GB — the conservative e4b default — so commissioning
    is never blocked by hardware detection.
    """

    try:
        from civiccast.platform import hardware

        return int(hardware.probe().ram.total_gb)
    except Exception:
        return 8


def _probed_summary_has_gpu() -> bool:
    """Whether the box has a real (NVML-detected) GPU, or False if probing fails.

    False is the conservative/CPU-only assumption -- gates the 12B adaptive
    summary default off rather than risking it on a box the probe could not
    read. See ``seed_ai_model_default`` / ``detect_summary_model_default``
    for why this matters: a false positive here would reintroduce the
    RAM-only rule field evidence retired (12B failing on a 32GB CPU-only
    reference station).
    """

    try:
        from civiccast.platform import hardware

        return hardware.probe().gpu is not None
    except Exception:
        return False


def complete_first_admin_setup(
    request: FirstAdminSetupRequest,
    *,
    console_url: str | None = None,
) -> FirstAdminSetupResponse:
    """Create the first local admin identity and recovery kit.

    Also seeds the S13 adaptive summary-model default into station-state from the live
    hardware probe (S13 §6.1 step 1) so the operator console has a first-run default to
    fall back to before any durable DB selection exists. A seeding failure must never
    block first-admin setup, so it is best-effort.
    """

    response = persist_first_admin_setup(
        request,
        operator_console_url=console_url or operator_console_url(),
    )
    with suppress(Exception):
        seed_ai_model_default(
            system_ram_total_gb=_probed_summary_ram_gb(),
            has_gpu=_probed_summary_has_gpu(),
        )
    return response


def login_station_admin(
    request: StationLoginRequest,
    *,
    console_url: str | None = None,
) -> StationAuthResponse:
    """Authenticate the local first admin and rotate the console token."""

    return persist_station_login(
        request,
        operator_console_url=console_url or operator_console_url(),
    )


def recover_station_admin(
    request: StationRecoveryRequest,
    *,
    console_url: str | None = None,
) -> StationAuthResponse:
    """Consume a recovery code and reset the local first-admin password."""

    return persist_station_recovery(
        request,
        operator_console_url=console_url or operator_console_url(),
    )


def revoke_other_station_sessions(caller_token: str) -> RevokeOtherSessionsResponse:
    """Sign out every operator-console session except the one calling this.

    ``caller_token`` is the plain bearer token the caller's own already-
    authenticated request carried (see civiccast.installer.router, which
    resolves it from the same ``Authorization`` header
    ``require_any_role("setup_admin")`` already verified for this request).
    """

    revoked_count = persist_other_session_revocation(caller_token)
    if revoked_count == 0:
        message = "No other operator-console sessions were signed in. This browser stays signed in."
    else:
        noun = "session" if revoked_count == 1 else "sessions"
        message = (
            f"Signed out {revoked_count} other operator-console {noun}. "
            "This browser stays signed in."
        )
    return RevokeOtherSessionsResponse(
        status="revoked",
        revoked_count=revoked_count,
        message=message,
        next_step="Sign in again on any device that should still have access.",
    )


def regenerate_station_recovery_kit() -> RecoveryKitRegenerateResponse:
    """Mint a fresh recovery kit for the already-signed-in local admin.

    Requires the caller to already be authenticated -- enforced by the
    router's ``require_any_role("setup_admin")`` dependency, not by this
    function. This is the "I still have my password but lost/never-saved my
    codes" path, not a lockout bypass: every code from the previous kit stops
    working the instant this call succeeds.
    """

    recovery_kit = persist_recovery_kit_regeneration()
    return RecoveryKitRegenerateResponse(
        status="regenerated",
        recovery_kit=recovery_kit,
        next_step="Save or print this new recovery kit now -- it replaces every earlier one.",
    )


def build_backup_status(*, destination: str | None = None) -> BackupStatus:
    """Verify the configured backup destination with a real write probe."""

    generated_at = datetime.now(UTC)
    state = _load_ops_state()
    configured_destination = (
        destination
        or os.getenv("CIVICCAST_BACKUP_DIR")
        or _state_string(state, "backup", "destination")
    )
    if not configured_destination:
        return BackupStatus(
            generated_at=generated_at,
            status="not_set_up",
            message="Backup is not set up yet.",
            next_step="Choose a backup destination in Setup before the first public meeting.",
        )

    path_for_status = configured_destination.strip()
    try:
        path = _backup_destination_path(configured_destination)
        path_for_status = str(path)
        verified_path = _write_directory_probe(path, prefix=".civiccast-backup-probe")
    except (OSError, ValueError) as exc:
        if destination is not None:
            _record_backup_state(
                destination=path_for_status,
                status="needs_attention",
                last_probe_at=None,
            )
        return BackupStatus(
            generated_at=generated_at,
            status="needs_attention",
            destination=path_for_status,
            message=f"CivicCast could not verify backup storage: {exc}.",
            next_step=(
                "Choose a folder CivicCast can write to, or ask IT to fix the "
                "backup destination permissions."
            ),
        )

    probe_at = datetime.now(UTC)
    _record_backup_state(
        destination=str(verified_path),
        status="ready",
        last_probe_at=probe_at,
    )
    return BackupStatus(
        generated_at=generated_at,
        status="ready",
        destination=str(verified_path),
        last_probe_at=probe_at,
        last_backup_at=_state_datetime(state, "backup", "last_backup_at"),
        message="Backup destination accepted a write/read/delete proof.",
        next_step="Keep this backup drive or folder available before public meetings.",
    )


def configure_backup(request: BackupSetupRequest) -> BackupStatus:
    """Set and verify the operator-selected backup destination."""

    return build_backup_status(destination=request.destination)


def _backup_destination_path(destination: str) -> Path:
    """Return the concrete runtime path used for backup probes.

    Used to also translate a Windows-style drive path (``C:\\...``) into its
    mounted equivalent when the control plane was itself running as a Linux
    process inside a WSL2 guest (``CIVICAST_WSL_DRIVE_MOUNT_ROOT``,
    ``CIVICAST_TRANSLATE_WINDOWS_BACKUP_PATHS``) -- the retired WSL product's
    own control plane. The native product's control plane runs directly on
    Windows (``os.name == "nt"``), so a Windows-style path was already used
    as-is there before this simplification; nothing changes for it.
    """

    candidate = destination.strip()
    if not candidate:
        raise ValueError("Choose a backup destination.")
    if _WSL_STYLE_PATH_RE.match(candidate):
        raise ValueError(
            "This looks like a WSL/Linux-style path (...\\mnt\\c\\... or /mnt/c/...), which "
            "does not exist on this Windows station and would mean nothing is actually being "
            "backed up. Use a real Windows path instead, for example C:\\CivicCastBackups or "
            "D:\\CivicCastBackups."
        )
    if _WINDOWS_DRIVE_RELATIVE_RE.match(candidate):
        raise ValueError(
            "Use an absolute Windows path such as C:\\CivicCastBackups, not a drive-relative path."
        )
    return Path(candidate).expanduser()


def save_provider_credentials(
    request: ProviderCredentialSetupRequest,
) -> ProviderCredentialSetupResponse:
    """Persist provider credentials locally and return only redacted metadata."""

    fields = _PROVIDER_CREDENTIAL_FIELDS.get(request.provider_id)
    if fields is None:
        raise ValueError("Choose a supported provider.")
    allowed = {field.id: field for field in fields}
    required = {field.id for field in fields if field.required}
    submitted = {key: value.strip() for key, value in request.values.items()}
    unknown = sorted(set(submitted) - set(allowed))
    if unknown:
        raise ValueError(f"Unsupported field for this provider: {', '.join(unknown)}.")
    missing = sorted(field_id for field_id in required if not submitted.get(field_id))
    if missing:
        raise ValueError(f"Enter all required provider fields: {', '.join(missing)}.")
    for key, value in submitted.items():
        if not value:
            continue
        if len(value) > 4000 or any(ord(ch) < 32 for ch in value):
            raise ValueError(f"{allowed[key].label} contains unsupported characters.")

    saved_at = datetime.now(UTC)
    handle = _provider_credential_handle(request.provider_id)

    def mutate(payload: dict[str, Any]) -> None:
        providers = payload.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            payload["providers"] = providers
        providers[request.provider_id] = {
            "saved_at": saved_at.isoformat(),
            "fields": submitted,
        }

    _mutate_provider_credentials(mutate)
    return ProviderCredentialSetupResponse(
        status="stored",
        provider_id=request.provider_id,
        credential_handle=handle,
        configured_fields=sorted(submitted),
        redacted_fields=sorted(field_id for field_id, field in allowed.items() if field.secret),
        message="Provider details were saved locally. Secret values will not be shown again.",
        next_step="Run provider readiness again, then run live proof before claiming this provider is ready.",
    )


def record_provider_proof(request: ProviderProofRecordRequest) -> ProviderProofRecordResponse:
    """Persist a redacted provider proof reference without storing provider secrets."""

    provider_id = _installer_provider_id_for_proof(request.provider_id)
    if provider_id is None:
        raise ValueError("Choose a supported provider proof lane.")
    if _PROVIDER_CREDENTIAL_FIELDS.get(provider_id) and not _provider_has_any_configuration(
        provider_id
    ):
        raise ValueError("Save provider credentials before recording live proof evidence.")
    evidence_reference = request.evidence_reference.strip()
    if not evidence_reference:
        raise ValueError("Enter a proof file path, URL, or release evidence reference.")
    if len(evidence_reference) > 500 or any(ord(ch) < 32 for ch in evidence_reference):
        raise ValueError("Proof evidence reference contains unsupported characters.")
    lowered = evidence_reference.lower()
    if any(marker in lowered for marker in ("token=", "secret=", "password=", "private_key=")):
        raise ValueError("Proof evidence reference appears to include a secret value.")

    recorded_at = datetime.now(UTC)
    proof_providers = _INSTALLER_PROVIDER_PROOF_PROVIDERS.get(provider_id, (request.provider_id,))

    def mutate(payload: dict[str, Any]) -> None:
        proofs = payload.setdefault("proofs", {})
        if not isinstance(proofs, dict):
            proofs = {}
            payload["proofs"] = proofs
        proofs[provider_id] = {
            "recorded_at": recorded_at.isoformat(),
            "evidence_reference": evidence_reference,
            "redaction_reviewed": True,
            "reviewer_note": request.reviewer_note or "",
            "proof_providers": list(proof_providers),
        }

    _mutate_provider_proofs(mutate)
    readiness = _provider_proof_readiness(provider_id=provider_id)
    return ProviderProofRecordResponse(
        status="stored",
        provider_id=provider_id,
        proof_status="proof_passed"
        if readiness["proof_status"] == "proof_passed"
        else "needs_live_proof",
        evidence_reference=evidence_reference,
        recorded_at=recorded_at,
        message="Redacted provider proof evidence was saved locally.",
        next_step="Review provider readiness and keep the evidence with release proof artifacts.",
    )


def _real_drill_summary() -> str | None:
    """The last REAL disaster-recovery drill result recorded by civiccast.dr.

    Distinct from the proof-token rehearsal below: this reflects an actual
    ``civiccast dr run-drill`` / ``POST .../dr/run-drill`` run that backed up
    the real database, restored it into a fresh database, and verified it.
    """

    return _state_string(_load_ops_state(), "restore", "real_drill_summary")


def build_restore_status() -> RestoreStatus:
    """Return honest restore status.

    Historical builds recorded a manifest-copy storage probe as a completed
    restore rehearsal.  That timestamp is intentionally ignored here: only
    the separate real DR drill may be described as a restore, and its current
    media/config boundaries still keep the full station restore status from
    turning green.
    """

    plan_steps = _restore_proof_plan_steps()
    real_drill_summary = _real_drill_summary()
    if real_drill_summary is not None:
        return RestoreStatus(
            generated_at=datetime.now(UTC),
            status="needs_attention",
            proof_summary=real_drill_summary,
            proof_items=_restore_proof_items(state="pending"),
            excluded_items=_restore_excluded_items(),
            plan_steps=plan_steps,
            message=(
                "A real database restore drill has run, but file-level media, "
                "configuration, and credential-metadata restore are not yet proven."
            ),
            next_step=(
                "Keep independent media and configuration backups, then run a full "
                "station restore on an isolated machine before relying on recovery."
            ),
            real_drill_summary=real_drill_summary,
        )
    backup = build_backup_status()
    if backup.status == "not_set_up":
        return RestoreStatus(
            generated_at=datetime.now(UTC),
            status="needs_attention",
            proof_items=_restore_proof_items(state="pending"),
            excluded_items=_restore_excluded_items(),
            plan_steps=plan_steps,
            message="Restore cannot be rehearsed until backup is set up.",
            next_step="Choose a backup destination, then run a restore rehearsal.",
            real_drill_summary=real_drill_summary,
        )
    return RestoreStatus(
        generated_at=datetime.now(UTC),
        status="not_tested",
        proof_items=_restore_proof_items(state="pending"),
        excluded_items=_restore_excluded_items(),
        plan_steps=plan_steps,
        message="Backup storage is available, but restore has not been rehearsed yet.",
        next_step="Run a private restore rehearsal before relying on this station for records.",
        real_drill_summary=real_drill_summary,
    )


def run_restore_rehearsal() -> RestoreStatus:
    """Check backup-storage round trip without claiming a station restore.

    This intentionally remains a small write/copy/checksum probe.  The real
    database restore drill is ``run_dr_drill``; neither operation currently
    restores the complete media/configuration footprint, so this function
    must never mark the full restore checklist passed.
    """

    backup = build_backup_status()
    if backup.status != "ready" or backup.destination is None:
        return RestoreStatus(
            generated_at=datetime.now(UTC),
            status="needs_attention",
            proof_items=_restore_proof_items(state="pending"),
            excluded_items=_restore_excluded_items(),
            plan_steps=_restore_proof_plan_steps(),
            message="Restore rehearsal is blocked until backup storage is ready.",
            next_step="Choose and verify a backup destination in Setup, then run restore rehearsal.",
        )

    backup_dir = Path(backup.destination).expanduser().resolve()
    rehearsal_id = "restore-" + uuid4().hex[:12]
    proof_items = _restore_proof_items(state="pending")
    payload = {
        "schema_version": 1,
        "rehearsal_id": rehearsal_id,
        "created_at": datetime.now(UTC).isoformat(),
        "purpose": "CivicCast backup-storage round-trip probe",
        "target_profile": "isolated-station-profile",
        "restore_scope": [item.id for item in proof_items if item.required],
        "proof_items": [item.model_dump(mode="json") for item in proof_items],
        "excluded_items": _restore_excluded_items(),
    }
    proof_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    expected_hash = sha256(proof_bytes).hexdigest()
    proof_path = backup_dir / f".civiccast-{rehearsal_id}.json"
    try:
        proof_path.write_bytes(proof_bytes)
        with tempfile.TemporaryDirectory(prefix="civiccast-restore-rehearsal-") as temp_dir:
            restored_path = Path(temp_dir) / proof_path.name
            shutil.copy2(proof_path, restored_path)
            observed_hash = sha256(restored_path.read_bytes()).hexdigest()
            if observed_hash != expected_hash:
                raise OSError("restore rehearsal checksum changed after copy")
            restored_payload = json.loads(restored_path.read_text(encoding="utf-8"))
            if restored_payload.get("restore_scope") != payload["restore_scope"]:
                raise OSError("restore rehearsal scope changed after isolated copy")
            restored_items = restored_payload.get("proof_items")
            if not isinstance(restored_items, list) or len(restored_items) != len(proof_items):
                raise OSError("restore rehearsal proof item manifest changed after isolated copy")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return RestoreStatus(
            generated_at=datetime.now(UTC),
            status="needs_attention",
            proof_items=_restore_proof_items(state="needs_attention"),
            excluded_items=_restore_excluded_items(),
            plan_steps=_restore_proof_plan_steps(),
            message=f"Restore rehearsal could not complete: {exc}.",
            next_step=(
                "Check backup destination permissions and available space, then run "
                "restore rehearsal again."
            ),
        )
    finally:
        with suppress(FileNotFoundError):
            proof_path.unlink()

    proof_at = datetime.now(UTC)
    proof_summary = (
        "Backup storage accepted a manifest write/copy/checksum round trip and "
        "temporary proof files were cleaned up. This did not restore the station "
        "database, media, configuration, captions, records, publish state, or credentials."
    )
    return RestoreStatus(
        generated_at=proof_at,
        status="needs_attention",
        proof_summary=proof_summary,
        proof_items=_restore_proof_items(state="pending"),
        excluded_items=_restore_excluded_items(),
        plan_steps=_restore_proof_plan_steps(),
        message=(
            "Backup storage passed its round-trip check; an actual full station "
            "restore has not been run."
        ),
        next_step=(
            "Run the real database disaster-recovery drill, keep independent media "
            "and configuration backups, and prove a full restore on an isolated machine."
        ),
        real_drill_summary=_real_drill_summary(),
    )


def run_dr_drill() -> DrillReport:
    """Run the REAL 0.5.0 disaster-recovery drill (0.5.0 gate).

    Backs up the actual configured database, restores it into a completely
    fresh database, and verifies it (row counts, checksums, app-store
    read-through, plus extensions/sequences on Postgres), then runs the
    daemon crash-recovery drill. The result is recorded into ops-state so
    ``build_restore_status``/``run_restore_rehearsal`` surface it via
    ``RestoreStatus.real_drill_summary``.

    Raises :class:`ValueError` if there is no ``DATABASE_URL`` configured, or
    if its scheme is neither SQLite nor Postgres.
    """

    from civiccast.dr.report import run_full_drill

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("No DATABASE_URL is configured; nothing to drill yet.")
    if not (database_url.startswith("sqlite") or database_url.startswith("postgresql")):
        raise ValueError(
            "Unsupported DATABASE_URL scheme for the DR drill; use sqlite:// or postgresql://."
        )

    backup_status = build_backup_status()
    if backup_status.destination is None:
        raise ValueError("Choose and verify a backup destination before running the DR drill.")
    backup_root = Path(backup_status.destination).expanduser().resolve()

    upload_dir = load_managed_upload_dir()
    report = run_full_drill(
        database_url=database_url,
        backup_dir=backup_root / "dr-drill" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        work_dir=Path(tempfile.gettempdir()) / "civiccast-dr-drill-work",
        media_root=Path(upload_dir) if upload_dir else None,
    )
    verdict = "PASSED" if report.ok else "FAILED"
    summary = (
        f"Real DR drill {verdict} at {report.generated_at.isoformat()}: "
        f"{len(report.restore.tables)} tables verified, "
        f"schema_ok={report.restore.schema_ok}, "
        f"crash-recovery {'passed' if report.crash.ok else 'FAILED'}."
    )
    _record_dr_drill_state(summary=summary)
    return report


def build_update_rollback_status() -> UpdateRollbackStatus:
    """Return operator-facing update and rollback readiness."""

    state = _load_ops_state()
    last_preflight_at = _state_datetime(state, "update", "last_preflight_at")
    preflight_current_version = _state_string(state, "update", "current_version")
    preflight_available_version = _state_string(state, "update", "available_version")
    raw_checkpoint_summary = _state_string(state, "update", "checkpoint_summary")
    last_rollback_test_at = _state_datetime(state, "update", "last_rollback_test_at")
    rollback_proof_summary = _state_string(state, "update", "rollback_proof_summary")
    last_post_update_proof_at = _state_datetime(state, "update", "last_post_update_proof_at")
    post_update_proof_summary = _state_string(state, "update", "post_update_proof_summary")
    post_update_proof_state: PostUpdateProofState = (
        "passed" if last_post_update_proof_at is not None else "not_run"
    )
    last_failed_update_rollback_at = _state_datetime(
        state, "update", "last_failed_update_rollback_at"
    )
    failed_update_rollback_summary = _state_string(
        state, "update", "failed_update_rollback_summary"
    )
    window_current_version = _state_string(state, "update", "maintenance_current_version")
    window_available_version = _state_string(state, "update", "maintenance_available_version")
    raw_maintenance_summary = _state_string(state, "update", "maintenance_window_summary")
    raw_maintenance_expires_at = _state_datetime(state, "update", "maintenance_window_expires_at")
    available_version = (
        os.getenv("CIVICCAST_AVAILABLE_VERSION")
        or os.getenv("CIVICCAST_TESTER_AVAILABLE_VERSION")
        or None
    )
    update_is_available = available_version is not None and available_version != __version__
    maintenance_matches_available = (
        raw_maintenance_expires_at is not None
        and window_current_version == __version__
        and window_available_version == available_version
        and update_is_available
    )
    maintenance_window_expires_at = (
        raw_maintenance_expires_at if maintenance_matches_available else None
    )
    maintenance_window_summary = raw_maintenance_summary if maintenance_matches_available else None
    maintenance_window_state: MaintenanceWindowState = "closed"
    if maintenance_window_expires_at is not None:
        maintenance_window_state = (
            "open" if maintenance_window_expires_at >= datetime.now(UTC) else "expired"
        )
    preflight_matches_available = (
        last_preflight_at is not None
        and preflight_current_version == __version__
        and preflight_available_version == available_version
    )
    matching_preflight_at = last_preflight_at if preflight_matches_available else None
    checkpoint_summary = raw_checkpoint_summary if preflight_matches_available else None
    rollback_path = os.getenv("CIVICCAST_ROLLBACK_ARTIFACT_PATH") or _state_string(
        state, "update", "rollback_artifact"
    )
    rollback_artifact_path = Path(rollback_path).expanduser() if rollback_path else None
    rollback_artifact = (
        str(rollback_artifact_path)
        if rollback_artifact_path is not None and rollback_artifact_path.exists()
        else None
    )
    rollback_available = rollback_artifact is not None
    recorded_rollback_sha256 = _state_string(state, "update", "rollback_artifact_sha256")
    rollback_artifact_sha256 = None
    if rollback_artifact_path is not None and rollback_available:
        with suppress(OSError):
            rollback_artifact_sha256 = _file_sha256(rollback_artifact_path)
    rollback_hash_matches = (
        recorded_rollback_sha256 is not None
        and rollback_artifact_sha256 == recorded_rollback_sha256
    )
    rollback_proof_state: RollbackProofState = (
        "passed"
        if rollback_available and last_rollback_test_at is not None and rollback_hash_matches
        else "needs_attention"
        if rollback_available and last_rollback_test_at is not None
        else "not_tested"
        if rollback_available
        else "not_configured"
    )
    failed_update_rollback_state: FailedUpdateRollbackProofState = (
        "passed"
        if last_failed_update_rollback_at is not None and rollback_hash_matches
        else "needs_attention"
        if last_failed_update_rollback_at is not None
        else "not_run"
    )
    backup = build_backup_status()
    restore = build_restore_status()
    plan_steps = _update_rollback_plan_steps(rollback_available=rollback_available)
    if update_is_available and backup.status != "ready":
        return UpdateRollbackStatus(
            generated_at=datetime.now(UTC),
            current_version=__version__,
            available_version=available_version,
            status="needs_attention",
            migration_state="Set up backup before applying a tester update.",
            rollback_available=rollback_available,
            rollback_artifact=rollback_artifact,
            rollback_artifact_sha256=rollback_artifact_sha256,
            rollback_proof_state=rollback_proof_state,
            last_rollback_test_at=last_rollback_test_at,
            rollback_proof_summary=rollback_proof_summary,
            post_update_proof_state=post_update_proof_state,
            last_post_update_proof_at=last_post_update_proof_at,
            post_update_proof_summary=post_update_proof_summary,
            maintenance_window_state=maintenance_window_state,
            maintenance_window_expires_at=maintenance_window_expires_at,
            maintenance_window_summary=maintenance_window_summary,
            failed_update_rollback_state=failed_update_rollback_state,
            last_failed_update_rollback_at=last_failed_update_rollback_at,
            failed_update_rollback_summary=failed_update_rollback_summary,
            safe_to_apply=False,
            last_preflight_at=matching_preflight_at,
            checkpoint_summary=checkpoint_summary,
            plan_steps=plan_steps,
            message="An update is available, but backup readiness has not passed.",
            next_step="Set up backup first, then review the update again.",
        )
    if update_is_available and restore.status != "passed":
        return UpdateRollbackStatus(
            generated_at=datetime.now(UTC),
            current_version=__version__,
            available_version=available_version,
            status="needs_attention",
            migration_state="Run restore rehearsal before applying a tester update.",
            rollback_available=rollback_available,
            rollback_artifact=rollback_artifact,
            rollback_artifact_sha256=rollback_artifact_sha256,
            rollback_proof_state=rollback_proof_state,
            last_rollback_test_at=last_rollback_test_at,
            rollback_proof_summary=rollback_proof_summary,
            post_update_proof_state=post_update_proof_state,
            last_post_update_proof_at=last_post_update_proof_at,
            post_update_proof_summary=post_update_proof_summary,
            maintenance_window_state=maintenance_window_state,
            maintenance_window_expires_at=maintenance_window_expires_at,
            maintenance_window_summary=maintenance_window_summary,
            failed_update_rollback_state=failed_update_rollback_state,
            last_failed_update_rollback_at=last_failed_update_rollback_at,
            failed_update_rollback_summary=failed_update_rollback_summary,
            safe_to_apply=False,
            last_preflight_at=matching_preflight_at,
            checkpoint_summary=checkpoint_summary,
            plan_steps=plan_steps,
            message="An update is available, but restore proof has not passed.",
            next_step="Run restore rehearsal, then run update preflight.",
        )
    if update_is_available:
        preflight_ready = matching_preflight_at is not None
        safe_to_apply = preflight_ready and maintenance_window_state == "open"
        if safe_to_apply:
            migration_state = "Update preflight and maintenance window are active."
            next_step = (
                "Close the operator console, apply the update package, then run post-update proof."
            )
        elif not preflight_ready:
            migration_state = "Backup and restore proof passed; update preflight has not run."
            next_step = "Run update preflight before applying this update."
        elif maintenance_window_state == "expired":
            migration_state = "Update preflight passed, but the maintenance window expired."
            next_step = "Open a fresh maintenance window before applying this update."
        else:
            migration_state = "Update preflight passed; maintenance window is closed."
            next_step = "Open the maintenance window before applying this update."
        return UpdateRollbackStatus(
            generated_at=datetime.now(UTC),
            current_version=__version__,
            available_version=available_version,
            status="update_available",
            migration_state=migration_state,
            rollback_available=rollback_available,
            rollback_artifact=rollback_artifact,
            rollback_artifact_sha256=rollback_artifact_sha256,
            rollback_proof_state=rollback_proof_state,
            last_rollback_test_at=last_rollback_test_at,
            rollback_proof_summary=rollback_proof_summary,
            post_update_proof_state=post_update_proof_state,
            last_post_update_proof_at=last_post_update_proof_at,
            post_update_proof_summary=post_update_proof_summary,
            maintenance_window_state=maintenance_window_state,
            maintenance_window_expires_at=maintenance_window_expires_at,
            maintenance_window_summary=maintenance_window_summary,
            failed_update_rollback_state=failed_update_rollback_state,
            last_failed_update_rollback_at=last_failed_update_rollback_at,
            failed_update_rollback_summary=failed_update_rollback_summary,
            safe_to_apply=safe_to_apply,
            last_preflight_at=matching_preflight_at,
            checkpoint_summary=checkpoint_summary,
            plan_steps=plan_steps,
            message=f"CivicCast {available_version} is available for tester review.",
            next_step=next_step,
        )
    return UpdateRollbackStatus(
        generated_at=datetime.now(UTC),
        current_version=__version__,
        available_version=available_version,
        status="current",
        migration_state=(
            "Backup readiness passed for the current station state."
            if backup.status == "ready"
            else "Set up backup before applying future updates."
        ),
        rollback_available=rollback_available,
        rollback_artifact=rollback_artifact,
        rollback_artifact_sha256=rollback_artifact_sha256,
        rollback_proof_state=rollback_proof_state,
        last_rollback_test_at=last_rollback_test_at,
        rollback_proof_summary=rollback_proof_summary,
        post_update_proof_state=post_update_proof_state,
        last_post_update_proof_at=last_post_update_proof_at,
        post_update_proof_summary=post_update_proof_summary,
        maintenance_window_state=maintenance_window_state,
        maintenance_window_expires_at=maintenance_window_expires_at,
        maintenance_window_summary=maintenance_window_summary,
        failed_update_rollback_state=failed_update_rollback_state,
        last_failed_update_rollback_at=last_failed_update_rollback_at,
        failed_update_rollback_summary=failed_update_rollback_summary,
        safe_to_apply=False,
        last_preflight_at=None,
        checkpoint_summary=None,
        plan_steps=plan_steps,
        message="CivicCast is on the current tester version.",
        next_step="Before each public meeting, confirm Safe to broadcast after any update.",
    )


def run_update_preflight() -> UpdateRollbackStatus:
    """Write and verify an update checkpoint before a tester update is applied."""

    status = build_update_rollback_status()
    if not status.available_version or status.available_version == status.current_version:
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            safe_to_apply=False,
            message="No newer tester update is available for preflight.",
            next_step="Set CIVICCAST_AVAILABLE_VERSION only when a reviewed update package exists.",
        )
    if status.status == "needs_attention":
        return status

    backup = build_backup_status()
    restore = build_restore_status()
    if backup.destination is None:
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            safe_to_apply=False,
            message="Update preflight is blocked because backup storage is missing.",
            next_step="Set up backup first, then rerun update preflight.",
        )

    backup_dir = Path(backup.destination).expanduser().resolve()
    checkpoint_id = "update-checkpoint-" + uuid4().hex[:12]
    payload = {
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "created_at": datetime.now(UTC).isoformat(),
        "purpose": "CivicCast safe update preflight checkpoint",
        "current_version": status.current_version,
        "available_version": status.available_version,
        "restore_last_passed_at": restore.last_restore_test_at.isoformat()
        if restore.last_restore_test_at
        else None,
        "checks": [
            "backup-ready",
            "restore-proof-passed",
            "rollback-artifact-recorded"
            if status.rollback_available
            else "rollback-artifact-not-configured",
            "post-update-safe-to-broadcast-required",
        ],
    }
    checkpoint_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    expected_hash = sha256(checkpoint_bytes).hexdigest()
    checkpoint_path = backup_dir / f".civiccast-{checkpoint_id}.json"
    try:
        checkpoint_path.write_bytes(checkpoint_bytes)
        with tempfile.TemporaryDirectory(prefix="civiccast-update-preflight-") as temp_dir:
            copied_path = Path(temp_dir) / checkpoint_path.name
            shutil.copy2(checkpoint_path, copied_path)
            observed_hash = sha256(copied_path.read_bytes()).hexdigest()
            if observed_hash != expected_hash:
                raise OSError("update preflight checkpoint checksum changed after copy")
    except OSError as exc:
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            safe_to_apply=False,
            message=f"Update preflight could not complete: {exc}.",
            next_step="Check backup permissions and free space, then rerun update preflight.",
        )
    finally:
        with suppress(FileNotFoundError):
            checkpoint_path.unlink()

    preflight_at = datetime.now(UTC)
    summary = (
        f"CivicCast wrote and verified update checkpoint {checkpoint_id} for "
        f"{status.current_version} -> {status.available_version}."
    )
    _record_update_preflight_state(
        last_preflight_at=preflight_at,
        current_version=status.current_version,
        available_version=status.available_version,
        checkpoint_summary=summary,
    )
    return build_update_rollback_status()


def run_maintenance_window_open(
    request: UpdateMaintenanceWindowRequest,
) -> UpdateRollbackStatus:
    """Open a time-boxed operator-visible window for applying a tester update."""

    status = build_update_rollback_status()
    if not status.available_version or status.available_version == status.current_version:
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            safe_to_apply=False,
            message="Maintenance window is blocked because no newer tester update is available.",
            next_step="Set CIVICCAST_AVAILABLE_VERSION only when a reviewed update package exists.",
        )
    if status.status == "needs_attention":
        return status
    if status.last_preflight_at is None or status.checkpoint_summary is None:
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            safe_to_apply=False,
            message="Maintenance window is blocked because update preflight has not passed.",
            next_step="Run update preflight, then open the maintenance window.",
        )
    if status.rollback_proof_state != "passed":
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            safe_to_apply=False,
            message="Maintenance window is blocked because rollback proof has not passed.",
            next_step="Choose a rollback artifact and run rollback rehearsal first.",
        )

    opened_at = datetime.now(UTC)
    expires_at = opened_at + timedelta(minutes=request.duration_minutes)
    summary = (
        f"Maintenance window opened for {status.current_version} -> "
        f"{status.available_version} until {expires_at.isoformat()}."
    )
    _record_maintenance_window_state(
        current_version=status.current_version,
        available_version=status.available_version,
        maintenance_window_expires_at=expires_at,
        maintenance_window_summary=summary,
    )
    return build_update_rollback_status()


def configure_rollback_artifact(request: RollbackArtifactRequest) -> UpdateRollbackStatus:
    """Record a local rollback artifact after verifying its bytes are readable."""

    artifact = Path(request.artifact_path).expanduser().resolve()
    if not artifact.is_file():
        return _replace_update_rollback_status(
            build_update_rollback_status(),
            status="needs_attention",
            rollback_available=False,
            rollback_artifact=None,
            rollback_artifact_sha256=None,
            rollback_proof_state="not_configured",
            message="Rollback artifact could not be verified because the file was not found.",
            next_step="Choose a readable rollback installer or package artifact.",
        )
    digest = _file_sha256(artifact)
    _record_rollback_artifact_state(
        artifact_path=str(artifact),
        artifact_sha256=digest,
    )
    return build_update_rollback_status()


def run_rollback_rehearsal() -> UpdateRollbackStatus:
    """Verify the configured rollback artifact without applying it."""

    status = build_update_rollback_status()
    if not status.rollback_artifact:
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            rollback_available=False,
            rollback_proof_state="not_configured",
            safe_to_apply=False,
            message="Rollback rehearsal is blocked because no rollback artifact is configured.",
            next_step="Choose a readable rollback installer or package artifact first.",
        )
    artifact = Path(status.rollback_artifact).expanduser().resolve()
    if not artifact.is_file():
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            rollback_available=False,
            rollback_proof_state="needs_attention",
            safe_to_apply=False,
            message="Rollback rehearsal is blocked because the configured artifact is missing.",
            next_step="Choose the rollback artifact again, then rerun rollback rehearsal.",
        )
    expected_hash = _file_sha256(artifact)
    try:
        with tempfile.TemporaryDirectory(prefix="civiccast-rollback-rehearsal-") as temp_dir:
            copied_path = Path(temp_dir) / artifact.name
            shutil.copy2(artifact, copied_path)
            observed_hash = _file_sha256(copied_path)
            if observed_hash != expected_hash:
                raise OSError("rollback artifact checksum changed after isolated copy")
    except OSError as exc:
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            rollback_proof_state="needs_attention",
            safe_to_apply=False,
            message=f"Rollback rehearsal could not complete: {exc}.",
            next_step="Check rollback artifact permissions and storage, then rerun rollback rehearsal.",
        )

    proof_at = datetime.now(UTC)
    summary = (
        f"CivicCast verified rollback artifact {artifact.name} with SHA-256 "
        f"{expected_hash} in an isolated rehearsal copy."
    )
    _record_rollback_rehearsal_state(
        last_rollback_test_at=proof_at,
        rollback_proof_summary=summary,
        artifact_sha256=expected_hash,
    )
    return build_update_rollback_status()


def run_failed_update_rollback_rehearsal() -> UpdateRollbackStatus:
    """Prove rollback from a controlled failed-update scenario without applying it."""

    status = build_update_rollback_status()
    if status.rollback_proof_state != "passed" or not status.rollback_artifact:
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            failed_update_rollback_state="needs_attention",
            safe_to_apply=False,
            message="Failed-update rehearsal is blocked because rollback proof has not passed.",
            next_step="Choose a rollback artifact and run rollback rehearsal first.",
        )

    backup = build_backup_status()
    if backup.destination is None:
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            failed_update_rollback_state="needs_attention",
            safe_to_apply=False,
            message="Failed-update rehearsal is blocked because backup storage is missing.",
            next_step="Set up backup first, then rerun failed-update rehearsal.",
        )

    artifact = Path(status.rollback_artifact).expanduser().resolve()
    if not artifact.is_file():
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            failed_update_rollback_state="needs_attention",
            safe_to_apply=False,
            message="Failed-update rehearsal is blocked because the rollback artifact is missing.",
            next_step="Choose the rollback artifact again, then rerun rollback rehearsal.",
        )

    expected_hash = _file_sha256(artifact)
    proof_id = "failed-update-rollback-" + uuid4().hex[:12]
    proof_payload = {
        "schema_version": 1,
        "proof_id": proof_id,
        "created_at": datetime.now(UTC).isoformat(),
        "purpose": "CivicCast controlled failed-update rollback rehearsal",
        "current_version": status.current_version,
        "available_version": status.available_version,
        "simulated_failure": "controlled-update-startup-check-failed",
        "rollback_artifact": artifact.name,
        "rollback_artifact_sha256": expected_hash,
        "excluded": [
            "provider secret values",
            "staff bearer token values",
            "admin password and recovery code plaintext",
        ],
    }
    proof_bytes = json.dumps(proof_payload, sort_keys=True).encode("utf-8")
    expected_proof_hash = sha256(proof_bytes).hexdigest()
    backup_dir = Path(backup.destination).expanduser().resolve()
    proof_path = backup_dir / f".civiccast-{proof_id}.json"
    try:
        proof_path.write_bytes(proof_bytes)
        with tempfile.TemporaryDirectory(prefix="civiccast-failed-update-rollback-") as temp_dir:
            temp_path = Path(temp_dir)
            copied_artifact = temp_path / artifact.name
            copied_proof = temp_path / proof_path.name
            shutil.copy2(artifact, copied_artifact)
            shutil.copy2(proof_path, copied_proof)
            if _file_sha256(copied_artifact) != expected_hash:
                raise OSError("rollback artifact checksum changed in failed-update rehearsal")
            if sha256(copied_proof.read_bytes()).hexdigest() != expected_proof_hash:
                raise OSError("failed-update proof checksum changed after isolated copy")
    except OSError as exc:
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            failed_update_rollback_state="needs_attention",
            safe_to_apply=False,
            message=f"Failed-update rehearsal could not complete: {exc}.",
            next_step="Check backup and rollback artifact permissions, then rerun rehearsal.",
        )
    finally:
        with suppress(FileNotFoundError):
            proof_path.unlink()

    proof_at = datetime.now(UTC)
    summary = (
        f"CivicCast simulated failed update {proof_id}, verified rollback artifact "
        f"{artifact.name}, and removed temporary proof files."
    )
    _record_failed_update_rollback_state(
        last_failed_update_rollback_at=proof_at,
        failed_update_rollback_summary=summary,
        artifact_sha256=expected_hash,
    )
    return build_update_rollback_status()


def run_post_update_proof() -> UpdateRollbackStatus:
    """Record post-update Safe to broadcast proof when the station is green."""

    status = build_update_rollback_status()
    report = build_system_health_report(
        live_preflight_ready=True,
        recording_write_probe_ready=True,
        resident_preview_confirmed=True,
    )
    if report.safe_to_broadcast != "green":
        return _replace_update_rollback_status(
            status,
            status="needs_attention",
            post_update_proof_state="needs_attention",
            safe_to_apply=False,
            message="Post-update proof failed because Safe to broadcast is not green.",
            next_step="Resolve System Health required checks, then rerun post-update proof.",
        )

    proof_at = datetime.now(UTC)
    summary = (
        f"Post-update Safe to broadcast proof passed at {proof_at.isoformat()} "
        f"with label: {report.label}."
    )
    _record_post_update_proof_state(
        last_post_update_proof_at=proof_at,
        post_update_proof_summary=summary,
    )
    return build_update_rollback_status()


def _replace_update_rollback_status(
    current: UpdateRollbackStatus,
    **updates: Any,
) -> UpdateRollbackStatus:
    payload = current.model_dump()
    payload.update(updates)
    return UpdateRollbackStatus(**payload)


def _restore_proof_plan_steps() -> list[str]:
    """Operator-facing restore proof plan for v1.5 resilience beta readiness."""

    return [
        "Use an isolated station profile; never restore over the active meeting station.",
        "Prove backup storage is writable, readable, and checksum-stable before restore.",
        "Restore database, media, config, station profile, schedules, captions, records, publish state, and credential metadata into the isolated profile.",
        "Verify restored admin state, portal state, media playback metadata, captions, records, publish status, and provider readiness.",
        "Record the restore proof summary, excluded items, and support-bundle context, then remove temporary rehearsal files.",
    ]


def _restore_proof_items(
    *, state: Literal["pending", "passed", "needs_attention"]
) -> list[RestoreProofItem]:
    message_by_state = {
        "pending": "Will be checked during the isolated restore rehearsal.",
        "passed": "Included in the isolated restore proof manifest.",
        "needs_attention": "Could not be verified during the restore rehearsal.",
    }
    message = message_by_state[state]
    return [
        RestoreProofItem(id=item_id, label=label, required=True, state=state, message=message)
        for item_id, label in (
            ("database", "Database state"),
            ("media", "Media files and playback metadata"),
            ("config", "Configuration"),
            ("station-profile", "Station profile"),
            ("schedules", "Schedules"),
            ("captions", "Captions"),
            ("records", "Signed records"),
            ("publish-state", "Publish status"),
            ("provider-readiness", "Provider readiness"),
            ("credential-metadata", "Credential metadata"),
        )
    ]


def _restore_excluded_items() -> list[str]:
    return [
        "provider secret values",
        "staff bearer token values",
        "admin password and recovery code plaintext",
    ]


def _update_rollback_plan_steps(*, rollback_available: bool) -> list[str]:
    """Operator-facing safe update and rollback plan for tester stations."""

    rollback_step = (
        "Keep the listed rollback artifact available until the next successful meeting proof."
        if rollback_available
        else "Choose or build a rollback artifact before applying an update to a beta station."
    )
    return [
        "Confirm no meeting is in progress and backup status is ready.",
        rollback_step,
        "Apply the update only inside a maintenance window with the operator console closed.",
        "Run database migrations and startup checks before reopening the console.",
        "Run Safe to broadcast and a private rehearsal after the update.",
        "If post-update proof fails, reinstall the rollback artifact and rerun Safe to broadcast.",
    ]


def build_provider_readiness_report() -> ProviderReadinessReport:
    """Return operator-facing provider setup cards without exposing secrets."""

    backup = build_backup_status()
    activitypub_status, activitypub_message, activitypub_next_step = _activitypub_provider_state()
    items = [
        ProviderReadinessItem(
            id="local-portal",
            label="Local resident portal",
            required=True,
            status="ready",
            message="The local resident portal is available for tester broadcasts.",
            next_step="Use Resident preview to confirm what viewers can see.",
            setup_steps=[
                "Open System Health.",
                "Choose Open resident preview.",
                "Confirm the resident page shows the expected station and meeting state.",
            ],
            proof_requirement="Resident preview must open from the same URL residents will use.",
            manual_section="publish-surfaces",
        ),
        ProviderReadinessItem(
            id="backup",
            label="Backup destination",
            required=True,
            status="ready" if backup.status == "ready" else "not_set_up",
            message=backup.message,
            next_step=backup.next_step,
            what_you_need=["A folder or drive CivicCast can write to during and after meetings."],
            setup_steps=[
                "Choose Backup destination in Setup.",
                "Enter a real Windows folder path, e.g. C:\\CivicCastBackups or D:\\CivicCastBackups.",
                "Run Verify backup and keep the destination connected before meetings.",
            ],
            proof_requirement="CivicCast writes, reads, verifies, and deletes a probe file.",
            manual_section="where-recordings-live",
        ),
        _provider_item(
            provider_id="internet-archive",
            label="Internet Archive",
            next_step="Optional. Paste your own Internet Archive keys, then run a live proof.",
            setup_url="https://archive.org/account/s3.php",
            what_you_need=[
                "A free Internet Archive account",
                "Your own S3-style access key and secret key",
            ],
            setup_steps=[
                "Create or sign in to the station's Internet Archive account (free).",
                "Open its S3 keys page and copy the access key and secret key it shows you.",
                "Paste both into the fields below yourself -- no admin needed.",
                "Run live proof before this shows as ready.",
            ],
            proof_requirement="A real upload proof is required before CivicCast marks this provider ready.",
            manual_section="provider-internet-archive",
        ),
        _provider_item(
            provider_id="youtube",
            label="YouTube",
            next_step="Optional. Connect YouTube only if the station wants an extra copy there.",
            setup_url="https://console.cloud.google.com/apis/credentials",
            what_you_need=[
                "The station's YouTube channel",
                "A Google OAuth client ID and secret (free to create)",
            ],
            setup_steps=[
                "Confirm the station owns or manages the YouTube channel.",
                "Create a free OAuth client in Google Cloud Console.",
                "Paste the client ID and secret into the fields below yourself.",
                "Run a private upload or stream proof before using YouTube for residents.",
            ],
            proof_requirement="A private YouTube proof is required before public claims.",
            manual_section="provider-youtube",
        ),
        _provider_item(
            provider_id="subscriber-notifications",
            label="Subscriber notices",
            next_step="Optional. Set up notices after the first successful local publish.",
            what_you_need=[
                "A webhook secret (CivicCast can generate one, or paste your own)",
                "A decision on the station's notification policy",
            ],
            setup_steps=[
                "Choose whether this station will email or webhook-notify subscribers.",
                "Paste a webhook secret below yourself, or generate one here.",
                "Send a test notification to a non-public test subscriber list.",
            ],
            proof_requirement="A redacted delivery proof is required before sending public notices.",
            manual_section="provider-subscriber-notifications",
        ),
        _provider_item(
            provider_id="local-nas",
            label="Local archive folder",
            next_step="Optional. Choose an archive folder if the station wants a second local copy.",
            what_you_need=[
                "A second drive, external drive, or network share the station already has"
            ],
            setup_steps=[
                "Confirm the folder or drive is connected before the meeting.",
                "Enter its path below yourself.",
                "Run the Local NAS proof so CivicCast writes and removes a probe file.",
            ],
            proof_requirement="CivicCast must prove write/read/delete access to the archive folder.",
            manual_section="provider-local-archive-folder",
        ),
        _provider_item(
            provider_id="cloudflare-r2",
            label="Cloudflare R2",
            next_step=(
                "Recommended and usually free. Use the CDN concierge below: one Cloudflare "
                "API token and CivicCast sets everything else up for you."
            ),
            setup_url="https://dash.cloudflare.com/",
            what_you_need=["A free Cloudflare account", "One Cloudflare API token"],
            setup_steps=[
                "Use the CDN concierge box on this card: create a free Cloudflare account.",
                "Create one API token scoped to R2 Edit, then paste it in and click Provision for me.",
                "CivicCast creates the bucket and turns on public access automatically.",
                "Prefer to do it by hand? Use the manual fields below the concierge box instead.",
            ],
            proof_requirement="A real object upload and public URL proof is required.",
            manual_section="provider-cloudflare-r2",
        ),
        _provider_item(
            provider_id="bunny",
            label="BunnyCDN",
            next_step="Optional, and not free -- check BunnyCDN's own pricing before choosing it over Cloudflare R2.",
            setup_url="https://dash.bunny.net/",
            what_you_need=[
                "A BunnyCDN account",
                "A storage zone (where the original files live)",
                "A Storage Zone API key",
                "The pull-zone hostname BunnyCDN gives you (its address for viewers)",
            ],
            setup_steps=[
                "Create or choose the station's storage zone and its pull-zone.",
                "Copy the Storage Zone API key and the pull-zone hostname.",
                "Paste the zone name, API key, and hostname into the fields below yourself.",
                "Run Test connection before advertising CDN playback.",
            ],
            proof_requirement="A real object upload and public URL proof is required.",
            manual_section="provider-alternative-cdns",
        ),
        _provider_item(
            provider_id="fastly",
            label="Fastly Object Storage",
            next_step="Optional, and not free -- for a station that already has a Fastly account.",
            setup_url="https://manage.fastly.com/",
            what_you_need=[
                "A Fastly account with Object Storage",
                "A bucket and its region",
                "An Object Storage access key",
                "The public URL Fastly gives the station's media",
            ],
            setup_steps=[
                "Create or choose the station's Object Storage bucket and note its region.",
                "Create an Object Storage access key with read and write access.",
                "Paste region, key, secret, bucket, and public URL into the fields below yourself.",
                "Run Test connection before advertising CDN playback.",
            ],
            proof_requirement="A real object upload and public URL proof is required.",
            manual_section="provider-alternative-cdns",
        ),
        _provider_item(
            provider_id="akamai",
            label="Akamai Object Storage",
            next_step="Optional, and not free -- for a station that already has an Akamai/Linode account.",
            setup_url="https://cloud.linode.com/object-storage",
            what_you_need=[
                "An Akamai/Linode account",
                "An Object Storage bucket and its region",
                "An Object Storage access key",
                "The public URL Akamai gives the station's media",
            ],
            setup_steps=[
                "Create or choose the station's Object Storage bucket and note its region.",
                "Create an Object Storage access key with read and write access.",
                "Paste region, key, secret, bucket, and public URL into the fields below yourself.",
                "Run Test connection before advertising CDN playback.",
            ],
            proof_requirement="A real object upload and public URL proof is required.",
            manual_section="provider-alternative-cdns",
        ),
        ProviderReadinessItem(
            id="podcast",
            label="Podcast feed",
            required=False,
            status="ready",
            message="Podcast feed generation is available locally.",
            next_step="Optional. Review podcast metadata before turning this on for residents.",
            setup_steps=[
                "Record, package, and approve a meeting on the Portal publish surface first --",
                "the podcast feed republishes audio from an already-published recording, nothing extra to configure before that.",
                "Review title, description, artwork, and category metadata.",
                "Open the feed preview before sharing the feed URL.",
            ],
            proof_requirement="Local feed generation is available; public distribution depends on station policy.",
            manual_section="provider-podcast-feed",
        ),
        ProviderReadinessItem(
            id="activitypub",
            label="Federation",
            required=False,
            status=activitypub_status,
            message=activitypub_message,
            next_step=activitypub_next_step,
            advanced=True,
            what_you_need=[
                "A decision on whether the station wants federation (most don't need it)"
            ],
            setup_steps=[
                "Read what federation is in the manual -- most stations leave it off.",
                "If the station wants it, open Federation and choose Generate station key.",
                "Run local and live interop proof before public federation claims.",
            ],
            proof_requirement="Federation must stay off until the station records an explicit proof.",
            manual_section="provider-federation",
        ),
    ]
    return ProviderReadinessReport(
        generated_at=datetime.now(UTC),
        items=items,
        next_step=(
            "Set up only the providers the station needs. Optional providers can "
            "remain not set up for local tester broadcasts."
        ),
    )


def build_source_setup_report(*, configured_source_count: int | None = 0) -> SourceSetupReport:
    """Return plain-language camera/source setup guidance."""

    count = max(configured_source_count or 0, 0)
    return SourceSetupReport(
        generated_at=datetime.now(UTC),
        status="ready" if count > 0 else "not_set_up",
        configured_source_count=count,
        options=[
            SourceSetupOption(
                id="usb-hdmi",
                label="Camera plugged into this computer",
                best_for="USB webcams or camcorders connected through an HDMI capture adapter.",
                source_type="upload",
                operator_steps=[
                    "Plug the camera or capture adapter into this computer.",
                    "Choose Camera in Run Meeting.",
                    "Confirm the preview shows video and audio before the meeting.",
                ],
            ),
            SourceSetupOption(
                id="phone-app",
                label="Phone or tablet broadcasting app",
                best_for="A phone on a tripod running a broadcast app.",
                source_type="rtmp",
                operator_steps=[
                    "Open the broadcast app on the phone.",
                    "Copy the CivicCast stream address from Run Meeting.",
                    "Start a private preflight before the public meeting.",
                ],
            ),
            SourceSetupOption(
                id="encoder",
                label="Hardware encoder or control-room system",
                best_for="A dedicated encoder from the AV rack or public access station.",
                source_type="rtmp",
                operator_steps=[
                    "Ask the AV operator to enter the CivicCast stream address.",
                    "Start test output from the encoder.",
                    "Run preflight and confirm audio, video, and recording.",
                ],
                needs_it_help=True,
            ),
            SourceSetupOption(
                id="ndi",
                label="NDI source on the meeting network",
                best_for="Public access stations that already use NDI video routing.",
                source_type="ndi",
                operator_steps=[
                    "Keep the camera and CivicCast computer on the same trusted network.",
                    "Choose the named NDI source in Run Meeting.",
                    "Run preflight while IT confirms the network allows NDI traffic.",
                ],
                needs_it_help=True,
            ),
            SourceSetupOption(
                id="sample-upload",
                label="Sample recording or uploaded test file",
                best_for="A no-camera rehearsal before the first real meeting.",
                source_type="upload",
                operator_steps=[
                    "Choose Upload test media in Run Meeting.",
                    "Use the bundled sample or a short local video.",
                    "Run rehearsal and review the resident preview.",
                ],
            ),
        ],
        next_step=(
            "Choose the option that matches the equipment in the room, then run preflight."
            if count == 0
            else "Run preflight for the configured source before the meeting starts."
        ),
    )


class SourceSetupError(ValueError):
    """Raised when a plain-language source setup request is invalid."""


class SourceSetupUnavailableError(RuntimeError):
    """Raised when source setup cannot run because a required service is absent."""


def create_source_from_setup(
    request: SourceSetupCreateRequest,
    *,
    live_source_store: Any,
) -> SourceSetupMutationResponse:
    """Create a live source from the operator setup wizard contract."""

    if live_source_store is None:
        raise SourceSetupUnavailableError(
            "Durable storage is not ready. Open Setup and prepare storage before adding a source."
        )

    source_type, endpoint_url = _source_setup_endpoint(request.kind, request.endpoint)
    live_source_id = _source_setup_id(request.kind, request.label)
    payload = LiveSourceCreate(
        live_source_id=live_source_id,
        channel_id=request.channel_id,
        name=request.label.strip(),
        source_type=source_type,
        endpoint_url=endpoint_url,
    )
    try:
        created = live_source_store.create(payload)
    except Exception as exc:
        if exc.__class__.__name__ == "LiveSourceAlreadyExistsError":
            raise SourceSetupError(
                "A source with this name already exists. Rename the source and try again."
            ) from exc
        raise
    return SourceSetupMutationResponse(
        status="ready",
        live_source_id=created.live_source_id,
        source_type=created.source_type,
        message=f"{created.name} is saved as a meeting source.",
        next_step="Open Run Meeting, choose this source, and run preflight.",
    )


_SAMPLE_REHEARSAL_SOURCE_ID = "civiccast-sample-test-source"
_SAMPLE_REHEARSAL_SOURCE_ENDPOINT = "rtmp://127.0.0.1/live/civiccast-sample-rehearsal"

# Public alias used by source setup callers and tests.
SAMPLE_REHEARSAL_SOURCE_ID = _SAMPLE_REHEARSAL_SOURCE_ID


def is_sample_rehearsal_source(source: Any) -> bool:
    """Recognize only the unchanged bundled sample configuration."""
    return (
        getattr(source, "live_source_id", None) == _SAMPLE_REHEARSAL_SOURCE_ID
        and getattr(source, "channel_id", None) == "government"
        and getattr(source, "source_type", None) == "rtmp"
        and getattr(source, "endpoint_url", None) == _SAMPLE_REHEARSAL_SOURCE_ENDPOINT
        and getattr(source, "credentials_handle", None) is None
    )


def create_sample_rehearsal_upload(
    *,
    postgres_store: Any,
    live_source_store: Any,
) -> SourceSetupSampleUploadResponse:
    """Generate and ingest a tiny bundled sample video for rehearsal."""

    if postgres_store is None:
        raise SourceSetupUnavailableError(
            "Durable storage is not ready. Open Setup and prepare storage before creating sample media."
        )
    if live_source_store is None:
        raise SourceSetupUnavailableError(
            "Meeting source storage is not ready. Open Setup and prepare storage before creating sample media."
        )
    upload_dir_raw = os.environ.get("CIVICCAST_UPLOAD_DIR")
    if not upload_dir_raw:
        raise SourceSetupUnavailableError(
            "Upload storage is not ready. Prepare storage before creating sample media."
        )
    upload_dir = Path(upload_dir_raw).expanduser().resolve()
    asset_id = _next_sample_asset_id(postgres_store)
    asset_dir = (upload_dir / asset_id).resolve()
    if not asset_dir.is_relative_to(upload_dir):
        raise SourceSetupError("Sample asset path resolved outside the upload directory.")
    asset_dir.mkdir(parents=True, exist_ok=True)
    sample_path = (asset_dir / "civiccast-sample-rehearsal.mp4").resolve()
    if not sample_path.is_relative_to(asset_dir):
        raise SourceSetupError("Sample media path resolved outside the asset directory.")

    try:
        _write_sample_video(sample_path)
        ffprobe_result = run_ffprobe(sample_path)
        validate_ingest(ffprobe_result)
        result = postgres_store.ingest_upload(
            asset_id=asset_id,
            title="CivicCast sample rehearsal",
            description="Short sample generated by setup for private rehearsal.",
            file_path=str(sample_path),
            file_size_bytes=sample_path.stat().st_size,
            ffprobe_result=ffprobe_result,
        )
    except FfmpegNotFoundError as exc:
        _clean_failed_sample_asset(asset_dir, sample_path)
        raise SourceSetupUnavailableError(str(exc)) from exc
    except FfprobeNotFoundError as exc:
        _clean_failed_sample_asset(asset_dir, sample_path)
        raise SourceSetupUnavailableError(str(exc)) from exc
    except UnsupportedFormatError as exc:
        _clean_failed_sample_asset(asset_dir, sample_path)
        raise SourceSetupError(exc.reason) from exc
    except AssetAlreadyExistsError as exc:
        _clean_failed_sample_asset(asset_dir, sample_path)
        raise SourceSetupError(f"Sample asset id already exists: {exc.asset_id}.") from exc
    except Exception:
        _clean_failed_sample_asset(asset_dir, sample_path)
        raise

    sample_source = _ensure_sample_rehearsal_source(live_source_store)
    record_sample_rehearsal_media(
        asset_id=result.asset_id,
        file_path=Path(result.file_path),
        upload_dir=upload_dir,
    )
    return SourceSetupSampleUploadResponse(
        status="ready",
        asset_id=result.asset_id,
        title=result.title,
        file_path=result.file_path,
        live_source_id=sample_source.live_source_id,
        source_type=sample_source.source_type,
        message="CivicCast created a short sample video and a no-camera test source for rehearsal.",
        next_step="Open System Health and select Check broadcast readiness, then confirm the resident preview.",
    )


def _ensure_sample_rehearsal_source(live_source_store: Any) -> Any:
    get_method = getattr(live_source_store, "get", None)
    if callable(get_method):
        existing = get_method(_SAMPLE_REHEARSAL_SOURCE_ID)
        if existing is not None:
            return existing

    payload = LiveSourceCreate(
        live_source_id=_SAMPLE_REHEARSAL_SOURCE_ID,
        channel_id="government",
        name="CivicCast sample test source",
        source_type="rtmp",
        endpoint_url=_SAMPLE_REHEARSAL_SOURCE_ENDPOINT,
    )
    try:
        return live_source_store.create(payload)
    except LiveSourceAlreadyExistsError:
        if callable(get_method):
            existing = get_method(_SAMPLE_REHEARSAL_SOURCE_ID)
            if existing is not None:
                return existing
        raise


def record_sample_rehearsal_media(*, asset_id: str, file_path: Path, upload_dir: Path) -> None:
    resolved_upload_dir = upload_dir.resolve()
    resolved_file = file_path.resolve()
    if not resolved_file.is_relative_to(resolved_upload_dir):
        raise SourceSetupError("Sample media path resolved outside the upload directory.")

    def mutate(state: dict[str, Any]) -> None:
        state["sample_rehearsal_media"] = {
            "asset_id": asset_id,
            "file_path": str(resolved_file),
        }

    _mutate_ops_state(mutate)


def _load_sample_rehearsal_media(upload_dir: Path) -> tuple[str, Path] | None:
    payload = _load_ops_state().get("sample_rehearsal_media")
    if not isinstance(payload, Mapping):
        return None
    asset_id = payload.get("asset_id")
    file_path = payload.get("file_path")
    if not isinstance(asset_id, str) or not asset_id or not isinstance(file_path, str):
        return None
    resolved_upload_dir = upload_dir.resolve()
    resolved_file = Path(file_path).expanduser().resolve()
    if not resolved_file.is_relative_to(resolved_upload_dir) or not resolved_file.is_file():
        return None
    return asset_id, resolved_file


def _probe_sample_rehearsal_media(
    source: Any,
    *,
    asset_id: str,
    file_path: Path,
) -> tuple[bool, str | None]:
    if not is_sample_rehearsal_source(source):
        return False, "The selected source does not match the validated rehearsal sample."
    try:
        validate_ingest(run_ffprobe(file_path))
    except (FfprobeNotFoundError, FfprobeError, UnsupportedFormatError, OSError) as exc:
        return False, f"The validated rehearsal sample is no longer usable: {exc}."
    return True, f"Validated recorded sample asset {asset_id} passed the media probe."


def build_sample_rehearsal_source_probe() -> Callable[[Any], tuple[bool, str | None]]:
    """Return a ``SourceProbe`` that validates the bundled sample by local file.

    Bug B1 (field evidence, native beta candidate #17): the shipped
    sample-rehearsal ``LiveSource`` carries a placeholder RTMP endpoint
    (``rtmp://127.0.0.1/live/civiccast-sample-rehearsal``, see
    ``_SAMPLE_REHEARSAL_SOURCE_ENDPOINT`` above) that no process in this
    product listens on -- CivicCast ships no RTMP broker, so nothing can
    ever push media there (chicken-and-egg: the real ingest engine only
    starts once a station commits to air, but committing to air requires
    this same probe to already pass). Probing that endpoint over the
    network -- the path every other, real source takes
    (``civiccast.live.source_probe.probe_live_source``) -- can therefore
    never pass, which made the bundled "no-camera rehearsal" unusable for
    exactly the case it exists to cover.

    This wraps the same validated-local-file check the installer's own
    private rehearsal already runs (``_probe_sample_rehearsal_media``) so
    ANY caller of the production pre-flight evaluator -- not just the
    installer's internal ``/rehearsal`` flow -- gets an honest, file-backed
    answer for the unchanged bundled sample configuration. Zero network hop, zero
    dependency on any ingest engine having started first. Every other
    source still goes through the real network probe;
    ``civiccast.app._resolve_preflight_evaluator`` only routes to this
    probe when the selected source retains its original ID, channel, type,
    placeholder endpoint and absent credentials. Edited sources use the network probe.
    """

    def _probe(source: Any) -> tuple[bool, str | None]:
        if not is_sample_rehearsal_source(source):
            return False, "This probe only validates CivicCast's bundled sample-rehearsal source."
        upload_dir_raw = os.environ.get("CIVICCAST_UPLOAD_DIR")
        if not upload_dir_raw:
            return False, (
                "Upload storage is not ready. Open Setup and prepare storage, then create the "
                "sample video and run pre-flight again."
            )
        upload_dir = Path(upload_dir_raw).expanduser().resolve()
        media = _load_sample_rehearsal_media(upload_dir)
        if media is None:
            return False, (
                "No validated sample rehearsal video exists yet. Open Setup and create the "
                "bundled sample video, then run pre-flight again."
            )
        asset_id, file_path = media
        return _probe_sample_rehearsal_media(source, asset_id=asset_id, file_path=file_path)

    return _probe


def _source_setup_endpoint(kind: str, endpoint: str) -> tuple[LiveSourceTypeValue, str]:
    trimmed = endpoint.strip()
    if not trimmed:
        raise SourceSetupError("Enter the stream address or NDI source name.")
    if any(ord(ch) < 32 for ch in trimmed):
        raise SourceSetupError("Source details cannot contain control characters.")

    if kind == "ndi":
        name = trimmed.removeprefix("ndi://").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            raise SourceSetupError("Enter a single NDI source name, not a path.")
        return "ndi", f"ndi://{name}"

    allowed_by_kind = {
        "usb-hdmi": {"rtmp", "rtmps"},
        "phone-app": {"rtmp", "rtmps", "srt"},
        "encoder": {"rtmp", "rtmps", "rtsp", "rtsps", "srt"},
    }
    allowed = allowed_by_kind.get(kind)
    if allowed is None:
        raise SourceSetupError("Choose a supported source type.")

    parsed = urlsplit(trimmed)
    scheme = parsed.scheme.lower()
    if scheme not in allowed or not parsed.netloc:
        readable = ", ".join(sorted(allowed))
        raise SourceSetupError(f"Use a {readable} stream address for this source.")
    if parsed.username or parsed.password:
        raise SourceSetupError(
            "Do not paste camera passwords into the stream address. Store credentials separately."
        )
    if scheme.startswith("rtmp"):
        source_type: LiveSourceTypeValue = "rtmp"
    elif scheme.startswith("rtsp"):
        source_type = "rtsp"
    else:
        source_type = "srt"
    return source_type, urlunsplit(parsed)


def _source_setup_id(kind: str, label: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    if not base:
        base = kind.replace("_", "-")
    base = re.sub(r"-+", "-", base)
    return f"{base[:44]}-{uuid4().hex[:8]}"


def _next_sample_asset_id(postgres_store: Any) -> str:
    get_staff_row = getattr(postgres_store, "get_staff_row", None)
    for _ in range(20):
        candidate = f"sample-rehearsal-{uuid4().hex[:10]}"
        if not callable(get_staff_row) or get_staff_row(candidate) is None:
            return candidate
    raise SourceSetupError("Could not allocate a unique sample asset id.")


def _clean_failed_sample_asset(asset_dir: Path, sample_path: Path) -> None:
    with suppress(OSError):
        sample_path.unlink(missing_ok=True)
    with suppress(OSError):
        if asset_dir.exists() and not any(asset_dir.iterdir()):
            asset_dir.rmdir()


def _write_sample_video(path: Path, *, duration_seconds: int = 2) -> None:
    result = run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000",
            "-t",
            str(duration_seconds),
            "-shortest",
            "-c:v",
            "h264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ]
    )
    if result.returncode != 0:
        raise SourceSetupUnavailableError(
            "FFmpeg could not create the sample rehearsal video. "
            "Use a short local test video or run installer dependency repair."
        )


# ===========================================================================
# First-run sample content + starter schedule seeding (audit finding A-1)
#
# When first-admin setup completes with sample_content_enabled=True, the
# router schedules run_first_run_seed() as a FastAPI background task so the
# operator's first look at the dashboard/portal shows a station that
# already works -- a sample asset published through the real ingest ->
# package -> publish(portal) pipeline, plus (if initial_schedule_enabled)
# a starter schedule item on the default channel.
#
# This reuses the same bundled ffmpeg generator as the rehearsal sample
# upload (_write_sample_video) rather than duplicating it, but deliberately
# does NOT reuse create_sample_rehearsal_upload's live-source wiring --
# that feature exists so an operator can rehearse with a no-camera RTMP
# source, which is a different concern from "seed day-one content."
#
# Failure handling is the load-bearing design constraint here (this repo
# was just burned by a silent best-effort seed -- see the
# ``with suppress(Exception): seed_ai_model_default(...)`` a few hundred
# lines up in complete_first_admin_setup, audit finding K3-1). Every step
# below is wrapped so a failure is caught at the step it happened, persisted
# to durable ops-state via _mutate_ops_state (the same JSON-file mechanism
# already used for rehearsal state and recovery-kit acknowledgement), and
# surfaced through read_first_run_seed_status() for a dismissible,
# retryable operator-console notice. First-admin setup itself never blocks
# or fails because of a seeding problem -- the response has already been
# returned by the time this runs.
# ===========================================================================

_FIRST_RUN_SAMPLE_TITLE = "Sample: Welcome to CivicCast"
_FIRST_RUN_SAMPLE_DESCRIPTION = (
    "A short bundled sample video CivicCast published automatically during first-run "
    "setup so the station has something to show on day one. Delete it like any other "
    "asset once real content is ready."
)
_FIRST_RUN_SAMPLE_DURATION_SECONDS = 20
_FIRST_RUN_SCHEDULE_WINDOW_SECONDS = _FIRST_RUN_SAMPLE_DURATION_SECONDS
_FIRST_RUN_SEED_OPERATOR_ID = "civiccast-setup"
_FIRST_RUN_SEED_OPERATOR_DISPLAY_NAME = "CivicCast Setup"

# How long a "pending" seed record is trusted before it's treated as
# abandoned (Codex review, PR #419). run_first_run_seed() catches every
# in-process exception and always persists "failed" or "succeeded" -- the
# gap is the whole process dying mid-run (killed, crashed, restarted
# between mark_first_run_seed_pending() and the background task actually
# starting), which leaves no code running to record anything. Ten minutes
# is generous headroom over the real budget (a 20-second synthetic clip:
# ffprobe + pack + publish, all local, is a few seconds) without false-
# positiving on a slow first-boot disk.
_FIRST_RUN_SEED_STALE_AFTER = timedelta(minutes=10)

_LOG = logging.getLogger(__name__)


class _FirstRunSeedStepError(RuntimeError):
    """One first-run seeding step failed; carries which step for the UI/record."""

    def __init__(self, step: SampleSeedStepValue, message: str) -> None:
        self.step: SampleSeedStepValue = step
        super().__init__(message)


def _next_first_run_sample_asset_id(postgres_store: Any) -> str:
    get_staff_row = getattr(postgres_store, "get_staff_row", None)
    for _ in range(20):
        candidate = f"sample-welcome-{uuid4().hex[:10]}"
        if not callable(get_staff_row) or get_staff_row(candidate) is None:
            return candidate
    raise _FirstRunSeedStepError(
        "ingest", "Could not allocate a unique id for the first-run sample asset."
    )


def _load_first_run_seed_state() -> dict[str, Any]:
    payload = _load_ops_state().get("first_run_sample_seed")
    return dict(payload) if isinstance(payload, dict) else {}


def _save_first_run_seed_state(**fields: Any) -> None:
    def mutate(state: dict[str, Any]) -> None:
        current = state.get("first_run_sample_seed")
        merged = dict(current) if isinstance(current, dict) else {}
        merged.update(fields)
        state["first_run_sample_seed"] = merged

    _mutate_ops_state(mutate)


def mark_first_run_seed_pending() -> None:
    """Record "seeding is in flight" synchronously, before the background task runs.

    Called from the router right after first-admin setup succeeds (when
    sample_content_enabled=True), so a GET of the seed status immediately
    after setup honestly reports "pending" instead of "nothing recorded yet"
    during the brief window before the FastAPI background task actually
    starts. Also used by retry_first_run_seed() to reset a failed record.
    """

    _save_first_run_seed_state(
        status="pending",
        started_at=datetime.now(UTC).isoformat(),
        completed_at=None,
        asset_id=None,
        schedule_item_id=None,
        failed_step=None,
        error_message=None,
        dismissed=False,
    )


def _seed_first_run_asset(
    postgres_store: Any,
    publish_store: Any,
) -> Any:
    """Ingest, package, and portal-publish the sample video. Returns the packaged StaffAssetRow.

    Mirrors civiccast.schedule.router.package_staff_asset's staging-directory
    swap (write to a sibling temp dir, only rename into place once a real
    manifest exists) and civiccast.publish.router.approve_publish_asset's
    portal-visibility step, but scoped to just the portal surface -- no
    Internet Archive / YouTube / ActivityPub delivery, which would either
    require real credentials a fresh station doesn't have yet or make
    outbound network calls during unattended first-run setup. A single
    first-run seed also has no concurrent packaging to race against (no
    other asset or operator can exist yet at this point in a station's
    life), so this does not take the schedule router's packaging admission
    semaphore.
    """

    if postgres_store is None:
        raise _FirstRunSeedStepError(
            "ingest",
            "Durable storage is not ready. Open Setup and prepare storage, then retry.",
        )
    upload_dir_raw = os.environ.get("CIVICCAST_UPLOAD_DIR")
    if not upload_dir_raw:
        raise _FirstRunSeedStepError(
            "ingest",
            "Upload storage is not configured. Open Setup and prepare storage, then retry.",
        )
    upload_dir = Path(upload_dir_raw).expanduser().resolve()
    asset_id = _next_first_run_sample_asset_id(postgres_store)
    asset_dir = (upload_dir / asset_id).resolve()
    if not asset_dir.is_relative_to(upload_dir):
        raise _FirstRunSeedStepError(
            "ingest", "Sample asset path resolved outside the upload directory."
        )
    asset_dir.mkdir(parents=True, exist_ok=True)
    sample_path = (asset_dir / "civiccast-welcome-sample.mp4").resolve()
    if not sample_path.is_relative_to(asset_dir):
        raise _FirstRunSeedStepError(
            "ingest", "Sample media path resolved outside the asset directory."
        )

    try:
        _write_sample_video(sample_path, duration_seconds=_FIRST_RUN_SAMPLE_DURATION_SECONDS)
        ffprobe_result = run_ffprobe(sample_path)
        validate_ingest(ffprobe_result)
        ingested = postgres_store.ingest_upload(
            asset_id=asset_id,
            title=_FIRST_RUN_SAMPLE_TITLE,
            description=_FIRST_RUN_SAMPLE_DESCRIPTION,
            file_path=str(sample_path),
            file_size_bytes=sample_path.stat().st_size,
            ffprobe_result=ffprobe_result,
        )
    except Exception as exc:
        _clean_failed_sample_asset(asset_dir, sample_path)
        raise _FirstRunSeedStepError(
            "ingest", f"CivicCast could not create or ingest the sample video: {exc}"
        ) from exc

    upload_root = resolve_upload_root()
    if upload_root is None:
        raise _FirstRunSeedStepError(
            "package",
            "Upload storage is not configured. Open Setup and prepare storage, then retry.",
        )
    package_root = resolve_vod_package_root(upload_root)
    package_dir = (package_root / asset_id).resolve()
    staging_dir = (package_root / f".{asset_id}-{uuid4().hex}.tmp").resolve()
    if not all(
        candidate.is_relative_to(upload_root)
        for candidate in (package_root, package_dir, staging_dir)
    ):
        raise _FirstRunSeedStepError(
            "package", "The package path resolved outside CivicCast upload storage."
        )
    try:
        result = pack_vod_asset(
            Path(ingested.file_path),
            staging_dir,
            # First-run seeding runs while the operator waits on the setup
            # screen: reuse the ffprobe ingest just did so the sample is
            # packaged at its own resolution instead of upscaled.
            source_width=ingested.width_px,
            source_height=ingested.height_px,
        )
        staged_manifest = Path(result.manifest_path).resolve()
        if not staged_manifest.is_relative_to(staging_dir) or not staged_manifest.is_file():
            raise PackagingError("The packager did not produce a usable manifest.")
        staging_dir.rename(package_dir)
    except Exception as exc:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise _FirstRunSeedStepError(
            "package", f"CivicCast could not package the sample video for playback: {exc}"
        ) from exc

    manifest_url = f"/media/vod/{quote(asset_id, safe='')}/playlist.m3u8"
    try:
        staff_asset = postgres_store.mark_packaged(asset_id, manifest_url)
    except Exception as exc:
        raise _FirstRunSeedStepError(
            "package",
            f"The sample video was packaged, but CivicCast could not save its "
            f"playback address: {exc}",
        ) from exc

    try:
        approval = PublishApprovalRequest(
            operator_id=_FIRST_RUN_SEED_OPERATOR_ID,
            operator_display_name=_FIRST_RUN_SEED_OPERATOR_DISPLAY_NAME,
            approved_surface_ids=["portal"],
        )
        approve_publish(asset=staff_asset, request=approval, store=publish_store)
        published_asset = postgres_store.mark_published(asset_id, published_at=datetime.now(UTC))
    except Exception as exc:
        raise _FirstRunSeedStepError(
            "publish",
            f"The sample video was packaged, but CivicCast could not publish it to "
            f"the portal: {exc}",
        ) from exc

    return published_asset


def _seed_first_run_schedule(schedule_store: Any, *, asset_id: str, channel_id: str) -> str:
    """Create a starter premiere schedule item for the seeded sample asset."""

    if schedule_store is None:
        raise _FirstRunSeedStepError(
            "schedule",
            "Durable storage is not ready. Open Setup and prepare storage, then retry.",
        )
    from civiccast.schedule.store import AssetNotFoundError, ScheduleConflictError

    payload = ScheduleItemCreate(
        asset_id=asset_id,
        channel_id=channel_id,
        mode="premiere",
        # Leave time to save the recovery kit and approve/start the channel.
        scheduled_at=datetime.now(UTC) + timedelta(minutes=5),
        duration_seconds=_FIRST_RUN_SCHEDULE_WINDOW_SECONDS,
        notes=(
            "Starter schedule item created automatically during first-run setup "
            "(sample content). Cancel it like any other schedule item once your "
            "real schedule is ready."
        ),
    )
    try:
        created = schedule_store.create(payload)
    except AssetNotFoundError as exc:
        raise _FirstRunSeedStepError(
            "schedule", f"The sample asset was not found when creating the schedule item: {exc}"
        ) from exc
    except ScheduleConflictError as exc:
        raise _FirstRunSeedStepError(
            "schedule",
            f"The starter schedule item conflicted with an existing schedule item: {exc}",
        ) from exc
    except Exception as exc:
        raise _FirstRunSeedStepError(
            "schedule", f"CivicCast could not create the starter schedule item: {exc}"
        ) from exc
    return str(created.id)


def run_first_run_seed(
    *,
    postgres_store: Any,
    publish_store: Any,
    schedule_store: Any,
    default_channel_id: str,
    initial_schedule_enabled: bool,
    resume_asset_id: str | None = None,
) -> None:
    """Seed a sample asset (and starter schedule item) after first-admin setup.

    Runs as a FastAPI background task -- see mark_first_run_seed_pending()
    for the synchronous "pending" record written before this starts. Never
    raises: every failure is caught at its step and persisted so the
    operator console can show a loud, dismissible, retryable notice instead
    of a silently-empty station (audit A-1 / K3-1).

    ``resume_asset_id``, set by :func:`retry_first_run_seed` (Codex review,
    PR #419 P2): when a previous attempt already published the sample asset
    and only the starter-schedule step failed afterward, re-running
    ``_seed_first_run_asset`` from scratch would publish a *second*,
    identical sample while the first stays public -- there is no unpublish
    path a background retry could use to clean up the orphan (portal
    visibility can only be withdrawn by an operator, via the Portal-scoped
    unpublish endpoint added alongside this fix). Passing the already-
    published asset id here skips straight to the schedule step and reuses
    it instead.
    """

    if resume_asset_id is not None:
        published_asset_id = resume_asset_id
    else:
        try:
            published_asset_id = _seed_first_run_asset(postgres_store, publish_store).asset_id
        except _FirstRunSeedStepError as exc:
            _LOG.warning("First-run sample content seeding failed at %s: %s", exc.step, exc)
            _save_first_run_seed_state(
                status="failed",
                completed_at=datetime.now(UTC).isoformat(),
                failed_step=exc.step,
                error_message=str(exc),
            )
            return
        except Exception as exc:  # pragma: no cover - defensive floor, see docstring
            _LOG.exception("First-run sample content seeding failed unexpectedly.")
            _save_first_run_seed_state(
                status="failed",
                completed_at=datetime.now(UTC).isoformat(),
                failed_step="ingest",
                error_message=str(exc),
            )
            return

    schedule_item_id: str | None = None
    if initial_schedule_enabled:
        try:
            schedule_item_id = _seed_first_run_schedule(
                schedule_store,
                asset_id=published_asset_id,
                channel_id=default_channel_id,
            )
        except _FirstRunSeedStepError as exc:
            _LOG.warning("First-run starter schedule seeding failed: %s", exc)
            _save_first_run_seed_state(
                status="failed",
                completed_at=datetime.now(UTC).isoformat(),
                asset_id=published_asset_id,
                failed_step=exc.step,
                error_message=str(exc),
            )
            return

    _LOG.info(
        "First-run sample content seeding complete: asset=%s schedule_item=%s",
        published_asset_id,
        schedule_item_id,
    )
    _save_first_run_seed_state(
        status="succeeded",
        completed_at=datetime.now(UTC).isoformat(),
        asset_id=published_asset_id,
        schedule_item_id=schedule_item_id,
        failed_step=None,
        error_message=None,
    )


def _first_run_seed_step_label(step: str | None) -> str:
    return {
        "ingest": "creating the sample video",
        "package": "packaging the sample video for playback",
        "publish": "publishing the sample video to the portal",
        "schedule": "creating the starter schedule item",
    }.get(step or "", "first-run sample setup")


def read_first_run_seed_status() -> SampleSeedStatus:
    """Read the current first-run sample content seeding state for the operator console."""

    setup_state = read_station_setup()
    profile = setup_state.profile
    sample_enabled = bool(profile.sample_content_enabled) if profile is not None else False
    schedule_enabled = bool(profile.initial_schedule_enabled) if profile is not None else False
    raw = _load_first_run_seed_state()
    dismissed = bool(raw.get("dismissed", False))

    if not sample_enabled:
        return SampleSeedStatus(
            status="not_applicable",
            sample_content_enabled=False,
            initial_schedule_enabled=schedule_enabled,
            dismissed=dismissed,
            message="Sample content was turned off during setup, so nothing was seeded.",
            next_step="Add real content from Assets and build your schedule from Schedule when you're ready.",
        )

    status_value = raw.get("status")
    started_at = _parse_optional_datetime(raw.get("started_at"))
    completed_at = _parse_optional_datetime(raw.get("completed_at"))

    if status_value not in ("pending", "succeeded", "failed"):
        return SampleSeedStatus(
            status="pending",
            sample_content_enabled=True,
            initial_schedule_enabled=schedule_enabled,
            dismissed=False,
            message="CivicCast is preparing the sample video and starter schedule.",
            next_step="This finishes in the background; check back in a few seconds.",
        )

    if (
        status_value == "pending"
        and started_at is not None
        and datetime.now(UTC) - started_at > _FIRST_RUN_SEED_STALE_AFTER
    ):
        # Reconciled here (on every read) rather than only at process
        # startup: startup-only reconciliation would miss a task abandoned
        # mid-run by something short of a full process restart, and every
        # path that surfaces this status to an operator -- this GET, and
        # the operator console's own 3-second poll (SampleSeedNotice.tsx)
        # -- already calls this function. Without this, "pending" persists
        # forever and SampleSeedNoticeView renders nothing for "pending":
        # no notice, no retry button, ever -- a silent failure, which is
        # exactly what audit K3-1 requires this feature never produce.
        abandoned_message = (
            "It was interrupted, likely by a restart, before it could finish "
            "or record what went wrong."
        )
        completed_at = datetime.now(UTC)
        _save_first_run_seed_state(
            status="failed",
            completed_at=completed_at.isoformat(),
            failed_step=None,
            error_message=abandoned_message,
        )
        status_value = "failed"
        raw = {**raw, "failed_step": None, "error_message": abandoned_message}

    if status_value == "failed":
        step_label = _first_run_seed_step_label(raw.get("failed_step"))
        error_message = str(raw.get("error_message") or "an unknown error")
        return SampleSeedStatus(
            status="failed",
            sample_content_enabled=True,
            initial_schedule_enabled=schedule_enabled,
            asset_id=raw.get("asset_id"),
            schedule_item_id=raw.get("schedule_item_id"),
            failed_step=raw.get("failed_step"),
            error_message=error_message,
            started_at=started_at,
            completed_at=completed_at,
            dismissed=dismissed,
            message=f"CivicCast could not finish {step_label}: {error_message}",
            next_step="Retry sample setup, or add content and a schedule manually.",
        )

    if status_value == "succeeded":
        schedule_item_id = raw.get("schedule_item_id")
        message = (
            "CivicCast published a sample video to the portal and created a starter schedule item."
            if schedule_item_id
            else "CivicCast published a sample video to the portal."
        )
        return SampleSeedStatus(
            status="succeeded",
            sample_content_enabled=True,
            initial_schedule_enabled=schedule_enabled,
            asset_id=raw.get("asset_id"),
            schedule_item_id=schedule_item_id,
            started_at=started_at,
            completed_at=completed_at,
            dismissed=dismissed,
            message=message,
            next_step="Review it on Assets, then replace it with real content when you're ready.",
        )

    # status_value == "pending"
    return SampleSeedStatus(
        status="pending",
        sample_content_enabled=True,
        initial_schedule_enabled=schedule_enabled,
        asset_id=raw.get("asset_id"),
        started_at=started_at,
        dismissed=False,
        message="CivicCast is preparing the sample video and starter schedule.",
        next_step="This finishes in the background; check back in a few seconds.",
    )


def _parse_optional_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def dismiss_first_run_seed_notice() -> SampleSeedStatus:
    """Record that the operator dismissed the first-run seeding notice."""

    _save_first_run_seed_state(dismissed=True)
    return read_first_run_seed_status()


def retry_first_run_seed(
    *,
    postgres_store: Any,
    publish_store: Any,
    schedule_store: Any,
) -> SampleSeedStatus:
    """Retry first-run sample content seeding on operator request.

    Runs synchronously (packaging a 20-second clip takes seconds, same
    tradeoff create_sample_rehearsal_upload already makes) so the operator
    sees the outcome immediately instead of polling a second time.

    Resumes from the schedule step alone when the previous attempt already
    published the sample asset and only the starter-schedule step failed
    afterward (Codex review, PR #419 P2): re-running the whole pipeline in
    that case would publish a second, identical sample asset while the
    first stays public, since nothing in this background-task path can
    clean up the orphan it would leave behind. Any other failure step
    (ingest/package/publish), or a record with no ``asset_id`` at all --
    e.g. an abandoned "pending" record reconciled by
    :func:`read_first_run_seed_status` with nothing actually seeded -- has
    no published asset to reuse and reseeds from scratch, same as before.
    """

    setup_state = read_station_setup()
    profile = setup_state.profile
    if profile is None or not profile.sample_content_enabled:
        raise SourceSetupError(
            "Sample content is turned off for this station, so there is nothing to retry."
        )
    raw = _load_first_run_seed_state()
    resume_asset_id = (
        raw.get("asset_id")
        if raw.get("status") == "failed" and raw.get("failed_step") == "schedule"
        else None
    )
    mark_first_run_seed_pending()
    if resume_asset_id is not None:
        # mark_first_run_seed_pending() just cleared asset_id along with the
        # rest of the record -- restore it immediately so a GET that lands
        # mid-retry (or a crash before run_first_run_seed returns) still
        # shows the asset that is genuinely already public, instead of
        # losing track of it.
        _save_first_run_seed_state(asset_id=resume_asset_id)
    run_first_run_seed(
        postgres_store=postgres_store,
        publish_store=publish_store,
        schedule_store=schedule_store,
        default_channel_id=profile.default_channel_id,
        initial_schedule_enabled=profile.initial_schedule_enabled,
        resume_asset_id=resume_asset_id,
    )
    return read_first_run_seed_status()


def create_diagnostic_bundle(
    request: DiagnosticBundleRequest,
    *,
    operations: Mapping[str, Any] | None = None,
    channel_ids: tuple[str, ...] = (),
) -> DiagnosticBundleResponse:
    """Write a redacted support bundle for tester troubleshooting.

    ``operations`` (S8-5 A1b) carries the redacted alerting/egress activity
    history collected by the router (recent alert events + delivery attempts,
    self-test history, resource samples, per-channel egress health/proof window).
    It is supplied pre-serialized and already JSON-safe; ``None`` means the
    alerting/egress stores were unavailable and the section is simply omitted.

    ``channel_ids`` (item #26) drives which per-channel egress log files get
    pulled into the bundle's ``logs`` section, in addition to the native
    runtime host's own diagnostic log. Every collected line passes through the same
    ``_redact_log_text`` choke point used for the rest of the bundle, so a
    stray secret in an FFmpeg/bootstrap log line cannot leave the machine
    unredacted."""

    generated_at = datetime.now(UTC)
    bundle_id = f"support-{generated_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    bundle_dir = _support_bundle_dir()
    bundle_dir.mkdir(parents=True, exist_ok=True)
    path = bundle_dir / f"{bundle_id}.json"
    station_setup = _redact_console_url_in_mapping(read_station_setup().model_dump(mode="json"))
    system_health = _redact_console_url_in_mapping(
        build_system_health_report().model_dump(mode="json")
    )
    logs = _collect_support_bundle_logs(channel_ids)
    payload = {
        "schema_version": 1,
        "bundle_id": bundle_id,
        "generated_at": generated_at.isoformat(),
        "civiccast_version": __version__,
        "platform": _platform_summary(),
        "environment": _redacted_environment_summary(os.environ),
        "station_setup": station_setup,
        "storage": _redacted_storage_summary(),
        "backup": build_backup_status().model_dump(mode="json"),
        "restore": build_restore_status().model_dump(mode="json"),
        "update_rollback": build_update_rollback_status().model_dump(mode="json"),
        "provider_readiness": build_provider_readiness_report().model_dump(mode="json"),
        "source_setup": build_source_setup_report().model_dump(mode="json"),
        "system_health": system_health,
        "operations": dict(operations) if operations else {},
        "logs": logs,
        "operator_note": {
            "provided": request.operator_note is not None,
            "length": len(request.operator_note or ""),
        },
        "redaction": {
            "tokens_passwords_private_keys_provider_credentials": "redacted",
            "subscriber_data": "excluded",
            "raw_logs": "tail, redacted",
        },
    }
    content = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    path.write_bytes(content)
    _restrict_ops_file(path)
    digest = sha256(content).hexdigest()

    def record_support_bundle(state: dict[str, Any]) -> None:
        state["support"] = {
            "last_bundle_id": bundle_id,
            "last_bundle_path": str(path),
            "last_generated_at": generated_at.isoformat(),
            "last_sha256": digest,
        }

    _mutate_ops_state(record_support_bundle)
    return DiagnosticBundleResponse(
        bundle_id=bundle_id,
        generated_at=generated_at,
        path=str(path),
        sha256=digest,
        redacted=True,
        contains=[
            "CivicCast version",
            "platform summary",
            "redacted environment presence",
            "setup and storage status",
            "safe-to-broadcast health",
            "backup, restore, update, provider, and source readiness",
            *(
                [
                    "recent alerts, delivery attempts, self-test history, "
                    "resource samples, and per-channel egress activity (redacted)"
                ]
                if operations
                else []
            ),
            *(["recent installer and per-channel egress logs (tail, redacted)"] if logs else []),
        ],
        excludes=[
            "bearer tokens",
            "passwords",
            "private keys",
            "provider credential values",
            "subscriber data",
            "raw media files",
        ],
        next_step="Attach this bundle to the tester bug report if support asks for it.",
    )


# Support-bundle log collection (item #26) -----------------------------------

# Bound how much of each log file is pulled in: a tail, not the whole history.
# Matches the byte budget of the existing egress continuity `_read_tail` idiom.
_SUPPORT_BUNDLE_LOG_TAIL_CHARS = 20_000
# Reuses the installer's env-var secret markers (`_SECRET_ENV_MARKERS`) plus a
# few log-specific ones (nonce, bearer/authorization headers). Any line
# containing one of these case-insensitive markers is dropped wholesale rather
# than partially redacted -- a key=value split can't be trusted to find every
# shape a log line takes (bare tokens in a URL, header lines, etc.), so this
# errs toward losing a line of diagnostic text over leaking a fragment of it.
_LOG_SECRET_MARKERS = (*_SECRET_ENV_MARKERS, "NONCE", "AUTHORIZATION", "BEARER")


def _redact_log_text(text: str) -> str:
    """Scrub secret-shaped content out of raw log text before it enters a bundle.

    Runs the existing console-URL nonce scrubber first (it already handles the
    ``?nonce=`` query-param shape precisely), then drops, line by line, any
    line that still contains a secret marker verbatim."""

    scrubbed = _redact_console_url_in_mapping({"line": text})["line"]
    assert isinstance(scrubbed, str)
    out_lines = [
        "[redacted line: contained a secret-shaped marker]"
        if any(marker in line.upper() for marker in _LOG_SECRET_MARKERS)
        else line
        for line in scrubbed.splitlines()
    ]
    return "\n".join(out_lines)


def _tail_log_file(path: Path, *, limit: int = _SUPPORT_BUNDLE_LOG_TAIL_CHARS) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return None
    return _redact_log_text(raw)


def _collect_support_bundle_logs(channel_ids: tuple[str, ...]) -> dict[str, Any]:
    """Gather the runtime/service logs named by item #26: the native runtime
    host's own diagnostic log and each live channel's FFmpeg stdout/stderr
    tail. Missing files (nothing has run yet, or this is not Windows) are
    simply omitted -- log collection never blocks bundle creation."""

    logs: dict[str, Any] = {}

    bootstrap_log = _installer_bootstrap_log_path()
    bootstrap_tail = _tail_log_file(bootstrap_log) if bootstrap_log else None
    if bootstrap_tail is not None:
        logs["installer_bootstrap"] = {
            "path": str(bootstrap_log),
            "tail": bootstrap_tail,
        }

    channels: dict[str, Any] = {}
    for channel_id in channel_ids:
        channel_dir = _egress_work_dir() / channel_id / "logs"
        channel_logs: dict[str, str] = {}
        for name, filename in (("stdout", "ffmpeg.stdout.log"), ("stderr", "ffmpeg.stderr.log")):
            tail = _tail_log_file(channel_dir / filename)
            if tail is not None:
                channel_logs[name] = tail
        if channel_logs:
            channels[channel_id] = channel_logs
    if channels:
        logs["egress_channels"] = channels

    return logs


def _installer_bootstrap_log_path() -> Path | None:
    """The native runtime host's own diagnostic log (main.rs's
    ``runtime_host_log``, under ``%USERPROFILE%\\.civiccast`` -- the SAME
    root ``installer_state_root()`` uses, per ``main.rs``'s
    ``installer_log_candidates``). Used to point at the retired WSL2 lane's
    ``bootstrap-wsl2-ubuntu.log`` under ``%LOCALAPPDATA%\\CivicCast``, a
    script and a path that no longer exist."""
    configured = os.getenv("CIVICCAST_INSTALLER_BOOTSTRAP_LOG")
    if configured:
        return Path(configured).expanduser()
    if os.name != "nt":
        return None
    root = os.getenv("USERPROFILE")
    if not root:
        return None
    return Path(root) / ".civiccast" / "runtime-host.log"


def _egress_work_dir() -> Path:
    from civiccast.egress.automation import default_egress_work_dir

    return default_egress_work_dir()


def _acceptance_packet_dir() -> Path:
    configured = os.getenv("CIVICCAST_ACCEPTANCE_PACKET_DIR")
    if configured:
        return Path(configured).expanduser()
    return station_state_path().with_name("acceptance-packets")


def create_acceptance_packet() -> AcceptancePacketResponse:
    """Write a redacted station acceptance packet (item #26).

    A station hands this to a franchise authority, board, or reviewer as
    evidence CivicCast is set up and operating: the same already-verified
    readiness sections the support bundle draws from (setup, safe-to-broadcast
    health, backup/restore/update, provider readiness, source setup), reframed
    as a standalone signed/hashed record rather than a troubleshooting dump.
    It asserts nothing beyond what those existing builders already compute --
    there is no new pass/fail check invented here, only a redacted snapshot."""

    generated_at = datetime.now(UTC)
    packet_id = f"acceptance-{generated_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    packet_dir = _acceptance_packet_dir()
    packet_dir.mkdir(parents=True, exist_ok=True)
    path = packet_dir / f"{packet_id}.json"

    station_setup = _redact_console_url_in_mapping(read_station_setup().model_dump(mode="json"))
    system_health_report = build_system_health_report()
    system_health = _redact_console_url_in_mapping(system_health_report.model_dump(mode="json"))
    payload = {
        "schema_version": 1,
        "packet_id": packet_id,
        "generated_at": generated_at.isoformat(),
        "civiccast_version": __version__,
        "platform": _platform_summary(),
        "station_setup": station_setup,
        "system_health": system_health,
        "backup": build_backup_status().model_dump(mode="json"),
        "restore": build_restore_status().model_dump(mode="json"),
        "update_rollback": build_update_rollback_status().model_dump(mode="json"),
        "provider_readiness": build_provider_readiness_report().model_dump(mode="json"),
        "source_setup": build_source_setup_report().model_dump(mode="json"),
        "redaction": {
            "tokens_passwords_private_keys_provider_credentials": "redacted",
            "subscriber_data": "excluded",
        },
    }
    content = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    path.write_bytes(content)
    _restrict_ops_file(path)
    digest = sha256(content).hexdigest()

    def record_acceptance_packet(state: dict[str, Any]) -> None:
        state["acceptance"] = {
            "last_packet_id": packet_id,
            "last_packet_path": str(path),
            "last_generated_at": generated_at.isoformat(),
            "last_sha256": digest,
        }

    _mutate_ops_state(record_acceptance_packet)
    return AcceptancePacketResponse(
        packet_id=packet_id,
        generated_at=generated_at,
        path=str(path),
        sha256=digest,
        redacted=True,
        safe_to_broadcast=system_health_report.safe_to_broadcast,
        contains=[
            "CivicCast version",
            "platform summary",
            "station setup state",
            "safe-to-broadcast health",
            "backup, restore, update, provider, and source readiness",
        ],
        next_step=(
            "Share this packet's path and SHA-256 with the franchise authority or "
            "reviewer requesting proof of station setup."
        ),
    )


#: Frontend dev-server fallback only (``npm run dev`` for portal-public,
#: which Vite serves on 5174 when 5173 -- portal-operator's dev port -- is
#: already taken). Bug B4 (field evidence, native beta candidate #17): this
#: was ALSO the unconditional default in production, so a packaged station
#: -- which mounts the real portal at its own control-plane origin, see
#: ``civiccast.app._mount_packaged_portals`` -- reported a dev-only URL
#: nothing was serving, and simultaneously called it "not_configured" even
#: though a real preview was one click away.
_RESIDENT_PORTAL_DEV_FALLBACK_URL = "http://127.0.0.1:5174"


def build_resident_preview() -> ResidentPreview:
    """Return the resident-facing preview target used by operator screens.

    Mirrors ``operator_console_url()``'s own packaged-vs-dev split just
    above: when this station has a packaged public portal
    (``CIVICCAST_PUBLIC_PORTAL_DIST`` -- the same env var
    ``civiccast.app._mount_packaged_portals`` checks before mounting the
    resident portal at ``/`` on the control plane's own origin), the real
    preview is CivicCast's own local URL, not a Vite dev-server port.
    """

    explicit_url = os.getenv("CIVICCAST_RESIDENT_PORTAL_URL")
    packaged = bool(os.getenv("CIVICCAST_PUBLIC_PORTAL_DIST"))
    packaged_default_url = (
        os.getenv("CIVICCAST_LOCAL_MEDIA_BASE_URL", "http://127.0.0.1:8000") + "/"
    )
    default_url = packaged_default_url if packaged else _RESIDENT_PORTAL_DEV_FALLBACK_URL
    public_url = explicit_url or default_url
    available = bool(explicit_url) or packaged
    return ResidentPreview(
        status="available" if available else "not_configured",
        public_url=public_url,
        message=(
            "Resident preview is pointed at the configured public portal."
            if explicit_url
            else (
                "Resident preview is using this station's own public portal until a "
                "public portal URL is set."
                if packaged
                else "Resident preview is not configured; this station has no packaged "
                "public portal and no public portal URL set."
            )
        ),
        next_step=(
            "Open the preview before the meeting and confirm residents can see the broadcast page."
            if available
            else "Set CIVICCAST_RESIDENT_PORTAL_URL when the public portal has a station URL."
        ),
    )


def build_system_health_report(
    *,
    profile: DeploymentProfile = "public-meetings",
    live_source_count: int | None = None,
    recording_target_count: int | None = None,
    live_preflight_ready: bool = False,
    recording_write_probe_ready: bool = False,
    resident_preview_confirmed: bool = False,
    channel_automation: Any = None,
    headend_readiness: Any = None,
    runtime_safe_to_air: Any = None,
    active_critical_alerts: int = 0,
    active_warning_alerts: int = 0,
    last_self_test: Any = None,
    latest_resource_sample: Any = None,
) -> SystemHealthReport:
    """Build the operator-facing System Health report.

    ``channel_automation`` is the CA-4 egress rollup
    (:class:`~civiccast.egress.models.ChannelAutomationRollup`) when durable
    storage is active; None omits the check (ephemeral mode, or callers that
    predate the cable-automation lane).
    """

    setup = read_station_setup()
    preview = build_resident_preview()
    rehearsal_flags = _last_rehearsal_flags(preview_public_url=preview.public_url)
    if live_source_count is None:
        live_source_count = _optional_count(rehearsal_flags.get("live_source_count"))
    if recording_target_count is None:
        recording_target_count = _optional_count(rehearsal_flags.get("recording_target_count"))
    live_preflight_ready = live_preflight_ready or rehearsal_flags["live_preflight_ready"]
    recording_write_probe_ready = (
        recording_write_probe_ready or rehearsal_flags["recording_write_probe_ready"]
    )
    resident_preview_confirmed = (
        resident_preview_confirmed or rehearsal_flags["resident_preview_confirmed"]
    )
    first_run = run_first_health_check(profile=profile)
    checks: list[SystemHealthCheck] = [
        _setup_health_check(setup),
        _recovery_kit_health_check(setup),
        _durable_storage_health_check(),
        _backup_health_check(),
        _live_source_health_check(live_source_count, live_preflight_ready=live_preflight_ready),
        _recording_path_health_check(
            recording_target_count,
            write_probe_ready=recording_write_probe_ready,
        ),
        _resident_portal_health_check(
            preview,
            preview_confirmed=resident_preview_confirmed,
        ),
        _station_policy_health_check(setup),
        _contributor_upload_health_check(),
        _caption_device_health_check(),
    ]
    checks.extend(_provider_health_checks(first_run.checks))
    if channel_automation is not None:
        checks.append(_channel_automation_health_check(channel_automation))
    if headend_readiness is not None:
        checks.append(_headend_readiness_health_check(headend_readiness))

    required_red = any(check.required and check.color == "red" for check in checks)
    required_yellow = any(check.required and check.color == "yellow" for check in checks)
    optional_yellow = any((not check.required) and check.color != "green" for check in checks)
    if required_red:
        color: Literal["green", "yellow", "red"] = "red"
        label = "Do not broadcast yet"
        operator_message = "A required item needs attention before the public broadcast starts."
    elif required_yellow:
        color = "yellow"
        label = "Check before meeting"
        operator_message = "Required checks are configured but still need live proof before the public broadcast starts."
    elif optional_yellow:
        color = "yellow"
        label = "Ready with optional items"
        operator_message = "Required checks passed. Review optional items before the meeting if policy requires them."
    else:
        color = "green"
        label = "Ready"
        operator_message = "You are ready to broadcast this meeting."

    return SystemHealthReport(
        generated_at=datetime.now(UTC),
        safe_to_broadcast=color,
        label=label,
        operator_message=operator_message,
        setup=setup,
        resident_preview=preview,
        checks=checks,
        runtime_safe_to_air=runtime_safe_to_air,
        active_critical_alerts=active_critical_alerts,
        active_warning_alerts=active_warning_alerts,
        last_self_test=last_self_test,
        latest_resource_sample=latest_resource_sample,
    )


def build_rehearsal_report(
    *,
    profile: DeploymentProfile = "public-meetings",
    live_source_count: int | None = None,
    recording_target_count: int | None = None,
    live_preflight_ready: bool = False,
    recording_write_probe_ready: bool = False,
    resident_preview_confirmed: bool = False,
) -> RehearsalReport:
    """Run a private rehearsal by evaluating the same readiness path."""

    health = build_system_health_report(
        profile=profile,
        live_source_count=live_source_count,
        recording_target_count=recording_target_count,
        live_preflight_ready=live_preflight_ready,
        recording_write_probe_ready=recording_write_probe_ready,
        resident_preview_confirmed=resident_preview_confirmed,
    )
    # This path evaluates readiness without running a private session, so the
    # rehearsal itself is reported as not run; only the gate is evaluated.
    return _rehearsal_report_from_health(
        rehearsal_id="rehearsal-" + uuid4().hex[:12],
        started_at=health.generated_at,
        health=health,
        evidence=[],
        rehearsal_result="not_run",
    )


def run_private_rehearsal(
    *,
    profile: DeploymentProfile = "public-meetings",
    live_session_store: Any,
    live_source_store: Any,
    recording_target_store: Any,
    preflight_evaluator: Any,
    finalizer: LiveRecordingFinalizer | None,
) -> RehearsalReport:
    """Run a private rehearsal through live-session and finalization contracts."""

    rehearsal_id = "rehearsal-" + uuid4().hex[:12]
    session_id = rehearsal_id
    started_at = datetime.now(UTC)
    evidence: list[str] = []

    live_source_count = _store_count(live_source_store)
    recording_target_count = count_production_recording_targets(recording_target_store)
    if live_session_store is None or recording_target_store is None or finalizer is None:
        health = build_system_health_report(
            profile=profile,
            live_source_count=live_source_count,
            recording_target_count=recording_target_count,
        )
        return _rehearsal_report_from_health(
            rehearsal_id=rehearsal_id,
            started_at=started_at,
            health=health,
            evidence=[
                "Durable live-session, recording-target, or finalization services are not ready."
            ],
            message_override=(
                "Private rehearsal is blocked because durable live recording services are not ready."
            ),
            next_step_override="Open Setup and prepare storage, then run rehearsal again.",
        )

    upload_dir_raw = os.environ.get("CIVICCAST_UPLOAD_DIR")
    if not upload_dir_raw:
        health = build_system_health_report(
            profile=profile,
            live_source_count=live_source_count,
            recording_target_count=recording_target_count,
        )
        return _rehearsal_report_from_health(
            rehearsal_id=rehearsal_id,
            started_at=started_at,
            health=health,
            evidence=["Upload storage is not configured."],
            message_override="Private rehearsal is blocked because upload storage is not ready.",
            next_step_override="Open Setup and prepare durable storage, then run rehearsal again.",
        )

    upload_dir = Path(upload_dir_raw).expanduser().resolve()
    sample_rehearsal_media = _load_sample_rehearsal_media(upload_dir)
    if sample_rehearsal_media is None:
        health = build_system_health_report(
            profile=profile,
            live_source_count=live_source_count,
            recording_target_count=recording_target_count,
        )
        return _rehearsal_report_from_health(
            rehearsal_id=rehearsal_id,
            started_at=started_at,
            health=health,
            evidence=[
                "No validated recorded sample is selected; CivicCast did not substitute unrelated media."
            ],
            message_override=(
                "Private rehearsal needs a validated recorded sample before it can run."
            ),
            next_step_override=(
                "Create sample media or upload a short test recording, then run private rehearsal again."
            ),
        )
    sample_asset_id, sample_path = sample_rehearsal_media
    rehearsal_dir = (upload_dir / "private-rehearsals" / rehearsal_id).resolve()
    if not rehearsal_dir.is_relative_to(upload_dir):
        raise SourceSetupError("Rehearsal path resolved outside the upload directory.")
    rehearsal_dir.mkdir(parents=True, exist_ok=True)
    recording_path = (rehearsal_dir / "private-rehearsal-recording.mp4").resolve()
    if not recording_path.is_relative_to(rehearsal_dir):
        raise SourceSetupError("Rehearsal recording path resolved outside its directory.")

    live_preflight_ready = False
    recording_write_probe_ready = False
    resident_preview_confirmed = False
    recording_asset_id: str | None = None
    resident_preview_proof: str | None = None

    try:
        sample_digest = hash_file(sample_path)
        shutil.copy2(sample_path, recording_path)
        if hash_file(recording_path) != sample_digest:
            raise SourceSetupError("The rehearsal copy did not match the validated sample asset.")
        evidence.append(
            f"Copied exact validated sample asset {sample_asset_id} into the private rehearsal."
        )
        ffprobe_result = run_ffprobe(recording_path)
        validate_ingest(ffprobe_result)
        evidence.append("Created and validated a short private rehearsal recording.")
        recording_write_probe_ready = True

        recording_target_count = ensure_default_recording_target(
            recording_target_store,
            upload_dir=upload_dir,
        )
        evidence.append("Production recording target is available for real recordings.")

        _ensure_rehearsal_recording_target(
            recording_target_store,
            upload_dir=upload_dir,
        )
        evidence.append("Recording target is available for the private rehearsal.")

        # The rehearsal creates its own private sample recording, so bind its
        # preflight to the matching sample source instead of implicitly using
        # whichever source happens to be first on the channel.  PreflightInputs
        # deliberately requires this identity so the later go-on-air gate can
        # prove the same selected source is still valid.
        rehearsal_source = _ensure_sample_rehearsal_source(live_source_store)
        live_source_count = _store_count(live_source_store)

        live_session_store.create_session(
            LiveSessionCreate(
                live_session_id=session_id,
                channel_id="government",
                title="Private first-broadcast rehearsal",
                notes="Generated by System Health rehearsal. Not a public meeting.",
            )
        )
        live_session_store.start_preflight(session_id)

        if preflight_evaluator is not None:
            free_bytes = shutil.disk_usage(upload_dir).free
            preflight_inputs = PreflightInputs(
                live_session_id=session_id,
                live_source_id=rehearsal_source.live_source_id,
                network_reachable=True,
                storage_free_bytes=free_bytes,
                ai_runtime_ready=True,
                operator_confirmed=True,
            )
            preflight = preflight_evaluator.evaluate(
                preflight_inputs,
                source_probe_override=lambda source: _probe_sample_rehearsal_media(
                    source,
                    asset_id=sample_asset_id,
                    file_path=recording_path,
                ),
            )
            live_preflight_ready = preflight.ready
            evidence.append(
                "Live preflight passed."
                if preflight.ready
                else "Live preflight ran but at least one required item did not pass."
            )
            if not preflight.ready:
                _clean_failed_sample_asset(rehearsal_dir, recording_path)
                health = build_system_health_report(
                    profile=profile,
                    live_source_count=live_source_count,
                    recording_target_count=recording_target_count,
                    live_preflight_ready=False,
                    recording_write_probe_ready=recording_write_probe_ready,
                    resident_preview_confirmed=False,
                )
                report = _rehearsal_report_from_health(
                    rehearsal_id=rehearsal_id,
                    started_at=started_at,
                    health=health,
                    evidence=evidence,
                    private_session_id=session_id,
                    rehearsal_result="failed",
                    message_override=(
                        "Private rehearsal stopped because the configured source did not "
                        "pass every server-side preflight check. No broadcast or recording "
                        "asset was created."
                    ),
                    next_step_override=(
                        "Connect a real source, verify that CivicCast receives media, then "
                        "run the private rehearsal again."
                    ),
                )
                _record_rehearsal_state(
                    report,
                    live_source_count=live_source_count,
                    recording_target_count=recording_target_count,
                    live_preflight_ready=False,
                    recording_write_probe_ready=recording_write_probe_ready,
                    resident_preview_confirmed=False,
                )
                return report
        else:
            evidence.append("Live preflight evaluator is not available.")
            _clean_failed_sample_asset(rehearsal_dir, recording_path)
            health = build_system_health_report(
                profile=profile,
                live_source_count=live_source_count,
                recording_target_count=recording_target_count,
                live_preflight_ready=False,
                recording_write_probe_ready=recording_write_probe_ready,
                resident_preview_confirmed=False,
            )
            report = _rehearsal_report_from_health(
                rehearsal_id=rehearsal_id,
                started_at=started_at,
                health=health,
                evidence=evidence,
                private_session_id=session_id,
                rehearsal_result="failed",
                message_override=(
                    "Private rehearsal stopped because CivicCast could not run server-side "
                    "preflight. No broadcast or recording asset was created."
                ),
                next_step_override=(
                    "Repair durable live services, then run the private rehearsal again."
                ),
            )
            _record_rehearsal_state(
                report,
                live_source_count=live_source_count,
                recording_target_count=recording_target_count,
                live_preflight_ready=False,
                recording_write_probe_ready=recording_write_probe_ready,
                resident_preview_confirmed=False,
            )
            return report

        live_session_store.go_on_air(session_id)
        live_session_store.end_broadcast(session_id)
        recording_uri = recording_path.as_uri()
        finalized = finalizer.finalize_recording(
            session_id,
            recording_uri=recording_uri,
            duration_seconds=ffprobe_result.duration_seconds,
        )
        recording_asset_id = finalized.asset.asset_id
        evidence.append(f"Finalized private recording as asset {finalized.asset.asset_id}.")

        resident_preview_confirmed, resident_preview_proof = _prove_resident_preview(
            build_resident_preview()
        )
        evidence.append(resident_preview_proof)

    except (
        FfmpegNotFoundError,
        FfprobeError,
        FfprobeNotFoundError,
        UnsupportedFormatError,
        LiveSessionAlreadyExistsError,
        LiveSessionNotFoundError,
        LiveSessionStateError,
        LiveRecordingAssetCollisionError,
        SourceSetupError,
        OSError,
    ) as exc:
        _clean_failed_sample_asset(rehearsal_dir, recording_path)
        health = build_system_health_report(
            profile=profile,
            live_source_count=live_source_count,
            recording_target_count=recording_target_count,
            live_preflight_ready=live_preflight_ready,
            recording_write_probe_ready=recording_write_probe_ready,
            resident_preview_confirmed=resident_preview_confirmed,
        )
        return _rehearsal_report_from_health(
            rehearsal_id=rehearsal_id,
            started_at=started_at,
            health=health,
            evidence=[*evidence, f"Rehearsal stopped: {exc}"],
            recording_uri=recording_path.as_uri(),
            resident_preview_proof=resident_preview_proof,
            rehearsal_result="failed",
            message_override=f"Private rehearsal could not complete: {str(exc).rstrip('.')}.",
            next_step_override="Fix the named issue, then run private rehearsal again.",
        )

    health = build_system_health_report(
        profile=profile,
        live_source_count=live_source_count,
        recording_target_count=recording_target_count,
        live_preflight_ready=live_preflight_ready,
        recording_write_probe_ready=recording_write_probe_ready,
        resident_preview_confirmed=resident_preview_confirmed,
    )
    report = _rehearsal_report_from_health(
        rehearsal_id=rehearsal_id,
        started_at=started_at,
        health=health,
        evidence=evidence,
        private_session_id=session_id,
        recording_asset_id=recording_asset_id,
        recording_uri=recording_path.as_uri(),
        resident_preview_proof=resident_preview_proof,
    )
    _record_rehearsal_state(
        report,
        live_source_count=live_source_count,
        recording_target_count=recording_target_count,
        live_preflight_ready=live_preflight_ready,
        recording_write_probe_ready=recording_write_probe_ready,
        resident_preview_confirmed=resident_preview_confirmed,
    )
    return report


def _rehearsal_outcome(
    safe_to_broadcast: SafeToBroadcastColor,
    *,
    required_needs_attention: bool = False,
    rehearsal_result: RehearsalResult = "not_run",
    gate: BroadcastGate | None = None,
) -> tuple[Literal["ready", "needs_attention", "blocked"], str, str]:
    # F-21: every branch states the rehearsal result it was given. A headline
    # must never claim a run ("ran", "passed") on a not_run or failed result;
    # the green and yellow branches used to ignore rehearsal_result entirely.
    if safe_to_broadcast == "green":
        if rehearsal_result == "passed":
            return (
                "ready",
                "Private rehearsal checks passed. The station can run the first broadcast flow.",
                "Use Run Meeting for the live event and keep System Health open.",
            )
        if rehearsal_result == "failed":
            return (
                "needs_attention",
                "Private rehearsal did not complete, although every required broadcast item is ready.",
                "Run private rehearsal again and keep System Health open.",
            )
        return (
            "ready",
            "Private rehearsal has not been run; every required broadcast item is ready.",
            "Run private rehearsal before the first public broadcast.",
        )
    if safe_to_broadcast == "yellow":
        if rehearsal_result == "passed":
            ran = "Private rehearsal ran, but"
            passed = "Private rehearsal passed required checks with"
        elif rehearsal_result == "failed":
            ran = "Private rehearsal did not complete, and"
            passed = "Private rehearsal did not complete; required checks are ready with"
        else:
            ran = "Private rehearsal has not been run, and"
            passed = "Private rehearsal has not been run; required checks are ready with"
        if required_needs_attention:
            return (
                "needs_attention",
                f"{ran} a required item still needs live proof before the public broadcast.",
                "Review the yellow required items, then run rehearsal again.",
            )
        return (
            "needs_attention",
            f"{passed} optional items still needing attention.",
            "Review the yellow items, then run rehearsal again if station policy requires them.",
        )
    blocking = gate.blocking if gate is not None else []
    names = ", ".join(item.label for item in blocking)
    count = len(blocking)
    plural = "item" if count == 1 else "items"
    fix_hint = f" ({blocking[0].next_step.rstrip('.')})" if count == 1 else ""
    next_step = (
        f"Fix {names}{fix_hint}, then run the check again."
        if names
        else "Fix the red items in System Health, then run rehearsal again."
    )
    if rehearsal_result == "passed":
        # F-21: the run itself succeeded; say so, and name what the gate is
        # waiting on instead of calling the rehearsal "blocked".
        return (
            "blocked",
            (
                f"Rehearsal passed, but the broadcast gate has {count} required {plural} "
                f"not ready: {names}."
                if names
                else "Rehearsal passed, but the broadcast gate has a required item not ready."
            ),
            next_step,
        )
    return (
        "blocked",
        (
            f"Private rehearsal is blocked because a required broadcast {plural} is not "
            f"ready: {names}."
            if names
            else "Private rehearsal is blocked because a required broadcast item is not ready."
        ),
        next_step,
    )


def build_broadcast_gate(health: SystemHealthReport) -> BroadcastGate:
    """Summarise the required-items gate from a System Health report."""

    blocking = [
        BroadcastGateItem(
            id=check.id, label=check.label, color=check.color, next_step=check.next_step
        )
        for check in health.checks
        if check.required and check.color == "red"
    ]
    attention = [
        BroadcastGateItem(
            id=check.id, label=check.label, color=check.color, next_step=check.next_step
        )
        for check in health.checks
        if check.required and check.color == "yellow"
    ]
    if blocking:
        plural = "item" if len(blocking) == 1 else "items"
        summary = (
            f"{len(blocking)} required {plural} not ready: "
            + ", ".join(item.label for item in blocking)
            + "."
        )
    elif attention:
        needs = "item still needs" if len(attention) == 1 else "items still need"
        summary = (
            f"{len(attention)} required {needs} attention before the public "
            "broadcast: " + ", ".join(item.label for item in attention) + "."
        )
    else:
        summary = "All required items are ready."
    return BroadcastGate(
        color=health.safe_to_broadcast,
        blocking=blocking,
        attention=attention,
        summary=summary,
    )


def _rehearsal_report_from_health(
    *,
    rehearsal_id: str,
    started_at: datetime,
    health: SystemHealthReport,
    evidence: list[str],
    private_session_id: str | None = None,
    recording_asset_id: str | None = None,
    recording_uri: str | None = None,
    resident_preview_proof: str | None = None,
    message_override: str | None = None,
    next_step_override: str | None = None,
    rehearsal_result: RehearsalResult | None = None,
) -> RehearsalReport:
    # A finalized recording asset is the proof the rehearsal ran end to end.
    # Callers that stopped a started run pass rehearsal_result="failed".
    if rehearsal_result is None:
        rehearsal_result = "passed" if recording_asset_id else "not_run"
    gate = build_broadcast_gate(health)
    status, message, next_step = _rehearsal_outcome(
        health.safe_to_broadcast,
        required_needs_attention=_has_required_yellow_check(health),
        rehearsal_result=rehearsal_result,
        gate=gate,
    )
    return RehearsalReport(
        rehearsal_id=rehearsal_id,
        started_at=started_at,
        status=status,
        safe_to_broadcast=health.safe_to_broadcast,
        rehearsal_result=rehearsal_result,
        gate=gate,
        message=message_override or message,
        resident_preview=health.resident_preview,
        checks=health.checks,
        private_session_id=private_session_id,
        recording_asset_id=recording_asset_id,
        recording_uri=recording_uri,
        resident_preview_proof=resident_preview_proof,
        evidence=evidence,
        next_step=next_step_override or next_step,
    )


def _has_required_yellow_check(health: SystemHealthReport) -> bool:
    return any(check.required and check.color == "yellow" for check in health.checks)


def _store_count(store: Any) -> int | None:
    if store is None:
        return None
    list_method = getattr(store, "list", None)
    if not callable(list_method):
        return None
    try:
        return len(list_method())
    except Exception:
        return None


def count_production_recording_targets(recording_target_store: Any) -> int | None:
    """Return configured local recording targets that production capture can use."""

    targets = _list_recording_targets(recording_target_store)
    if targets is None:
        return None
    return sum(1 for target in targets if _is_production_local_recording_target(target))


def ensure_default_recording_target(
    recording_target_store: Any,
    *,
    upload_dir: Path,
) -> int | None:
    """Create the installer-managed production recording target when needed.

    Private rehearsals deliberately use a separate target id. A clean installer
    profile still needs a real local target before Record Now or live-session
    finalization can run without hidden manual setup.
    """

    if recording_target_store is None:
        return None
    targets = _list_recording_targets(recording_target_store)
    if targets is not None and any(_is_production_local_recording_target(t) for t in targets):
        return count_production_recording_targets(recording_target_store)
    target_dir = (upload_dir / DEFAULT_RECORDING_TARGET_DIR_NAME).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    with suppress(RecordingTargetAlreadyExistsError):
        recording_target_store.create(
            RecordingTargetCreate(
                recording_target_id=DEFAULT_RECORDING_TARGET_ID,
                name=DEFAULT_RECORDING_TARGET_NAME,
                target_uri=target_dir.as_uri(),
            )
        )
    return count_production_recording_targets(recording_target_store)


def _ensure_rehearsal_recording_target(
    recording_target_store: Any,
    *,
    upload_dir: Path,
) -> None:
    if recording_target_store is None:
        return
    target_dir = (upload_dir / "private-rehearsals").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    with suppress(RecordingTargetAlreadyExistsError):
        recording_target_store.create(
            RecordingTargetCreate(
                recording_target_id=REHEARSAL_RECORDING_TARGET_ID,
                name="Local rehearsal recordings",
                target_uri=target_dir.as_uri(),
            )
        )


def _list_recording_targets(recording_target_store: Any) -> list[Any] | None:
    if recording_target_store is None:
        return None
    list_method = getattr(recording_target_store, "list", None)
    if not callable(list_method):
        return None
    try:
        return list(list_method())
    except Exception:
        return None


def _is_production_local_recording_target(target: Any) -> bool:
    target_id = getattr(target, "recording_target_id", None)
    target_uri = getattr(target, "target_uri", None)
    if target_id == REHEARSAL_RECORDING_TARGET_ID or not isinstance(target_uri, str):
        return False
    return local_recording_path(target_uri) is not None


def _prove_resident_preview(preview: ResidentPreview) -> tuple[bool, str]:
    parsed = urlsplit(preview.public_url)
    if parsed.scheme not in {"http", "https"}:
        return False, "Resident preview must use an http or https URL."
    # Scheme-only validation (http/https above), NOT destination-restricted
    # like the siblings (_require_local_http in ollama_client pins loopback;
    # _validate_probe_url in lpm_lab_stage45 rejects public URLs): the
    # resident preview URL is operator-configured and legitimately points at
    # the station's own possibly-LAN/public portal, so a loopback guard
    # would break the rehearsal proof this function exists for.
    request = urllib.request.Request(  # noqa: S310 - scheme checked above; see comment.
        preview.public_url,
        headers={"User-Agent": "CivicCast rehearsal proof"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310  # nosec B310
            content_type = response.headers.get("content-type", "")
            body = response.read(200_000).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return False, f"Resident preview did not load during rehearsal: {exc}."
    if "<html" in body.lower() or "civiccast" in body.lower() or "root" in body.lower():
        return (
            True,
            f"Resident preview loaded ({content_type or 'unknown content type'}).",
        )
    return False, "Resident preview responded, but the page body did not look like a portal page."


def _last_rehearsal_flags(*, preview_public_url: str | None = None) -> dict[str, Any]:
    rehearsal = _load_ops_state().get("rehearsal")
    if not isinstance(rehearsal, Mapping):
        return {
            "live_source_count": None,
            "recording_target_count": None,
            "live_preflight_ready": False,
            "recording_write_probe_ready": False,
            "resident_preview_confirmed": False,
        }
    resident_preview_confirmed = bool(rehearsal.get("resident_preview_confirmed"))
    if preview_public_url and rehearsal.get("resident_preview_url") != preview_public_url:
        resident_preview_confirmed = False
    return {
        "live_source_count": _optional_count(rehearsal.get("live_source_count")),
        "recording_target_count": _optional_count(rehearsal.get("recording_target_count")),
        "live_preflight_ready": bool(rehearsal.get("live_preflight_ready")),
        "recording_write_probe_ready": bool(rehearsal.get("recording_write_probe_ready")),
        "resident_preview_confirmed": resident_preview_confirmed,
    }


def _optional_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _record_rehearsal_state(
    report: RehearsalReport,
    *,
    live_source_count: int | None,
    recording_target_count: int | None,
    live_preflight_ready: bool,
    recording_write_probe_ready: bool,
    resident_preview_confirmed: bool,
) -> None:
    def mutate(state: dict[str, Any]) -> None:
        state["rehearsal"] = {
            "rehearsal_id": report.rehearsal_id,
            "started_at": report.started_at.isoformat(),
            "status": report.status,
            "safe_to_broadcast": report.safe_to_broadcast,
            "private_session_id": report.private_session_id,
            "recording_asset_id": report.recording_asset_id,
            "recording_uri": report.recording_uri,
            "resident_preview_url": report.resident_preview.public_url,
            "resident_preview_proof": report.resident_preview_proof,
            "live_source_count": live_source_count,
            "recording_target_count": recording_target_count,
            "live_preflight_ready": live_preflight_ready,
            "recording_write_probe_ready": recording_write_probe_ready,
            "resident_preview_confirmed": resident_preview_confirmed,
        }

    _mutate_ops_state(mutate)


def _backup_health_check() -> SystemHealthCheck:
    backup = build_backup_status()
    if backup.status == "ready":
        return SystemHealthCheck(
            id="backup-status",
            label="Backup",
            kind="required",
            required=True,
            state="ready",
            color="green",
            message=backup.message,
            next_step=backup.next_step,
        )
    return SystemHealthCheck(
        id="backup-status",
        label="Backup",
        kind="required",
        required=True,
        state="not_set_up" if backup.status == "not_set_up" else "needs_attention",
        color="red",
        message=backup.message,
        next_step=backup.next_step,
    )


def _provider_item(
    *,
    provider_id: str,
    label: str,
    next_step: str,
    setup_url: str | None = None,
    what_you_need: list[str] | None = None,
    setup_steps: list[str] | None = None,
    proof_requirement: str | None = None,
    manual_section: str | None = None,
) -> ProviderReadinessItem:
    env_names = _INSTALLER_PROVIDER_ENV_NAMES.get(provider_id, ())
    fields = list(_PROVIDER_CREDENTIAL_FIELDS.get(provider_id, ()))
    stored_fields = _stored_provider_fields(provider_id)
    configured = [name for name in env_names if os.getenv(name)]
    credential_handle = _provider_credential_handle(provider_id) if stored_fields else None
    real_settings_error = (
        _real_provider_settings_error(provider_id)
        if provider_id in _PROVIDERS_WITH_REAL_SETTINGS_VALIDATION
        else None
    )
    if _provider_credentials_are_valid(provider_id):
        readiness = _provider_proof_readiness(provider_id=provider_id, label=label)
        status: ProviderReadinessStatus = "ready" if readiness["ready"] else "needs_live_proof"
        return ProviderReadinessItem(
            id=provider_id,
            label=label,
            required=False,
            status=status,
            message=readiness["message"],
            next_step=readiness["next_step"] or next_step,
            what_you_need=what_you_need or [],
            setup_steps=setup_steps or [],
            setup_url=setup_url,
            proof_requirement=proof_requirement,
            proof_status=readiness["proof_status"],
            evidence_reference=readiness["evidence_reference"],
            proof_recorded_at=readiness["recorded_at"],
            redaction_reviewed=readiness["redaction_reviewed"],
            credential_fields=fields,
            credential_handle=credential_handle,
            manual_section=manual_section,
        )
    if configured or stored_fields:
        # rc17 D4: when the real settings loader rejected what's configured,
        # surface its own actionable reason instead of a generic message --
        # "incomplete" used to be shown even when what was set was simply the
        # wrong variable entirely.
        message = (
            f"{label} setup is incomplete: {real_settings_error}"
            if real_settings_error
            else f"{label} setup is incomplete."
        )
        item_next_step = (
            real_settings_error or "Finish the missing setup fields, then run live proof."
        )
        return ProviderReadinessItem(
            id=provider_id,
            label=label,
            required=False,
            status="needs_it_help",
            message=message,
            next_step=item_next_step,
            what_you_need=what_you_need or [],
            setup_steps=setup_steps or [],
            setup_url=setup_url,
            proof_requirement=proof_requirement,
            proof_status="not_configured",
            credential_fields=fields,
            credential_handle=credential_handle,
            manual_section=manual_section,
        )
    return ProviderReadinessItem(
        id=provider_id,
        label=label,
        required=False,
        status="not_set_up",
        message=f"{label} is optional. Skip it for now if the station doesn't need it yet.",
        next_step=next_step,
        what_you_need=what_you_need or [],
        setup_steps=setup_steps or [],
        setup_url=setup_url,
        proof_requirement=proof_requirement,
        proof_status="not_configured",
        credential_fields=fields,
        credential_handle=None,
        manual_section=manual_section,
    )


def _provider_proof_readiness(
    *,
    provider_id: str,
    label: str | None = None,
) -> dict[str, Any]:
    proof_credentials = _INSTALLER_PROVIDER_PROOF_CREDENTIALS.get(provider_id)
    proof_providers = _INSTALLER_PROVIDER_PROOF_PROVIDERS.get(provider_id)
    provider_label = label or provider_id.replace("-", " ").title()
    if proof_credentials is None or proof_providers is None:
        return {
            "ready": False,
            "proof_status": "needs_live_proof",
            "evidence_reference": None,
            "recorded_at": None,
            "redaction_reviewed": False,
            "message": f"{provider_label} credentials are present, but live proof has not passed.",
            "next_step": None,
        }
    stored_proof = _stored_provider_proof(provider_id)
    evidence_reference = _proof_string(stored_proof, "evidence_reference")
    redaction_reviewed = bool(stored_proof.get("redaction_reviewed"))
    recorded_at = _proof_datetime(stored_proof, "recorded_at")
    passed_evidence = (
        dict.fromkeys(proof_providers, evidence_reference) if evidence_reference else {}
    )
    redacted_evidence = proof_providers if redaction_reviewed else ()
    plan = build_provider_proof_plan(configured_credentials=proof_credentials)
    if evidence_reference:
        plan = build_provider_proof_plan(
            configured_credentials=proof_credentials,
            passed_evidence=passed_evidence,
            redacted_evidence=redacted_evidence,
        )
    relevant = [item for item in plan if item.provider in proof_providers]
    if not relevant:
        return {
            "ready": False,
            "proof_status": "needs_live_proof",
            "evidence_reference": evidence_reference,
            "recorded_at": recorded_at,
            "redaction_reviewed": redaction_reviewed,
            "message": f"{provider_label} credentials are present, but live proof has not passed.",
            "next_step": None,
        }
    if all(item.ready_for_public_release for item in relevant):
        return {
            "ready": True,
            "proof_status": "proof_passed",
            "evidence_reference": evidence_reference,
            "recorded_at": recorded_at,
            "redaction_reviewed": redaction_reviewed,
            "message": f"{provider_label} has redacted live proof evidence.",
            "next_step": "Keep the proof evidence with release artifacts and rotate credentials if it was exposed.",
        }
    if any(item.status == "proof_failed_redaction" for item in relevant):
        return {
            "ready": False,
            "proof_status": "proof_failed_redaction",
            "evidence_reference": evidence_reference,
            "recorded_at": recorded_at,
            "redaction_reviewed": redaction_reviewed,
            "message": f"{provider_label} proof exists, but redaction has not been confirmed.",
            "next_step": "Review and redact the evidence before it can count for release.",
        }
    goals = "; ".join(item.next_step for item in relevant)
    return {
        "ready": False,
        "proof_status": "needs_live_proof",
        "evidence_reference": evidence_reference,
        "recorded_at": recorded_at,
        "redaction_reviewed": redaction_reviewed,
        "message": f"{provider_label} credentials are present, but live proof has not passed.",
        "next_step": f"Run controlled live proof: {goals}",
    }


def _activitypub_provider_state() -> tuple[
    Literal["ready", "not_set_up", "needs_live_proof", "needs_it_help"],
    str,
    str,
]:
    try:
        from civiccast.activitypub.config import load_activitypub_config

        config = load_activitypub_config()
    except Exception as exc:
        return (
            "needs_it_help",
            f"Federation configuration could not be read: {exc}.",
            "Leave federation off for tester broadcasts or ask IT to inspect ActivityPub config.",
        )
    if config.federation_mode == "disabled":
        return (
            "not_set_up",
            "Federation is optional and off by default.",
            "Leave it off unless the station intentionally opts into federation.",
        )
    return (
        "needs_live_proof",
        f"Federation is configured in {config.federation_mode} mode.",
        "Run signed federation proof before advertising the station actor publicly.",
    )


def _ops_state_path() -> Path:
    configured = os.getenv("CIVICCAST_TESTER_OPS_STATE_PATH")
    if configured:
        return Path(configured).expanduser()
    return station_state_path().with_name("tester-ops-state.json")


def _load_ops_state() -> dict[str, Any]:
    with _ops_state_lock():
        return _load_ops_state_unlocked()


def _load_ops_state_unlocked() -> dict[str, Any]:
    path = _ops_state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"schema_version": _OPS_STATE_SCHEMA_VERSION}
    if not isinstance(payload, dict):
        return {"schema_version": _OPS_STATE_SCHEMA_VERSION}
    payload.setdefault("schema_version", _OPS_STATE_SCHEMA_VERSION)
    return payload


def _save_ops_state(payload: Mapping[str, Any]) -> None:
    with _ops_state_lock():
        _write_ops_state_unlocked(payload)


def _mutate_ops_state(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Serialize read-modify-write updates to tester ops state."""

    with _ops_state_lock():
        state = _load_ops_state_unlocked()
        mutator(state)
        _write_ops_state_unlocked(state)
        return state


def _write_ops_state_unlocked(payload: Mapping[str, Any]) -> None:
    path = _ops_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.{uuid4().hex}.tmp")
    tmp_path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _restrict_ops_file(tmp_path)
    tmp_path.replace(path)
    _restrict_ops_file(path)


def _provider_credentials_path() -> Path:
    configured = os.getenv("CIVICCAST_PROVIDER_CREDENTIALS_FILE")
    if configured:
        return Path(configured).expanduser()
    return station_state_path().with_name("provider-credentials.json")


def _provider_proofs_path() -> Path:
    configured = os.getenv("CIVICCAST_PROVIDER_PROOFS_FILE")
    if configured:
        return Path(configured).expanduser()
    return station_state_path().with_name("provider-proof-evidence.json")


def _provider_credential_handle(provider_id: str) -> str:
    return f"civiccast-provider://{provider_id}"


def _load_provider_credentials_unlocked() -> dict[str, Any]:
    path = _provider_credentials_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"schema_version": _PROVIDER_CREDENTIALS_SCHEMA_VERSION, "providers": {}}
    if not isinstance(payload, dict):
        return {"schema_version": _PROVIDER_CREDENTIALS_SCHEMA_VERSION, "providers": {}}
    payload.setdefault("schema_version", _PROVIDER_CREDENTIALS_SCHEMA_VERSION)
    providers = payload.get("providers")
    if not isinstance(providers, dict):
        payload["providers"] = {}
    return payload


def _mutate_provider_credentials(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _ops_state_lock():
        payload = _load_provider_credentials_unlocked()
        mutator(payload)
        _write_provider_credentials_unlocked(payload)
        return payload


def _write_provider_credentials_unlocked(payload: Mapping[str, Any]) -> None:
    path = _provider_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.{uuid4().hex}.tmp")
    tmp_path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _restrict_secret_file(tmp_path)
    tmp_path.replace(path)
    _restrict_secret_file(path)


def _load_provider_proofs_unlocked() -> dict[str, Any]:
    path = _provider_proofs_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"schema_version": _PROVIDER_PROOF_SCHEMA_VERSION, "proofs": {}}
    if not isinstance(payload, dict):
        return {"schema_version": _PROVIDER_PROOF_SCHEMA_VERSION, "proofs": {}}
    payload.setdefault("schema_version", _PROVIDER_PROOF_SCHEMA_VERSION)
    proofs = payload.get("proofs")
    if not isinstance(proofs, dict):
        payload["proofs"] = {}
    return payload


def _mutate_provider_proofs(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _ops_state_lock():
        payload = _load_provider_proofs_unlocked()
        mutator(payload)
        _write_provider_proofs_unlocked(payload)
        return payload


def _write_provider_proofs_unlocked(payload: Mapping[str, Any]) -> None:
    path = _provider_proofs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.{uuid4().hex}.tmp")
    tmp_path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    _restrict_ops_file(tmp_path)
    tmp_path.replace(path)
    _restrict_ops_file(path)


def stored_provider_field_values(provider_id: str) -> dict[str, str]:
    """Return the stored non-empty credential field values for ``provider_id``.

    Public accessor for the CDN bridge: turns the saved setup-wizard
    credentials into the field->value mapping the CDN adapter constructors
    expect. Empty dict when nothing is saved for the provider.
    """
    with _ops_state_lock():
        payload = _load_provider_credentials_unlocked()
    providers = payload.get("providers")
    if not isinstance(providers, Mapping):
        return {}
    provider_payload = providers.get(provider_id)
    if not isinstance(provider_payload, Mapping):
        return {}
    fields = provider_payload.get("fields")
    if not isinstance(fields, Mapping):
        return {}
    return {
        key: value
        for key, value in fields.items()
        if isinstance(key, str) and isinstance(value, str) and value
    }


def _stored_provider_fields(provider_id: str) -> set[str]:
    return set(stored_provider_field_values(provider_id))


def _provider_has_any_configuration(provider_id: str) -> bool:
    # rc17 D4: delegates to the same validity check the readiness report uses
    # (a genuine, real-adapter-backed validation for providers that have one,
    # and ALL -- not ANY -- required stored/env fields otherwise) so proof can
    # never be recorded against credentials that are absent or unusable.
    return _provider_credentials_are_valid(provider_id)


def _installer_provider_id_for_proof(provider_id: str) -> str | None:
    if provider_id in _INSTALLER_PROVIDER_PROOF_PROVIDERS:
        return provider_id
    return _INSTALLER_PROVIDER_BY_PROOF.get(provider_id)


def _stored_provider_proof(provider_id: str) -> Mapping[str, Any]:
    with _ops_state_lock():
        payload = _load_provider_proofs_unlocked()
    proofs = payload.get("proofs")
    if not isinstance(proofs, Mapping):
        return {}
    proof = proofs.get(provider_id)
    return proof if isinstance(proof, Mapping) else {}


def _proof_string(proof: Mapping[str, Any], key: str) -> str | None:
    value = proof.get(key)
    return value if isinstance(value, str) and value else None


def _proof_datetime(proof: Mapping[str, Any], key: str) -> datetime | None:
    raw = _proof_string(proof, key)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


@contextmanager
def _ops_state_lock() -> Iterator[None]:
    """Serialize tester ops-state writes across concurrent health requests."""

    state_path = _ops_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(_OPS_STATE_LOCK_NAME)
    with lock_path.open("a+b") as lock_file:
        _acquire_ops_state_lock(lock_file)
        try:
            yield
        finally:
            _release_ops_state_lock(lock_file)


def _acquire_ops_state_lock(lock_file: Any) -> None:
    deadline = time.monotonic() + _OPS_STATE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            if os.name == "nt":
                msvcrt: Any = __import__("msvcrt")

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl: Any = __import__("fcntl")
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise OSError(
                    "Timed out waiting for another CivicCast process to update tester ops state."
                ) from exc
            time.sleep(0.05)


def _release_ops_state_lock(lock_file: Any) -> None:
    if os.name == "nt":
        msvcrt: Any = __import__("msvcrt")

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl: Any = __import__("fcntl")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _restrict_ops_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _restrict_secret_file(path: Path) -> None:
    if os.name == "nt":
        _restrict_windows_secret_file_acl(path)
        return
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise OSError(f"Provider credential file permissions are too broad: {path}")


def _restrict_windows_secret_file_acl(path: Path) -> None:
    username = os.environ.get("USERNAME") or os.environ.get("USER")
    if not username:
        raise OSError("Cannot restrict provider credential ACL because USERNAME is unset.")
    icacls = shutil.which("icacls.exe") or shutil.which("icacls")
    if not icacls:
        raise OSError("Cannot restrict provider credential ACL because icacls is unavailable.")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(  # noqa: S603 - fixed icacls argv; no shell or user-built command line.
        [
            icacls,
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{username}:F",
            "SYSTEM:F",
            "Administrators:F",
        ],
        check=True,
        capture_output=True,
        text=True,
        creationflags=creationflags,
    )


def _state_string(state: Mapping[str, Any], section: str, key: str) -> str | None:
    section_payload = state.get(section)
    if not isinstance(section_payload, Mapping):
        return None
    value = section_payload.get(key)
    return value if isinstance(value, str) and value else None


def _state_datetime(state: Mapping[str, Any], section: str, key: str) -> datetime | None:
    raw = _state_string(state, section, key)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _record_backup_state(
    *,
    destination: str,
    status: Literal["ready", "needs_attention"],
    last_probe_at: datetime | None,
) -> None:
    def mutate(state: dict[str, Any]) -> None:
        backup = state.get("backup")
        if not isinstance(backup, dict):
            backup = {}
        backup["destination"] = destination
        backup["status"] = status
        if last_probe_at is not None:
            backup["last_probe_at"] = last_probe_at.isoformat()
        state["backup"] = backup

    _mutate_ops_state(mutate)


def _record_dr_drill_state(*, summary: str) -> None:
    """Record the last REAL disaster-recovery drill summary (civiccast.dr)."""

    def mutate(state: dict[str, Any]) -> None:
        restore = state.get("restore")
        if not isinstance(restore, dict):
            restore = {}
        restore["real_drill_summary"] = summary
        state["restore"] = restore

    _mutate_ops_state(mutate)


def _record_update_preflight_state(
    *,
    last_preflight_at: datetime,
    current_version: str,
    available_version: str,
    checkpoint_summary: str,
) -> None:
    def mutate(state: dict[str, Any]) -> None:
        update = state.get("update")
        if not isinstance(update, dict):
            update = {}
        update["last_preflight_at"] = last_preflight_at.isoformat()
        update["current_version"] = current_version
        update["available_version"] = available_version
        update["checkpoint_summary"] = checkpoint_summary
        state["update"] = update

    _mutate_ops_state(mutate)


def _record_maintenance_window_state(
    *,
    current_version: str,
    available_version: str,
    maintenance_window_expires_at: datetime,
    maintenance_window_summary: str,
) -> None:
    def mutate(state: dict[str, Any]) -> None:
        update = state.get("update")
        if not isinstance(update, dict):
            update = {}
        update["maintenance_current_version"] = current_version
        update["maintenance_available_version"] = available_version
        update["maintenance_window_expires_at"] = maintenance_window_expires_at.isoformat()
        update["maintenance_window_summary"] = maintenance_window_summary
        state["update"] = update

    _mutate_ops_state(mutate)


def _record_rollback_artifact_state(
    *,
    artifact_path: str,
    artifact_sha256: str,
) -> None:
    def mutate(state: dict[str, Any]) -> None:
        update = state.get("update")
        if not isinstance(update, dict):
            update = {}
        update["rollback_artifact"] = artifact_path
        update["rollback_artifact_sha256"] = artifact_sha256
        update.pop("last_rollback_test_at", None)
        update.pop("rollback_proof_summary", None)
        update.pop("last_failed_update_rollback_at", None)
        update.pop("failed_update_rollback_summary", None)
        state["update"] = update

    _mutate_ops_state(mutate)


def _record_rollback_rehearsal_state(
    *,
    last_rollback_test_at: datetime,
    rollback_proof_summary: str,
    artifact_sha256: str,
) -> None:
    def mutate(state: dict[str, Any]) -> None:
        update = state.get("update")
        if not isinstance(update, dict):
            update = {}
        update["last_rollback_test_at"] = last_rollback_test_at.isoformat()
        update["rollback_proof_summary"] = rollback_proof_summary
        update["rollback_artifact_sha256"] = artifact_sha256
        state["update"] = update

    _mutate_ops_state(mutate)


def _record_post_update_proof_state(
    *,
    last_post_update_proof_at: datetime,
    post_update_proof_summary: str,
) -> None:
    def mutate(state: dict[str, Any]) -> None:
        update = state.get("update")
        if not isinstance(update, dict):
            update = {}
        update["last_post_update_proof_at"] = last_post_update_proof_at.isoformat()
        update["post_update_proof_summary"] = post_update_proof_summary
        state["update"] = update

    _mutate_ops_state(mutate)


def _record_failed_update_rollback_state(
    *,
    last_failed_update_rollback_at: datetime,
    failed_update_rollback_summary: str,
    artifact_sha256: str,
) -> None:
    def mutate(state: dict[str, Any]) -> None:
        update = state.get("update")
        if not isinstance(update, dict):
            update = {}
        update["last_failed_update_rollback_at"] = last_failed_update_rollback_at.isoformat()
        update["failed_update_rollback_summary"] = failed_update_rollback_summary
        update["rollback_artifact_sha256"] = artifact_sha256
        state["update"] = update

    _mutate_ops_state(mutate)


def _write_directory_probe(path: Path, *, prefix: str) -> Path:
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    probe_bytes = b"CivicCast tester readiness directory probe\n"
    expected_hash = sha256(probe_bytes).hexdigest()
    probe_path = resolved / f"{prefix}-{uuid4().hex}.txt"
    try:
        probe_path.write_bytes(probe_bytes)
        observed_hash = sha256(probe_path.read_bytes()).hexdigest()
        if observed_hash != expected_hash:
            raise OSError("probe hash changed after write")
    finally:
        with suppress(FileNotFoundError):
            probe_path.unlink()
    return resolved


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _support_bundle_dir() -> Path:
    configured = os.getenv("CIVICCAST_SUPPORT_BUNDLE_DIR")
    if configured:
        return Path(configured).expanduser()
    return station_state_path().with_name("support-bundles")


_SUPPORT_BUNDLE_ID_RE = re.compile(r"^support-\d{8}T\d{6}Z-[0-9a-f]{8}$")


def diagnostic_bundle_path(bundle_id: str) -> Path | None:
    """Resolve one generated support bundle without exposing arbitrary files."""

    if _SUPPORT_BUNDLE_ID_RE.fullmatch(bundle_id) is None:
        return None
    bundle_dir = _support_bundle_dir().resolve()
    candidate = (bundle_dir / f"{bundle_id}.json").resolve()
    try:
        candidate.relative_to(bundle_dir)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _platform_summary() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


def _redacted_environment_summary(env: Mapping[str, str]) -> dict[str, dict[str, object]]:
    return {key: _redacted_env_value(key, env.get(key)) for key in _SUPPORT_ENV_KEYS}


def _redacted_env_value(key: str, value: str | None) -> dict[str, object]:
    if not value:
        return {"configured": False}
    if _env_key_is_secret(key):
        return {"configured": True, "value": "[redacted]"}
    return {"configured": True, "value": value}


def _env_key_is_secret(key: str) -> bool:
    upper = key.upper()
    return any(marker in upper for marker in _SECRET_ENV_MARKERS)


def _redact_console_url_in_mapping(data: dict[str, object]) -> dict[str, object]:
    """Strip any ``?nonce=`` query param from an embedded operator-console URL.

    ``operator_console_url()`` no longer bakes a nonce into the URL (the
    installer-handoff nonce was retired 2026-08-29 -- owner decision -- once
    the control plane's loopback-only bind made it a redundant gate), so this
    is now defense-in-depth against a legacy/pre-upgrade value rather than an
    everyday scrub. Diagnostic bundles are explicitly meant to leave the
    machine (sent to support), so any nested model carrying that URL
    (``StationSetupState.operator_console_url``, reachable both directly and
    via ``SystemHealthReport.setup``) still gets any stray nonce redacted
    before serialization. Recurses through dicts/lists so any future nesting
    point is covered without a second fix site.
    """

    def _scrub(value: object) -> object:
        if isinstance(value, dict):
            return {key: _scrub(sub) for key, sub in value.items()}
        if isinstance(value, list):
            return [_scrub(item) for item in value]
        if isinstance(value, str) and "nonce=" in value:
            parts = urlsplit(value)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            if "nonce" in query:
                query["nonce"] = "redacted"
                return urlunsplit(
                    (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
                )
        return value

    return _scrub(data)  # type: ignore[return-value]


def _redacted_storage_summary() -> dict[str, object]:
    storage = durable_storage_status()
    return {
        "status": storage.status,
        "database_url_configured": bool(storage.database_url),
        "database_kind": _database_kind(storage.database_url),
        "database_path": storage.database_path,
        "upload_dir": storage.upload_dir,
        "storage_dir": storage.storage_dir,
        "migrations_applied": storage.migrations_applied,
        "configured_at": storage.configured_at.isoformat(),
        "operator_message": storage.operator_message,
        "next_step": storage.next_step,
    }


def _database_kind(database_url: str) -> str:
    if database_url.startswith("sqlite"):
        return "sqlite"
    if database_url.startswith("postgresql"):
        return "postgresql"
    return "external"


def _setup_health_check(setup: StationSetupState) -> SystemHealthCheck:
    if setup.setup_complete:
        return SystemHealthCheck(
            id="first-admin",
            label="First admin",
            kind="required",
            required=True,
            state="ready",
            color="green",
            message=f"{setup.profile.admin_display_name if setup.profile else 'First admin'} can manage this station.",
            next_step="Keep the recovery kit somewhere separate from this computer.",
        )
    return SystemHealthCheck(
        id="first-admin",
        label="First admin",
        kind="required",
        required=True,
        state="not_set_up",
        color="red",
        message="Create the first local admin before running a public meeting.",
        next_step="Open Setup and complete first-admin setup.",
    )


def _recovery_kit_health_check(setup: StationSetupState) -> SystemHealthCheck:
    if setup.recovery_kit_created:
        return SystemHealthCheck(
            id="recovery-kit",
            label="Recovery kit",
            kind="required",
            required=True,
            state="ready",
            color="green",
            message="A recovery kit was generated during setup.",
            next_step="Confirm someone printed or saved the kit before the first broadcast.",
        )
    return SystemHealthCheck(
        id="recovery-kit",
        label="Recovery kit",
        kind="required",
        required=True,
        state="not_set_up",
        color="red",
        message="The station does not have a recovery kit yet.",
        next_step="Create the first admin and save the recovery kit.",
    )


def _durable_storage_health_check() -> SystemHealthCheck:
    storage = durable_storage_status()
    if storage.status == "ready":
        return SystemHealthCheck(
            id="durable-storage",
            label="Durable records storage",
            kind="required",
            required=True,
            state="ready",
            color="green",
            message=storage.operator_message,
            next_step="Keep automated database backups enabled before public meetings.",
        )
    return SystemHealthCheck(
        id="durable-storage",
        label="Durable records storage",
        kind="required",
        required=True,
        state="needs_it_help",
        color="red",
        message="Durable database storage is not configured for this process.",
        next_step="Open Setup and choose Prepare storage before broadcasting.",
    )


def _contributor_upload_health_check() -> SystemHealthCheck:
    """QA-2 (Critical): operator visibility into contributor-upload disk usage.

    Before this check existed, an operator's only signal that the
    contributor upload directory was filling up was a 507 the first time a
    contributor's upload was refused. Reuses the SAME usage/ceiling helpers
    the upload route itself enforces against, so this check and the actual
    507 threshold can never disagree.
    """
    from civiccast.contribute.router import _upload_dir_bytes, _upload_dir_max_bytes
    from civiccast.contribute.store import default_contributor_upload_dir

    gib = 1024**3
    upload_dir = default_contributor_upload_dir()
    ceiling_bytes = _upload_dir_max_bytes()
    used_bytes = _upload_dir_bytes(upload_dir)
    used_percent = (used_bytes / ceiling_bytes * 100) if ceiling_bytes > 0 else 0.0
    message = (
        f"Contributor upload storage: {used_bytes / gib:.2f} GB used of "
        f"{ceiling_bytes / gib:.2f} GB ({used_percent:.0f}%)."
    )
    if used_percent >= 100:
        return SystemHealthCheck(
            id="contributor-upload-storage",
            label="Contributor upload storage",
            kind="optional",
            required=False,
            state="needs_it_help",
            color="red",
            message=message + " New contributor uploads are being refused.",
            next_step=(
                "Open the contributor review queue and clear accepted, declined, or "
                "published submissions' source files, or raise "
                "CIVICCAST_CONTRIBUTOR_UPLOAD_DIR_MAX_BYTES."
            ),
        )
    if used_percent >= 80:
        return SystemHealthCheck(
            id="contributor-upload-storage",
            label="Contributor upload storage",
            kind="optional",
            required=False,
            state="needs_attention",
            color="yellow",
            message=message,
            next_step="Review pending contributor submissions before this directory fills up.",
        )
    return SystemHealthCheck(
        id="contributor-upload-storage",
        label="Contributor upload storage",
        kind="optional",
        required=False,
        state="ready",
        color="green",
        message=message,
        next_step="No action needed.",
    )


def _caption_device_health_check() -> SystemHealthCheck:
    """Show caption configuration without claiming observed inference activity.

    Whistle is the CPU primary. The presence-gated Whisper device decision
    describes fallback configuration, including explicit operator overrides;
    it cannot establish that a worker is running or that CUDA loaded.

    Always informational: ``kind="optional"``/``required=False`` and never a
    non-green color, because CPU captioning is the pack contract's validated,
    fully-functional baseline -- this check exists so the operator can SEE
    the GPU path is available once the component pack ships, never so it can
    block readiness in the meantime. Imports ``station_runtime`` lazily
    (module import graph hygiene, matching every other lazy import in this
    file) rather than at module scope; ``station_runtime`` itself imports
    ``pynvml`` only inside its private probe, never at its own module scope,
    so neither module pays for pynvml's absence. Uses only
    ``station_runtime``'s PUBLIC ``resolve_whisper_device`` /
    ``whisper_device_capability`` pair -- never that module's private probe
    or threshold directly -- so this check's explanation can never drift
    from the decision it is explaining.

    Passes the SAME two roots ``resolve_whisper_device`` actually gates cuda
    selection on: ``CIVICCAST_NATIVE_STATION_ROOT`` (the elevated staging
    root the running control plane already carries, set by
    ``load_native_station_environment``) and the chain-H1 acquisition root
    ``<PROGRAMDATA>\\CivicCast`` (``default_program_data_root() /
    "CivicCast"`` -- the SAME value ``load_native_station_environment``
    computes for the identical reason). Without the second root a
    GUI-downloaded CUDA runtime component would be invisible to both the
    resolver and this check, and worse, the two could disagree with each
    other about why captions are on CPU.
    """

    from civiccast.native.station_runtime import (
        resolve_whisper_device,
        whisper_device_capability,
    )
    from civiccast.native.supervisor.install_layout import default_program_data_root

    from .station_state import resolve_live_captions_enabled_or_default

    install_root_value = os.environ.get("CIVICCAST_NATIVE_STATION_ROOT", "").strip()
    install_root = Path(install_root_value) if install_root_value else None
    acquisition_root = default_program_data_root() / "CivicCast"
    device, _compute_type = resolve_whisper_device(install_root, acquisition_root=acquisition_root)
    capable_gpu, libs_present = whisper_device_capability(
        install_root, acquisition_root=acquisition_root
    )
    caption_state = (
        "" if resolve_live_captions_enabled_or_default() else "Live captions are disabled. "
    )
    primary = caption_state + "Whistle device: CPU. "

    if device == "cuda":
        return SystemHealthCheck(
            id="caption-device",
            label="Caption inference device",
            kind="optional",
            required=False,
            state="ready",
            color="green",
            message=primary + "Whisper device: GPU (CUDA) selected.",
            next_step="See channel caption status for the current engine and health.",
        )
    if capable_gpu and not libs_present:
        return SystemHealthCheck(
            id="caption-device",
            label="Caption inference device",
            kind="optional",
            required=False,
            state="ready",
            color="green",
            message=(
                primary + "Whisper device: CPU selected. "
                "GPU fallback requires the GPU runtime component."
            ),
            next_step="Install the GPU runtime component to make GPU fallback available.",
        )
    return SystemHealthCheck(
        id="caption-device",
        label="Caption inference device",
        kind="optional",
        required=False,
        state="ready",
        color="green",
        message=primary + "Whisper device: CPU selected.",
        next_step="See channel caption status for the current engine and health.",
    )


def _live_source_health_check(
    live_source_count: int | None,
    *,
    live_preflight_ready: bool,
) -> SystemHealthCheck:
    if live_source_count is None:
        return SystemHealthCheck(
            id="source-preflight",
            label="Camera or meeting source",
            kind="required",
            required=True,
            state="needs_it_help",
            color="red",
            message="CivicCast could not check configured camera sources.",
            next_step="Confirm the API is connected to its database and refresh System Health.",
        )
    if live_source_count > 0 and live_preflight_ready:
        return SystemHealthCheck(
            id="source-preflight",
            label="Camera or meeting source",
            kind="required",
            required=True,
            state="ready",
            color="green",
            message=f"{live_source_count} camera or meeting source passed preflight.",
            next_step="Keep the source running until the meeting ends.",
        )
    if live_source_count > 0:
        return SystemHealthCheck(
            id="source-preflight",
            label="Camera or meeting source",
            kind="required",
            required=True,
            state="needs_attention",
            color="yellow",
            message=f"{live_source_count} camera or meeting source is configured, but live preflight has not passed in this report.",
            next_step="Open Run Meeting and run preflight before going live.",
        )
    return SystemHealthCheck(
        id="source-preflight",
        label="Camera or meeting source",
        kind="required",
        required=True,
        state="not_set_up",
        color="red",
        message="No camera or meeting source is configured.",
        next_step="Open Run Meeting and add the camera, encoder, Zoom, or NDI source.",
    )


def _headend_readiness_health_check(rollup: Any) -> SystemHealthCheck:
    """Roll up headend stream verification (cable automation CA-7).

    Optional check, like channel automation: cable delivery problems alarm
    the operator without blocking meeting-broadcast readiness.
    """

    if rollup.udp_channels == 0:
        return SystemHealthCheck(
            id="headend-readiness",
            label="Cable headend verification",
            kind="optional",
            required=False,
            state="not_set_up",
            color="green",
            message="No channels deliver to a cable headend.",
            next_step=(
                "Apply a headend delivery preset on a channel to start "
                "sending it to your cable operator."
            ),
        )
    if not rollup.tsduck_installed:
        return SystemHealthCheck(
            id="headend-readiness",
            label="Cable headend verification",
            kind="optional",
            required=False,
            state="needs_attention",
            color="yellow",
            message=(
                f"{rollup.udp_channels} channel(s) deliver to a headend but "
                "TSDuck is not installed, so the stream cannot be verified."
            ),
            next_step=(
                "Install the free TSDuck toolkit from tsduck.io (or set "
                "CIVICCAST_TSDUCK_PATH) and run a verification from the "
                "channel's headend panel."
            ),
        )
    if rollup.fails:
        failing = ", ".join(sorted(rollup.fails))
        return SystemHealthCheck(
            id="headend-readiness",
            label="Cable headend verification",
            kind="optional",
            required=False,
            state="needs_attention",
            color="red",
            message=f"Last verification FAILED on: {failing}.",
            next_step=(
                "Open the channel's headend panel, read the failing checks, "
                "and re-run the verification after fixing the cause."
            ),
        )
    if rollup.passes:
        passing = ", ".join(sorted(rollup.passes))
        return SystemHealthCheck(
            id="headend-readiness",
            label="Cable headend verification",
            kind="optional",
            required=False,
            state="ready",
            color="green",
            message=f"Last verification passed on: {passing}.",
            next_step=("Re-run a verification after changing encode or delivery settings."),
        )
    return SystemHealthCheck(
        id="headend-readiness",
        label="Cable headend verification",
        kind="optional",
        required=False,
        state="needs_attention",
        color="yellow",
        message=(f"{rollup.udp_channels} headend channel(s) have never been verified."),
        next_step=(
            "Run a verification from the channel's headend panel while the channel is on air."
        ),
    )


def _channel_automation_health_check(rollup: Any) -> SystemHealthCheck:
    """Roll up auto_start channel state (cable automation CA-4).

    Optional check: a dark cable channel should alarm the operator without
    blocking meeting-broadcast readiness (the meeting path has its own
    required checks).
    """

    if rollup.automated == 0:
        return SystemHealthCheck(
            id="channel-automation",
            label="24/7 channel automation",
            kind="optional",
            required=False,
            state="not_set_up",
            color="green",
            message="No channels are set to run 24/7.",
            next_step=(
                "Turn on auto-start in a channel's egress settings to run it around the clock."
            ),
        )
    if rollup.dark:
        dark_list = ", ".join(sorted(rollup.dark))
        return SystemHealthCheck(
            id="channel-automation",
            label="24/7 channel automation",
            kind="optional",
            required=False,
            state="needs_attention",
            color="red",
            message=(
                f"{len(rollup.dark)} of {rollup.automated} automated channel(s) "
                f"are dark: {dark_list}."
            ),
            next_step=(
                "Open the channel's egress screen, check the last error, and "
                "start the channel; the automation driver will keep it running."
            ),
        )
    return SystemHealthCheck(
        id="channel-automation",
        label="24/7 channel automation",
        kind="optional",
        required=False,
        state="ready",
        color="green",
        message=(
            f"{rollup.automated} automated channel(s): {rollup.on_air} on air, "
            f"{rollup.on_slate} on schedule-gap filler."
        ),
        next_step="Review the program log if filler time is higher than expected.",
    )


def _recording_path_health_check(
    recording_target_count: int | None,
    *,
    write_probe_ready: bool,
) -> SystemHealthCheck:
    if recording_target_count is None:
        return SystemHealthCheck(
            id="recording-path",
            label="Local recording",
            kind="required",
            required=True,
            state="needs_it_help",
            color="red",
            message="CivicCast could not check recording targets.",
            next_step="Confirm the API is connected to its database and refresh System Health.",
        )
    if recording_target_count > 0 and write_probe_ready:
        return SystemHealthCheck(
            id="recording-path",
            label="Local recording",
            kind="required",
            required=True,
            state="ready",
            color="green",
            message=f"{recording_target_count} recording target passed a write/read/delete proof.",
            next_step="Keep enough free disk space for the whole meeting.",
        )
    if recording_target_count > 0:
        return SystemHealthCheck(
            id="recording-path",
            label="Local recording",
            kind="required",
            required=True,
            state="needs_attention",
            color="yellow",
            message=f"{recording_target_count} recording target is configured, but no write/read/delete proof is attached to this report.",
            next_step="Run the recording-path proof before relying on this station for a public meeting.",
        )
    return SystemHealthCheck(
        id="recording-path",
        label="Local recording",
        kind="required",
        required=True,
        state="not_set_up",
        color="red",
        message="No local recording target is configured.",
        next_step="Choose where CivicCast should save meeting recordings.",
    )


def _resident_portal_health_check(
    preview: ResidentPreview,
    *,
    preview_confirmed: bool,
) -> SystemHealthCheck:
    if preview.status == "available" and preview_confirmed:
        return SystemHealthCheck(
            id="resident-portal",
            label="Resident portal",
            kind="required",
            required=True,
            state="ready",
            color="green",
            message="The resident preview URL is configured and confirmed for this report.",
            next_step="Keep the resident preview open before the meeting starts.",
        )
    if preview.status == "available":
        return SystemHealthCheck(
            id="resident-portal",
            label="Resident portal",
            kind="required",
            required=True,
            state="needs_attention",
            color="yellow",
            message="A public resident portal URL is configured, but the resident preview has not been confirmed in this report.",
            next_step="Open the preview and confirm residents can see the meeting page.",
        )
    return SystemHealthCheck(
        id="resident-portal",
        label="Resident portal",
        kind="required",
        required=True,
        state="needs_attention",
        color="yellow",
        message="Resident preview is available locally, but no public portal URL is configured.",
        next_step="Set the public portal URL before sending residents to the meeting page.",
    )


def _station_policy_health_check(setup: StationSetupState) -> SystemHealthCheck:
    if setup.setup_complete:
        return SystemHealthCheck(
            id="station-policy",
            label="Station policy",
            kind="required",
            required=True,
            state="ready",
            color="green",
            message="Default station policy allows required portal broadcast with optional surfaces off.",
            next_step="Adjust caption, archive, and provider policy after the first successful rehearsal.",
        )
    return SystemHealthCheck(
        id="station-policy",
        label="Station policy",
        kind="required",
        required=True,
        state="not_set_up",
        color="red",
        message="Station policy is not active until first-admin setup is complete.",
        next_step="Complete Setup before running a meeting.",
    )


def _provider_health_checks(first_run_checks: list[HealthCheckItem]) -> list[SystemHealthCheck]:
    provider_ids = {
        "youtube": "YouTube",
        "subscriber-notifications": "Subscriber notices",
        "activitypub": "Federation",
        "internet-archive": "Internet Archive",
        "local-nas": "Archive storage",
        "mtls-local-ca": "Internal service certificates",
    }
    advanced_ids = {"mtls-local-ca"}
    checks: list[SystemHealthCheck] = []
    for item in first_run_checks:
        if item.id not in provider_ids:
            continue
        advanced = item.id in advanced_ids
        ready = item.state == "ok"
        checks.append(
            SystemHealthCheck(
                id=item.id,
                label=provider_ids[item.id],
                kind="advanced" if advanced else "optional",
                required=False,
                state="ready" if ready else "needs_it_help" if advanced else "needs_attention",
                color="green" if ready else "yellow",
                message=item.message,
                next_step=item.next_step,
            )
        )
    return checks


def _external_target_check(
    *,
    check_id: str,
    label: str,
    env_name: str,
    credential: str | None,
    configured_message: str,
    missing_message: str,
    configured_next_step: str,
) -> HealthCheckItem:
    """Return a blocked external-target check until live proof exists."""

    if credential:
        return HealthCheckItem(
            id=check_id,
            label=label,
            state="credential_or_secret_required",
            message=configured_message,
            next_step=configured_next_step,
        )
    return HealthCheckItem(
        id=check_id,
        label=label,
        state="credential_or_secret_required",
        message=missing_message,
        next_step=f"Set {env_name}, then rerun the first-run health check.",
    )


def _mtls_local_ca_check() -> HealthCheckItem:
    """Verify required local-CA mTLS readiness through the cert package.

    NATS JetStream was removed from the product (owner decision 2026-08-20;
    see ADR 0023, which supersedes ADR 0001) -- this used to be paired with
    ``_nats_jetstream_check`` (deleted); the local-CA mTLS check stands on
    its own now, covering only the ``civiccast-api`` and ``civiccast-worker``
    service identities.
    """

    try:
        from civiccast.certs import readiness

        ready = readiness.check_mtls_readiness()
    except Exception as exc:
        return HealthCheckItem(
            id="mtls-local-ca",
            label="Local CA mTLS",
            state="failed",
            message=f"Local CA mTLS certificate readiness is blocked: {exc}",
            next_step=(
                "Run `civiccast cert rotate civiccast-api` and `civiccast cert rotate "
                "civiccast-worker`, then rerun installer health-check."
            ),
        )
    if ready is True:
        return HealthCheckItem(
            id="mtls-local-ca",
            label="Local CA mTLS",
            state="ok",
            message="Local CA and required service certificates are present and valid.",
            next_step="Rotate internal service certificates every 90 days.",
        )
    return HealthCheckItem(
        id="mtls-local-ca",
        label="Local CA mTLS",
        state="failed",
        message="Local CA mTLS readiness did not return a positive proof.",
        next_step="Inspect certificates with the installer readiness command and rotate stale identities.",
    )


def _local_nas_check(nas_path: str | None) -> HealthCheckItem:
    """Verify the configured NAS target with a real write/hash/delete probe."""

    if not nas_path:
        return HealthCheckItem(
            id="local-nas",
            label="Local NAS",
            state="failed",
            message="Local NAS verification is blocked until an archive path is configured.",
            next_step="Configure CIVICCAST_NAS_ARCHIVE_PATH, then rerun verification.",
        )

    archive_path = Path(nas_path)
    if not archive_path.exists() or not archive_path.is_dir():
        return HealthCheckItem(
            id="local-nas",
            label="Local NAS",
            state="failed",
            message=f"Local NAS path {archive_path} is not a reachable directory.",
            next_step=(
                "Create the directory or set CIVICCAST_NAS_ARCHIVE_PATH to a mounted archive "
                "directory, then rerun verification."
            ),
        )

    probe_bytes = b"CivicCast first-run local NAS verification\n"
    expected_hash = sha256(probe_bytes).hexdigest()
    probe_path = archive_path / f".civiccast-first-run-{uuid4().hex}.probe"
    try:
        probe_path.write_bytes(probe_bytes)
        observed_hash = sha256(probe_path.read_bytes()).hexdigest()
    except OSError as exc:
        return HealthCheckItem(
            id="local-nas",
            label="Local NAS",
            state="failed",
            message=f"Local NAS path {archive_path} could not be written and read: {exc}.",
            next_step="Fix archive directory permissions or mount state, then rerun verification.",
        )
    try:
        probe_path.unlink()
    except OSError as exc:
        return HealthCheckItem(
            id="local-nas",
            label="Local NAS",
            state="failed",
            message=f"Local NAS path {archive_path} could not delete its probe file: {exc}.",
            next_step=(
                "Fix archive directory permissions, retention rules, or sync locks, then rerun "
                "verification."
            ),
        )
    if probe_path.exists():
        return HealthCheckItem(
            id="local-nas",
            label="Local NAS",
            state="failed",
            message=f"Local NAS path {archive_path} left the probe file behind after delete.",
            next_step=(
                "Fix archive directory permissions, retention rules, or sync locks, then rerun "
                "verification."
            ),
        )

    if observed_hash != expected_hash:
        return HealthCheckItem(
            id="local-nas",
            label="Local NAS",
            state="failed",
            message="Local NAS verification wrote a probe file but read back a different hash.",
            next_step="Check the archive storage path for corruption or sync filters, then rerun.",
        )

    return HealthCheckItem(
        id="local-nas",
        label="Local NAS",
        state="ok",
        message="Local NAS archive path accepted a write/read/delete hash probe.",
        next_step="Keep CIVICCAST_NAS_ARCHIVE_PATH mounted before approving publish.",
    )


def _activitypub_health_check() -> HealthCheckItem:
    from civiccast.activitypub.config import load_activitypub_config

    config = load_activitypub_config()
    if config.federation_mode == "disabled":
        return HealthCheckItem(
            id="activitypub",
            label="ActivityPub federation",
            state="ok",
            message="ActivityPub federation is disabled by default, so the station actor is not exposed.",
            next_step="Leave disabled unless the station intentionally opts into federation.",
        )
    return HealthCheckItem(
        id="activitypub",
        label="ActivityPub federation",
        state="ok",
        message=f"ActivityPub is configured in {config.federation_mode} mode with explicit station identity and key material.",
        next_step="Run a signed federation smoke test before publicizing the station actor.",
    )


def build_model_bundle_manifest(request: ModelBundleRequest) -> ModelBundleManifest:
    """Build a hash manifest for air-gapped model installs."""

    items: list[ModelBundleItem] = []
    if request.include_captions:
        items.append(
            ModelBundleItem(
                id="faster-whisper-large-v3",
                label="Caption model",
                filename="faster-whisper-large-v3.tar.zst",
                sha256=_MODEL_BUNDLE_HASHES["faster-whisper-large-v3"],
            )
        )
    if request.include_summary:
        items.append(
            ModelBundleItem(
                id="gemma-4-12b-summary",
                label="Summary model (12B, >=16GB default)",
                filename="gemma-4-12b-summary.tar.zst",
                sha256=_MODEL_BUNDLE_HASHES["gemma-4-12b-summary"],
            )
        )
        items.append(
            ModelBundleItem(
                id="gemma-4e4b-summary",
                label="Summary model (e4b, <16GB fallback)",
                filename="gemma-4e4b-summary.tar.zst",
                sha256=_MODEL_BUNDLE_HASHES["gemma-4e4b-summary"],
            )
        )
    if request.include_translation:
        items.append(
            ModelBundleItem(
                id="translate-gemma-4b",
                label="Translation model",
                filename="translate-gemma-4b.tar.zst",
                sha256=_MODEL_BUNDLE_HASHES["translate-gemma-4b"],
            )
        )
    # The 12B summary model adds ~8GB to the air-gap bundle on top of the e4b trio.
    estimated_size_gb = 20.0 if request.include_summary else 12.0
    return ModelBundleManifest(
        profile=request.profile,
        bundle_name=f"civiccast-models-{request.profile}-v{__version__}.tar",
        estimated_size_gb=estimated_size_gb,
        items=items,
    )


def operator_console_url() -> str:
    """Return the operator console handoff target used by installer surfaces.

    Used to append a ``?nonce=`` query param from ``CIVICCAST_SETUP_NONCE``.
    That installer-handoff nonce was retired 2026-08-29 (owner decision): the
    control plane binds ``127.0.0.1`` only, so first setup is unreachable
    from the network by construction and the nonce was a redundant,
    failure-prone gate. The URL is now always the plain console target.
    """

    default_url = (
        "http://127.0.0.1:8000/operator/"
        if os.getenv("CIVICCAST_OPERATOR_CONSOLE_DIST")
        else "http://127.0.0.1:5173"
    )
    return os.getenv("CIVICCAST_OPERATOR_CONSOLE_URL", default_url)


_OPTIONAL_INSTALLER_LANE_IDS = frozenset({"ffmpeg", "ndi"})
"""Lanes that report an optional local capability.

CivicCast starts, serves the operator dashboard, and answers ``/health``
without FFmpeg on PATH or an NDI runtime present. Neither is resolved while
starting the service: ``civiccast.stream._ffmpeg`` looks the binary up with
``shutil.which`` inside ``run_ffmpeg``/``start_ffmpeg`` (call time, not
import time), and ``check_ndi_runtime`` is only reached from the CLI, the
egress NDI relay, and the beta-handoff proof. These lanes therefore keep
reporting their own truth (an absent capability stays not-ready and says
what it costs), but they do not decide whether the install is usable.
Overall readiness is computed over the remaining, required lanes so that
"CivicCast is running but cannot process video" and "CivicCast is broken"
are distinguishable states instead of one shared red.
"""


_NATIVE_STATION_PLATFORM_READY_NEXT_STEP = (
    "CivicCast is installed and activated on this computer. Continue setup and open the dashboard."
)
_NATIVE_STATION_PLATFORM_BLOCKED_NEXT_STEP = (
    "CivicCast is installed on this computer, but setup has not finished: its "
    "station files and packaged caption model are not activated yet. Let the "
    "CivicCast installer finish setting up this computer, then reopen this window."
)


def _native_station_activated() -> bool:
    """True when this native station has completed activation.

    This is the native platform prerequisite the WSL tooling check stood in
    for, and it is a real, falsifiable one. The supervisor sets
    ``CIVICCAST_NATIVE_STATION=1`` only after
    ``load_native_station_environment`` has validated the station-set
    identity, the activation self-test receipt, and the packaged caption
    model; on a fresh, installed-but-not-activated station it catches
    ``NativeStationNotActivatedError`` and starts the control plane with NO
    station env at all (``native/supervisor/service.py``). Absence therefore
    means "setup has not finished", and the lane fails CLOSED. The manifest
    file is re-checked on disk so a stale exported variable alone can never
    turn the lane green.
    """

    if os.environ.get("CIVICCAST_NATIVE_STATION", "").strip() != "1":
        return False
    manifest = os.environ.get("CIVICCAST_NATIVE_STATION_MANIFEST", "").strip()
    if not manifest:
        return False
    try:
        return Path(manifest).is_file()
    except OSError:
        return False


def build_installer_summary() -> InstallerSummary:
    """Build the tester install summary without mixing in release-proof lanes."""

    is_windows = platform.system().lower() == "windows"
    # The platform lane asserts "CivicCast has a supported place to run", and
    # WHAT that means depends on the deployment. ``build_bootstrap_plan``
    # (Linux/macOS only, see civiccast.installer.platform's module doc)
    # answers it for those two; Windows is decided entirely by this
    # process's own native-station signals below, never by a generic
    # multi-OS plan -- the retired WSL2 lane used to be the ONLY thing that
    # answered it for Windows, which meant a native station could never
    # clear this lane no matter how healthy it was until that lane's own
    # activation state was consulted directly.
    platform_plan = None if is_windows else _detected_bootstrap_plan()
    storage = durable_storage_status()
    storage_ready = storage.status == "ready"
    runtime_ready = sys.version_info >= (3, 12)
    ffmpeg_ready = shutil.which("ffmpeg") is not None
    if is_windows:
        platform_ready = _native_station_activated()
        platform_status: Literal["ready", "blocked"] = "ready" if platform_ready else "blocked"
        platform_next_step = (
            _NATIVE_STATION_PLATFORM_READY_NEXT_STEP
            if platform_ready
            else _NATIVE_STATION_PLATFORM_BLOCKED_NEXT_STEP
        )
    else:
        assert platform_plan is not None  # narrows for the type checker
        platform_ready = platform_plan.status == "ready"
        platform_status = platform_plan.status
        platform_blocker = " ".join(platform_plan.blockers)
        platform_next_step = (
            platform_plan.next_step
            if platform_plan.status == "ready"
            else f"{platform_blocker} {platform_plan.next_step}".strip()
        )
    service_ready = platform_ready and storage_ready
    lanes = [
        InstallerLane(
            id="platform",
            label="Setting up CivicCast",
            status=platform_status,
            ready=platform_ready,
            next_step=platform_next_step,
        ),
        InstallerLane(
            id="runtime",
            label="Preparing CivicCast tools",
            status="ready" if runtime_ready else "blocked",
            ready=runtime_ready,
            next_step=(
                "The tools CivicCast needs are available."
                if runtime_ready
                else "Choose Repair so CivicCast can finish preparing its local tools."
            ),
        ),
        InstallerLane(
            id="ffmpeg",
            label="Preparing video tools",
            # "unavailable", not "blocked": the installer GUI keys both its
            # "Repair this step" button (lane-affordances.ts canRepairLane)
            # and its dependency-repair action off "blocked"/"error", and
            # that repair path (main.rs's launch_civiccast_runtime_bootstrap)
            # restarts the native runtime host -- it cannot install a missing
            # ffmpeg dependency onto the control plane's PATH. An affordance
            # that cannot deliver is worse than none, so this lane offers the
            # remedy that works.
            status="ready" if ffmpeg_ready else "unavailable",
            ready=ffmpeg_ready,
            next_step=(
                "FFmpeg is available for ingest, packaging, and upload checks."
                if ffmpeg_ready
                else (
                    "CivicCast cannot find FFmpeg on this computer. CivicCast still "
                    "starts and opens the dashboard, but recording ingest, packaging, "
                    "and upload checks stay unavailable. Install FFmpeg on this "
                    "computer, make sure it is on PATH, then reopen this installer."
                )
            ),
        ),
        InstallerLane(
            id="ndi",
            label="Checking optional NDI support",
            status="ready",
            ready=True,
            next_step=(
                "Optional NDI camera support is available."
                if _ndi_runtime_detected()
                else "NDI is optional. Skip this unless this station uses NDI cameras."
            ),
        ),
        InstallerLane(
            id="storage",
            label="Preparing local storage",
            status="ready" if storage_ready else "planned",
            ready=storage_ready,
            # An EXTERNAL DATABASE_URL station cannot "Prepare storage" -- the
            # setup routes return durable_storage_status() unchanged when
            # DATABASE_URL is set (installer/router.py staff_storage_setup /
            # public_storage_setup), so that copy would name an action which
            # provably does nothing. The probe already worked out WHICH of
            # unreachable / schema-behind / database-missing / misconfigured
            # this is (installer/storage.py _probe_external_database) and wrote
            # both a diagnosis and a remedy for each. This lane's next_step is
            # the ONLY field the installer GUI renders for a lane (api.ts
            # fromApiSummary maps next_step to both `detail` and `nextStep`),
            # so it carries both halves -- the same "<blocker> <next step>"
            # shape the platform lane above already uses. The managed/SQLite
            # path keeps its wording verbatim: it can never produce one of
            # those statuses.
            next_step=(
                "Local database and upload storage are ready."
                if storage_ready
                else (
                    f"{storage.operator_message} {storage.next_step}"
                    if storage.status in EXTERNAL_DATABASE_NOT_READY_STATUSES
                    else "Choose Prepare storage so CivicCast can create its database, upload folder, and migrations."
                )
            ),
        ),
        InstallerLane(
            id="secrets",
            label="Generating local secrets",
            status="ready" if storage_ready else "planned",
            ready=storage_ready,
            next_step=(
                "Local secrets are generated and stored by the setup flow."
                if storage_ready
                else "Prepare storage first; CivicCast will generate local secrets during setup."
            ),
        ),
        InstallerLane(
            id="service",
            label="Starting CivicCast",
            status="ready" if service_ready else "blocked",
            ready=service_ready,
            next_step=(
                "CivicCast is ready to start."
                if service_ready
                else "Finish the blocked installer steps, then choose Repair if CivicCast still does not start."
            ),
        ),
        InstallerLane(
            id="dashboard",
            label="Opening the dashboard",
            status="ready" if service_ready else "planned",
            ready=service_ready,
            next_step=(
                "Open the operator console, create the first admin, and run rehearsal."
                if service_ready
                else "CivicCast will open the dashboard after setup finishes."
            ),
        ),
    ]
    # ``platform`` is a CONTRACT the installer GUI switches affordances on, not
    # a label: ``apps/installer/src/lane-affordances.ts``'s
    # ``isWindowsPlatform`` gates "Open installer log" on it being
    # "windows-native". "windows-wsl2" is kept in the type only so a
    # pre-native build's on-disk state or cached progress still type-checks
    # (see api.ts's ``withHonestNativePlatform``); this function never
    # PRODUCES it -- every Windows control plane running today's code is the
    # native station, full stop.
    platform_field: Literal["linux", "macos", "windows-native", "windows-wsl2"]
    if is_windows:
        platform_field = "windows-native"
    else:
        assert platform_plan is not None  # narrows for the type checker
        platform_field = platform_plan.os_family
    return InstallerSummary(
        ready=all(lane.ready for lane in lanes if lane.id not in _OPTIONAL_INSTALLER_LANE_IDS),
        platform=platform_field,
        operator_console_url=operator_console_url(),
        lanes=lanes,
    )


def _detected_bootstrap_plan() -> PlatformBootstrapPlan:
    """Detect the runtime that is evaluating installer readiness.

    Only ever called for the Linux/macOS native deployments -- the caller
    (``build_installer_summary``) branches Windows off before reaching this,
    since Windows readiness is decided by this process's own native-station
    signals, never by a generic multi-OS plan. This function used to also
    detect a Linux process running inside a WSL2 Ubuntu guest (the retired
    WSL product's own control plane) and a bare Windows host; both branches
    were removed with that product under the owner's "no linux" decision
    (2026-08-19).
    """

    system = platform.system().lower()
    os_family: OsFamily = "macos" if system == "darwin" else "linux"
    if os_family == "linux":
        return build_bootstrap_plan(
            os_family="linux",
            detected_tools={
                "systemd": Path("/run/systemd/system").exists(),
                "package_manager": "apt" if shutil.which("apt") else "dnf",
            },
        )
    return build_bootstrap_plan(
        os_family="macos",
        detected_tools={
            "launchd": shutil.which("launchctl") is not None,
            "pkgbuild": shutil.which("pkgbuild") is not None,
        },
    )


def _ndi_runtime_detected() -> bool:
    if os.getenv("CIVICCAST_NDI_RUNTIME_DIR"):
        return True
    candidates = []
    if os.name == "nt":
        program_files = os.getenv("PROGRAMFILES")
        program_files_x86 = os.getenv("PROGRAMFILES(X86)")
        for root in [program_files, program_files_x86]:
            if root:
                candidates.extend(
                    [
                        Path(root) / "NDI" / "NDI 6 Runtime",
                        Path(root) / "NDI" / "NDI 5 Runtime",
                    ]
                )
    else:
        candidates.extend(
            [
                Path("/usr/lib/libndi.so"),
                Path("/usr/local/lib/libndi.so"),
                Path("/Library/NDI SDK for Apple/lib/macOS/libndi.dylib"),
            ]
        )
    return any(path.exists() for path in candidates)
