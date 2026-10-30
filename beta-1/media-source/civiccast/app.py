# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI app for the CivicCast umbrella.

Sprint 0.1 ships three endpoints:

- /health           — liveness probe, no auth, returns build info
- /api/version      — version string
- /api/hardware     — hardware probe per spec §5.4 / §7.7

Later rungs add /api/ollama/*, /staff/*, /public/*, etc. The umbrella's
FastAPI app is the single mount point for all platform-substrate and
module routers in Mode A; in Mode B the host suite mounts these routers
into its own app.
"""

from __future__ import annotations

import builtins
import logging
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, cast

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi import status as http_status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from civiccast._version import __version__
from civiccast.activitypub.config import load_activitypub_config
from civiccast.activitypub.rate_limit import InboxRateLimiter
from civiccast.activitypub.remote import HttpxActivityPubDeliveryClient, HttpxRemoteActorFetcher
from civiccast.activitypub.retry_worker import ActivityPubRetrySettings, ActivityPubRetryWorker
from civiccast.activitypub.router import router as activitypub_router
from civiccast.activitypub.store import InMemoryActivityPubStore, PostgresActivityPubStore
from civiccast.agenda.router import (
    get_agenda_service,
    get_agenda_store,
)
from civiccast.agenda.router import public_router as agenda_public_router
from civiccast.agenda.router import staff_router as agenda_staff_router
from civiccast.agenda.service import AgendaService
from civiccast.agenda.store import AgendaStore
from civiccast.agenda_import.provenance import AgendaImportProvenanceStore
from civiccast.agenda_import.router import get_agenda_import_provenance_store
from civiccast.agenda_import.router import router as agenda_import_staff_router
from civiccast.ai_models.router import get_ai_model_service
from civiccast.ai_models.router import staff_router as ai_models_staff_router
from civiccast.ai_models.runtime import (
    build_caption_runtime,
    build_summary_model,
    build_translator,
)
from civiccast.ai_models.service import AiModelService
from civiccast.ai_models.store import AiModelStore
from civiccast.ai_runtime.ollama_client import OllamaRuntimeUnavailableError
from civiccast.alerting.credentials import FileCredentialStore, credential_reader
from civiccast.alerting.delivery import AlertDeliveryDispatch, AlertRetryWorker
from civiccast.alerting.evaluator import AlertEvaluator
from civiccast.alerting.resource_sampler import default_resource_probes
from civiccast.alerting.router import (
    get_alerting_session_factory,
    get_credential_writer,
    get_self_test_deps,
)
from civiccast.alerting.router import staff_router as alerting_staff_router
from civiccast.alerting.self_test import default_self_test_deps
from civiccast.alerting.store import record_alert_condition
from civiccast.alerting.worker import AlertingMaintenanceSettings, AlertingMaintenanceWorker
from civiccast.analytics.pg_store import (
    AnalyticsRollupSettings,
    AnalyticsRollupWorker,
    PostgresAnalyticsStore,
    backfill_json_events,
)
from civiccast.analytics.router import staff_router as analytics_staff_router
from civiccast.analytics.store import AnalyticsStore, default_analytics_state_path
from civiccast.app_platform.build_router import (
    build_staff_router as app_platform_build_staff_router,
)
from civiccast.app_platform.router import public_router as app_platform_public_router
from civiccast.app_platform.router import staff_router as app_platform_staff_router
from civiccast.auth.cors import cors_allowed_origins
from civiccast.auth.middleware import staff_auth_middleware
from civiccast.auth.rate_limit import AuthRateLimiter, validate_auth_rate_limit_config
from civiccast.auth.router import staff_router as auth_staff_router
from civiccast.auth.security_headers import security_headers_middleware
from civiccast.auth.store import PostgresStaffTokenStore
from civiccast.auth.tokens import validate_staff_token_config
from civiccast.cable.router import public_router as cable_public_router
from civiccast.cable.router import staff_router as cable_staff_router
from civiccast.captions.cdn_republish import VodPackageCdnRepublisher
from civiccast.captions.persistence import (
    PostgresCaptionReviewStore,
    PostgresOfflineCaptionJobStore,
)
from civiccast.captions.review import InMemoryCaptionReviewStore
from civiccast.captions.router import staff_router as captions_staff_router
from civiccast.captions.settings import (
    RecordedCaptionSettings,
    RecordedCaptionSettingsStore,
    default_recorded_caption_settings_path,
)
from civiccast.captions.settings_router import staff_router as recorded_caption_settings_router
from civiccast.captions.tap_worker import CaptionTapWorkerSettings, build_tap_worker
from civiccast.captions.vod_job import (
    InMemoryOfflineCaptionJobStore,
    OfflineCaptionJobSettings,
    OfflineCaptionJobWorker,
)
from civiccast.cg.board_router import board_staff_router as cg_board_staff_router
from civiccast.cg.board_router import (
    get_cg_audio_asset_resolver,
    get_cg_board_service,
    get_cg_depth_store,
)
from civiccast.cg.board_service import CgBoardService
from civiccast.cg.board_store import CgBoardStore
from civiccast.cg.bulletin_expiry_worker import BulletinExpirySettings, BulletinExpiryWorker
from civiccast.cg.bulletin_store import PostgresCgBulletinStore
from civiccast.cg.depth_store import CgDepthStore
from civiccast.cg.router import get_cg_board_service as get_cg_feed_board_service
from civiccast.cg.router import get_cg_bulletin_store, get_eas_overlay_provider
from civiccast.cg.router import public_router as cg_public_router
from civiccast.cg.router import staff_router as cg_staff_router
from civiccast.contribute.router import ContributorUploadByteBudget
from civiccast.contribute.router import public_router as contribute_public_router
from civiccast.contribute.router import staff_router as contribute_staff_router
from civiccast.contribute.store import ContributorUploadReapWorker
from civiccast.control_room.router import (
    get_control_room_service,
    get_control_room_store,
    get_device_secret_writer,
)
from civiccast.control_room.router import staff_router as control_room_staff_router
from civiccast.control_room.secrets import save_device_secret
from civiccast.control_room.service import ControlRoomService
from civiccast.control_room.store import ControlRoomStore
from civiccast.control_room.tsr_client import HttpTsrClient, NullTsrClient, TsrClient
from civiccast.db import bind_engine, connect_options, get_session
from civiccast.db.url import normalize_database_url
from civiccast.docsite.router import router as manual_router
from civiccast.eas.models import EasCapSource
from civiccast.eas.router import get_eas_service, get_eas_store
from civiccast.eas.router import staff_router as eas_staff_router
from civiccast.eas.service import EasDisplayService
from civiccast.eas.store import EasStore
from civiccast.eas.workers import EasPollWorker, SourceHealthHook, build_http_fetcher
from civiccast.egress.audio_router import get_audio_track_store
from civiccast.egress.audio_router import public_router as audio_tracks_public_router
from civiccast.egress.audio_router import staff_router as audio_tracks_staff_router
from civiccast.egress.audio_tracks import AudioTrackStore
from civiccast.egress.automation import ChannelAutomationSettings, build_channel_automation
from civiccast.egress.bulletin_filler import _default_audio_resolver
from civiccast.egress.caption_feed import build_caption_feed_worker
from civiccast.egress.caption_proof_process import CaptionProofProcessSupervisor
from civiccast.egress.caption_proof_worker import build_caption_proof_worker
from civiccast.egress.dispatcher import PlayoutDispatcher
from civiccast.egress.router import get_egress_store, get_takeover_service
from civiccast.egress.router import public_router as egress_public_router
from civiccast.egress.router import staff_router as egress_staff_router
from civiccast.egress.store import PostgresEgressStore
from civiccast.egress.takeover_service import TakeoverService
from civiccast.egress.takeover_store import PostgresTakeoverAuditStore
from civiccast.facility.router import staff_router as facility_staff_router
from civiccast.installer.commissioning_router import get_commissioning_egress_store
from civiccast.installer.commissioning_router import staff_router as commissioning_staff_router
from civiccast.installer.router import get_live_recording_finalizer
from civiccast.installer.router import public_router as installer_public_router
from civiccast.installer.router import staff_router as installer_staff_router
from civiccast.installer.storage import (
    ManagedStorageError,
    ensure_managed_storage,
    load_managed_database_url,
    load_managed_upload_dir,
)
from civiccast.live.cdn_targets import build_asset_cdn_package_target_lookup
from civiccast.live.contribution.bridge import NullVdoNinjaBridge, UrlVdoNinjaBridge
from civiccast.live.contribution.coprocess import (
    ContributionCoprocessSettings,
    ContributionCoprocessSupervisor,
    contribution_diagnostics_snapshot,
    contribution_turn_connectivity_test,
    set_active_supervisor,
)
from civiccast.live.contribution.program import GuestProgramRouter
from civiccast.live.contribution.router import get_contribution_service
from civiccast.live.contribution.router import public_router as contribution_public_router
from civiccast.live.contribution.router import staff_router as contribution_staff_router
from civiccast.live.contribution.service import ContributionService
from civiccast.live.contribution.store import ContributionStore
from civiccast.live.finalization import LiveRecordingFinalizer
from civiccast.live.finalization_worker import (
    FinalizationWorkerSettings,
    FinalizationWorkerSupervisor,
    LiveFinalizationWorker,
    build_worker,
)
from civiccast.live.models import LiveIngestPlan, RecordingTargetCreate
from civiccast.live.network_probe import build_network_probe
from civiccast.live.preflight import PreflightEvaluator
from civiccast.live.readiness_service import LiveSourceReadinessService, TakeoverReadiness
from civiccast.live.recording_paths import (
    DEFAULT_RECORDING_TARGET_DIR_NAME,
    DEFAULT_RECORDING_TARGET_ID,
    DEFAULT_RECORDING_TARGET_NAME,
    REHEARSAL_RECORDING_TARGET_ID,
    local_recording_path,
)
from civiccast.live.relay import build_ingest_plan
from civiccast.live.router import (
    get_live_finalization_worker,
    get_live_relay_config_store,
    get_live_session_store,
    get_live_source_readiness_service,
    get_live_source_store,
    get_preflight_evaluator,
    get_recording_target_store,
)
from civiccast.live.router import public_router as live_public_router
from civiccast.live.router import staff_router as live_staff_router
from civiccast.live.source_probe import build_source_probe
from civiccast.live.storage_probe import build_storage_probe
from civiccast.live.store import (
    LiveRelayConfigStore,
    LiveSessionStore,
    LiveSourceStore,
    RecordingTargetAlreadyExistsError,
    RecordingTargetStore,
)
from civiccast.live.surge_service import SurgeSwitchService
from civiccast.metadata.router import get_custom_field_service
from civiccast.metadata.router import public_router as metadata_public_router
from civiccast.metadata.router import staff_router as metadata_staff_router
from civiccast.metadata.service import CustomFieldService
from civiccast.metadata.store import CustomFieldStore
from civiccast.migrate.router import get_migration_service
from civiccast.migrate.router import staff_router as migrate_staff_router
from civiccast.migrate.service import MigrationService
from civiccast.paywall.router import (
    get_paywall_service,
    get_paywall_store,
)
from civiccast.paywall.router import public_router as paywall_public_router
from civiccast.paywall.router import staff_router as paywall_staff_router
from civiccast.paywall.router import webhook_router as paywall_webhook_router
from civiccast.paywall.service import PaywallService
from civiccast.paywall.store import PaywallStore
from civiccast.platform.hardware import HardwareProbe, probe, public_hardware_probe
from civiccast.platform.providers import PROVIDER_KIND_WEBHOOK, default_registry
from civiccast.platform.station_router import box_profile_router as station_box_profile_router
from civiccast.platform.station_router import staff_router as station_profile_staff_router
from civiccast.platform.stores import AppStoreBundle
from civiccast.platform.worker_runtime import ThreadSupervisor
from civiccast.playback_policy.router import public_router as playback_policy_public_router
from civiccast.playback_policy.router import staff_router as playback_policy_staff_router
from civiccast.podcast.router import public_router as podcast_public_router
from civiccast.podcast.router import staff_router as podcast_staff_router
from civiccast.podcast.store import InMemoryPodcastStore, PostgresPodcastStore
from civiccast.producer_ops.router import get_producer_ops_store
from civiccast.producer_ops.router import staff_router as producer_ops_staff_router
from civiccast.producer_ops.store import ProducerOpsStore
from civiccast.programlog.materializer import ProgramLogMaterializer, ProgramLogSettings
from civiccast.programlog.router import (
    get_program_log_asset_titler,
    get_program_log_materializer,
    get_program_log_store,
)
from civiccast.programlog.router import (
    public_router as programlog_public_router,
)
from civiccast.programlog.router import (
    staff_router as programlog_staff_router,
)
from civiccast.programlog.store import PostgresProgramLogStore
from civiccast.publish.router import staff_router as publish_staff_router
from civiccast.publish.store import InMemoryPublishStore, PostgresPublishStore
from civiccast.publish.targets import (
    SqlChannelAssociationLookup,
    StaticChannelAssociationLookup,
)
from civiccast.recording.input_presets import RecordingInputPresetCatalog
from civiccast.recording.router import (
    get_recording_input_catalog,
    get_recording_service,
    get_recording_store,
)
from civiccast.recording.router import staff_router as recording_staff_router
from civiccast.recording.runtime import (
    FfmpegScheduledCapturePipeline,
    RecordingAlertSink,
    ScheduledRecordingAssetFinalizer,
    ScheduledRecordingSettings,
    ScheduledRecordingWorker,
)
from civiccast.recording.service import RecordingService
from civiccast.recording.store import RecordingStore
from civiccast.records.router import get_disposition_review_reader
from civiccast.records.router import staff_router as records_staff_router
from civiccast.records.store import InMemoryRecordStore, PostgresRecordStore
from civiccast.release_api import staff_router as release_staff_router
from civiccast.reporting.epg import EpgExporter
from civiccast.reporting.router import (
    get_epg_exporter,
    get_reporting_service,
    get_reporting_store,
)
from civiccast.reporting.router import public_router as reporting_public_router
from civiccast.reporting.router import staff_router as reporting_staff_router
from civiccast.reporting.schedule_adapter import PostgresCommittedScheduleReader
from civiccast.reporting.service import ReportingService
from civiccast.reporting.store import ReportingStore
from civiccast.schedule.autoschedule_router import get_autoschedule_service
from civiccast.schedule.autoschedule_router import (
    staff_router as autoschedule_staff_router,
)
from civiccast.schedule.autoschedule_service import AutoScheduleService
from civiccast.schedule.autoschedule_store import AutoScheduleStore
from civiccast.schedule.autoschedule_worker import (
    AutoScheduleCompileSettings,
    AutoScheduleCompileWorker,
)
from civiccast.schedule.commit_service import CommitDryRunService, CommitService
from civiccast.schedule.media_integrity_worker import (
    MediaIntegrityWorker,
    MediaIntegrityWorkerSettings,
)
from civiccast.schedule.media_lifecycle_router import (
    get_media_lifecycle_store,
    get_missing_media_reader,
    get_watch_folder_worker,
)
from civiccast.schedule.media_lifecycle_router import staff_router as media_lifecycle_staff_router
from civiccast.schedule.media_lifecycle_store import MediaLifecycleStore
from civiccast.schedule.media_lifecycle_worker import (
    MediaLifecycleWorker,
    MediaLifecycleWorkerSettings,
)
from civiccast.schedule.models import (
    ASSET_STATE_VALIDATED,
    SCHEDULE_STATE_CANCELLED,
    SCHEDULE_STATE_PUBLISHED,
    SCHEDULE_STATE_SCHEDULED,
    AssetMetadataUpdate,
    AssetStateValue,
    ScheduleItemCreate,
    ScheduleItemResponse,
    ScheduleStateValue,
    StaffAssetRow,
    UploadedAssetResponse,
)
from civiccast.schedule.playout_router import get_commit_service
from civiccast.schedule.playout_router import (
    staff_router as playout_staff_router,
)
from civiccast.schedule.retention_worker import (
    RetentionEnforcementWorker,
    RetentionWorkerSettings,
)
from civiccast.schedule.router import (
    get_postgres_store,
    get_schedule_store,
)
from civiccast.schedule.router import (
    public_router as schedule_public_router,
)
from civiccast.schedule.router import (
    source_media_router as schedule_source_media_router,
)
from civiccast.schedule.router import (
    staff_router as schedule_staff_router,
)
from civiccast.schedule.store import (
    AssetNotFoundError,
    PostgresAssetStore,
    PostgresScheduleStore,
    ScheduleItemNotFoundError,
)
from civiccast.schedule.watch_folder_worker import (
    WatchFolderWorker,
    WatchFolderWorkerSettings,
)
from civiccast.stream.cdn import CDNAdapter
from civiccast.stream.cdn.factory import CdnSettings, build_cdn_adapter
from civiccast.stream.media_router import live_router as media_live_public_router
from civiccast.stream.media_router import router as media_public_router
from civiccast.stream.router import staff_router as stream_staff_router
from civiccast.subscribe.outcome_store import (
    InMemoryNotificationDeliveryStore,
    PostgresNotificationDeliveryStore,
)
from civiccast.subscribe.rate_limit import SubscribeRateLimiter
from civiccast.subscribe.retry_worker import WebhookRetrySettings, WebhookRetryWorker
from civiccast.subscribe.router import public_router as subscribe_public_router
from civiccast.subscribe.router import staff_router as subscribe_staff_router
from civiccast.subscribe.secrets import load_subscription_secrets
from civiccast.subscribe.store import InMemorySubscribeStore, PostgresSubscribeStore
from civiccast.summary.generate import SummaryModel
from civiccast.summary.job import (
    InMemorySummaryGenerationJobStore,
    SummaryGenerationJobSettings,
    SummaryGenerationJobWorker,
)
from civiccast.summary.persistence import PostgresSummaryGenerationJobStore
from civiccast.summary.router import OLLAMA_NOT_CONFIGURED_MESSAGE, get_summary_model
from civiccast.summary.router import staff_router as summary_staff_router
from civiccast.summary.store import InMemorySummaryStore, PostgresSummaryStore
from civiccast.underwriting.router import (
    get_affidavit_service,
    get_trafficking_compiler,
    get_underwriting_store,
)
from civiccast.underwriting.router import staff_router as underwriting_staff_router
from civiccast.underwriting.service import AffidavitService, TraffickingCompiler
from civiccast.underwriting.store import UnderwritingStore
from civiccast.vod.models import AssetMetadata
from civiccast.vod.router import router as vod_router
from civiccast.vod.store import InMemoryAssetStore

_LOG = logging.getLogger(__name__)

#: Read by :func:`_maybe_configure_control_plane_logging`; written
#: unconditionally by ``civiccast.native.supervisor.children.
#: control_plane_child_spec`` (``CIVICCAST_SUPERVISED_ENV_VAR``) into the
#: control-plane child's env. Duplicated as a plain string (not imported)
#: so importing this module never pulls in the ``civiccast.native.supervisor``
#: package at collection time -- ``_maybe_configure_control_plane_logging``
#: imports that package lazily, only when the var is actually set.
_CONTROL_PLANE_SUPERVISED_ENV_VAR = "CIVICCAST_SUPERVISED"

#: Process-global idempotency guard -- see
#: :func:`_maybe_configure_control_plane_logging`.
_control_plane_logging_configured = False

#: Process-global latch for :func:`_maybe_contain_own_descendants`.
_descendants_contained = False


def _maybe_contain_own_descendants() -> None:
    """Put this process in its own kill-on-close Job Object when (and only
    when) it is running as the native supervisor's control-plane child.

    U31. The live defect (station, 2026-09-25, ``supervisor.log``
    07:20:32-07:32:15): the supervisor killed an unready control plane, and the
    three HLS relay ffmpegs that control plane had spawned SURVIVED as orphans
    holding the relays' UDP ports -- so every supervised relay after that failed
    with ``[udp @ ...] bind failed: Error number -10048`` until the orphans were
    killed by hand, and the channels were off air for eleven minutes.

    Inherited membership in the supervisor's job is not enough to prevent that:
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` fires when the last HANDLE to the job
    closes, and the supervisor holds its handle for its own lifetime, so killing
    one member reaps nothing. See ``civiccast.platform.child_containment`` for
    the mechanism, the measurement behind it, and why the job deliberately still
    permits ``CREATE_BREAKAWAY_FROM_JOB`` (the tier-5 detached GStreamer repair
    launches from this process that way).

    Same guard as :func:`_maybe_configure_control_plane_logging` and for the
    same reason: containment belongs to the supervised control plane, not to a
    test run, a bare ``uvicorn`` dev run, or any other unsupervised context that
    calls ``create_app()`` -- none of which set the var, and none of which want
    their own child processes reaped when the process exits.

    Called from the TOP of ``create_app``, before ``_maybe_configure_control_plane_logging()``
    and before anything can spawn a child, because containment only holds for
    processes created AFTER the assignment.

    A failure here is logged at ERROR and does NOT stop startup: a station that
    cannot establish containment must still come on air, but the operator has to
    be able to find out from the log that its relays are not protected."""

    global _descendants_contained
    if _descendants_contained:
        return
    if os.environ.get(_CONTROL_PLANE_SUPERVISED_ENV_VAR) != "1":
        return
    # Lazy import for the same reason the logging helper imports lazily: no
    # reason for every civiccast.app import to pull this in when unsupervised.
    from civiccast.platform.child_containment import contain_own_descendants

    status = contain_own_descendants()
    if status.active:
        _descendants_contained = True
        _LOG.info("control-plane containment active: %s", status.detail)
    else:
        _LOG.error(
            "control-plane containment INACTIVE (%s): relay ffmpegs and other "
            "long-lived children started by this process will NOT be reaped if it "
            "dies, and will hold their UDP ports against the supervised replacement",
            status.detail,
        )


def _maybe_configure_control_plane_logging() -> None:
    """Attach the ``civiccast`` package's INFO file logger for this process
    when (and only when) it is running as the native supervisor's
    control-plane child -- see ``civiccast.native.supervisor.children.
    control_plane_child_spec`` (sets ``CIVICCAST_SUPERVISED=1``
    unconditionally into the child's env) and
    ``civiccast.native.supervisor.service.configure_control_plane_logging``
    for what gets attached and the diagnosability gap it closes (Gate A T4:
    ``engine_state=FALLBACK_SLATE`` with no INFO trail explaining why).

    Guarded on ``CIVICCAST_SUPERVISED`` so calling ``create_app()`` in tests,
    a bare ``uvicorn civiccast.app:create_app`` dev run, or any other
    unsupervised context -- none of which set the var -- never touches
    ``%ProgramData%\\CivicCast\\logs``; ``default_log_root()`` would
    otherwise create real directories a test run has no business writing,
    every time ``create_app()`` is called (which test suites do a lot).

    ALSO guarded on a process-global flag so a second ``create_app()`` call
    in the SAME supervised process never double-attaches a handler.
    ``configure_control_plane_logging`` is itself idempotent (replaces its
    own prior handler rather than stacking), but re-running it still means
    re-opening the log file for no reason; the flag skips the work entirely
    after the first call, which is what makes "configures the logger exactly
    once" a property this function actually holds, not just an accident of
    idempotent replacement."""

    global _control_plane_logging_configured
    if _control_plane_logging_configured:
        return
    if os.environ.get(_CONTROL_PLANE_SUPERVISED_ENV_VAR) != "1":
        return
    # Lazy import: civiccast.native.supervisor is Windows-service-layer code
    # (harmless to import off-Windows -- see that module's own docstring --
    # but there is no reason for every civiccast.app import, including every
    # test collecting this module, to pull it in when unsupervised).
    from civiccast.native.supervisor.service import configure_control_plane_logging

    configure_control_plane_logging()
    _control_plane_logging_configured = True


def _build_eas_health_hook(session_factory: Callable[[], Any]) -> SourceHealthHook:
    """S11c: route EAS source-poll health into the S8 operational alert hub.

    Raises an ``eas-source-unavailable`` condition when a CAP source's poll starts
    failing and resolves it when the source recovers — gated on STATE CHANGE so a
    healthy (or persistently-failing) source never spams the alert hub."""
    from civiccast.alerting.store import record_alert_condition

    last_state: dict[str, bool] = {}

    def _hook(source: EasCapSource, healthy: bool, detail: str) -> None:
        prev = last_state.get(source.source_id)
        if prev == healthy:
            return  # no change since the last scan
        last_state[source.source_id] = healthy
        if prev is None and healthy:
            return  # first sight and healthy — nothing to report
        try:
            with session_factory() as session:
                record_alert_condition(
                    session,
                    kind="eas-source-unavailable",
                    resource_ref=source.source_id,
                    source_section="eas",
                    summary=(
                        f"EAS source '{source.label}' is unavailable"
                        if not healthy
                        else f"EAS source '{source.label}' recovered"
                    ),
                    detail=detail,
                    resolved=healthy,
                )
                session.commit()
        except Exception:
            _LOG.exception("Failed to record EAS source health for %s", source.source_id)

    return _hook


def _build_eas_auto_surface(session_factory: Callable[[], Any]) -> Callable[[], None]:
    """S11c: auto-surface severe+ active alerts onto every ON_AIR channel (crawl/overlay).

    Geo relevance is already enforced at ingest by each source's geocode_filter, so an
    ingested active alert is in the station's service area. forced_slate is NEVER created
    here (auto_surface_active only emits crawl/overlay) — full-screen pre-emption always
    requires an operator (decision 3). Idempotent per (channel, alert)."""
    from civiccast.eas.service import EasDisplayService
    from civiccast.eas.store import EasStore as _EasStore
    from civiccast.egress.store import PostgresEgressStore

    def _run() -> None:
        egress_store = PostgresEgressStore(session_factory)
        service = EasDisplayService(_EasStore(session_factory))
        for config in egress_store.list_configs():
            row = egress_store.read_state(config.channel_id)
            if row is not None and row.state == "ON_AIR":
                service.auto_surface_active(channel_id=config.channel_id)

    return _run


_PUBLIC_ANALYTICS_INGEST_PATH = "/api/public/app/analytics/events"
_PUBLIC_ANALYTICS_MAX_CONTENT_LENGTH = 16_384
STAFF_BEARER_SCHEME = "CivicCastStaffBearer"


class EphemeralStoreConfigurationError(RuntimeError):
    """Raised when CivicCast would otherwise start with volatile staff stores."""


# RAT-001: the maintenance-mode contract version this control plane understands.
# The supervisor stamps CIVICCAST_SUPERVISOR_MODE_CONTRACT into the child's env
# when it starts the control plane in maintenance; a mismatch (or absence, when
# mode=="maintenance") is fail-closed to "unknown" — never silently "normal".
_SUPERVISOR_MODE_CONTRACT_VERSION = "1"
_SUPERVISOR_MODE_MAINTENANCE = "maintenance"
_SUPERVISOR_MODE_NORMAL = "normal"
_SUPERVISOR_MODE_UNKNOWN = "unknown"
# RAT-001 (fail-closed, coordinator review F-CP-1): the modes in which the app
# holds back workers/writers and refuses mutating routes. BOTH "maintenance"
# AND "unknown" freeze -- "unknown" means the supervisor set the maintenance
# env but with a contract version this control plane does not understand; the
# supervisor CLEARLY intended a freeze, so the app must NOT fall back to normal
# and start workers/writers during the intended window (that is exactly the
# RAT-001 fail-open: "an upgrade health start brings up worker automation while
# the maintenance marker is held"). The contract version only governs whether
# /health's attestation is TRUSTED by the supervisor's readiness gate -- never
# whether the app freezes.
_SUPERVISOR_MODE_FROZEN = frozenset({_SUPERVISOR_MODE_MAINTENANCE, _SUPERVISOR_MODE_UNKNOWN})
# Mutating HTTP verbs the maintenance_guard middleware refuses with 503 while
# app.state.supervisor_mode is frozen (maintenance/unknown); GET/HEAD/OPTIONS
# always serve.
_MAINTENANCE_GUARD_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# RAT-004: default per-shutdown drain-all deadline, matching D5's per-child
# graceful-stop budget (15s then TerminateProcess).
_DEFAULT_EGRESS_DRAIN_DEADLINE_SECONDS = 15.0
# Startup hooks are housekeeping (alert reconciliation, orphan recording
# cleanup), not the air path.  A broken database or a stalled query must not
# hold the control plane in a half-started state while /health keeps serving.
# Hooks still get a short grace period so the normal, fast path preserves its
# existing startup semantics; slow hooks finish in their daemon thread and are
# retried by the next normal reconciliation cadence/restart.
_DEFAULT_STARTUP_HOOK_TIMEOUT_SECONDS = 1.0


def _egress_drain_deadline_seconds() -> float:
    raw = os.environ.get("CIVICCAST_EGRESS_DRAIN_DEADLINE_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_EGRESS_DRAIN_DEADLINE_SECONDS
    try:
        return float(raw)
    except ValueError:
        _LOG.warning(
            "CIVICCAST_EGRESS_DRAIN_DEADLINE_SECONDS=%r is not a number; using the %ss default.",
            raw,
            _DEFAULT_EGRESS_DRAIN_DEADLINE_SECONDS,
        )
        return _DEFAULT_EGRESS_DRAIN_DEADLINE_SECONDS


def _startup_hook_timeout_seconds() -> float:
    raw = os.environ.get("CIVICCAST_STARTUP_HOOK_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_STARTUP_HOOK_TIMEOUT_SECONDS
    try:
        parsed = float(raw)
    except ValueError:
        return _DEFAULT_STARTUP_HOOK_TIMEOUT_SECONDS
    return parsed if parsed > 0 else _DEFAULT_STARTUP_HOOK_TIMEOUT_SECONDS


# Peer of the egress drain deadline: the per-shutdown budget for gracefully
# finalizing in-flight scheduled recordings to a valid asset before process
# exit tears them down. Same 15s default as the egress drain.
_DEFAULT_RECORDING_DRAIN_DEADLINE_SECONDS = 15.0


def _recording_drain_deadline_seconds() -> float:
    raw = os.environ.get("CIVICCAST_RECORDING_DRAIN_DEADLINE_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_RECORDING_DRAIN_DEADLINE_SECONDS
    try:
        return float(raw)
    except ValueError:
        _LOG.warning(
            "CIVICCAST_RECORDING_DRAIN_DEADLINE_SECONDS=%r is not a number; using the %ss default.",
            raw,
            _DEFAULT_RECORDING_DRAIN_DEADLINE_SECONDS,
        )
        return _DEFAULT_RECORDING_DRAIN_DEADLINE_SECONDS


def _supervisor_mode() -> str:
    """RAT-001: read the WSL-never-sets-this, supervisor-only env contract.

    ABSENT (the WSL/plain-``uvicorn`` case: the env var is genuinely unset) ->
    ``"normal"``: the existing WSL/normal-boot behavior is unchanged and
    backward-compatible. An explicit ``"normal"`` -> ``"normal"``.
    ``"maintenance"`` with a matching ``CIVICCAST_SUPERVISOR_MODE_CONTRACT`` ->
    ``"maintenance"``; with a wrong or absent contract version -> ``"unknown"``
    (fail-closed). CC-WS5-009: an explicitly-PRESENT value that is none of these
    -> ``"unknown"`` (fail-closed), NOT ``"normal"``. This includes a
    PRESENT-BUT-BLANK value (``" "``, ``""``, ``"\t"``): a launch that set the
    var to a blank string specified a mode and got a blank one, so it fails
    closed to ``"unknown"`` -- an incorrectly or maliciously launched control
    plane must never obtain writer-capable "normal" behavior from an
    unrecognized or blank mode string. ONLY a genuinely-absent env
    (``os.environ.get`` returns ``None``) is backward-compat "normal".
    """

    raw = os.environ.get("CIVICCAST_SUPERVISOR_MODE")
    if raw is None:
        return _SUPERVISOR_MODE_NORMAL  # genuinely unset -> WSL/plain-boot backward-compat
    mode = raw.strip().lower()
    if mode == "":
        return _SUPERVISOR_MODE_UNKNOWN  # present-but-blank -> fail-closed (CC-WS5-009)
    if mode == _SUPERVISOR_MODE_NORMAL:
        return _SUPERVISOR_MODE_NORMAL
    if mode == _SUPERVISOR_MODE_MAINTENANCE:
        contract = os.environ.get("CIVICCAST_SUPERVISOR_MODE_CONTRACT", "").strip()
        if contract != _SUPERVISOR_MODE_CONTRACT_VERSION:
            return _SUPERVISOR_MODE_UNKNOWN
        return _SUPERVISOR_MODE_MAINTENANCE
    return _SUPERVISOR_MODE_UNKNOWN  # explicitly present but unrecognized -> fail-closed


async def _maintenance_guard_middleware(request: Any, call_next: Any) -> Any:
    """RAT-001: 503 every mutating route while app.state.supervisor_mode is
    "maintenance" — POST/PUT/PATCH/DELETE, including the egress command-enqueue
    endpoints (ordinary POSTs under /api/staff/egress, covered by the same
    method check). GET/read routes are never touched. Registered outermost
    (see create_app) so a mutating request is refused before staff-auth or
    rate-limit bookkeeping runs.
    """

    mode = getattr(request.app.state, "supervisor_mode", _SUPERVISOR_MODE_NORMAL)
    if mode in _SUPERVISOR_MODE_FROZEN and request.method in _MAINTENANCE_GUARD_MUTATING_METHODS:
        return JSONResponse(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, content={"error": "maintenance"}
        )
    return await call_next(request)


@asynccontextmanager
async def _app_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start/stop app-owned background services with the server lifecycle.

    The inline finalization worker thread (Stage B+D, ENG-002 hybrid
    architecture) starts here — not at wiring time — so plain ``create_app()``
    calls (tests, artifact generation) never spawn threads. Durable storage
    activated mid-flight (installer-prepared storage observed by the
    ``_sync_durable_storage`` middleware) starts the worker from
    ``_install_durable_store_wiring`` because by then the lifespan has begun.

    RAT-001: ``app.state.supervisor_mode`` is resolved FIRST, before any
    worker/supervisor is started, so the maintenance gate below sees it on
    the very first start attempt (including one that races in from durable
    storage activating mid-flight, since both gates re-read this same state).
    """

    app.state.lifespan_started = True
    app.state.supervisor_mode = _supervisor_mode()
    # Audit ENG-004: schema-currency self-diagnosis runs HERE, not in
    # create_app - plain create_app() calls must never touch the database
    # (pinned by test_create_app_does_not_call_engine_connect).
    from civiccast.schema_check import check_schema_currency

    app.state.schema_status = check_schema_currency(os.environ.get("DATABASE_URL"))
    # <fix/health-readiness-order-independence> This checkpoint used to be
    # left unset here -- only `_refresh_schema_status` (mid-flight storage
    # activation) set it. `_maybe_refresh_schema_status` treats an unset
    # checkpoint as "definitely stale" (`last is None` skips the TTL
    # comparison entirely), so EVERY `/health` call recomputed
    # `check_schema_currency` from scratch instead of respecting
    # SCHEMA_STATUS_TTL_SECONDS -- defeating the TTL's whole purpose (a hot
    # liveness path opening a DB connection per request) and silently
    # discarding any schema_status a caller set directly (e.g. a test fixture
    # simulating the "unknown" state) on the very next `/health` request.
    app.state.schema_status_checked_monotonic = time.monotonic()
    _maybe_start_finalization_worker(app)
    _maybe_start_background_supervisors(app)
    if app.state.supervisor_mode not in _SUPERVISOR_MODE_FROZEN:
        from civiccast.syndicate.live_wiring import restore_youtube_schedules

        restore_youtube_schedules(app)
    try:
        backup_service = getattr(app.state, "station_backup_service", None)
        if backup_service is not None and app.state.supervisor_mode not in _SUPERVISOR_MODE_FROZEN:
            backup_service.start_worker()
        yield
    finally:
        app.state.lifespan_started = False
        youtube_output = getattr(app.state, "youtube_live_output", None)
        if youtube_output is not None:
            youtube_output.close()
        # Durable storage can be prepared or rebound while this lifespan is
        # active. Stop the current service, not the boot-time instance captured
        # before the first request.
        backup_service = getattr(app.state, "station_backup_service", None)
        if backup_service is not None:
            backup_service.stop_worker()
        # RAT-004: drain every live channel through its owner (observed exit,
        # escalating to a kill past the deadline) BEFORE background.stop()
        # halts the automation poll loop, so channels are drained on
        # shutdown instead of orphaned to the Job Object backstop. A plain
        # app instance with no durable storage never wires an egress daemon
        # at all, so this is a no-op there (getattr default None).
        egress_daemon = getattr(app.state, "egress_daemon", None)
        if egress_daemon is not None:
            egress_daemon.stop_all_channels(deadline_seconds=_egress_drain_deadline_seconds())
        guest_program = getattr(app.state, "guest_program_router", None)
        if guest_program is not None:
            guest_program.close()
        # Recording peer of the egress drain: gracefully finalize any in-flight
        # scheduled recording (arming/recording/finalizing) to a valid asset
        # BEFORE background.stop() halts the scheduler poll thread, so a
        # mid-recording shutdown produces a finalized asset instead of an
        # orphan the next boot's reconcile_orphans can only mark failed. Runs
        # safely alongside the still-live poll thread via the capture
        # pipeline's per-job lock (see RecordingService.drain_in_flight). A
        # plain app instance with no durable storage never wires a recording
        # worker, so this is a no-op there (getattr default None).
        scheduled_recording_worker = getattr(app.state, "scheduled_recording_worker", None)
        if scheduled_recording_worker is not None:
            with suppress(Exception):
                scheduled_recording_worker.drain_in_flight(
                    deadline_seconds=_recording_drain_deadline_seconds()
                )
        supervisor = getattr(app.state, "finalization_worker_supervisor", None)
        if supervisor is not None:
            supervisor.stop()
        for background in getattr(app.state, "background_supervisors", []):
            background.stop()


def _wire_finalization_worker(app: FastAPI, session_factory: Any) -> None:
    """Register the worker supervisor whenever durable storage is wired."""

    settings = FinalizationWorkerSettings.from_env()  # raises on invalid mode: fail fast
    app.state.finalization_worker_supervisor = FinalizationWorkerSupervisor(
        session_factory,
        settings,
        # Beta B4 (decision #7A): completed packages publish through the
        # config-selected CDN adapter; None (provider=off) keeps local-only.
        cdn_adapter=app.state.resolve_cdn_adapter(),
    )
    _maybe_start_finalization_worker(app)


def _maybe_start_finalization_worker(app: FastAPI) -> None:
    """Start the finalization worker — the write/publish surface RAT-001 holds
    back in maintenance mode (the supervisor never starts a control plane in
    maintenance with a worker that publishes files)."""

    supervisor = getattr(app.state, "finalization_worker_supervisor", None)
    if supervisor is None or not getattr(app.state, "lifespan_started", False):
        return
    if getattr(app.state, "supervisor_mode", _SUPERVISOR_MODE_NORMAL) in _SUPERVISOR_MODE_FROZEN:
        return
    supervisor.start()  # no-op unless mode == "inline"


def _maybe_start_background_supervisors(app: FastAPI) -> None:
    """RAT-001: in maintenance mode, start ONLY the (currently empty)
    read-path allow-list — concretely, this holds back ChannelAutomationService
    (the daemon-driving, encoder-spawning worker/write surface) along with
    every other background supervisor, since none of the currently-registered
    supervisors are purely read-only (each writes some DB row: retention
    flags, program-log materialization, bulletin expiry, etc.) per the design
    addendum's "anything not on [the allow-list] is skipped" rule. A future
    genuinely read-only supervisor (e.g. a resource sampler) can be added to
    the allow-list by name without changing this gate's shape.
    """

    if not getattr(app.state, "lifespan_started", False):
        return
    if getattr(app.state, "supervisor_mode", _SUPERVISOR_MODE_NORMAL) in _SUPERVISOR_MODE_FROZEN:
        return
    _prewarm_native_live_caption_runtime(app)
    for supervisor in getattr(app.state, "background_supervisors", []):
        supervisor.start()  # each no-ops unless its mode is "inline"
    # One-shot startup-condition hooks (e.g. the caption-tier degrade alert):
    # drained so a hook runs exactly once whether durable storage was wired at
    # boot (lifespan reaches here first) or mid-flight ("Prepare storage" ->
    # _wire_stage_f_workers -> here with the lifespan already started). These
    # hooks are housekeeping, not the air path. Run each in its own daemon
    # thread and wait only a short grace period: a database query or other
    # startup repair that stalls must not leave the control plane serving
    # /health while never completing the rest of startup. The fast path still
    # completes before this function returns, preserving the old behavior for
    # ordinary local databases.
    hooks = getattr(app.state, "startup_condition_hooks", [])
    while hooks:
        hook = hooks.pop(0)
        finished = threading.Event()

        def _run_startup_hook(
            startup_hook: Callable[[], None] = hook,
            done: threading.Event = finished,
        ) -> None:
            try:
                startup_hook()
            except Exception:
                # Hook implementations already fail closed, but the wrapper
                # must keep that invariant if a future hook does not.
                _LOG.exception("Startup condition hook failed; continuing startup.")
            finally:
                done.set()

        thread = threading.Thread(
            target=_run_startup_hook,
            name="civiccast-startup-condition",
            daemon=True,
        )
        thread.start()
        thread.join(timeout=_startup_hook_timeout_seconds())
        if not finished.is_set():
            _LOG.warning(
                "Startup condition hook exceeded %.1fs; leaving it in its daemon "
                "thread so the control plane can finish startup.",
                _startup_hook_timeout_seconds(),
            )


def _prewarm_native_live_caption_runtime(app: FastAPI) -> None:
    """Load the native live-caption model before channel automation can air.

    Native stations start their channel-automation supervisor and their caption
    tap from the same application lifespan. Starting automation first lets a
    media writer produce settled audio while the first caption scan is still
    loading CUDA/model state; at three channels that cold interval can trip the
    existing fail-closed backlog gate. The native activation contract already
    identifies the deployment where live captions are part of the station air
    path, so prewarm only that path. A load error is logged and leaves the
    existing lazy worker behavior intact; it must not turn a best-effort caption
    feature into a control-plane startup outage.
    """

    if os.environ.get("CIVICCAST_NATIVE_STATION", "").strip() != "1":
        return
    if getattr(app.state, "caption_runtime_prewarmed", False):
        return
    worker = getattr(app.state, "caption_tap_worker", None)
    runtime = getattr(worker, "_runtime", None) if worker is not None else None
    prepare = getattr(runtime, "prepare", None)
    if not callable(prepare):
        return

    started = time.monotonic()
    try:
        prepare()
    except Exception:
        _LOG.exception("Live caption runtime prewarm failed; continuing with lazy model loading.")
        return

    app.state.caption_runtime_prewarmed = True
    whisper_runtime = getattr(runtime, "_whisper", runtime)
    app.state.caption_live_runtime_model_loaded_at_startup = (
        getattr(whisper_runtime, "_model", None) is not None
    )
    from civiccast.captions.tap_worker import _resolved_runtime_identity

    identity = _resolved_runtime_identity(runtime)
    _LOG.info(
        "Live caption runtime prewarmed before channel automation: "
        "requested_device=%s requested_compute_type=%s loaded_device=%s "
        "loaded_compute_type=%s on_cuda=%s num_workers=%s source=%s elapsed_ms=%.0f",
        identity[0],
        identity[1],
        identity[2],
        identity[3],
        identity[4],
        identity[5],
        identity[6],
        (time.monotonic() - started) * 1000.0,
    )


def _build_program_log_materializer(session_factory: Any) -> ProgramLogMaterializer:
    """Construct the CA-1 materializer (shared by the worker + on-demand DI)."""

    asset_store = PostgresAssetStore(session_factory)
    return ProgramLogMaterializer(
        PostgresProgramLogStore(session_factory),
        PostgresScheduleStore(session_factory),
        asset_store.get_staff_row,
        settings=ProgramLogSettings.from_env(),
        tz=_station_tz,
    )


def _cg_upcoming_reader(
    session_factory: Any,
) -> Callable[[str, datetime], list[tuple[datetime, str]]]:
    """Build the CG "coming up next" reader (S6 CG depth, DC-CG4 live path).

    Returns a channel's next program-log occurrences as ``(starts_at, title)``;
    the title is the slot's override or its asset id (richer asset titles are a
    follow-up). The CG board designer's live preview renders these in a
    ``schedule`` zone.
    """

    store = PostgresProgramLogStore(session_factory)

    def read(channel_id: str, now: datetime) -> list[tuple[datetime, str]]:
        titles = {
            slot.slot_id: (slot.title_override or slot.asset_id)
            for slot in store.list_slots(channel_id=channel_id)
        }
        # Scope the occurrence read to THIS channel's slots and cap it at the DB,
        # so a multi-channel station with a deep rolling horizon does not load
        # every future occurrence into the process on each preview poll.
        occurrences = store.list_occurrences(slot_ids=set(titles.keys()), start_from=now, limit=8)
        return [
            (occ.occurrence_start, titles[occ.slot_id])
            for occ in occurrences
            if occ.slot_id in titles
        ][:8]

    return read


def _station_tz() -> tzinfo:
    """The station's local timezone for daypart auto-scheduling.

    Resolution order (M3 fix — the installer persists ``station_timezone`` at
    first-admin setup, but nothing previously propagated it to the running
    service, so every station silently ran on UTC regardless of what the
    operator chose):

    1. ``CIVICCAST_STATION_TZ`` (an IANA name, e.g. ``America/New_York``) —
       kept as an explicit override for support/ops sessions, matching the
       documented env-var contract (docs/USER-MANUAL.md).
    2. The ``station_timezone`` persisted into station-state JSON by
       :func:`civiccast.installer.station_state.complete_first_admin_setup`.
       This is the normal path: the service reads what the installer wrote
       instead of the installer having to also copy the value into the
       service's process environment as a second, driftable source of truth
       (the same pattern the S13 AI-model first-run override uses via
       ``read_ai_model_seed``).

    Defaults to UTC (no warning) when neither source names a real zone --
    including the ``"local"`` sentinel default written before an operator
    picks a real zone -- and falls back to UTC (with a warning) on an
    unknown/invalid IANA name. S18 dayparts are wall-clock in this zone —
    without it, "prime time 18:00" would fire at 18:00 UTC for an off-UTC
    station.

    S1 (StationBoxProfile): the actual env-override-vs-persisted-vs-default
    precedence chain lives in exactly one place —
    :func:`civiccast.installer.station_state.resolve_station_timezone` —
    this function is just the ``str -> tzinfo`` conversion + honest-fallback
    layer on top of that shared loader.
    """
    from civiccast.installer.station_state import resolve_station_timezone

    name = resolve_station_timezone()
    source = (
        "CIVICCAST_STATION_TZ"
        if os.environ.get("CIVICCAST_STATION_TZ")
        else "the persisted station_timezone"
    )
    if not name or name == "local":
        return UTC
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        _LOG.warning("%s=%r is not a valid IANA zone; using UTC.", source, name)
        return UTC


#: The env var station_runtime's tier resolution serializes its selection
#: event into (civiccast.native.station_runtime.load_native_station_environment).
_CAPTION_TIER_EVENT_ENV = "CIVICCAST_CAPTION_TIER_EVENT"
_CAPTION_TIER_ALERT_REF = "caption-tier"


def _build_caption_tier_startup_condition(session_factory: Any) -> Callable[[], None]:
    """One-shot startup hook surfacing a degraded caption tier to the operator.

    PR #80's orphaned-tier fallback (an uninstall/reinstall upgrade preserves
    ``components/captions-large-v3`` but not its activation receipt) degrades
    the station to the proven floor tier and starts it -- correct, but the only
    trace was a supervisor-process log line. The full record of that decision
    already reaches this process as ``CIVICCAST_CAPTION_TIER_EVENT`` (set by
    ``load_native_station_environment`` for every start), so this hook reads it
    once at lifespan startup and lands the condition in the S8 alert hub the
    System Health surface already shows:

    - ``fallback: true`` -> raise ``caption-tier-degraded`` (hub de-dupes
      across restarts of the same degraded state);
    - a healthy start -> resolve a previously-firing event, guarded on
      ``_find_firing_event`` so a normal boot never writes a spurious
      pre-resolved audit row (same posture as ``alerting.self_test``).

    Never raises: an alert-sink failure must not take down the control plane.
    """
    import json as _json

    from civiccast.alerting.store import _find_firing_event

    def _run() -> None:
        raw = os.environ.get(_CAPTION_TIER_EVENT_ENV, "").strip()
        if not raw:
            return  # not a native station start (dev/test/WSL-era env)
        try:
            event = _json.loads(raw)
        except ValueError:
            _LOG.exception("%s is not valid JSON; skipping", _CAPTION_TIER_EVENT_ENV)
            return
        if not isinstance(event, dict):
            return
        fallback = bool(event.get("fallback"))
        requested = str(event.get("requested") or event.get("tier") or "unknown")
        try:
            with session_factory() as session:
                if fallback:
                    record_alert_condition(
                        session,
                        kind="caption-tier-degraded",
                        resource_ref=_CAPTION_TIER_ALERT_REF,
                        source_section="captions",
                        summary=(
                            "Captions are running on the standard tier; the large "
                            f"caption model ({requested}) needs re-validation "
                            "-- open AI Models"
                        ),
                        detail=str(event.get("reason") or ""),
                    )
                elif (
                    _find_firing_event(session, "caption-tier-degraded", _CAPTION_TIER_ALERT_REF)
                    is not None
                ):
                    record_alert_condition(
                        session,
                        kind="caption-tier-degraded",
                        resource_ref=_CAPTION_TIER_ALERT_REF,
                        source_section="captions",
                        summary=f"Caption tier restored: station started on {requested}",
                        resolved=True,
                    )
                session.commit()
        except Exception:
            _LOG.exception("Failed to record the caption-tier startup condition")

    return _run


def _build_recording_reconcile_startup_condition(recording_svc: Any) -> Callable[[], None]:
    """One-shot startup hook that fails any recording job orphaned by a crash.

    ``RecordingService.reconcile_orphans`` (wrapping
    ``RecordingStore.reconcile_orphaned_active_jobs``) exists precisely to
    fail a job stuck in ``arming``/``recording``/``finalizing`` past its
    planned window after an unclean process exit -- otherwise the row is
    never resolved, and because ``recording`` is an overlap-blocking state
    (see ``RecordingStore.find_overlaps``), every future recording on that
    source is silently skipped forever. It was previously only exercised by
    tests; this hook is the production call site, run once per lifespan
    start (same posture as the caption-tier condition above). Never raises:
    a reconciliation failure must not take down the control plane -- a
    truly stuck job just gets retried on the next restart.
    """

    def _run() -> None:
        try:
            transitioned = recording_svc.reconcile_orphans()
            if transitioned:
                _LOG.warning(
                    "recording.reconcile_orphans failed %d job(s) orphaned by a prior "
                    "restart (stuck past their planned window in an active state)",
                    transitioned,
                )
        except Exception:
            _LOG.exception("Failed to reconcile orphaned recording jobs at startup")

    return _run


def _build_contribution_alert_hook(session_factory: Any) -> Callable[[str, str], None]:
    """An (kind, detail) -> S8 sink for the S17 co-process supervisor + service.

    Records remote-contribution co-process-down / TURN-unreachable / guest-drop
    conditions through the alerting hub (the hub de-dupes). An alert-sink failure
    is logged, never raised — it must not crash supervision or fail a guest drop.
    """
    summaries = {
        "remote-contribution-coprocess-down": "A remote-contribution co-process is down",
        "remote-contribution-turn-unreachable": "The TURN server is unreachable",
        "remote-contribution-guest-drop": "An on-air remote guest was dropped",
    }

    def _hook(kind: str, detail: str) -> None:
        try:
            with session_factory() as session:
                record_alert_condition(
                    session,
                    kind=kind,  # type: ignore[arg-type]
                    resource_ref="remote-contribution",
                    source_section="S17",
                    summary=summaries.get(kind, "Remote contribution alert"),
                    detail=detail,
                )
                session.commit()
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to record remote-contribution alert %s.", kind
            )

    return _hook


def _build_ai_model_service(session_factory: Any) -> AiModelService:
    """The S13 service that resolves each feature's operator-selected model.

    Seeds ``system_ram_total_gb`` AND ``has_gpu`` from the live hardware probe so
    summary's adaptive default (12B only with a real GPU present and >=16GB RAM;
    e4b on every CPU-only box regardless of RAM) matches the box. Field evidence
    (candidate #17, 32GB CPU-only reference station) retired the old RAM-only rule:
    it picked 12B on that box, which took 366s to complete a summary once and then
    failed twice more under realistic memory pressure, while e4b completed every
    attempt. With no operator selection, the service returns each feature's catalog
    default; a station with no NVIDIA GPU (``probe().gpu is None`` -- ADR 0005, NVML
    only) now gets the model that actually finishes there.
    """
    hardware = probe()
    ram_total_gb = int(hardware.ram.total_gb)
    has_gpu = hardware.gpu is not None
    return AiModelService(
        AiModelStore(session_factory), system_ram_total_gb=ram_total_gb, has_gpu=has_gpu
    )


def _wire_stage_f_workers(app: FastAPI, session_factory: Any) -> None:
    """Register the ActivityPub retry and retention workers (Stage F).

    Both validate their env settings here (fail fast at startup) and run as
    lifespan-supervised threads when their mode is ``inline`` (the default).
    The retention worker only flags expired assets for records-clerk review —
    it never deletes; the disposition queue endpoint reads through the DI
    override installed here.
    """

    retry_settings = ActivityPubRetrySettings.from_env()
    retention_settings = RetentionWorkerSettings.from_env()
    retry_worker = ActivityPubRetryWorker(
        PostgresActivityPubStore(session_factory),
        app.state.activitypub_delivery_client,
        settings=retry_settings,
    )
    retention_worker = RetentionEnforcementWorker(session_factory, settings=retention_settings)
    # 4.0 media-library-hardening (scope item 5): periodically re-check
    # every asset's backing file and flag file_status='missing' when it's
    # gone. Same shape as the retention worker just above — never mutates
    # or deletes, only flags for an operator to act on (via the relink
    # endpoint).
    media_integrity_settings = MediaIntegrityWorkerSettings.from_env()
    media_integrity_worker = MediaIntegrityWorker(
        session_factory, settings=media_integrity_settings
    )
    # S7 media lifecycle: readiness computation, ingest-time transcode
    # dispatch, and the CLAUDE.md §4.6 archival verification gate. Same
    # env-gated inline/off + poll-seconds shape as the workers above;
    # CIVICCAST_MEDIA_LIFECYCLE_WORKER_DRY_RUN additionally lets an operator
    # audit what a pass WOULD do before it writes anything.
    media_lifecycle_settings = MediaLifecycleWorkerSettings.from_env()
    media_lifecycle_worker = MediaLifecycleWorker(
        session_factory, settings=media_lifecycle_settings
    )
    app.state.media_lifecycle_worker = media_lifecycle_worker
    # S7 watch-folder poll daemon: PR #19 built the config CRUD/UI only and
    # explicitly deferred this. Polls each enabled WatchFolderConfig's path
    # (local/USB/NAS/SMB) on its own poll_interval_seconds, and ingests via
    # the SAME upload/replace-source pipeline as an operator action -- never
    # a parallel pipeline. See civiccast.schedule.watch_folder_worker's
    # module docstring for the settle-window, degraded-state, and
    # processed-file-disposition design (ADR 0024).
    watch_folder_settings = WatchFolderWorkerSettings.from_env()
    watch_folder_worker = WatchFolderWorker(session_factory, settings=watch_folder_settings)
    app.state.watch_folder_worker = watch_folder_worker
    # Issue #111: failed subscriber webhook deliveries are re-driven from the
    # durable queue with backoff; the client is the config-selected provider
    # (mock by default), so the worker only ever sees real traffic when the
    # station opted into CIVICCAST_PROVIDER_WEBHOOK=real.
    webhook_retry_settings = WebhookRetrySettings.from_env()
    webhook_retry_worker = WebhookRetryWorker(
        PostgresSubscribeStore(session_factory),
        default_registry().resolve(PROVIDER_KIND_WEBHOOK),
        load_subscription_secrets(),
        settings=webhook_retry_settings,
        delivery_store=PostgresNotificationDeliveryStore(session_factory),
        publish_store=PostgresPublishStore(session_factory),
    )
    app.state.background_supervisors = [
        ThreadSupervisor(
            name="civiccast-activitypub-retry-worker",
            run_forever=retry_worker.run_forever,
            poll_seconds=retry_settings.poll_seconds,
            enabled=retry_settings.mode == "inline",
        ),
        ThreadSupervisor(
            name="civiccast-retention-worker",
            run_forever=retention_worker.run_forever,
            poll_seconds=retention_settings.poll_seconds,
            enabled=retention_settings.mode == "inline",
        ),
        ThreadSupervisor(
            name="civiccast-webhook-retry-worker",
            run_forever=webhook_retry_worker.run_forever,
            poll_seconds=webhook_retry_settings.poll_seconds,
            enabled=webhook_retry_settings.mode == "inline",
        ),
        ThreadSupervisor(
            name="civiccast-media-integrity-worker",
            run_forever=media_integrity_worker.run_forever,
            poll_seconds=media_integrity_settings.poll_seconds,
            enabled=media_integrity_settings.mode == "inline",
        ),
        ThreadSupervisor(
            name="civiccast-media-lifecycle-worker",
            run_forever=media_lifecycle_worker.run_forever,
            poll_seconds=media_lifecycle_settings.poll_seconds,
            enabled=media_lifecycle_settings.mode == "inline",
        ),
        ThreadSupervisor(
            name="civiccast-watch-folder-worker",
            run_forever=watch_folder_worker.run_forever,
            poll_seconds=watch_folder_settings.poll_seconds,
            enabled=watch_folder_settings.mode == "inline",
        ),
    ]
    # S14: fold raw viewership_events into pre-aggregated viewership_rollups
    # on a short interval (default 5 min, per the spec's §10 recommendation)
    # so the analytics dashboard reads pre-computed buckets rather than
    # scanning raw events on every request.
    analytics_rollup_settings = AnalyticsRollupSettings.from_env()
    analytics_rollup_worker = AnalyticsRollupWorker(
        session_factory, settings=analytics_rollup_settings
    )
    app.state.background_supervisors.append(
        ThreadSupervisor(
            name="civiccast-analytics-rollup-worker",
            run_forever=analytics_rollup_worker.run_forever,
            poll_seconds=analytics_rollup_settings.poll_seconds,
            enabled=analytics_rollup_settings.mode == "inline",
        )
    )
    # QA-2 (Critical): reap stale, unreferenced contributor uploads so an
    # anonymous upload no longer sits on disk forever. The worker's first
    # sweep runs immediately when this supervisor starts (see
    # ContributorUploadReapWorker.run_forever), so this single wiring covers
    # both "reap at startup" and "reap on a periodic timer" from the finding.
    # re-gate TE-3: hand the reaper the SAME per-app byte budget the upload route
    # uses (created unconditionally in create_app, so it already exists by the
    # time this durable-store wiring runs) so it can return a deleted file's
    # bytes to the address that uploaded it.
    contributor_reap_worker = ContributorUploadReapWorker(
        byte_budget=getattr(app.state, "contributor_upload_byte_budget", None)
    )
    _reap_poll_raw = os.environ.get("CIVICCAST_CONTRIBUTOR_UPLOAD_REAP_POLL_SECONDS", "").strip()
    try:
        contributor_reap_poll_seconds = float(_reap_poll_raw) if _reap_poll_raw else 3600.0
    except ValueError:
        contributor_reap_poll_seconds = 3600.0
    if contributor_reap_poll_seconds <= 0:
        contributor_reap_poll_seconds = 3600.0
    app.state.background_supervisors.append(
        ThreadSupervisor(
            name="civiccast-contributor-upload-reap",
            run_forever=contributor_reap_worker.run_forever,
            poll_seconds=contributor_reap_poll_seconds,
            enabled=os.environ.get("CIVICCAST_CONTRIBUTOR_UPLOAD_REAP", "inline") != "off",
        )
    )
    # Cable automation CA-1: keep the channel program log materialized over
    # its rolling horizon so the playout path always has upcoming items.
    program_log_settings = ProgramLogSettings.from_env()
    program_log_materializer = _build_program_log_materializer(session_factory)
    app.state.background_supervisors.append(
        ThreadSupervisor(
            name="civiccast-program-log-materializer",
            run_forever=program_log_materializer.run_forever,
            poll_seconds=program_log_settings.poll_seconds,
            enabled=program_log_settings.mode == "inline",
        )
    )
    # Cable automation CA-2: the app drives enabled channels 24/7 —
    # auto_start channels come back on air after restarts (join-in-progress
    # source plans resume the current program at the wall-clock offset), and
    # slate gaps re-plan when a scheduled program becomes due. Safe at the
    # default inline mode: no encoder spawns without an operator start or an
    # auto_start flag.
    # S8: operational alerting. The evaluator runs off each egress health sample
    # (wired as the daemon's alert_evaluator_hook below); dispatch sends to the
    # operator's configured channels — none on a fresh install, so a new box never
    # pages. The maintenance worker samples host resources + runs scheduled
    # self-tests + drives delivery retries. Secrets live in the 0600 credential file.
    from sqlalchemy import text as _sql_text

    alert_credential_store = FileCredentialStore()
    alert_credentials = credential_reader(alert_credential_store)
    # Channel CRUD persists operator-supplied secrets to the same store (write-only).
    app.dependency_overrides[get_credential_writer] = lambda: alert_credential_store.put
    alert_dispatch = AlertDeliveryDispatch(session_factory, alert_credentials)
    alert_evaluator = AlertEvaluator(session_factory, dispatch=alert_dispatch)
    from civiccast.egress.store import PostgresEgressStore

    caption_health_egress_store = PostgresEgressStore(session_factory)

    def _alert_evaluator_hook(
        channel_id: str, state: str, encoder_fps: float | None, encoder_bitrate_kbps: float | None
    ) -> None:
        from civiccast.alerting.runtime_status import read_live_caption_processing_status
        from civiccast.egress.automation import default_egress_work_dir
        from civiccast.installer.station_state import resolve_live_captions_enabled_or_default

        captions_expected = resolve_live_captions_enabled_or_default()
        try:
            state_row = caption_health_egress_store.read_state(channel_id)
        except Exception:
            _LOG.debug("Could not read channel state for caption alert evaluation", exc_info=True)
            state_row = None
        status = read_live_caption_processing_status(
            default_egress_work_dir(),
            channel_id,
            egress_state=state,
            captions_expected=captions_expected,
            now=datetime.now(UTC),
            on_air_since=(
                state_row.updated_at
                if state == "ON_AIR" and state_row is not None and state_row.state == "ON_AIR"
                else None
            ),
        )
        additional_conditions = []
        if (
            state == "ON_AIR"
            and captions_expected
            and status.processing_state in {"failed", "stalled"}
        ):
            additional_conditions.append(
                (
                    "live-caption-failure",
                    f"{channel_id} live-caption worker {status.processing_state}",
                )
            )
        alert_evaluator.evaluate_channel(
            channel_id,
            state,
            encoder_fps=encoder_fps,
            encoder_bitrate_kbps=encoder_bitrate_kbps,
            additional_conditions=additional_conditions,
        )

    def _db_reachable() -> bool:
        try:
            with session_factory() as ping_session:
                ping_session.execute(_sql_text("SELECT 1"))
            return True
        except Exception:
            return False

    # Self-tests: the light in-process checks (readiness / backup / model-ping) run
    # live on the daily+weekly schedule; the heavy live-engine proofs stay excluded
    # until their probes are wired (they are never faked). Same deps back the
    # on-demand POST /self-tests/run endpoint.
    alert_self_test_deps = default_self_test_deps(
        session_factory=session_factory, credential_reader=alert_credentials
    )
    app.dependency_overrides[get_self_test_deps] = lambda: alert_self_test_deps
    alert_maintenance = AlertingMaintenanceWorker(
        session_factory,
        resource_probes=default_resource_probes(
            # service liveness is asserted by the external supervisor (systemd /
            # Windows service); an in-process probe cannot observe its own death,
            # so this is True here and a real service-down is surfaced by the
            # delivery dead-letter path, not by self-monitoring.
            db_reachable=_db_reachable,
            service_running=lambda: True,
        ),
        self_test_deps=alert_self_test_deps,
        # No frozen availability snapshot: the worker recomputes availability per
        # scheduled run (via its session_factory) so checks enabled mid-session run
        # on the next cadence without a restart.
        retry_worker=AlertRetryWorker(session_factory, alert_credentials),
    )
    # PR #80 follow-up: surface an orphaned-caption-tier degrade (or its
    # recovery) through the alert hub once per startup. Registered as a
    # startup-condition hook rather than run inline because create_app() must
    # never touch the database (pinned by
    # test_create_app_does_not_call_engine_connect); the lifespan runs it.
    if not hasattr(app.state, "startup_condition_hooks"):
        app.state.startup_condition_hooks = []
    app.state.startup_condition_hooks.append(_build_caption_tier_startup_condition(session_factory))
    alert_settings = AlertingMaintenanceSettings()
    app.state.background_supervisors.append(
        ThreadSupervisor(
            name="civiccast-alerting-maintenance",
            run_forever=alert_maintenance.run_forever,
            poll_seconds=alert_settings.poll_seconds,
            enabled=os.environ.get("CIVICCAST_ALERTING", "inline") != "off",
        )
    )

    # S17: remote-contribution co-process supervision (VDO.Ninja + coturn). Off by
    # default (CIVICCAST_REMOTE_CONTRIBUTION=off); when on, keeps both co-processes
    # up with identity-safe restarts and feeds the URL bridge's diagnostics probe.
    # The S8 alert hook (co-process-down / TURN-unreachable / guest-drop) is wired
    # in slice 3d-iii; the supervisor restarts + logs regardless.
    contribution_coprocess_settings = ContributionCoprocessSettings.from_env()
    contribution_supervisor = ContributionCoprocessSupervisor(
        contribution_coprocess_settings,
        alert_hook=_build_contribution_alert_hook(session_factory),
        mix_health_check=lambda: (
            router.check_health()
            if (router := getattr(app.state, "guest_program_router", None)) is not None
            else None
        ),
    )
    set_active_supervisor(contribution_supervisor)
    app.state.contribution_supervisor = contribution_supervisor
    app.state.background_supervisors.append(
        ThreadSupervisor(
            name="civiccast-remote-contribution",
            run_forever=contribution_supervisor.run_forever,
            poll_seconds=contribution_coprocess_settings.poll_seconds,
            enabled=contribution_coprocess_settings.enabled,
        )
    )

    automation_settings = ChannelAutomationSettings.from_env()
    channel_automation = build_channel_automation(
        session_factory,
        alert_evaluator_hook=_alert_evaluator_hook,
        channel_start_hook=lambda channel_id: (
            tap_worker.begin_channel_session(channel_id)
            if (tap_worker := getattr(app.state, "caption_tap_worker", None)) is not None
            else None
        ),
    )
    # RAT-004: the lifespan shutdown finally block drains this daemon (the
    # graceful drain-all owner) before background.stop() halts the poll loop.
    app.state.egress_daemon = channel_automation.daemon
    app.state.background_supervisors.append(
        ThreadSupervisor(
            name="civiccast-channel-automation",
            run_forever=channel_automation.run_forever,
            poll_seconds=automation_settings.poll_seconds,
            enabled=automation_settings.mode == "inline",
        )
    )

    # S18 periodic auto-schedule compile (periodic compiler behavior). Idempotent
    # and safe on a fresh box (no rules -> no-op); off via CIVICCAST_AUTOSCHEDULE=off.
    autoschedule_compile = AutoScheduleCompileWorker(session_factory, tz=_station_tz)
    autoschedule_compile_settings = AutoScheduleCompileSettings()
    app.state.background_supervisors.append(
        ThreadSupervisor(
            name="civiccast-autoschedule-compile",
            run_forever=autoschedule_compile.run_forever,
            poll_seconds=autoschedule_compile_settings.poll_seconds,
            enabled=os.environ.get("CIVICCAST_AUTOSCHEDULE", "inline") != "off",
        )
    )
    scheduled_recording_worker = getattr(app.state, "scheduled_recording_worker", None)
    scheduled_recording_settings = getattr(
        app.state, "scheduled_recording_settings", ScheduledRecordingSettings.from_env()
    )
    if scheduled_recording_worker is not None:
        app.state.background_supervisors.append(
            ThreadSupervisor(
                name="civiccast-scheduled-recording",
                run_forever=scheduled_recording_worker.run_forever,
                poll_seconds=scheduled_recording_settings.poll_seconds,
                enabled=scheduled_recording_settings.mode == "inline",
            )
        )

    # S6 (build step 7): daily purge of long-expired community bulletins. The
    # filler already hides expired bulletins; this just bounds the table.
    # Idempotent + safe on a fresh box (nothing to purge -> no-op); off via
    # CIVICCAST_BULLETIN_EXPIRY=off.
    bulletin_expiry = BulletinExpiryWorker(session_factory)
    bulletin_expiry_settings = BulletinExpirySettings()
    app.state.background_supervisors.append(
        ThreadSupervisor(
            name="civiccast-bulletin-expiry",
            run_forever=bulletin_expiry.run_forever,
            poll_seconds=bulletin_expiry_settings.poll_seconds,
            enabled=os.environ.get("CIVICCAST_BULLETIN_EXPIRY", "inline") != "off",
        )
    )
    # Live caption tap (Beta B6, decision #1A). Off by default — live
    # captioning needs a local transcription model, so a station opts in
    # (CIVICCAST_CAPTION_TAP=inline + CIVICCAST_CAPTION_TAP_DIR). Settings
    # validate fail-fast here either way; the runtime (and its model import)
    # is only constructed when the tap is actually enabled inline.
    tap_settings = CaptionTapWorkerSettings.from_env()
    if tap_settings.mode == "inline":
        # S13: the caption runtime loads the operator-selected model (faster-whisper
        # ``large-v3`` by default; a selection swaps the loaded model id). The
        # translation provider is likewise the operator-selected translation model
        # (local TranslateGemma by default; a cloud selection routes through the cloud
        # adapter) — T3/M4: translation is now wired, not just captions/summary.
        ai_model_service = _build_ai_model_service(session_factory)
        # live=True: this runtime is the LIVE tap's, sharing the box with
        # playout, so it is sized conservatively (1-2 CTranslate2 intra-op
        # threads, core-count-aware and capped -- see
        # civiccast.captions.runtime.default_live_tap_cpu_threads -- and
        # greedy decoding on CPU). The offline caption job below deliberately
        # keeps the batch sizing -- it runs against a published file, not
        # against the air signal.
        caption_runtime = build_caption_runtime(ai_model_service, live=True)
        from civiccast.installer.station_state import resolve_live_captions_enabled_or_default

        tap_worker = build_tap_worker(
            tap_settings,
            PostgresCaptionReviewStore(session_factory),
            runtime=caption_runtime,
            translation_provider=build_translator(ai_model_service),
            # The operator's station-level switch
            # (``StationProfile.live_captions_enabled``, default on), read on
            # EVERY scan rather than once here: an activated native station
            # forces ``CIVICCAST_CAPTION_TAP=inline`` into its environment, so
            # this is the only way an operator can stop live captioning -- and
            # it has to work without restarting a control plane that is on air.
            is_enabled=resolve_live_captions_enabled_or_default,
        )
        # Exposed so app-factory wiring tests can assert the operator-selected
        # translation/caption models reached the running worker (T3/T4).
        app.state.caption_tap_worker = tap_worker
        app.state.caption_live_model_key = ai_model_service.effective_selection("captions").tier.key
        app.state.caption_live_runtime_model_loaded_at_startup = False
        app.state.background_supervisors.append(
            ThreadSupervisor(
                name="civiccast-caption-tap-worker",
                run_forever=tap_worker.run_forever,
                poll_seconds=tap_settings.poll_seconds,
                enabled=True,
            )
        )
    # Recorded-caption settings default from legacy environment values until
    # the operator saves a station settings file. The worker reloads that
    # file each scan so enabling, disabling, and language changes take effect
    # without restarting the service.
    offline_caption_settings = OfflineCaptionJobSettings.from_env()
    app.state.recorded_caption_settings_store = RecordedCaptionSettingsStore(
        default_recorded_caption_settings_path(),
        defaults=RecordedCaptionSettings(
            enabled=offline_caption_settings.enabled,
            languages=offline_caption_settings.languages,
            completion_requirement=offline_caption_settings.completion_requirement,
        ),
    )
    offline_caption_worker = OfflineCaptionJobWorker(
        PostgresOfflineCaptionJobStore(session_factory),
        PostgresCaptionReviewStore(session_factory),
        # Same seam the live path uses: resolves the operator-selected
        # caption tier and inherits the hardware-adaptive device the native
        # station runtime published into the environment (PR #398).
        runtime_factory=lambda: build_caption_runtime(_build_ai_model_service(session_factory)),
        # Selected Spanish tracks use operator-reviewed translations.
        # Mirrors the LIVE tap's translation wiring above
        # (build_translator at the CaptionTapWorker construction) -- the
        # operator-selected translation tier (local TranslateGemma by
        # default). Built lazily per attempt, same reason as the runtime
        # factory: a station with nothing queued never loads the model.
        translation_provider_factory=lambda: build_translator(
            _build_ai_model_service(session_factory)
        ),
        # Caption attach rewrites the LOCAL manifest. When this asset's
        # package is served to residents through a CDN, that copy still has
        # the pre-caption manifest, so the reviewed EN/ES tracks and the
        # rewritten manifest are pushed back to the same prefix the package
        # was published under. No-op (returns None) on a station with no CDN
        # configured, or for a package this station never CDN-published.
        cdn_republisher=VodPackageCdnRepublisher(
            # Resolved per call so setup-wizard credentials entered after
            # startup take effect, same seam the surge switch uses.
            lambda: app.state.resolve_cdn_adapter(),
            build_asset_cdn_package_target_lookup(session_factory),
        ),
        settings=offline_caption_settings,
        settings_provider=app.state.recorded_caption_settings_store.load,
    )
    app.state.offline_caption_worker = offline_caption_worker
    app.state.background_supervisors.append(
        ThreadSupervisor(
            name="civiccast-offline-caption-worker",
            run_forever=offline_caption_worker.run_forever,
            poll_seconds=offline_caption_settings.poll_seconds,
            enabled=True,
        )
    )
    # Async summary generation job: field evidence 2026-08-29 (candidate #17) --
    # POST /api/staff/summaries/generate 503'd at ~120s even on a warm model,
    # because a legitimate CPU-only summary generation (measured 94-366s+ on the
    # same hardware class) cannot survive one HTTP request/response cycle, and
    # discarded a completion Ollama had already finished computing. On by
    # default, idle until an operator queues a meeting -- see
    # civiccast/summary/job.py. Settings validate fail-fast here either way.
    summary_job_settings = SummaryGenerationJobSettings.from_env()
    summary_job_worker = SummaryGenerationJobWorker(
        PostgresSummaryGenerationJobStore(session_factory),
        PostgresSummaryStore(session_factory),
        # Lazy per-attempt, same reason as the caption runtime_factory above:
        # each attempt picks up the operator's CURRENT model selection rather
        # than one captured at worker-construction time, and a station with
        # nothing queued never loads a multi-gigabyte local model.
        model_factory=lambda: build_summary_model(_build_ai_model_service(session_factory)),
        settings=summary_job_settings,
    )
    app.state.summary_job_worker = summary_job_worker
    app.state.background_supervisors.append(
        ThreadSupervisor(
            name="civiccast-summary-job-worker",
            run_forever=summary_job_worker.run_forever,
            poll_seconds=summary_job_settings.poll_seconds,
            enabled=summary_job_settings.mode == "inline",
        )
    )
    # S11a caption decode-back proof loop. Only runs when CEA-708 embedding is on
    # (CIVICCAST_EGRESS_EMBED_CAPTIONS) — it's the loop that proves embedded captions
    # survived to the emitted stream and flips caption_status to on (fail-closed: no
    # fresh PASS -> not-verified). The capture is the WSL/LPM live edge.
    embed_captions = os.environ.get("CIVICCAST_EGRESS_EMBED_CAPTIONS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if embed_captions:
        # Second gate, re-read on EVERY scan by both workers below (not once
        # here): the operator's live-captions switch
        # (``StationProfile.live_captions_enabled``; off by default in beta.5).
        # With it off no channel builds an embed leg and the tap blanks every
        # sidecar, so the feed would poll every 2 s for nothing and the proof
        # would spend a 6 s ffmpeg capture per channel every 30 s to write a
        # guaranteed FAIL row. Re-reading per scan (rather than gating the
        # supervisors) means flipping the switch ON later resumes both loops
        # without restarting a control plane that is on air -- the same shape
        # as the caption tap worker's ``is_enabled`` above.
        from civiccast.installer.station_state import resolve_live_captions_enabled_or_default

        # The FEED: push each ON_AIR channel's caption cues into the live appsrc (the
        # production caller of send_caption_cue) so captions actually reach the encoder.
        caption_feed_worker = build_caption_feed_worker(
            session_factory,
            send_caption_cue=channel_automation.daemon.send_caption_cue,
            is_enabled=resolve_live_captions_enabled_or_default,
        )
        app.state.background_supervisors.append(
            ThreadSupervisor(
                name="civiccast-caption-feed",
                run_forever=caption_feed_worker.run_forever,
                poll_seconds=float(os.environ.get("CIVICCAST_CAPTION_FEED_POLL_SECONDS", "2")),
                enabled=True,
            )
        )
        # The PROOF: sample the emitted stream + flip caption_status on a fresh PASS.
        # In the native supervised service this runs in a separate Python child;
        # the control-plane thread only supervises that child.  Unsupervised
        # unit/dev apps retain the in-process seam so tests do not spawn a child.
        proof_poll_seconds = float(os.environ.get("CIVICAST_CAPTION_PROOF_POLL_SECONDS", "30"))
        proof_timeout_seconds = float(
            os.environ.get("CIVICAST_CAPTION_PROOF_TIMEOUT_SECONDS", "15")
        )
        proof_process_mode = os.environ.get("CIVICCAST_CAPTION_PROOF_PROCESS", "").strip().lower()
        if not proof_process_mode:
            proof_process_mode = (
                "process" if os.environ.get("CIVICCAST_SUPERVISED") == "1" else "inline"
            )
        if proof_process_mode == "off":
            _LOG.warning(
                "Caption proof worker disabled by CIVICCAST_CAPTION_PROOF_PROCESS=off; "
                "emitted-caption proof is not being collected."
            )
        elif proof_process_mode == "process":
            proof_process = CaptionProofProcessSupervisor(
                poll_seconds=proof_poll_seconds,
                timeout_seconds=proof_timeout_seconds,
            )
            app.state.caption_proof_process_supervisor = proof_process
            app.state.background_supervisors.append(
                ThreadSupervisor(
                    name="civiccast-caption-proof",
                    run_forever=proof_process.run_forever,
                    poll_seconds=proof_poll_seconds,
                    enabled=True,
                )
            )
        elif proof_process_mode == "inline":
            caption_proof_worker = build_caption_proof_worker(
                session_factory,
                is_enabled=resolve_live_captions_enabled_or_default,
                ffmpeg_timeout_seconds=proof_timeout_seconds,
            )
            app.state.background_supervisors.append(
                ThreadSupervisor(
                    name="civiccast-caption-proof",
                    run_forever=caption_proof_worker.run_forever,
                    poll_seconds=proof_poll_seconds,
                    enabled=True,
                )
            )
        else:
            raise ValueError(
                "CIVICCAST_CAPTION_PROOF_PROCESS must be 'off', 'inline', or 'process'; "
                f"got {proof_process_mode!r}."
            )
    # S11c EAS poll worker — polls enabled CAP/IPAWS/NWS/AMBER sources, ingests
    # filtered alerts, expires stale ones. Off by default (CIVICCAST_EAS=inline to
    # enable); fail-closed (a feed failure surfaces source-health, never fabricates).
    if os.environ.get("CIVICCAST_EAS", "off") != "off":
        eas_poll_worker = EasPollWorker(
            store=EasStore(session_factory),
            fetcher=build_http_fetcher(resolve_secret=lambda ref: os.environ.get(ref)),
            health_hook=_build_eas_health_hook(session_factory),
            # Opt-in auto-surface (CIVICCAST_EAS_AUTO_SURFACE): after each ingest scan,
            # auto-display severe+ active alerts on every ON_AIR channel as a crawl/
            # overlay (geo already scoped by the source geocode_filter). forced_slate
            # is never auto — full-screen pre-emption always needs an operator.
            post_scan=(
                _build_eas_auto_surface(session_factory)
                if os.environ.get("CIVICCAST_EAS_AUTO_SURFACE", "").strip().lower()
                in ("1", "true", "yes", "on")
                else None
            ),
        )
        app.state.background_supervisors.append(
            ThreadSupervisor(
                name="civiccast-eas-poll",
                run_forever=eas_poll_worker.run_forever,
                poll_seconds=float(os.environ.get("CIVICCAST_EAS_POLL_SECONDS", "60")),
                enabled=True,
            )
        )
    app.dependency_overrides[get_disposition_review_reader] = lambda: retention_worker
    app.dependency_overrides[get_missing_media_reader] = lambda: media_lifecycle_worker
    _maybe_start_background_supervisors(app)


class _EphemeralAssetStore:
    """Volatile asset store used only when explicitly requested for dev/tests."""

    def __init__(self) -> None:
        self._public_assets: dict[str, AssetMetadata] = {}
        self._staff_rows: dict[str, StaffAssetRow] = {}

    def get(self, asset_id: str) -> AssetMetadata | None:
        asset = self._public_assets.get(asset_id)
        return asset if asset is not None and asset.published_at is not None else None

    def list(self) -> list[AssetMetadata]:
        return sorted(
            (asset for asset in self._public_assets.values() if asset.published_at is not None),
            key=lambda asset: (
                asset.published_at is None,
                -(asset.published_at.timestamp() if asset.published_at else 0),
                asset.asset_id,
            ),
        )

    def create(self, asset: AssetMetadata) -> AssetMetadata:
        if asset.asset_id in self._public_assets or asset.asset_id in self._staff_rows:
            from civiccast.vod.store import AssetAlreadyExistsError

            raise AssetAlreadyExistsError(asset_id=asset.asset_id)
        self._public_assets[asset.asset_id] = asset
        self._staff_rows[asset.asset_id] = StaffAssetRow(
            asset_id=asset.asset_id,
            title=asset.title,
            description=asset.description,
            state=cast(AssetStateValue, ASSET_STATE_VALIDATED),
            manifest_url=str(asset.manifest_url),
            published_at=asset.published_at,
            duration_seconds=asset.duration_seconds,
        )
        return asset

    def list_all(self) -> builtins.list[StaffAssetRow]:
        return sorted(
            self._staff_rows.values(),
            key=lambda row: (
                row.published_at is None,
                -(row.published_at.timestamp() if row.published_at else 0),
                row.asset_id,
            ),
        )

    def list_all_page(
        self, *, limit: int = 50, offset: int = 0
    ) -> tuple[builtins.list[StaffAssetRow], int]:
        """Ephemeral-store sibling of :meth:`PostgresAssetStore.list_all_page`.

        Throwaway/dev mode only (``CIVICCAST_ALLOW_EPHEMERAL_STORES=1``);
        slices the same in-memory ordering ``list_all`` already produces.
        """
        rows = self.list_all()
        return rows[offset : offset + limit], len(rows)

    def get_staff_row(self, asset_id: str) -> StaffAssetRow | None:
        return self._staff_rows.get(asset_id)

    def mark_packaged(self, asset_id: str, manifest_url: str) -> StaffAssetRow:
        """Ephemeral-store sibling of :meth:`PostgresAssetStore.mark_packaged`.

        Field gap (night-a4): ``schedule.router.package_staff_asset`` calls
        this on whatever store ``get_postgres_store`` resolves to -- in
        throwaway/dev mode (``CIVICCAST_ALLOW_EPHEMERAL_STORES=1``) that is
        this class, not :class:`PostgresAssetStore`. Before this method
        existed, packaging ANY validated asset in that mode -- including one
        a contributor submission's ``accept`` action had just ingested --
        raised ``AttributeError`` and surfaced as a 503 that never recorded a
        ``manifest_url``. The asset row existed (accept itself worked, see
        :func:`civiccast.contribute.router._ingest_accepted_media`) but could
        never reach the packaged/playable state that makes it actually
        airable, so it sat on the schedule with nothing to play. Mirrors the
        Postgres sibling: only ``manifest_url`` changes; ``state`` stays
        ``"validated"`` (this codebase encodes "packaged" as manifest_url
        being set, not as a separate state value -- see
        :meth:`civiccast.schedule.store.PostgresAssetStore.mark_packaged`).
        """
        row = self._staff_rows.get(asset_id)
        if row is None:
            raise AssetNotFoundError(asset_id)
        updated = row.model_copy(update={"manifest_url": manifest_url})
        self._staff_rows[asset_id] = updated
        return updated

    def mark_published(self, asset_id: str, *, published_at: datetime) -> StaffAssetRow:
        row = self._staff_rows.get(asset_id)
        if row is None:
            raise ValueError(f"Asset not found: {asset_id}")
        if not row.manifest_url:
            raise ValueError(f"Asset is not packaged: {asset_id}")
        # WP-08: mirrors civiccast.schedule.store.PostgresAssetStore.mark_published
        # -- capture retention_anchor_at on FIRST publish only, never move it.
        update_fields: dict[str, Any] = {"published_at": published_at}
        if row.retention_anchor_at is None:
            update_fields["retention_anchor_at"] = published_at
        updated = row.model_copy(update=update_fields)
        self._staff_rows[asset_id] = updated
        public = self._public_assets.get(asset_id)
        if public is not None:
            self._public_assets[asset_id] = public.model_copy(update={"published_at": published_at})
        return updated

    def mark_unpublished(self, asset_id: str) -> StaffAssetRow:
        """Ephemeral-store sibling of :meth:`PostgresAssetStore.mark_unpublished`.

        Same rationale as :meth:`mark_packaged` above -- ``schedule.router.
        unpublish_asset`` calls this through the same ``get_postgres_store``
        dependency, and it was missing here too. Idempotent, matching the
        Postgres sibling's contract (unpublishing an already-unpublished
        asset is a no-op).
        """
        row = self._staff_rows.get(asset_id)
        if row is None:
            raise AssetNotFoundError(asset_id)
        if row.published_at is not None:
            updated = row.model_copy(update={"published_at": None})
            self._staff_rows[asset_id] = updated
            row = updated
        public = self._public_assets.get(asset_id)
        if public is not None and public.published_at is not None:
            self._public_assets[asset_id] = public.model_copy(update={"published_at": None})
        return row

    def list_broken(self) -> builtins.list[StaffAssetRow]:
        """Ephemeral-store sibling of :meth:`PostgresAssetStore.list_broken`.

        No background integrity worker runs against the ephemeral store
        (it isn't durable storage), so this is always empty in practice —
        provided for interface parity so the router's dependency doesn't
        need to special-case ephemeral mode.
        """
        return [row for row in self._staff_rows.values() if row.file_status == "missing"]

    def list_duplicates(self) -> builtins.list[builtins.list[StaffAssetRow]]:
        """Ephemeral-store sibling of :meth:`PostgresAssetStore.list_duplicates`."""
        groups: dict[str, builtins.list[StaffAssetRow]] = {}
        for row in self._staff_rows.values():
            if row.content_hash is not None:
                groups.setdefault(row.content_hash, []).append(row)
        return [group for group in groups.values() if len(group) > 1]

    def relink(
        self,
        asset_id: str,
        *,
        new_file_path: str,
        ffprobe_result: Any,
        content_hash: str | None,
    ) -> StaffAssetRow:
        """Ephemeral-store sibling of :meth:`PostgresAssetStore.relink`."""
        row = self._staff_rows.get(asset_id)
        if row is None:
            raise AssetNotFoundError(asset_id)
        next_values = row.model_dump()
        next_values.update(
            file_path=new_file_path,
            file_status="relinked",
            duration_seconds=ffprobe_result.duration_seconds,
            codec_video=ffprobe_result.codec_video,
            codec_audio=ffprobe_result.codec_audio,
            width_px=ffprobe_result.width_px,
            height_px=ffprobe_result.height_px,
            bitrate_bps=ffprobe_result.bitrate_bps,
            format_name=ffprobe_result.format_name,
            content_hash=content_hash if content_hash is not None else row.content_hash,
            version=row.version + 1,
        )
        updated = StaffAssetRow(**next_values)
        self._staff_rows[asset_id] = updated
        return updated

    def update_metadata(self, asset_id: str, update: AssetMetadataUpdate) -> StaffAssetRow:
        row = self._staff_rows.get(asset_id)
        if row is None:
            raise AssetNotFoundError(asset_id)
        patch = update.model_dump(exclude_unset=True)
        patch.pop("expected_version", None)
        next_values = row.model_dump()
        term_unit = patch.get("retention_term_unit")
        if term_unit is not None:
            # WP-08: ephemeral-store sibling of
            # PostgresAssetStore._apply_retention_term. Dev/throwaway mode
            # only (CIVICCAST_ALLOW_EPHEMERAL_STORES=1); no durable audit
            # log exists here, so the anchor-fallback case is a no-op
            # beyond setting the anchor -- there is nothing durable to
            # write it to in this store.
            from civiccast.installer.station_state import resolve_station_timezone
            from civiccast.schedule.models import RETENTION_DEFAULT, RETENTION_PERMANENT
            from civiccast.schedule.retention_terms import (
                RETENTION_TERM_UNIT_FOREVER,
                compute_retention_until,
            )

            anchor = row.retention_anchor_at or datetime.now(UTC)
            next_values["retention_anchor_at"] = anchor
            next_values["retention_term_unit"] = term_unit
            next_values["retention_term_value"] = patch.get("retention_term_value")
            next_values["retention_until"] = compute_retention_until(
                anchor_at=anchor,
                unit=term_unit,
                value=patch.get("retention_term_value"),
                station_tz_name=resolve_station_timezone(),
            )
            next_values["retention_policy"] = (
                RETENTION_PERMANENT
                if term_unit == RETENTION_TERM_UNIT_FOREVER
                else RETENTION_DEFAULT
            )
            patch.pop("retention_term_unit", None)
            patch.pop("retention_term_value", None)
        next_values.update(patch)
        next_values["version"] = row.version + 1
        updated = StaffAssetRow(**next_values)
        self._staff_rows[asset_id] = updated
        return updated

    def ingest_upload(
        self,
        *,
        asset_id: str,
        title: str,
        description: str | None,
        file_path: str,
        file_size_bytes: int,
        ffprobe_result: Any,
        content_hash: str | None = None,
        thumbnail_path: str | None = None,
    ) -> UploadedAssetResponse:
        if asset_id in self._staff_rows:
            from civiccast.vod.store import AssetAlreadyExistsError

            raise AssetAlreadyExistsError(asset_id=asset_id)
        response = UploadedAssetResponse(
            asset_id=asset_id,
            title=title,
            description=description,
            state=cast(AssetStateValue, ASSET_STATE_VALIDATED),
            file_path=file_path,
            file_size_bytes=file_size_bytes,
            duration_seconds=ffprobe_result.duration_seconds,
            codec_video=ffprobe_result.codec_video,
            codec_audio=ffprobe_result.codec_audio,
            width_px=ffprobe_result.width_px,
            height_px=ffprobe_result.height_px,
            bitrate_bps=ffprobe_result.bitrate_bps,
            format_name=ffprobe_result.format_name,
        )
        self._staff_rows[asset_id] = StaffAssetRow(
            **response.model_dump(),
            content_hash=content_hash,
            thumbnail_path=thumbnail_path,
        )
        return response


class _EphemeralScheduleStore:
    """Volatile schedule store paired with _EphemeralAssetStore."""

    def __init__(self, asset_store: _EphemeralAssetStore) -> None:
        self._asset_store = asset_store
        self._items: dict[uuid.UUID, ScheduleItemResponse] = {}

    def create(self, payload: ScheduleItemCreate) -> ScheduleItemResponse:
        asset = self._asset_store.get_staff_row(payload.asset_id)
        if asset is None:
            raise AssetNotFoundError(f"asset_id {payload.asset_id!r} does not exist.")
        item = ScheduleItemResponse(
            id=uuid.uuid4(),
            asset_id=payload.asset_id,
            asset_title=asset.title,
            channel_id=payload.channel_id,
            mode=payload.mode,
            state=cast(ScheduleStateValue, SCHEDULE_STATE_SCHEDULED),
            scheduled_at=payload.scheduled_at.astimezone(UTC),
            duration_seconds=payload.duration_seconds,
            notes=payload.notes,
            created_at=datetime.now(UTC),
        )
        self._items[item.id] = item
        return item

    def list(
        self,
        *,
        channel_id: str | None = None,
        states: tuple[str, ...] | None = None,
    ) -> list[ScheduleItemResponse]:
        items = list(self._items.values())
        if channel_id is not None:
            items = [item for item in items if item.channel_id == channel_id]
        if states is not None:
            items = [item for item in items if item.state in states]
        return sorted(items, key=lambda item: item.scheduled_at)

    def get(self, schedule_id: object) -> ScheduleItemResponse | None:
        try:
            key = schedule_id if isinstance(schedule_id, uuid.UUID) else uuid.UUID(str(schedule_id))
        except ValueError:
            return None
        return self._items.get(key)

    def cancel(self, schedule_id: object) -> ScheduleItemResponse:
        item = self.get(schedule_id)
        if item is None:
            raise ScheduleItemNotFoundError(schedule_id)
        if item.state == SCHEDULE_STATE_SCHEDULED:
            item = item.model_copy(
                update={"state": cast(ScheduleStateValue, SCHEDULE_STATE_CANCELLED)}
            )
            self._items[item.id] = item
        return item

    def mark_published(self, schedule_ids: Sequence[object]) -> int:
        """Flip scheduled items straight to ``published`` (dev/ephemeral-mode only).

        Mirrors the sqlite/Postgres-backed store's test-suite shortcut
        (:meth:`ScheduleStore.mark_published`) so a plain
        ``CIVICCAST_ALLOW_EPHEMERAL_STORES=1`` boot can also reach the
        published state for manual/local verification, without the full
        Postgres-only commit-to-air/playout round-trip. Only rows currently
        ``scheduled`` are flipped; unknown ids are silently ignored.
        """
        transitioned = 0
        for schedule_id in schedule_ids:
            item = self.get(schedule_id)
            if item is None or item.state != SCHEDULE_STATE_SCHEDULED:
                continue
            self._items[item.id] = item.model_copy(
                update={"state": cast(ScheduleStateValue, SCHEDULE_STATE_PUBLISHED)}
            )
            transitioned += 1
        return transitioned


#: Set to "1" by ``civiccast.native.station_runtime`` for the control-plane
#: child of a native station -- on BOTH the activated and the pre-activation
#: path. See :func:`create_app` for what it turns off and why.
LAN_ONLY_STATION_ENV_VAR = "CIVICCAST_LAN_ONLY_STATION"


def _lan_only_station() -> bool:
    return os.getenv(LAN_ONLY_STATION_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


#: Set by ``civiccast.native.station_runtime`` (from ``civiccast._native_version
#: .__version__``) for a native station -- native-windows chain J, 2026-08-02.
#: When present, ``/health`` and ``/api/version`` report THIS value instead of
#: ``civiccast._version.__version__``. Every other hosting context, including
#: the WSL product line (which never sets this), is completely unaffected --
#: unset falls back to the exact pre-chain-J behavior. This exists because the
#: native and WSL product lines now carry deliberately DIFFERENT version
#: identities (see ``evidence/chainJ-analysis.md``), but both run the same
#: shared backend code, which can only report one process-wide __version__
#: unless told otherwise.
NATIVE_REPORTED_VERSION_ENV_VAR = "CIVICCAST_NATIVE_REPORTED_VERSION"


def _reported_version() -> str:
    return os.getenv(NATIVE_REPORTED_VERSION_ENV_VAR, "").strip() or __version__


def _make_agenda_chapter_provider(asset_store: Any) -> Callable[[str], list[Any]]:
    """Read and validate chapter markers from the operator asset projection."""
    from pydantic import ValidationError

    from civiccast.schedule.models import Chapter

    def _chapter_provider(meeting_asset_id: str) -> list[Any]:
        row = asset_store.get_staff_row(meeting_asset_id)
        if row is None:
            return []
        chapters_attr = getattr(row, "chapters_json", None) or getattr(row, "chapters", None) or []
        if isinstance(chapters_attr, str):
            import json as _json

            try:
                chapters_attr = _json.loads(chapters_attr)
            except _json.JSONDecodeError:
                chapters_attr = []
        if not isinstance(chapters_attr, list):
            return []
        # A single malformed persisted chapter must not block valid chapter
        # markers, but ``StaffAssetRow.chapters`` already contains Chapter
        # instances. model_validate accepts both those and legacy dictionaries.
        chapters: list[Any] = []
        trim_start = getattr(row, "trim_in_seconds", None) or 0.0
        trim_end = getattr(row, "trim_out_seconds", None)
        for raw in chapters_attr:
            try:
                chapter = Chapter.model_validate(raw)
                # Editor markers use source time; agenda links seek in the
                # packaged playback range. Exclude markers outside that range
                # and rebase a copy, preserving the operator's source markers.
                if chapter.t < trim_start or (trim_end is not None and chapter.t >= trim_end):
                    continue
                chapters.append(chapter.model_copy(update={"t": chapter.t - trim_start}))
            except (ValidationError, TypeError) as exc:
                _LOG.error(
                    "Skipping malformed chapter in meeting asset %r: %r (payload=%r)",
                    meeting_asset_id,
                    exc,
                    raw,
                )
        return chapters

    return _chapter_provider


def create_app() -> FastAPI:
    """Build the FastAPI app. Lives behind a factory so tests can pin
    fresh app instances and so configuration injection has a clean seam.

    F-16 (sandbox newcomer re-walk ``dd7f835f``, 2026-08-01): on a native
    station the interactive API doc UIs are NOT served. FastAPI's built-in
    renderers fetch their assets from the public internet -- ``/docs`` pulls
    Swagger UI's bundle and stylesheet from ``cdn.jsdelivr.net`` plus a favicon
    from ``fastapi.tiangolo.com``, and ``/redoc`` adds
    ``fonts.googleapis.com`` on top (measured against this app, not assumed;
    ``/redoc`` was not in the finding and is the worse of the two). A
    council-chamber station sits on a municipal LAN, frequently firewalled
    outbound and sometimes air-gapped, so those pages render blank there and
    hand the operator a broken screen with no explanation.

    ``/openapi.json`` STAYS SERVED, BEHIND THE STAFF TOKEN. The F-16 finding
    named it too, but for the wrong reason: it is JSON this app generates, it
    references no external host at all, and it is the machine-readable
    contract an integrator actually needs, so the network constraint never
    applied to it. The 2026-09-09 walkthrough of a running beta.5 station
    (finding F-06) raised the reason that does apply: on a LAN-only station
    it enumerated every ``/api/staff/*`` path to a caller with no credential.
    So on a LAN-only station the schema is served by
    :func:`_install_lan_only_openapi_route` and requires the same staff
    bearer token every ``/api/staff/*`` route does; ``create_app().openapi()``
    (what ``scripts/generate-openapi-artifacts.py`` uses) is unaffected.

    DISABLED RATHER THAN VENDORED, as a deliberate choice. Serving the UIs
    locally means shipping ``swagger-ui-dist`` and ReDoc's bundle -- roughly
    1.5 MB of third-party JavaScript and CSS that exist nowhere in this repo
    today -- inside the native app payload, with a new build step, new hash
    pinning in the pack manifest, and a new third-party licence/attribution
    obligation on a product that does not yet have an attribution surface at
    all. That is a real cost for an interactive API explorer no council-chamber
    operator opens. The repo's existing offline posture points the same way:
    ``build_native_bootstrap.py``'s "air-gapped setup must not depend on a
    download", and the activated station environment already asserting
    ``HF_HUB_OFFLINE`` / ``TRANSFORMERS_OFFLINE``.

    OFF BY STATION, NOT OFF BY DEFAULT. A container or cloud deployment that
    can reach a CDN is not the defect; a municipal station that cannot is. Any
    deployment without the flag keeps both UIs exactly as before.
    """
    # Before anything else: containment only covers children created AFTER the
    # job assignment, so it must precede every spawn site in startup.
    _maybe_contain_own_descendants()
    _maybe_configure_control_plane_logging()
    if os.environ.get("CIVICCAST_SUPERVISED") == "1":
        from civiccast.native.station_secrets import configure_station_keyring

        configure_station_keyring()
    lan_only_station = _lan_only_station()
    app = FastAPI(
        title="CivicCast",
        description=(
            "Open-source, self-hostable civic broadcast platform. "
            "This is the umbrella API surface; per-module routers mount under it. "
            "Error contract (audit QA-005): the `detail` field's SHAPE varies by "
            "error class - a string for domain errors (404/409/503), an object "
            "for structured conflicts (409 with version fields), and an array of "
            "field errors for validation failures (422, FastAPI standard). "
            "Integrators should branch on status code first, then on the type "
            "of `detail`."
        ),
        version=__version__,
        docs_url=None if lan_only_station else "/docs",
        redoc_url=None if lan_only_station else "/redoc",
        # On a LAN-only station the schema route is installed by
        # _install_lan_only_openapi_route (staff token required, F-06) instead
        # of FastAPI's unauthenticated default; every other deployment keeps
        # the default. Self-contained either way: no external host.
        openapi_url=None if lan_only_station else "/openapi.json",
        lifespan=_app_lifespan,
    )
    if lan_only_station:
        _LOG.info(
            "%s is set, so the interactive API doc UIs (/docs, /redoc) are NOT served: "
            "their assets load from cdn.jsdelivr.net / fastapi.tiangolo.com / "
            "fonts.googleapis.com, which a LAN-only station cannot reach. The OpenAPI "
            "schema itself is still served at /openapi.json, to callers holding a "
            "staff bearer token.",
            LAN_ONLY_STATION_ENV_VAR,
        )
    app.middleware("http")(security_headers_middleware)
    app.middleware("http")(staff_auth_middleware)
    # RAT-001: registered LAST of the three (see the CORS comment below for why
    # registration order matters) so it is OUTERMOST and refuses a mutating
    # request in maintenance mode before staff-auth/rate-limit bookkeeping runs.
    app.middleware("http")(_maintenance_guard_middleware)

    @app.middleware("http")
    async def _packaged_tv_public_app_cors(request: Request, call_next: Any) -> Response:
        """Permit packaged TV shells to read only the public app contract.

        Tizen/webOS package pages have the opaque ``null`` origin. Their GET
        requests carry only the safelisted Accept header, so no preflight or
        credentialed cross-origin request is needed. Staff routes keep the
        ordinary explicit-origin policy below.
        """
        response = await call_next(request)
        if (
            request.method == "GET"
            and request.headers.get("origin") == "null"
            and (
                request.url.path.startswith("/api/public/app/")
                or request.url.path == "/api/public/assets"
            )
        ):
            response.headers["Access-Control-Allow-Origin"] = "null"
            response.headers["Vary"] = "Origin"
        return cast(Response, response)

    allowed_origins = cors_allowed_origins()
    if allowed_origins:
        # No cross-origin browser use case ships by default (audit item #27);
        # this only activates for an operator who explicitly opts in via
        # CIVICCAST_CORS_ALLOWED_ORIGINS. Never a wildcard — see auth/cors.py.
        # Registered LAST (not first): add_middleware()/middleware("http")()
        # both insert at position 0 of the middleware stack, so the last one
        # registered ends up outermost. CORSMiddleware must be outermost so
        # it — not staff_auth_middleware — handles CORS preflight OPTIONS
        # requests, which never carry an Authorization header. Real requests
        # still pass through to staff_auth_middleware unauthenticated —
        # CORSMiddleware only adds response headers after the inner app runs.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )
    app.state.auth_rate_limiter = AuthRateLimiter()
    # QA-2 part 3: per-app instance (never a shared module singleton) so tests
    # and Mode-B hosts never share contributor-upload byte budgets, same
    # reasoning as auth_rate_limiter just above. Created here UNCONDITIONALLY
    # (not inside the database_url-gated durable-store wiring) so the upload
    # route always has a per-app budget -- an ephemeral-mode app that fell back
    # to the module singleton would leak one caller's budget into the next.
    # The reap worker (durable-store wiring only) reuses this same instance.
    app.state.contributor_upload_byte_budget = ContributorUploadByteBudget()
    # Fail fast on malformed env staff tokens (QA-002: a role-less token is a
    # startup error, never a silent full-admin grant).
    validate_staff_token_config()
    validate_auth_rate_limit_config()
    # Config-driven CDN selector (Stage C): validate the env provider choice and
    # its credentials at startup (fail-fast preserved). resolve_cdn_adapter hands
    # out the active adapter -- the env-selected one when set, otherwise a
    # setup-wizard-configured one built on demand, so credentials entered in the
    # operator portal take effect on the next worker build without env config.
    env_cdn_adapter = build_cdn_adapter(CdnSettings.from_env())

    def _resolve_cdn_adapter() -> CDNAdapter | None:
        if env_cdn_adapter is not None:
            return env_cdn_adapter
        from civiccast.installer.cdn_bridge import resolve_stored_cdn_adapter

        return resolve_stored_cdn_adapter()

    app.state.resolve_cdn_adapter = _resolve_cdn_adapter
    # WP-03: one provider registry per app instance, read by both Publish
    # preflight and approval (civiccast.publish.router.get_provider_registry)
    # so they can never disagree about a surface's real-provider readiness.
    # Registration is side-effect-free (civiccast.platform.providers docstring);
    # this never touches the network or a credential store on its own.
    from civiccast.syndicate.live_wiring import build_youtube_output

    app.state.youtube_live_output = build_youtube_output(app)
    app.state.provider_registry = default_registry(youtube_output=app.state.youtube_live_output)
    # Surge switch is off unless CIVICCAST_LIVE_SURGE_THRESHOLD is set AND durable
    # storage is ready (it needs an egress store to resolve live dirs). The
    # durable-storage branch below replaces this when both hold.
    app.state.surge_switch_service = None
    storage_error: str | None = None
    try:
        database_url = _resolve_database_url()
    except ManagedStorageError as exc:
        database_url = None
        storage_error = str(exc)
        _LOG.error("CivicCast managed storage is not ready: %s", exc)
    _require_durable_or_explicit_ephemeral(database_url)

    ephemeral_mode = os.environ.get("CIVICCAST_ALLOW_EPHEMERAL_STORES") == "1"
    ephemeral_asset_store = _EphemeralAssetStore() if ephemeral_mode else None
    asset_store = ephemeral_asset_store or InMemoryAssetStore()
    ephemeral_schedule_store = (
        _EphemeralScheduleStore(ephemeral_asset_store)
        if ephemeral_asset_store is not None
        else None
    )
    caption_review_store = InMemoryCaptionReviewStore()
    caption_job_store = InMemoryOfflineCaptionJobStore()
    summary_store = InMemorySummaryStore()
    summary_job_store = InMemorySummaryGenerationJobStore()
    record_store = InMemoryRecordStore()
    publish_store = InMemoryPublishStore()
    subscribe_store = InMemorySubscribeStore()
    notification_delivery_store = InMemoryNotificationDeliveryStore()
    # No durable schedule/live tables in ephemeral mode, so every asset
    # resolves to the station's default channel (WP-05) rather than guessing.
    publication_target_lookup = StaticChannelAssociationLookup()
    podcast_store = InMemoryPodcastStore()
    activitypub_store = InMemoryActivityPubStore()
    analytics_store = (
        AnalyticsStore()
        if os.environ.get("CIVICCAST_ALLOW_EPHEMERAL_STORES") == "1"
        else AnalyticsStore(default_analytics_state_path())
    )
    app.state.activitypub_config = load_activitypub_config()
    app.state.activitypub_rate_limiter = InboxRateLimiter()
    app.state.subscribe_secrets = load_subscription_secrets()
    app.state.subscribe_rate_limiter = SubscribeRateLimiter()
    app.state.activitypub_actor_fetcher = HttpxRemoteActorFetcher(
        allow_http=app.state.activitypub_config.lab_allow_local,
        allow_local=app.state.activitypub_config.lab_allow_local,
    )
    app.state.activitypub_delivery_client = HttpxActivityPubDeliveryClient(
        config=app.state.activitypub_config,
        allow_http=app.state.activitypub_config.lab_allow_local,
        allow_local=app.state.activitypub_config.lab_allow_local,
    )
    app.state.store_bundle = AppStoreBundle(
        asset_store=lambda: asset_store,
        caption_review_store=lambda: caption_review_store,
        caption_job_store=lambda: caption_job_store,
        summary_store=lambda: summary_store,
        summary_job_store=lambda: summary_job_store,
        record_store=lambda: record_store,
        publish_store=lambda: publish_store,
        subscribe_store=lambda: subscribe_store,
        notification_delivery_store=lambda: notification_delivery_store,
        publication_target_lookup=lambda: publication_target_lookup,
        podcast_store=lambda: podcast_store,
        activitypub_store=lambda: activitypub_store,
        analytics_store=lambda: analytics_store,
    )
    app.state.durable_storage_error = storage_error
    app.state.durable_storage_active = False
    app.state.activate_durable_storage = lambda url, upload_dir=None: _install_durable_store_wiring(
        app, url, upload_dir=upload_dir
    )
    app.state.sync_durable_storage = lambda: _sync_durable_storage(app)

    @app.middleware("http")
    async def public_analytics_body_limit_middleware(request: Any, call_next: Any) -> Any:
        if request.url.path == _PUBLIC_ANALYTICS_INGEST_PATH:
            raw_length = request.headers.get("content-length")
            if raw_length is not None:
                try:
                    content_length = int(raw_length)
                except ValueError:
                    return Response("Invalid content length.", status_code=400)
                if content_length > _PUBLIC_ANALYTICS_MAX_CONTENT_LENGTH:
                    return Response("Analytics event payload is too large.", status_code=413)
            # Starlette's public Request API buffers the body before route parsing; this
            # small guarded receive shim keeps the analytics 413 cap in front of parsing.
            original_receive = request._receive
            bytes_seen = 0
            body_parts: list[bytes] = []
            more_body = True

            while more_body:
                message = await original_receive()
                if message.get("type") != "http.request":
                    break
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    bytes_seen += len(body)
                    if bytes_seen > _PUBLIC_ANALYTICS_MAX_CONTENT_LENGTH:
                        return Response("Analytics event payload is too large.", status_code=413)
                    body_parts.append(body)
                more_body = bool(message.get("more_body", False))
            body_replayed = False

            async def replay_body() -> Any:
                nonlocal body_replayed
                if body_replayed:
                    return {"type": "http.request", "body": b"", "more_body": False}
                body_replayed = True
                return {
                    "type": "http.request",
                    "body": b"".join(body_parts),
                    "more_body": False,
                }

            request._receive = replay_body
            return await call_next(request)
        return await call_next(request)

    @app.middleware("http")
    async def durable_storage_sync_middleware(request: Any, call_next: Any) -> Any:
        syncer = getattr(request.app.state, "sync_durable_storage", None)
        if callable(syncer):
            syncer()
        return await call_next(request)

    @app.get("/api/health", include_in_schema=False)
    @app.get("/health", tags=["platform"], summary="Liveness plus station readiness")
    def health(
        egress_store: Any = Depends(get_egress_store),  # noqa: B008
    ) -> dict[str, str | bool | int]:
        """Liveness probe carrying a readiness verdict; no auth required.

        Spec §5.4 names this as the standard CivicSuite-shaped liveness path.
        ``schema`` reports migration currency (audit ENG-004): ``current`` /
        ``behind`` / ``not-configured`` / ``unknown``.

        Two signals, deliberately separated (walkthrough W-1):

        * **HTTP status is liveness.** Always 200 while the process answers, in
          every schema state. Restart-on-failure supervisors and the installer's
          own probe key on this; a station mid-setup is alive by design.
        * **``status`` is readiness.** ``healthy`` only when the schema matches
          the running code and live-caption readiness is neither ``degraded``
          nor ``unknown``; otherwise ``degraded``. Uptime monitors should alert
          on this field, not on the status code -- a station with no database
          cannot serve a recording, and reporting "healthy" for it told an
          operator the opposite of the truth.
        """
        from civiccast.schema_check import SchemaStatus

        # <installer-path-audit MA-13> The schema verdict used to be a
        # BOOT-TIME snapshot that was never refreshed: computed once in the
        # lifespan, with the only other write reachable solely from
        # _install_durable_store_wiring. So a database unreachable at boot read
        # `unknown` forever; an operator who then ran `alembic upgrade head`
        # still saw `behind` until the service was bounced, and a monitor keyed
        # on the (correct) status=="healthy" && schema=="current" rule alerted
        # indefinitely. In the other direction a database changed under a
        # running app kept reporting `current` -- and that is the exact field
        # Gate A's post-upgrade guard and the installer's own D4 readiness gate
        # now read. Recompute with a short TTL, reusing the existing 15s
        # bounded read; the TTL keeps a hot liveness path from opening a
        # connection per request.
        _maybe_refresh_schema_status(app)
        schema_status: SchemaStatus = getattr(
            app.state, "schema_status", SchemaStatus(state="unknown")
        )
        readiness = "healthy" if schema_status.state == "current" else "degraded"
        from civiccast.alerting.runtime_status import live_caption_readiness
        from civiccast.egress.automation import default_egress_work_dir
        from civiccast.installer.station_state import resolve_live_captions_enabled_or_default

        live_captions = live_caption_readiness(
            egress_store,
            captions_expected=resolve_live_captions_enabled_or_default(),
            work_dir=default_egress_work_dir(),
        )
        if live_captions in {"degraded", "unknown"}:
            readiness = "degraded"
        # Annotated to match the declared return type. Inferred from this
        # literal alone it is dict[str, str], which then rejects the bool and
        # int fields the maintenance branch adds below -- and dict is
        # invariant, so the return failed too. Four errors, one missing
        # annotation.
        body: dict[str, str | bool | int] = {
            "status": readiness,
            "version": _reported_version(),
            "schema": schema_status.state,
            "live_captions": live_captions,
        }
        bootstrap_instance_id = os.environ.get("CIVICCAST_BOOTSTRAP_INSTANCE_ID")
        if bootstrap_instance_id:
            body["bootstrap_instance_id"] = bootstrap_instance_id
        runtime_build_id = os.environ.get("CIVICCAST_RUNTIME_BUILD_ID")
        if runtime_build_id:
            body["runtime_build_id"] = runtime_build_id
        # Gate A run 33681670855: unconditional, not just on "behind". A
        # harness/judge that wants to assert "post-upgrade DB revision ==
        # code head" (the actual D3 upgrade proof, not just the healthy/
        # degraded label) needs both numbers regardless of which state they
        # landed in -- gating this on "behind" only forced every OTHER
        # consumer to re-derive schema_expected_head some other way (or, as
        # happened here, not check it at all and trust the label).
        body["schema_db_revision"] = schema_status.db_revision or "none"
        body["schema_expected_head"] = schema_status.expected_head or "unknown"
        mode = getattr(app.state, "supervisor_mode", _SUPERVISOR_MODE_NORMAL)
        body["mode"] = mode
        if mode == _SUPERVISOR_MODE_MAINTENANCE:
            body["workers_started"] = False
            body["mutating_disabled"] = True
            body["mode_contract"] = int(_SUPERVISOR_MODE_CONTRACT_VERSION)
        return body

    @app.get("/api/version", tags=["platform"])
    def get_version() -> dict[str, str]:
        """Return the running CivicCast version."""
        return {"version": _reported_version()}

    @app.get("/api/hardware", response_model=HardwareProbe, tags=["platform"])
    def get_hardware() -> HardwareProbe:
        """Hardware probe — CPU, RAM, disk, GPU/VRAM, OS, recommended tier.

        Mirrored from AgentSuiteLocal's /api/hardware (spec §5.4) with the
        VRAM extension required by the spec §7.7 tier decision tree.

        Public by design (the installer sizes a deployment before any staff
        token exists), so it answers through ``public_hardware_probe``: the
        disk path is reduced to its volume anchor rather than the probed home
        directory, which used to disclose the OS account name to any caller
        (GauntletGate W-3).
        """
        return public_hardware_probe()

    app.include_router(vod_router)
    app.include_router(manual_router)
    app.include_router(auth_staff_router)
    from civiccast.dr.station_backup_router import staff_router as station_backup_router
    from civiccast.federation.router import router as federation_router
    from civiccast.native.network import (
        NetworkSettings,
        NetworkSettingsStore,
        default_network_settings_path,
    )
    from civiccast.native.network_router import router as station_network_router

    app.state.network_settings_store = NetworkSettingsStore(default_network_settings_path())
    active_network = os.environ.get("CIVICCAST_ACTIVE_NETWORK_SETTINGS")
    app.state.network_active_settings = (
        NetworkSettings.model_validate_json(active_network) if active_network else NetworkSettings()
    )
    app.include_router(station_network_router)
    app.include_router(federation_router)
    app.include_router(station_backup_router)
    app.include_router(schedule_public_router)
    # S7 media lifecycle: MUST be registered before schedule_staff_router --
    # its literal /assets/readiness-dashboard path would otherwise be
    # swallowed by schedule_staff_router's GET /assets/{asset_id} (see the
    # route-ordering note in civiccast.schedule.media_lifecycle_router).
    app.include_router(media_lifecycle_staff_router)
    app.include_router(schedule_staff_router)
    app.include_router(schedule_source_media_router)
    app.include_router(playout_staff_router)
    app.include_router(autoschedule_staff_router)
    app.include_router(programlog_staff_router)
    app.include_router(programlog_public_router)
    app.include_router(live_public_router)
    app.include_router(live_staff_router)
    app.include_router(cable_public_router)
    app.include_router(cable_staff_router)
    app.include_router(app_platform_public_router)
    app.include_router(app_platform_staff_router)
    app.include_router(app_platform_build_staff_router)
    app.include_router(analytics_staff_router)
    app.include_router(cg_public_router)
    app.include_router(cg_staff_router)
    app.include_router(cg_board_staff_router)
    app.include_router(facility_staff_router)
    app.include_router(stream_staff_router)
    app.include_router(contribute_public_router)
    app.include_router(contribute_staff_router)
    app.include_router(installer_public_router)
    app.include_router(installer_staff_router)
    app.include_router(station_box_profile_router)
    app.include_router(station_profile_staff_router)
    app.include_router(commissioning_staff_router)
    app.include_router(alerting_staff_router)
    app.include_router(control_room_staff_router)
    app.include_router(contribution_staff_router)
    app.include_router(contribution_public_router)
    app.include_router(eas_staff_router)
    app.include_router(audio_tracks_staff_router)
    app.include_router(audio_tracks_public_router)
    app.include_router(captions_staff_router)
    app.include_router(recorded_caption_settings_router)
    app.include_router(summary_staff_router)
    app.include_router(ai_models_staff_router)
    app.include_router(metadata_staff_router)
    app.include_router(metadata_public_router)
    app.include_router(reporting_staff_router)
    app.include_router(reporting_public_router)
    app.include_router(underwriting_staff_router)
    app.include_router(agenda_staff_router)
    app.include_router(agenda_public_router)
    app.include_router(migrate_staff_router)
    app.include_router(agenda_import_staff_router)
    app.include_router(paywall_staff_router)
    app.include_router(paywall_public_router)
    app.include_router(paywall_webhook_router)
    app.include_router(producer_ops_staff_router)
    app.include_router(recording_staff_router)
    app.include_router(records_staff_router)
    app.include_router(release_staff_router)
    app.include_router(publish_staff_router)
    app.include_router(subscribe_public_router)
    app.include_router(subscribe_staff_router)
    app.include_router(podcast_public_router)
    app.include_router(podcast_staff_router)
    app.include_router(playback_policy_public_router)
    app.include_router(playback_policy_staff_router)
    app.include_router(activitypub_router)
    app.include_router(egress_public_router)
    app.include_router(egress_staff_router)
    app.include_router(media_public_router)
    app.include_router(media_live_public_router)

    if not os.environ.get("CIVICCAST_AUTH_ACK"):
        _LOG.warning(
            "CivicCast now enforces first-party bearer-token auth on /api/staff/* "
            "routes. Keep the API bound to loopback (127.0.0.1) or behind an "
            "authenticating reverse proxy for network and TLS policy; see "
            "docs/ops/staff-route-protection.md. Set CIVICCAST_AUTH_ACK=1 only "
            "after confirming that deployment posture."
        )

    if (
        ephemeral_mode
        and not database_url
        and ephemeral_asset_store is not None
        and ephemeral_schedule_store is not None
    ):
        _install_ephemeral_store_wiring(app, ephemeral_asset_store, ephemeral_schedule_store)

    # DI wiring for stores. Per v1.2, default stores are owned by this app
    # instance through ``app.state.store_bundle``. The database wiring stays
    # lazy: ``create_engine`` does no I/O, and the actual DB connection happens
    # inside the request handler.
    if database_url:
        _install_durable_store_wiring(
            app,
            database_url,
            upload_dir=_managed_upload_dir_if_ready(),
        )

    _install_staff_openapi_contract(app)
    if lan_only_station:
        # BEFORE the portal mount, deliberately. Starlette matches in
        # registration order and the resident portal is mounted at "/" as a
        # catch-all; a route added after it is shadowed and /openapi.json
        # came back as the SPA's index.html (200 text/html, no gate) on every
        # real station, where station_runtime sets both this flag and
        # CIVICCAST_PUBLIC_PORTAL_DIST (HIGH 1, hostile review of PR #215).
        _install_lan_only_openapi_route(app)
    _mount_packaged_portals(app)
    return app


def _mount_packaged_portals(app: FastAPI) -> None:
    """Serve bundled tester portals when the installer starts CivicCast."""

    operator_dist = _configured_static_dir("CIVICCAST_OPERATOR_CONSOLE_DIST")
    if operator_dist is not None:
        app.mount(
            "/operator",
            SpaStaticFiles(directory=operator_dist, html=True),
            name="operator-console",
        )

    public_dist = _configured_static_dir("CIVICCAST_PUBLIC_PORTAL_DIST")
    if public_dist is not None:
        app.mount(
            "/",
            SpaStaticFiles(directory=public_dist, html=True),
            name="resident-portal",
        )


class SpaStaticFiles(StaticFiles):
    """Serve an SPA shell for direct deep links under a packaged portal.

    W-5 (audit walkthrough): an unmatched ``/api/*`` path must never fall
    through to the SPA shell. Before this fix, a browser-style request (any
    ``Accept: text/html`` or ``Accept: */*``, which is what browsers,
    ``fetch()`` defaults, and most HTTP clients send) for a typo'd or removed
    API path -- e.g. ``/api/does-not-exist`` -- matched none of the routers
    mounted on ``app`` and fell through to this class's ``/`` catch-all mount,
    which swallowed the 404 and served ``index.html`` with status 200. A
    client (or a monitoring probe) asking a JSON API for a resource got an
    HTML document back with a success status, with no honest signal the path
    doesn't exist. ``/api/*`` paths are never part of either SPA's routable
    surface, so they are excluded from the index.html fallback entirely and
    always get a real ``application/json`` 404. Non-``/api`` paths are
    unaffected -- the SPA fallback still serves deep links exactly as before.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if _is_api_path(scope):
            await _api_not_found_response(scope)(scope, receive, send)
            return
        try:
            await super().__call__(scope, receive, send)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not _accepts_html(scope):
                raise
            response = await super().get_response("index.html", scope)
            await response(scope, receive, send)

    async def get_response(self, path: str, scope: Scope) -> Response:
        if _is_api_path(scope):
            return _api_not_found_response(scope)
        response = await super().get_response(path, scope)
        if response.status_code != 404:
            return response
        if not _accepts_html(scope):
            return response
        return await super().get_response("index.html", scope)


def _is_api_path(scope: Scope) -> bool:
    """True for any request path under ``/api`` (W-5). Checked against the
    ASGI scope's own ``path`` rather than the ``path`` argument ``get_response``
    receives, because that argument is already resolved relative to the
    static directory (e.g. rewritten to ``index.html``) by the time some call
    sites reach it."""

    # Scope is a Mapping[str, Any], so scope.get returns Any and the
    # comparison below is Any-typed too -- mypy cannot see that this
    # returns a bool. Bind it as str first, which is also what the
    # startswith call already assumes.
    path: str = scope.get("path", "")
    return path == "/api" or path.startswith("/api/")


def _api_not_found_response(scope: Scope) -> JSONResponse:
    path = scope.get("path", "")
    _LOG.info("Unmatched API path served a JSON 404 (W-5): %s", path)
    return JSONResponse({"detail": "Not Found"}, status_code=http_status.HTTP_404_NOT_FOUND)


def _accepts_html(scope: Scope) -> bool:
    accept = ""
    for key, value in scope.get("headers", []):
        if key == b"accept":
            accept = value.decode("latin-1")
            break
    return "text/html" in accept or "*/*" in accept


def _configured_static_dir(env_name: str) -> str | None:
    """Resolve a packaged portal's build directory, or say loudly why not.

    Both failure paths used to be quiet -- an unset variable returned ``None``
    with no log line at all, and a missing directory logged only a WARNING. On
    a native station NEITHER variable was ever set (only the WSL
    ``headless-bootstrap.ps1`` set them), so the control plane started, passed
    ``/health``, and 404'd ``/operator/`` and ``/`` -- the two surfaces the
    product is reached through -- while looking completely healthy in the logs.

    Now ERROR level for both, because on any real station these are required
    members of the civiccast wheel
    (``scripts/build_native_app_payload.assert_civiccast_wheel_layout``) and
    their absence means a broken install. Still returns ``None`` rather than
    raising: a control plane that answers ``/health`` and can be diagnosed
    beats one that refuses to boot.
    """

    configured = os.environ.get(env_name)
    if not configured:
        _LOG.error(
            "%s is not set, so that packaged portal will NOT be served; the station's "
            "front door is missing. It is set for a native station by "
            "civiccast.native.station_runtime.load_native_station_environment.",
            env_name,
        )
        return None
    path = Path(configured).expanduser().resolve()
    if not path.is_dir():
        _LOG.error(
            "%s points at a missing portal build directory, so that portal will NOT be served: %s",
            env_name,
            path,
        )
        return None
    return str(path)


def _wire_durable_stores(app: FastAPI) -> None:
    """Register every per-request SQL store/service resolver + dependency
    override for durable (Postgres) storage.

    Single shared wiring path for both durable-storage entry points:
    ``create_app()`` when ``DATABASE_URL`` is set at boot, and
    ``_install_durable_store_wiring()`` when an operator runs "Prepare storage"
    at runtime. Keeping it in one place closes the ENG-001 class of bug, where a
    resolver registered on only one of the two paths 503s until a restart on a
    freshly provisioned station. New routers add their resolver + override here
    once and BOTH entry points pick it up. The caller owns ``bind_engine`` +
    upload-dir configuration + the ``durable_storage_active`` flag.
    """

    @contextmanager
    def _session_factory() -> Iterator[Session]:
        # Drain the get_session generator so its finally: block runs
        # session.close() at exit. StopIteration is the expected signal
        # that the generator has finished.
        gen = get_session()
        session = next(gen)
        try:
            yield session
        finally:
            with suppress(StopIteration):
                next(gen)

    def _resolve_postgres_store() -> PostgresAssetStore:
        # Wrap the session factory into a fresh PostgresAssetStore per
        # request (Decision 5 -- lazy posture; no I/O at import time).
        return PostgresAssetStore(_session_factory)

    def _resolve_schedule_store() -> PostgresScheduleStore:
        # Same lazy posture for the schedule store. The schedule store is a
        # separate aggregate from the asset store; a different SA model +
        # different conflict-detection rules.
        return PostgresScheduleStore(_session_factory)

    def _resolve_media_lifecycle_store() -> MediaLifecycleStore:
        # S7 media lifecycle: readiness/watch-folder/retention-policy/
        # storage-budget queries. Same lazy per-request posture as every
        # other store above.
        return MediaLifecycleStore(_session_factory)

    def _resolve_live_session_store() -> LiveSessionStore:
        return LiveSessionStore(_session_factory)

    def _resolve_preflight_evaluator() -> PreflightEvaluator:
        # B2 fix: a real ffprobe-backed source probe, not the no-probe default
        # that fails every live_source check closed (REASON_LIVE_SOURCE_NOT_PROBED)
        # and 409s every real go-on-air attempt. The installer's private
        # rehearsal path still overrides this per-call with its own sample-file
        # probe via `source_probe_override` (civiccast/installer/service.py);
        # this is the probe every other caller -- i.e. a real station -- gets.
        #
        # B3 fix: network + storage probes run here too, so "Run pre-flight"
        # answers its own "not probed" questions instead of depending on a
        # caller (the operator UI) that never actually probed.
        return PreflightEvaluator(
            _session_factory,
            source_probe=build_source_probe(),
            network_probe=build_network_probe(),
            storage_probe=build_storage_probe(),
        )

    def _resolve_live_source_store() -> LiveSourceStore:
        return LiveSourceStore(_session_factory)

    def _resolve_live_source_readiness_service() -> LiveSourceReadinessService:
        # WP-07: the operator's explicit "Check source" action and the
        # production takeover gate share one service so they can never disagree
        # about what "ready" means for the same row.
        return LiveSourceReadinessService(LiveSourceStore(_session_factory))

    def _resolve_live_relay_config_store() -> LiveRelayConfigStore:
        return LiveRelayConfigStore(_session_factory)

    def _resolve_recording_target_store() -> RecordingTargetStore:
        return RecordingTargetStore(_session_factory)

    def _resolve_live_recording_finalizer() -> LiveRecordingFinalizer:
        return LiveRecordingFinalizer(_session_factory)

    _wire_finalization_worker(app, _session_factory)

    def _resolve_live_finalization_worker() -> LiveFinalizationWorker:
        # Same settings + CDN adapter as the loop thread so the endpoints
        # and the worker agree on configuration (ENG-006).
        supervisor = app.state.finalization_worker_supervisor
        return build_worker(
            _session_factory, supervisor.settings, cdn_adapter=supervisor.cdn_adapter
        )

    def _resolve_summary_store() -> PostgresSummaryStore:
        return PostgresSummaryStore(_session_factory)

    def _resolve_summary_job_store() -> PostgresSummaryGenerationJobStore:
        # A summary generation job outlives the request that queued it -- a
        # CPU-only generation can legitimately run for minutes (field evidence
        # 2026-08-29, see civiccast/summary/job.py).
        return PostgresSummaryGenerationJobStore(_session_factory)

    def _resolve_summary_model() -> SummaryModel:
        # S13: the summary adapter loads the operator-selected model (adaptive local
        # default — gemma4:12b on >=16GB, gemma4:e4b below — when unselected).
        #
        # This is the durable-storage-wired override of get_summary_model
        # (registered below via app.dependency_overrides) -- a second,
        # independent path to the same local-Ollama build besides
        # summary/router.py's own default. Both raise
        # OllamaRuntimeUnavailableError when the daemon is unreachable and
        # both must report it the same clean "not configured" way rather
        # than a raw 500.
        try:
            return build_summary_model(_build_ai_model_service(_session_factory))
        except OllamaRuntimeUnavailableError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=OLLAMA_NOT_CONFIGURED_MESSAGE,
            ) from exc

    def _resolve_record_store() -> PostgresRecordStore:
        return PostgresRecordStore(_session_factory)

    def _resolve_publish_store() -> PostgresPublishStore:
        return PostgresPublishStore(_session_factory)

    def _resolve_subscribe_store() -> PostgresSubscribeStore:
        return PostgresSubscribeStore(_session_factory)

    def _resolve_notification_delivery_store() -> PostgresNotificationDeliveryStore:
        return PostgresNotificationDeliveryStore(_session_factory)

    def _resolve_publication_target_lookup() -> SqlChannelAssociationLookup:
        return SqlChannelAssociationLookup(_session_factory)

    def _resolve_podcast_store() -> PostgresPodcastStore:
        return PostgresPodcastStore(_session_factory)

    def _resolve_activitypub_store() -> PostgresActivityPubStore:
        return PostgresActivityPubStore(_session_factory)

    def _resolve_egress_store() -> PostgresEgressStore:
        return PostgresEgressStore(_session_factory)

    def _resolve_commit_service() -> CommitService:
        # S4 Commit-to-Air gate: compose the dry-run (schedule + asset stores),
        # the report store, and the engine dispatcher (writing to the same
        # egress command queue the daemon consumes). Stateless -- built fresh
        # per request like the other store resolvers.
        schedule_store = PostgresScheduleStore(_session_factory)
        asset_store = PostgresAssetStore(_session_factory)
        return CommitService(
            CommitDryRunService(schedule_store, asset_store),
            schedule_store,
            PlayoutDispatcher(PostgresEgressStore(_session_factory)),
        )

    def _resolve_autoschedule_service() -> AutoScheduleService:
        # S18 auto-scheduling: CRUD over saved searches / daypart blocks /
        # rules, plus preview + compile. Holds the session factory so the
        # compile path can open a materializer session. Stateless.
        return AutoScheduleService(
            AutoScheduleStore(_session_factory),
            session_factory=_session_factory,
            tz=_station_tz,
        )

    def _resolve_takeover_service() -> TakeoverService:
        # S5 live takeover: audit store + egress command queue + a live
        # ingest-plan provider.
        #
        # WP-07 / audit ENG-003: this provider used to pass relay configs
        # ONLY. ``/api/staff/live/ingest-plan`` (civiccast.live.router) had
        # already been fixed to include the channel's ``LiveSourceStore``
        # rows, so the API showed the operator a plan containing their real
        # encoder while the production takeover service was built from a
        # different, source-less plan -- a configured source could appear in
        # the plan and be invisible to takeover, and on a station with no
        # relay row at all takeover had nothing ready to select. Both
        # providers now read the same two stores, scoped to the same channel.
        relay_store = LiveRelayConfigStore(_session_factory)
        source_store = LiveSourceStore(_session_factory)

        def _ingest_plan(channel_id: str) -> LiveIngestPlan:
            return build_ingest_plan(
                channel_id,
                relay_store.list(channel_id=channel_id, enabled=True),
                live_sources=source_store.list(channel_id=channel_id),
            )

        readiness = LiveSourceReadinessService(source_store)

        def _verify_readiness(
            channel_id: str, path_id: str, endpoint_url: str
        ) -> TakeoverReadiness:
            return readiness.verify_for_takeover(
                channel_id=channel_id, path_id=path_id, endpoint_url=endpoint_url
            )

        return TakeoverService(
            PostgresTakeoverAuditStore(_session_factory),
            PostgresEgressStore(_session_factory),
            _ingest_plan,
            readiness_verifier=_verify_readiness,
        )

    def _resolve_caption_review_store() -> PostgresCaptionReviewStore:
        # Stage E: caption review decisions are durable whenever durable
        # storage is active (previously in-memory even on this path).
        return PostgresCaptionReviewStore(_session_factory)

    def _resolve_caption_job_store() -> PostgresOfflineCaptionJobStore:
        # K3: an offline caption job outlives the request that queued it
        # and usually outlives the process, since stage two waits on an
        # operator's review.
        return PostgresOfflineCaptionJobStore(_session_factory)

    # S14: durable Postgres-backed analytics store replaces the JSON file
    # (AnalyticsStore) whenever durable storage is active. One-time,
    # idempotent backfill of any legacy analytics-events.json rows runs here
    # (never inside the migration -- see 0076_analytics_viewership's
    # docstring) and is a fast no-op once viewership_events has any row.
    analytics_store: object = PostgresAnalyticsStore(_session_factory)
    try:
        backfill_json_events(_session_factory, default_analytics_state_path())
    except Exception:
        _LOG.exception(
            "Legacy analytics JSON backfill failed; continuing with the durable "
            "store (new events are unaffected)."
        )

    app.state.store_bundle = AppStoreBundle(
        asset_store=_resolve_postgres_store,
        caption_review_store=_resolve_caption_review_store,
        caption_job_store=_resolve_caption_job_store,
        summary_store=_resolve_summary_store,
        summary_job_store=_resolve_summary_job_store,
        record_store=_resolve_record_store,
        publish_store=_resolve_publish_store,
        subscribe_store=_resolve_subscribe_store,
        notification_delivery_store=_resolve_notification_delivery_store,
        publication_target_lookup=_resolve_publication_target_lookup,
        podcast_store=_resolve_podcast_store,
        activitypub_store=_resolve_activitypub_store,
        analytics_store=lambda: analytics_store,
    )
    app.state.staff_token_store = PostgresStaffTokenStore(_session_factory)

    # Double-override per Decision Q4b: both the vod router's ``get_store`` and
    # the schedule router's ``get_asset_store`` resolve to the SAME
    # Postgres-backed factory so the two routers see the same active store
    # object per request.
    app.dependency_overrides[get_postgres_store] = _resolve_postgres_store
    app.dependency_overrides[get_summary_model] = _resolve_summary_model
    app.dependency_overrides[get_schedule_store] = _resolve_schedule_store
    app.dependency_overrides[get_media_lifecycle_store] = _resolve_media_lifecycle_store
    # Finding 4 (candidate #17): the "Scan now" staff action needs the SAME
    # WatchFolderWorker instance the background ThreadSupervisor drives
    # (app.state.watch_folder_worker, wired above), never a second one --
    # a second instance would have its own in-memory nothing-durable state
    # but more importantly would double the settle-window/ingest work.
    app.dependency_overrides[get_watch_folder_worker] = lambda: getattr(
        app.state, "watch_folder_worker", None
    )
    app.dependency_overrides[get_live_session_store] = _resolve_live_session_store
    app.dependency_overrides[get_preflight_evaluator] = _resolve_preflight_evaluator
    app.dependency_overrides[get_live_source_store] = _resolve_live_source_store
    app.dependency_overrides[get_live_source_readiness_service] = (
        _resolve_live_source_readiness_service
    )
    app.dependency_overrides[get_live_relay_config_store] = _resolve_live_relay_config_store
    app.dependency_overrides[get_recording_target_store] = _resolve_recording_target_store
    app.dependency_overrides[get_live_finalization_worker] = _resolve_live_finalization_worker
    app.dependency_overrides[get_live_recording_finalizer] = _resolve_live_recording_finalizer
    app.dependency_overrides[get_egress_store] = _resolve_egress_store
    app.dependency_overrides[get_commissioning_egress_store] = _resolve_egress_store
    # Enable the surge switch now that durable storage (egress store) is ready;
    # from_env returns None unless CIVICCAST_LIVE_SURGE_THRESHOLD is set.
    app.state.surge_switch_service = SurgeSwitchService.from_env(
        egress_store_provider=_resolve_egress_store,
        cdn_adapter_provider=app.state.resolve_cdn_adapter,
    )
    app.dependency_overrides[get_commit_service] = _resolve_commit_service
    app.dependency_overrides[get_autoschedule_service] = _resolve_autoschedule_service
    app.dependency_overrides[get_takeover_service] = _resolve_takeover_service
    app.dependency_overrides[get_alerting_session_factory] = lambda: _session_factory

    def _resolve_program_log_store() -> PostgresProgramLogStore:
        return PostgresProgramLogStore(_session_factory)

    def _resolve_cg_bulletin_store() -> PostgresCgBulletinStore:
        # Cable automation CA-3: durable community bulletins back the operator
        # board and the bulletin filler rotation.
        return PostgresCgBulletinStore(_session_factory)

    def _resolve_cg_board_service() -> CgBoardService:
        # S6 (build step 7): the operator CG bulletin-board designer.
        return CgBoardService(
            CgBoardStore(_session_factory),
            upcoming_reader=_cg_upcoming_reader(_session_factory),
        )

    def _resolve_program_log_materializer() -> ProgramLogMaterializer:
        # Cable automation CA-1: same construction as the lifespan worker so
        # on-demand "refresh guide" runs agree with the loop.
        return _build_program_log_materializer(_session_factory)

    def _resolve_program_log_asset_titler() -> Any:
        # CA-5: the public guide joins display titles server-side so residents
        # never see raw asset ids.
        return PostgresAssetStore(_session_factory).get_staff_row

    app.dependency_overrides[get_cg_bulletin_store] = _resolve_cg_bulletin_store
    app.dependency_overrides[get_cg_board_service] = _resolve_cg_board_service
    app.dependency_overrides[get_cg_depth_store] = lambda: CgDepthStore(_session_factory)
    app.dependency_overrides[get_cg_audio_asset_resolver] = lambda: _default_audio_resolver(
        _session_factory
    )
    # WP-06: the public feed catalog / portal display (civiccast.cg.router)
    # defines its own lighter DI seam of the same name to avoid pulling
    # board_router's egress/ffmpeg import chain into the public router --
    # both seams resolve to the same durable CgBoardService instance factory.
    app.dependency_overrides[get_cg_feed_board_service] = _resolve_cg_board_service
    app.dependency_overrides[get_program_log_store] = _resolve_program_log_store
    app.dependency_overrides[get_program_log_materializer] = _resolve_program_log_materializer
    app.dependency_overrides[get_program_log_asset_titler] = _resolve_program_log_asset_titler

    def _resolve_control_room_store() -> ControlRoomStore:
        return ControlRoomStore(_session_factory)

    def _resolve_control_room_service() -> ControlRoomService:
        # S16: drive devices through the Node TSR sidecar when its localhost URL
        # is configured (CIVICCAST_CONTROL_ROOM_TSR_URL, e.g. http://127.0.0.1:7717);
        # otherwise the fail-closed NullTsrClient — plan opens no socket, probe
        # reports unreachable, and fire audits "failed" then surfaces a 502
        # rather than silently "succeeding" against a control plane that isn't there.
        tsr: TsrClient
        tsr_url = os.environ.get("CIVICCAST_CONTROL_ROOM_TSR_URL")
        if tsr_url:
            try:
                tsr = HttpTsrClient(tsr_url)
            except ValueError as exc:
                tsr = NullTsrClient(
                    f"Invalid CIVICCAST_CONTROL_ROOM_TSR_URL: {exc}; cue fire/probe paths fail closed."
                )
        else:
            tsr = NullTsrClient()
        return ControlRoomService(ControlRoomStore(_session_factory), tsr)

    app.dependency_overrides[get_control_room_store] = _resolve_control_room_store
    app.dependency_overrides[get_control_room_service] = _resolve_control_room_service
    app.dependency_overrides[get_device_secret_writer] = lambda: save_device_secret

    if not hasattr(app.state, "guest_program_lock"):
        app.state.guest_program_router = None
        app.state.guest_program_lock = threading.Lock()

    def _guest_program() -> GuestProgramRouter:
        if os.environ.get("CIVICCAST_REMOTE_CONTRIBUTION", "off").strip().lower() != "on":
            raise RuntimeError("Remote guest broadcast is disabled")
        with app.state.guest_program_lock:
            existing = getattr(app.state, "guest_program_router", None)
            if existing is not None:
                return cast(GuestProgramRouter, existing)
            public_url = os.environ.get("CIVICCAST_REMOTE_CONTRIBUTION_MEDIAMTX_URL")
            rtsp_url = os.environ.get(
                "CIVICCAST_REMOTE_CONTRIBUTION_RTSP_URL", "rtsp://127.0.0.1:8554"
            )
            srt_url = os.environ.get(
                "CIVICCAST_REMOTE_CONTRIBUTION_SRT_URL", "srt://127.0.0.1:8890"
            )
            if not public_url:
                raise RuntimeError(
                    "Remote guest broadcast needs a browser-accessible MediaMTX HTTPS URL"
                )
            egress_store = PostgresEgressStore(_session_factory)
            router = GuestProgramRouter(
                ContributionStore(_session_factory),
                LiveSourceStore(_session_factory),
                _resolve_takeover_service,
                rtsp_base=rtsp_url,
                srt_base=srt_url,
                proof_events=lambda channel_id, limit: egress_store.recent_proof_events(
                    channel_id, limit
                ),
            )
            app.state.guest_program_router = router
            return router

    def _resolve_contribution_service() -> ContributionService:
        # S17: mint real VDO.Ninja join URLs when a self-hosted base URL is
        # configured (CIVICCAST_REMOTE_CONTRIBUTION_VDO_URL); otherwise the
        # fail-closed NullVdoNinjaBridge — open/invite raise 503 rather than
        # minting dead join links to a VDO that isn't there.
        # Per-request env reads — the operator can update the VDO.Ninja URL
        # via env and activate it without a server restart (hot-switch; same
        # pattern as the TSR URL in _resolve_control_room_service above).
        vdo_url = os.environ.get("CIVICCAST_REMOTE_CONTRIBUTION_VDO_URL")
        bridge = (
            UrlVdoNinjaBridge(
                vdo_url,
                diagnostics_probe=contribution_diagnostics_snapshot,
                connectivity_test=contribution_turn_connectivity_test,
                mediamtx_url=os.environ.get("CIVICCAST_REMOTE_CONTRIBUTION_MEDIAMTX_URL"),
            )
            if vdo_url
            else NullVdoNinjaBridge()
        )
        egress_store = PostgresEgressStore(_session_factory)

        def _channel_enabled(channel_id: str) -> bool | None:
            config = egress_store.get_config(channel_id)
            return config.enabled if config is not None else None

        return ContributionService(
            ContributionStore(_session_factory),
            bridge,
            channel_enabled=_channel_enabled,
            on_air_hook=lambda guest, room: _guest_program().on_air(guest, room),
            off_air_hook=lambda guest, room: _guest_program().off_air(guest, room),
            audio_mute_hook=lambda guest, room, muted: _guest_program().audio_mute(
                guest, room, muted
            ),
            alert_hook=_build_contribution_alert_hook(_session_factory),
        )

    app.dependency_overrides[get_contribution_service] = _resolve_contribution_service

    # S11c public-safety (EAS): per-request store/service + the public CG
    # emergency-overlay provider (now reflects real ingested alerts, never labeled EAS).
    def _resolve_eas_store() -> EasStore:
        return EasStore(_session_factory)

    def _resolve_eas_service() -> EasDisplayService:
        return EasDisplayService(EasStore(_session_factory))

    app.dependency_overrides[get_eas_store] = _resolve_eas_store
    app.dependency_overrides[get_eas_service] = _resolve_eas_service

    # S13: the operator AI-model selection API. The service seeds its adaptive
    # summary default from the live hardware probe (same helper the summary/caption
    # runtime wiring uses) so the registry the operator reads agrees with what the
    # feature runtimes actually load.
    def _resolve_ai_model_service() -> AiModelService:
        return _build_ai_model_service(_session_factory)

    app.dependency_overrides[get_ai_model_service] = _resolve_ai_model_service

    # S22 custom metadata fields: per-request validation+orchestration service over the
    # durable store. Bind the asset reference-resolver + public-asset lister to THIS app's
    # session factory (not the service's module-global default engine): they share its
    # dialect + schema-translate map, so under SQLite they query "assets", not
    # "civiccast.assets". The global default leaks across full-suite ordering and 500s with
    # "no such table: civiccast.assets" — this keeps the resolver consistent with the app DB.
    # ``producer_exists`` is bound to THIS app's contributor identity source the same way
    # (the module default constructs a path-less, empty contributor store): prefer the store
    # already on ``app.state`` (shared with the contributor router), else the durable path —
    # so producer_ref resolution shares the app's producer-identity plane and resolves any
    # producer the station has on record (not only one with a queued submission).
    def _producer_exists(producer_id: str) -> bool:
        from civiccast.contribute.store import (
            ContributorSubmissionStore,
            default_contributor_store_path,
        )

        store = getattr(app.state, "contributor_submission_store", None)
        if not isinstance(store, ContributorSubmissionStore):
            store = ContributorSubmissionStore(default_contributor_store_path())
        return producer_id in store.known_producer_ids()

    def _resolve_custom_field_service() -> CustomFieldService:
        from civiccast.captions.vod import published_caption_sidecar
        from civiccast.egress.caption_embed import load_caption_cues_from_timed_text
        from civiccast.metadata.models import PublicTranscriptCue
        from civiccast.stream.media_router import _package_dir_for_asset

        asset_store = PostgresAssetStore(_session_factory)

        def _transcript(asset_id: str) -> list[PublicTranscriptCue]:
            # Read the actual published artifact, already reviewed and mapped to
            # the package timeline by F1. Never rebase mutable source cues again.
            with _session_factory() as session:
                try:
                    package = _package_dir_for_asset(asset_id, session)
                except HTTPException as exc:
                    if exc.status_code == 404:
                        return []
                    raise
            sidecar = published_caption_sidecar(package).resolve()
            if not sidecar.is_relative_to(package.resolve()):
                return []
            try:
                cues = load_caption_cues_from_timed_text(sidecar, source_id=asset_id)
            except FileNotFoundError:
                return []
            return [
                PublicTranscriptCue(
                    cue_id=cue.cue_id,
                    start_seconds=cue.start_seconds,
                    end_seconds=cue.end_seconds,
                    text=cue.text,
                )
                for cue in cues
            ]

        return CustomFieldService(
            CustomFieldStore(_session_factory),
            asset_exists=lambda asset_id: asset_store.get_staff_row(asset_id) is not None,
            producer_exists=_producer_exists,
            public_asset_lister=lambda: [
                asset for asset in asset_store.list() if asset.published_at is not None
            ],
            transcript_provider=_transcript,
        )

    app.dependency_overrides[get_custom_field_service] = _resolve_custom_field_service
    app.dependency_overrides[get_eas_overlay_provider] = lambda: (
        EasDisplayService(EasStore(_session_factory)).active_emergency_overlay
    )
    app.dependency_overrides[get_audio_track_store] = lambda: AudioTrackStore(_session_factory)

    # S23 franchise reporting + EPG export: a per-request reporting service binds the SAME
    # app session factory the metadata service uses (so report queries see the live schema
    # under SQLite + Postgres alike). The EPG exporter wraps the published schedule store so
    # generated guides match what the operator committed for air. No category enrichment
    # by default — the operator may wire a S22 cf-resolver later when categories are needed
    # in the guide payload.
    from civiccast.schedule.store import PostgresScheduleStore as _PgScheduleStore

    def _resolve_reporting_service() -> ReportingService:
        return ReportingService(_session_factory)

    def _resolve_reporting_store() -> ReportingStore:
        return ReportingStore(_session_factory)

    def _resolve_epg_exporter() -> EpgExporter:
        reader = PostgresCommittedScheduleReader(_PgScheduleStore(_session_factory))
        return EpgExporter(schedule_reader=reader)

    app.dependency_overrides[get_reporting_service] = _resolve_reporting_service
    app.dependency_overrides[get_reporting_store] = _resolve_reporting_store
    app.dependency_overrides[get_epg_exporter] = _resolve_epg_exporter

    # S24 underwriting / sponsorship-spot management: a per-request store binds the
    # SAME app session factory used elsewhere; the affidavit service composes the
    # underwriting store with S23's ReportingStore so the as_run_log join in
    # AffidavitService.for_underwriter has a single source of truth for actual air
    # times. The trafficking compiler is wired with a daypart resolver that walks
    # the S19 AutoScheduleStore — DC-1 daypart enforcement is honored in
    # production (E-3) rather than silently no-op'd. The ``CIVICCAST_REQUIRE_FCC_ACK``
    # env knob (DC-5 station policy) is read once at wire time so a fresh
    # operator restart picks up policy changes.

    def _resolve_underwriting_store() -> UnderwritingStore:
        return UnderwritingStore(_session_factory)

    def _resolve_affidavit_service() -> AffidavitService:
        return AffidavitService(
            underwriting_store=UnderwritingStore(_session_factory),
            reporting_store=ReportingStore(_session_factory),
        )

    def _resolve_trafficking_compiler() -> TraffickingCompiler:
        autoschedule_store = AutoScheduleStore(_session_factory)
        return TraffickingCompiler(
            UnderwritingStore(_session_factory),
            daypart_resolver=autoschedule_store.get_schedule_block,
            require_fcc_ack=os.environ.get("CIVICCAST_REQUIRE_FCC_ACK") == "1",
            station_id=os.environ.get("CIVICCAST_STATION_ID") or "civiccast-station",
        )

    app.dependency_overrides[get_underwriting_store] = _resolve_underwriting_store
    app.dependency_overrides[get_affidavit_service] = _resolve_affidavit_service
    app.dependency_overrides[get_trafficking_compiler] = _resolve_trafficking_compiler

    # S25 meeting-agenda integration: per-request agenda store + service.
    # The service's chapter provider walks the asset store's ``chapters_json``
    # so "sync-from-chapters" projects the SAME chapter list the player /
    # WebVTT track sees (DC-3 — single source of truth). ``chapters_json``
    # may be a JSON string (Postgres jsonb roundtrip on some adapters) or
    # already a decoded list — the resolver handles both.
    def _resolve_agenda_store() -> AgendaStore:
        return AgendaStore(_session_factory)

    def _resolve_agenda_service() -> AgendaService:
        from civiccast.captions.vod import package_source_range
        from civiccast.stream.media_router import _package_dir_for_asset

        asset_store = PostgresAssetStore(_session_factory)

        def playback_window(asset_id: str) -> tuple[float, float | None] | None:
            with _session_factory() as session:
                try:
                    package = _package_dir_for_asset(asset_id, session)
                except HTTPException as exc:
                    if exc.status_code == 404:
                        return None
                    raise
            return package_source_range(package)

        return AgendaService(
            AgendaStore(_session_factory),
            asset_chapter_provider=_make_agenda_chapter_provider(asset_store),
            playback_window_provider=playback_window,
        )

    app.dependency_overrides[get_agenda_store] = _resolve_agenda_store
    app.dependency_overrides[get_agenda_service] = _resolve_agenda_service

    # 0.4.0 migration core: per-request MigrationService over the SAME
    # session factory every other durable store here uses — imported shows
    # and schedule items land in the real ``assets`` / ``schedule_items``
    # tables (see civiccast.migrate.service), never a parallel database.
    def _resolve_migration_service() -> MigrationService:
        return MigrationService(_session_factory)

    app.dependency_overrides[get_migration_service] = _resolve_migration_service

    # 4.1.0 Agenda Bridge Phase 1: agenda import provenance (optional
    # bookkeeping -- see civiccast/agenda_import/provenance.py).
    def _resolve_agenda_import_provenance_store() -> AgendaImportProvenanceStore:
        return AgendaImportProvenanceStore(_session_factory)

    app.dependency_overrides[get_agenda_import_provenance_store] = (
        _resolve_agenda_import_provenance_store
    )

    # S26 paywall integration (OPTIONAL / default OFF). Stripe-hosted
    # Checkout + Customer Portal means card data never reaches our
    # servers (DC-4); we only persist Stripe-side ids. The signing-secret
    # getter walks the per-station config so the secret can be rotated
    # via PATCH /api/staff/paywall/config without restarting the app.
    def _resolve_paywall_store() -> PaywallStore:
        return PaywallStore(_session_factory)

    def _resolve_paywall_service() -> PaywallService:
        paywall_store = PaywallStore(_session_factory)

        def _signing_secret_getter(sid: str) -> str | None:
            cfg = paywall_store.get_config_for_station(sid)
            return cfg.signing_secret if cfg else None

        return PaywallService(
            paywall_store,
            signing_secret_getter=_signing_secret_getter,
        )

    app.dependency_overrides[get_paywall_store] = _resolve_paywall_store
    app.dependency_overrides[get_paywall_service] = _resolve_paywall_service

    # Item 23 producer/volunteer/equipment ops: series applications,
    # volunteer roster, call sheets, equipment roster + checkouts, training
    # badges, and access rules. Staff-only (no public routes).
    def _resolve_producer_ops_store() -> ProducerOpsStore:
        return ProducerOpsStore(_session_factory)

    app.dependency_overrides[get_producer_ops_store] = _resolve_producer_ops_store

    # S21 scheduled recording. Production wiring supplies the S15/S7/S8
    # protocol seams so Record Now and worker-driven scheduled captures produce
    # normal recorded assets instead of returning an unwired 503.
    recording_store = RecordingStore(_session_factory)
    scheduled_recording_settings = ScheduledRecordingSettings.from_env()
    recording_input_catalog = RecordingInputPresetCatalog.from_env()
    recording_svc = RecordingService(
        recording_store,
        capture_pipeline=FfmpegScheduledCapturePipeline(
            _session_factory,
            settings=scheduled_recording_settings,
            hardware_input_args_resolver=recording_input_catalog.resolve_args,
        ),
        asset_finalizer=ScheduledRecordingAssetFinalizer(_session_factory),
        alert_sink=RecordingAlertSink(_session_factory),
        tz=_station_tz,
    )
    app.state.scheduled_recording_settings = scheduled_recording_settings
    app.state.scheduled_recording_worker = ScheduledRecordingWorker(
        recording_svc,
        station_id=os.environ.get("CIVICCAST_STATION_ID") or "civiccast-station",
        settings=scheduled_recording_settings,
    )

    def _resolve_recording_store() -> RecordingStore:
        return recording_store

    def _resolve_recording_input_catalog() -> RecordingInputPresetCatalog:
        return recording_input_catalog

    def _resolve_recording_service() -> RecordingService:
        return recording_svc

    app.dependency_overrides[get_recording_store] = _resolve_recording_store
    app.dependency_overrides[get_recording_input_catalog] = _resolve_recording_input_catalog
    app.dependency_overrides[get_recording_service] = _resolve_recording_service

    _wire_stage_f_workers(app, _session_factory)
    # BLOCKER fix: reconcile_orphans() exists (recording/service.py) but was
    # never called from production -- a restart mid-recording left the job
    # "recording" forever and permanently blocked that source's future
    # captures (overlap-blocking state). Registered as a one-shot startup
    # condition hook (list is guaranteed to exist after _wire_stage_f_workers
    # above) rather than run inline, matching the caption-tier hook's
    # "create_app() must never touch the database" posture.
    app.state.startup_condition_hooks.append(
        _build_recording_reconcile_startup_condition(recording_svc)
    )
    if os.environ.get(
        "CIVICCAST_REMOTE_CONTRIBUTION", "off"
    ).strip().lower() == "on" and os.environ.get("CIVICCAST_REMOTE_CONTRIBUTION_MEDIAMTX_URL"):
        app.state.startup_condition_hooks.append(lambda: _guest_program().recover())


def _install_ephemeral_store_wiring(
    app: FastAPI,
    asset_store: _EphemeralAssetStore,
    schedule_store: _EphemeralScheduleStore,
) -> None:
    """Wire explicit throwaway/dev mode to volatile staff stores."""

    app.dependency_overrides[get_postgres_store] = lambda: asset_store
    app.dependency_overrides[get_schedule_store] = lambda: schedule_store


def _install_durable_store_wiring(
    app: FastAPI,
    database_url: str,
    *,
    upload_dir: str | None = None,
) -> None:
    """Bind durable SQL stores into an already-running app instance."""

    os.environ["DATABASE_URL"] = database_url
    _configure_upload_dir(upload_dir=upload_dir or _managed_upload_dir_if_ready())
    bind_engine(_create_database_engine(database_url))
    app.state.durable_storage_error = None
    app.state.durable_storage_active = True

    _wire_durable_stores(app)
    _ensure_default_local_recording_target(app)
    _refresh_schema_status(app)
    _wire_station_backup_service(app)


def _wire_station_backup_service(app: FastAPI) -> None:
    """Bind the native station backup service whenever durable stores change."""

    # Installed stations need backups before AI/media activation as well.
    if (
        os.environ.get("CIVICCAST_NATIVE_STATION") != "1"
        and os.environ.get("CIVICCAST_SUPERVISED") != "1"
    ):
        return

    from civiccast.dr.station_backup_runtime import build_station_backup_service

    replacement = build_station_backup_service()
    current = getattr(app.state, "station_backup_service", None)
    if current is replacement:
        return
    if current is not None:
        current.stop_worker()
    app.state.station_backup_service = replacement
    if (
        getattr(app.state, "lifespan_started", False)
        and getattr(app.state, "supervisor_mode", _SUPERVISOR_MODE_UNKNOWN)
        not in _SUPERVISOR_MODE_FROZEN
    ):
        replacement.start_worker()


def _refresh_schema_status(app: FastAPI) -> None:
    """Re-run the schema-currency check after storage is wired mid-flight.

    ``schema_status`` is computed at lifespan startup, but an operator who runs
    "Prepare storage" from the console configures the database AFTER that -- so
    the cached verdict stayed ``not-configured`` until the service was bounced.
    Once ``/health``'s ``status`` derives from it (walkthrough W-1), that
    staleness would have pinned a freshly-prepared station at ``degraded``.

    Guarded on ``lifespan_started``: ``create_app()`` also wires durable storage
    when ``DATABASE_URL`` is set at boot, and create_app must never touch the
    database (pinned by ``test_create_app_does_not_call_engine_connect``).
    """

    if not getattr(app.state, "lifespan_started", False):
        return
    from civiccast.schema_check import check_schema_currency

    app.state.schema_status = check_schema_currency(os.environ.get("DATABASE_URL"))
    app.state.schema_status_checked_monotonic = time.monotonic()


#: <installer-path-audit MA-13> How long ``/health`` may serve a cached schema
#: verdict before recomputing it. Short enough that an operator who just ran
#: ``alembic upgrade head`` sees the truth on their next refresh, and that the
#: post-upgrade guards Gate A and the installer now key on cannot read a
#: pre-upgrade snapshot; long enough that a liveness path a supervisor polls
#: every second does not open a database connection per request. The read
#: itself is already hard-bounded at 15s by
#: ``schema_check._READ_DB_REVISION_CEILING_SECONDS``.
SCHEMA_STATUS_TTL_SECONDS = 5.0


def _maybe_refresh_schema_status(app: FastAPI) -> None:
    """Recompute ``app.state.schema_status`` when the cached one is stale.

    Never raises: ``check_schema_currency`` is itself total (it reports
    ``unknown`` rather than propagating), and a failure to refresh must leave
    the previous verdict rather than break the liveness path.
    """

    if not getattr(app.state, "lifespan_started", False):
        return
    last = getattr(app.state, "schema_status_checked_monotonic", None)
    now = time.monotonic()
    if last is not None and (now - last) < SCHEMA_STATUS_TTL_SECONDS:
        return
    try:
        _refresh_schema_status(app)
    except Exception:  # pragma: no cover - defensive; the callee is total
        _LOG.exception("Schema-status refresh failed; keeping the previous verdict.")


def _ensure_default_local_recording_target(app: FastAPI) -> None:
    """Seed the production recording target when durable storage comes up.

    Runs inside ``create_app`` on every durable-storage start, so nothing here
    may stop the control plane from serving ``/health``: an unwritable or
    unavailable recording location is an operator-fixable condition, not a boot
    failure (the same posture ``_require_durable_or_explicit_ephemeral`` takes
    for storage that is not ready at all). Every filesystem and store call below
    is therefore guarded.

    The seed itself is deliberate, not incidental. ``preflight.py``'s
    ``recording_target`` check is a REQUIRED check and no shipped operator
    screen can create a target (the console's API client exposes
    ``listRecordingTargets`` only), so a station with no target cannot go on air
    and cannot fix that from the UI. The row created here is byte-identical to
    the one ``installer/service.py``'s ``ensure_default_recording_target``
    writes -- same id, same name, same ``<upload_dir>/recordings`` path -- so
    this converges on the state the System Health rehearsal produces rather than
    inventing a new one, and it defers to any production target that already
    exists. A directory that cannot be created skips the row rather than
    advertising a location the app has just proved it cannot write, because a
    target row is what makes preflight pass.
    """

    upload_dir = os.environ.get("CIVICCAST_UPLOAD_DIR")
    if not upload_dir:
        return
    resolver = app.dependency_overrides.get(get_recording_target_store)
    recording_target_store = resolver() if callable(resolver) else None
    if recording_target_store is None:
        return
    try:
        targets = list(recording_target_store.list())
    except Exception:
        _LOG.warning("Could not inspect recording targets while seeding managed storage.")
        return
    if any(_is_production_local_recording_target(target) for target in targets):
        return
    try:
        target_dir = (Path(upload_dir) / DEFAULT_RECORDING_TARGET_DIR_NAME).resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOG.warning(
            "Could not create the default recording directory under %s (%s). CivicCast "
            "is starting without a default recording target; configure where recordings "
            "should be saved before going on air.",
            upload_dir,
            exc,
        )
        return
    try:
        recording_target_store.create(
            RecordingTargetCreate(
                recording_target_id=DEFAULT_RECORDING_TARGET_ID,
                name=DEFAULT_RECORDING_TARGET_NAME,
                target_uri=target_dir.as_uri(),
            )
        )
    except RecordingTargetAlreadyExistsError:
        return
    except Exception:
        _LOG.warning(
            "Could not create the default recording target at %s. CivicCast is starting "
            "without one; configure where recordings should be saved before going on air.",
            target_dir,
        )
        return
    _LOG.info(
        "Created the default CivicCast recording target %r at %s. Recordings are saved "
        "there until an operator configures a different target.",
        DEFAULT_RECORDING_TARGET_ID,
        target_dir,
    )


def _is_production_local_recording_target(target: Any) -> bool:
    target_id = getattr(target, "recording_target_id", None)
    target_uri = getattr(target, "target_uri", None)
    if target_id == REHEARSAL_RECORDING_TARGET_ID or not isinstance(target_uri, str):
        return False
    return local_recording_path(target_uri) is not None


def _configure_upload_dir(*, upload_dir: str | None) -> None:
    """Set the managed upload directory unless an operator configured one."""

    if os.environ.get("CIVICCAST_UPLOAD_DIR") or not upload_dir:
        return
    os.environ["CIVICCAST_UPLOAD_DIR"] = upload_dir


def _managed_upload_dir_if_ready() -> str | None:
    try:
        return load_managed_upload_dir()
    except ManagedStorageError:
        return None


def _sync_durable_storage(app: FastAPI) -> None:
    """Let workers observe managed storage prepared by another worker."""

    if getattr(app.state, "durable_storage_active", False):
        return
    if os.environ.get("CIVICCAST_ALLOW_EPHEMERAL_STORES") == "1":
        return
    if os.environ.get("DATABASE_URL"):
        _install_durable_store_wiring(app, os.environ["DATABASE_URL"])
        return
    try:
        managed_url = load_managed_database_url()
    except ManagedStorageError as exc:
        app.state.durable_storage_error = str(exc)
        return
    if managed_url:
        _install_durable_store_wiring(app, managed_url, upload_dir=load_managed_upload_dir())


def _resolve_database_url() -> str | None:
    """Return configured or installer-managed durable storage URL."""

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url
    if os.environ.get("CIVICCAST_ALLOW_EPHEMERAL_STORES") == "1":
        return None
    managed_url = load_managed_database_url()
    if managed_url is None:
        return None
    try:
        storage = ensure_managed_storage()
    except ManagedStorageError:
        raise
    except Exception as exc:
        raise ManagedStorageError(f"Could not prepare CivicCast managed storage: {exc}") from exc
    os.environ["DATABASE_URL"] = storage.database_url
    return storage.database_url


def _create_database_engine(database_url: str) -> Engine:
    """Build the SQLAlchemy engine used by app-owned durable stores."""

    database_url = normalize_database_url(database_url)

    if database_url.startswith("sqlite"):
        engine = create_engine(
            database_url,
            future=True,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 15.0},
        )
        _install_sqlite_pragmas(engine)
        return engine.execution_options(schema_translate_map={"civiccast": None})
    return create_engine(
        database_url, future=True, pool_pre_ping=True, **connect_options(database_url)
    )


def _install_sqlite_pragmas(engine: Engine) -> None:
    """Enable SQLite settings needed for the managed local storage mode."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=15000")
        finally:
            cursor.close()


def _require_durable_or_explicit_ephemeral(database_url: str | None) -> None:
    """Fail closed unless volatile stores are an explicit local/test choice."""

    if database_url:
        return
    if os.environ.get("CIVICCAST_ALLOW_EPHEMERAL_STORES") == "1":
        _LOG.warning(
            "CIVICCAST_ALLOW_EPHEMERAL_STORES=1: starting CivicCast with "
            "volatile in-memory staff stores. This is for tests or throwaway "
            "development only; operator and beta stations use installer-managed "
            "durable storage unless a technical admin sets DATABASE_URL."
        )
        return
    _LOG.warning(
        "Durable storage is not ready. CivicCast is starting in local setup "
        "mode so the installer and operator Setup screen can prepare managed "
        "storage. Staff-write routes remain unavailable until storage is ready."
    )


def _install_lan_only_openapi_route(app: FastAPI) -> None:
    """Serve ``/openapi.json`` on a LAN-only station to staff-token holders only.

    F-06 (walkthrough on a running beta.5 station, 2026-09-09): FastAPI's
    default schema route answered any unauthenticated caller with the full
    list of ``/api/staff/*`` paths. The schema stays available -- it is the
    machine-readable contract an integrator needs and has no external
    dependency -- but the caller now proves the same staff bearer token that
    every path it describes already requires. ``app.openapi()`` itself is
    untouched, so the docs generator keeps working from the app object.
    """

    from civiccast.auth.middleware import authenticate_staff_request

    async def lan_only_openapi(request: Request) -> JSONResponse:
        # MINOR-3 (hostile review of PR #215): the same verify-and-throttle
        # routine the staff middleware runs, so a wrong bearer here spends the
        # ``staff-auth-fail:<ip>`` budget exactly like one on /api/staff/*.
        rejected = authenticate_staff_request(request)
        if rejected is not None:
            return rejected
        return JSONResponse(app.openapi())

    app.add_api_route(
        "/openapi.json",
        lan_only_openapi,
        methods=["GET"],
        include_in_schema=False,
        name="openapi",
    )


def _install_staff_openapi_contract(app: FastAPI) -> None:
    """Expose middleware-enforced staff auth in the machine-readable schema."""

    def custom_openapi() -> dict[str, object]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes[STAFF_BEARER_SCHEME] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "CivicCast staff token",
            "description": (
                "CivicCast staff bearer token issued with `civiccast token issue` "
                "or configured through the legacy environment-token path."
            ),
        }
        for path, path_item in schema.get("paths", {}).items():
            if not str(path).startswith("/api/staff/") or not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method.lower() not in {"delete", "get", "patch", "post", "put"}:
                    continue
                if not isinstance(operation, dict):
                    continue
                operation["security"] = [{STAFF_BEARER_SCHEME: []}]
                responses = operation.setdefault("responses", {})
                responses.setdefault(
                    "401",
                    {
                        "description": (
                            "Missing, invalid, revoked, or misconfigured CivicCast "
                            "staff bearer token."
                        )
                    },
                )
                responses.setdefault(
                    "429",
                    {
                        "description": (
                            "The observed peer exceeded the failed staff authentication "
                            "budget. Wait for Retry-After before another invalid attempt; "
                            "valid staff tokens remain accepted."
                        ),
                        "headers": {
                            "Retry-After": {
                                "description": "Seconds until the failure window reopens.",
                                "schema": {"type": "integer"},
                            }
                        },
                    },
                )
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


app = create_app()
