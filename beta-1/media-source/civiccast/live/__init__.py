# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""civiccast.live - live-broadcast spine.

Sprint 0.4 Slice 1 module. Owns the live session state machine, the
configured-source descriptors (RTMP / RTSP / NDI / SRT), the recording
target descriptors, the pre-flight checklist evaluator, the staff
``/api/staff/live/*`` API surface, and the recording-finalization
path that lands a recorded session as an asset at state ``recorded``.

Slice 1 currently ships the live data spine, the LiveSessionStore
state-machine transitions, the pre-flight checklist evaluator, the
staff router, and the recording-finalization handler (Slice 1
Commit 7). Per the v0.4 scope-lock at
``docs/releases/v0.4-scope-lock.md`` and the design note at
``docs/research/v04-slice1-broadcast-spine-design.md``, the
remaining Slice 1 commits land backend correctness fixes
(QA-005 + QA-007) and test-infra promotions (TEST-004..009).

The Alembic migration directory ``civiccast/live/migrations/versions/``
is one of several per-module version locations the single Alembic
runner walks. The load-bearing registration lives in ``alembic.ini``'s
``version_locations`` list, which Alembic reads to build
``ScriptDirectory`` before ``alembic/env.py`` runs; ``env.py`` also
resolves locations at runtime via ``discover_version_locations`` as a
defense-in-depth helper, but the ini list is what makes the migration
visible to ``alembic upgrade head``. The first commit that adds a new
per-module migrations directory must add that directory's path to
``alembic.ini``.
"""

from civiccast.live.finalization import (
    FinalizationResult,
    LiveRecordingAssetCollisionError,
    LiveRecordingFinalizer,
)
from civiccast.live.finalization_worker import LiveFinalizationWorker
from civiccast.live.models import (
    FINALIZATION_STATE_COMPLETED,
    FINALIZATION_STATE_FAILED,
    FINALIZATION_STATE_PENDING,
    FINALIZATION_STATE_RUNNING,
    LIVE_SESSION_EVENT_ENDED,
    LIVE_SESSION_EVENT_FINALIZED,
    LIVE_SESSION_EVENT_STARTED,
    LIVE_SESSION_STATE_ENDING,
    LIVE_SESSION_STATE_IDLE,
    LIVE_SESSION_STATE_ON_AIR,
    LIVE_SESSION_STATE_PREFLIGHT,
    LIVE_SESSION_STATE_RECORDED,
    SOURCE_TYPE_NDI,
    SOURCE_TYPE_RTMP,
    SOURCE_TYPE_RTSP,
    SOURCE_TYPE_SRT,
    LiveFinalizationJob,
    LiveFinalizationStatusResponse,
    LiveRelayConfig,
    LiveRelayConfigCreate,
    LiveRelayConfigResponse,
    LiveRelayHealthUpdate,
    LiveSession,
    LiveSessionCreate,
    LiveSessionEvent,
    LiveSessionEventResponse,
    LiveSessionResponse,
    LiveSource,
    LiveSourceCreate,
    LiveSourceResponse,
    RecordingTarget,
    RecordingTargetCreate,
    RecordingTargetResponse,
)
from civiccast.live.preflight import (
    PREFLIGHT_CHECK_AI_RUNTIME,
    PREFLIGHT_CHECK_INTERNET_ARCHIVE,
    PREFLIGHT_CHECK_LIVE_SOURCE,
    PREFLIGHT_CHECK_NAS,
    PREFLIGHT_CHECK_NETWORK,
    PREFLIGHT_CHECK_OPERATOR_CONFIRM,
    PREFLIGHT_CHECK_RECORDING_TARGET,
    PREFLIGHT_CHECK_STORAGE,
    PREFLIGHT_CHECK_SYNDICATION,
    PREFLIGHT_STATUS_FAIL,
    PREFLIGHT_STATUS_NOT_CONFIGURED,
    PREFLIGHT_STATUS_PASS,
    PreflightCheckResult,
    PreflightEvaluation,
    PreflightEvaluator,
    PreflightInputs,
)
from civiccast.live.source_probe import (
    DEFAULT_SOURCE_PROBE_TIMEOUT_SECONDS,
    build_source_probe,
    probe_live_source,
)
from civiccast.live.store import (
    LiveRelayConfigAlreadyExistsError,
    LiveRelayConfigNotFoundError,
    LiveRelayConfigStore,
    LiveSessionAlreadyExistsError,
    LiveSessionNotFoundError,
    LiveSessionStateError,
    LiveSessionStore,
    LiveSourceAlreadyExistsError,
    LiveSourceStore,
    RecordingTargetAlreadyExistsError,
    RecordingTargetStore,
)

__all__ = [
    "DEFAULT_SOURCE_PROBE_TIMEOUT_SECONDS",
    "FINALIZATION_STATE_COMPLETED",
    "FINALIZATION_STATE_FAILED",
    "FINALIZATION_STATE_PENDING",
    "FINALIZATION_STATE_RUNNING",
    "LIVE_SESSION_EVENT_ENDED",
    "LIVE_SESSION_EVENT_FINALIZED",
    "LIVE_SESSION_EVENT_STARTED",
    "LIVE_SESSION_STATE_ENDING",
    "LIVE_SESSION_STATE_IDLE",
    "LIVE_SESSION_STATE_ON_AIR",
    "LIVE_SESSION_STATE_PREFLIGHT",
    "LIVE_SESSION_STATE_RECORDED",
    "PREFLIGHT_CHECK_AI_RUNTIME",
    "PREFLIGHT_CHECK_INTERNET_ARCHIVE",
    "PREFLIGHT_CHECK_LIVE_SOURCE",
    "PREFLIGHT_CHECK_NAS",
    "PREFLIGHT_CHECK_NETWORK",
    "PREFLIGHT_CHECK_OPERATOR_CONFIRM",
    "PREFLIGHT_CHECK_RECORDING_TARGET",
    "PREFLIGHT_CHECK_STORAGE",
    "PREFLIGHT_CHECK_SYNDICATION",
    "PREFLIGHT_STATUS_FAIL",
    "PREFLIGHT_STATUS_NOT_CONFIGURED",
    "PREFLIGHT_STATUS_PASS",
    "SOURCE_TYPE_NDI",
    "SOURCE_TYPE_RTMP",
    "SOURCE_TYPE_RTSP",
    "SOURCE_TYPE_SRT",
    "FinalizationResult",
    "LiveFinalizationJob",
    "LiveFinalizationStatusResponse",
    "LiveFinalizationWorker",
    "LiveRecordingAssetCollisionError",
    "LiveRecordingFinalizer",
    "LiveRelayConfig",
    "LiveRelayConfigAlreadyExistsError",
    "LiveRelayConfigCreate",
    "LiveRelayConfigNotFoundError",
    "LiveRelayConfigResponse",
    "LiveRelayConfigStore",
    "LiveRelayHealthUpdate",
    "LiveSession",
    "LiveSessionAlreadyExistsError",
    "LiveSessionCreate",
    "LiveSessionEvent",
    "LiveSessionEventResponse",
    "LiveSessionNotFoundError",
    "LiveSessionResponse",
    "LiveSessionStateError",
    "LiveSessionStore",
    "LiveSource",
    "LiveSourceAlreadyExistsError",
    "LiveSourceCreate",
    "LiveSourceResponse",
    "LiveSourceStore",
    "PreflightCheckResult",
    "PreflightEvaluation",
    "PreflightEvaluator",
    "PreflightInputs",
    "RecordingTarget",
    "RecordingTargetAlreadyExistsError",
    "RecordingTargetCreate",
    "RecordingTargetResponse",
    "RecordingTargetStore",
    "build_source_probe",
    "probe_live_source",
]
