# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The offline caption job -- CivicCast One keystone K3.

Every piece of offline captioning already existed (runtime, review queue,
WebVTT renderer, HLS attach); nothing connected them for a published
recording. This module is that connection, built on the repo's established
background-job shape (durable queue row + ``run_once``/``run_forever``
worker under :class:`~civiccast.platform.worker_runtime.ThreadSupervisor`,
the shape the finalization and ActivityPub-retry workers set).

One job row per asset, two stages, because operator approval sits between
them (spec §4.1 -- no AI text reaches a public surface unreviewed):

``pending``
    Transcribe the recording's audio with the station's staged caption
    model and file every cue in the operator review queue. Publishes
    nothing. -> ``awaiting_review`` (or ``complete`` when the recording
    produced no speech at all).

``awaiting_review``
    Re-checked each poll. Once the operator has decided every queued cue,
    the approved/edited text is rendered and attached to the packaged VOD
    -- segmented WebVTT track in the manifest plus the flat
    ``captions.vtt`` records artifact. -> ``complete``.

``complete`` / ``failed``
    Terminal. ``failed`` means the attempt budget is spent; the reason is
    on the row, and the recording stays uncaptioned rather than shipping
    something unverified.

The stage-one work is the expensive half (a model pass over a whole
meeting), so it is retried with bounded exponential backoff exactly like
the other durable workers. Stage two is cheap and idempotent -- re-running
it just rewrites the same track from the same decisions.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from civiccast.captions.cdn_republish import CaptionPackageCdnRepublisher
from civiccast.captions.models import CaptionCue
from civiccast.captions.retention import CaptionEvidenceRetentionPolicy, CaptionRetentionResult
from civiccast.captions.review import CaptionReviewStore
from civiccast.captions.runtime import CaptionRuntime
from civiccast.captions.settings import RecordedCaptionSettings
from civiccast.captions.vod import (
    OFFLINE_CAPTION_CHUNK_SECONDS,
    OFFLINE_CAPTION_LANGUAGE,
    SPANISH_CAPTION_LANGUAGE,
    ReviewedCaptions,
    attach_reviewed_captions,
    caption_cues_for_package,
    published_caption_sidecar,
    queue_translated_captions,
    recorded_translation_cue_id,
    reviewed_caption_cues,
    transcribe_asset_captions,
)
from civiccast.captions.webvtt import render_webvtt
from civiccast.recorded_languages import RECORDED_TRANSLATION_LANGUAGES
from civiccast.translate.models import TranslationTarget
from civiccast.translate.service import TranslationProvider

_LOG = logging.getLogger(__name__)

OfflineCaptionJobState = Literal["pending", "awaiting_review", "complete", "failed", "cancelled"]

# Annotated with the Literal rather than left as bare str. Untyped, each of
# these infers as `str`, so passing one where OfflineCaptionJobState is
# expected -- as create_offline_caption_job does -- was an arg-type error, and
# a typo in any of these four strings would have type-checked clean.
OFFLINE_CAPTION_JOB_STATE_PENDING: OfflineCaptionJobState = "pending"
OFFLINE_CAPTION_JOB_STATE_AWAITING_REVIEW: OfflineCaptionJobState = "awaiting_review"
OFFLINE_CAPTION_JOB_STATE_COMPLETE: OfflineCaptionJobState = "complete"
OFFLINE_CAPTION_JOB_STATE_FAILED: OfflineCaptionJobState = "failed"
OFFLINE_CAPTION_JOB_STATES = (
    OFFLINE_CAPTION_JOB_STATE_PENDING,
    OFFLINE_CAPTION_JOB_STATE_AWAITING_REVIEW,
    OFFLINE_CAPTION_JOB_STATE_COMPLETE,
    OFFLINE_CAPTION_JOB_STATE_FAILED,
    "cancelled",
)
#: States the worker still has work to do in.
OFFLINE_CAPTION_JOB_ACTIVE_STATES = (
    OFFLINE_CAPTION_JOB_STATE_PENDING,
    OFFLINE_CAPTION_JOB_STATE_AWAITING_REVIEW,
)


OFFLINE_CAPTION_JOB_MODE_INLINE = "inline"
OFFLINE_CAPTION_JOB_MODE_OFF = "off"
_OFFLINE_CAPTION_JOB_MODES = (OFFLINE_CAPTION_JOB_MODE_INLINE, OFFLINE_CAPTION_JOB_MODE_OFF)

#: Legacy environment setting retained as the initial default when no station
#: settings file has been saved. The operator API and JSON file are authoritative
#: once present.
CAPTIONS_SPANISH_ENV_VAR = "CIVICCAST_OFFLINE_CAPTION_SPANISH"

_CAPTION_CDN_REPUBLISH_ERROR_PREFIX = "Caption CDN republish failed: "

#: Put on the job row when the station has no translation runtime wired but
#: Spanish is selected. Names the thing to fix, not the internal symbol.
MISSING_TRANSLATOR_REMEDIATION = (
    "This recording's English captions are approved, but CivicCast has no translation "
    "model available to produce the selected Spanish track. Install or repair the "
    "station's translation model (Settings > "
    "AI Models > Translation) and run 'civiccast doctor'; the job retries on its own."
)

#: Put on the job row when the operator rejected every Spanish cue.
ALL_SPANISH_REJECTED_REMEDIATION = (
    "Every Spanish caption cue for this recording was rejected, so the Spanish track "
    "has not been attached. Open the caption review queue, filter to "
    "Spanish, and edit the cues with the correct wording (or approve the ones that are "
    "right); the selected Spanish track attaches after its cues are reviewed."
)

#: Put on the job row when the operator rejected every English cue.
ALL_ENGLISH_REJECTED_REMEDIATION = (
    "Every English caption cue for this recording was rejected, so it has no caption "
    "track. Spanish uses the reviewed English text as its source, so the job remains "
    "open. Open the caption review queue, filter to English, and edit the cues with the "
    "correct wording (or approve the ones that are right)."
)

#: Put on the job row when the translation pass could not produce a review
#: row for every approved English cue. A short Spanish track must never be
#: published as if it were the whole recording.
INCOMPLETE_TRANSLATION_REMEDIATION = (
    "CivicCast could translate only {queued} of this recording's {expected} approved "
    "English caption cues into Spanish, so the selected Spanish track would be "
    "incomplete and is being held. This is "
    "usually the translation model failing partway through a long meeting. Check the "
    "station's translation model (Settings > AI Models > Translation) and run "
    "'civiccast doctor'; the job retries the missing cues on its own."
)
SPANISH_REVIEW_PENDING_REMEDIATION = (
    "The reviewed English caption track is attached. The selected Spanish track remains "
    "in progress and will attach after its cues are reviewed."
)


class OfflineCaptionTranslationError(RuntimeError):
    """The Spanish pass could not produce a row for every approved English cue.

    Raised rather than returning a short set, so the only way past the
    translation stage is a complete queue -- ``_publish_if_reviewed`` turns
    this into a job failure carrying
    :data:`INCOMPLETE_TRANSLATION_REMEDIATION`, and no truncated track is
    ever attached.
    """


CaptionRuntimeFactory = Callable[[], CaptionRuntime]
#: Lazy builder for the translation adapter -- same shape as the runtime
#: factory, so a station with the worker enabled but nothing to translate
#: never loads the translation model.
TranslationProviderFactory = Callable[[], TranslationProvider]

__all__ = [
    "ALL_ENGLISH_REJECTED_REMEDIATION",
    "ALL_SPANISH_REJECTED_REMEDIATION",
    "CAPTIONS_SPANISH_ENV_VAR",
    "INCOMPLETE_TRANSLATION_REMEDIATION",
    "MISSING_TRANSLATOR_REMEDIATION",
    "OFFLINE_CAPTION_JOB_ACTIVE_STATES",
    "OFFLINE_CAPTION_JOB_MODE_INLINE",
    "OFFLINE_CAPTION_JOB_MODE_OFF",
    "OFFLINE_CAPTION_JOB_STATES",
    "OFFLINE_CAPTION_JOB_STATE_AWAITING_REVIEW",
    "OFFLINE_CAPTION_JOB_STATE_COMPLETE",
    "OFFLINE_CAPTION_JOB_STATE_FAILED",
    "OFFLINE_CAPTION_JOB_STATE_PENDING",
    "SPANISH_REVIEW_PENDING_REMEDIATION",
    "InMemoryOfflineCaptionJobStore",
    "OfflineCaptionJobConflictError",
    "OfflineCaptionJobRecord",
    "OfflineCaptionJobSettings",
    "OfflineCaptionJobState",
    "OfflineCaptionJobStore",
    "OfflineCaptionJobWorker",
    "OfflineCaptionTranslationError",
    "enqueue_offline_caption_job",
    "new_offline_caption_job_id",
]


class OfflineCaptionJobConflictError(Exception):
    """Raised when a concurrent enqueue lost the race for an asset's active job.

    Two concurrent ``enqueue_offline_caption_job`` calls for the same asset
    (a publish approval racing a retry, for instance) can both pass the
    ``active_for_asset`` pre-check before either has inserted. The DB-level
    partial-unique index (``ix_offline_caption_jobs_one_active_per_asset``
    in ``0075_offline_caption_jobs``) is the real guard; a store raises this
    from ``enqueue`` when it loses that race, so the caller can recover the
    winning job instead of surfacing a raw integrity error.
    """

    def __init__(self, asset_id: str) -> None:
        self.asset_id = asset_id
        super().__init__(f"Offline caption job already active for asset {asset_id!r}")


def new_offline_caption_job_id() -> str:
    """Return a fresh opaque job id (same shape as the other queue ids)."""

    return "ocj_" + secrets.token_urlsafe(16).replace("-", "").replace("_", "")


class OfflineCaptionJobRecord(BaseModel):
    """One asset's offline captioning job."""

    model_config = ConfigDict(extra="forbid")

    job_id: Annotated[str, Field(min_length=1, max_length=120)]
    asset_id: Annotated[str, Field(min_length=1, max_length=160)]
    #: The recording ffmpeg reads caption audio from.
    source_path: Annotated[str, Field(min_length=1, max_length=1000)]
    #: The packaged HLS directory the reviewed track is attached to. Whoever
    #: enqueues the job resolves this (today, always
    #: civiccast.publish.router._queue_offline_captions via
    #: resolve_vod_package_dir, the UPLOAD-only convention -- see the
    #: KNOWN FOLLOW-UP comment there and "Known follow-ups" in
    #: docs/ops/background-workers.md for the LIVE-finalized-recording gap
    #: this leaves, out of CivicCast One v1 scope).
    package_dir: Annotated[str, Field(min_length=1, max_length=1000)]
    state: OfflineCaptionJobState
    attempts: int = 0
    next_attempt_at: datetime | None = None
    #: Cues the model produced (stage one).
    cue_count: int = 0
    #: Cues an operator approved/edited and that were published (stage two).
    published_cue_count: int = 0
    last_error: str = ""
    created_at: datetime
    updated_at: datetime


class _TranslationCaptionArguments(TypedDict, total=False):
    translated_cues: dict[str, list[CaptionCue]]


class OfflineCaptionJobStore(Protocol):
    """Storage contract for the offline caption job queue."""

    def enqueue(self, record: OfflineCaptionJobRecord) -> OfflineCaptionJobRecord:
        """Persist a new job row.

        Raises :class:`OfflineCaptionJobConflictError` when ``record`` is in
        an active state and another active job already exists for its
        asset -- the DB-level partial-unique index losing a concurrent race.
        """

    def save(
        self, record: OfflineCaptionJobRecord, *, preserve_cancelled: bool = False
    ) -> OfflineCaptionJobRecord:
        """Persist an updated job row."""

    def get(self, job_id: str) -> OfflineCaptionJobRecord | None:
        """Return one job row when present."""

    def active_for_asset(self, asset_id: str) -> OfflineCaptionJobRecord | None:
        """Return the asset's non-terminal job, when one exists."""

    def due(
        self,
        *,
        now: datetime,
        states: Sequence[str] = OFFLINE_CAPTION_JOB_ACTIVE_STATES,
    ) -> list[OfflineCaptionJobRecord]:
        """Return job rows in ``states`` whose next attempt is due."""

    def list(
        self,
        *,
        asset_id: str | None = None,
        state: str | None = None,
    ) -> list[OfflineCaptionJobRecord]:
        """Return job rows for operator visibility, optionally filtered.

        Unlike :meth:`due`, this is not restricted to active states or to
        rows whose backoff clock has elapsed -- it is the read path for the
        staff "what's stuck and why" view (finding 4), so a ``failed`` or
        ``complete`` row must come back too.
        """


class InMemoryOfflineCaptionJobStore:
    """In-memory job queue for tests and non-DB development."""

    def __init__(self) -> None:
        self._jobs: dict[str, OfflineCaptionJobRecord] = {}

    def enqueue(self, record: OfflineCaptionJobRecord) -> OfflineCaptionJobRecord:
        if record.state in OFFLINE_CAPTION_JOB_ACTIVE_STATES:
            conflict = next(
                (
                    row
                    for row in self._jobs.values()
                    if row.asset_id == record.asset_id
                    and row.state in OFFLINE_CAPTION_JOB_ACTIVE_STATES
                ),
                None,
            )
            if conflict is not None:
                # Mirrors the durable store's partial-unique index: at most
                # one active job per asset, enforced here too so the
                # in-memory store honors the same contract in tests.
                raise OfflineCaptionJobConflictError(record.asset_id)
        self._jobs[record.job_id] = record
        return record.model_copy(deep=True)

    def save(
        self, record: OfflineCaptionJobRecord, *, preserve_cancelled: bool = False
    ) -> OfflineCaptionJobRecord:
        if (
            preserve_cancelled
            and (current := self._jobs.get(record.job_id)) is not None
            and current.state == "cancelled"
        ):
            return current.model_copy(deep=True)
        self._jobs[record.job_id] = record
        return record.model_copy(deep=True)

    def get(self, job_id: str) -> OfflineCaptionJobRecord | None:
        record = self._jobs.get(job_id)
        return record.model_copy(deep=True) if record is not None else None

    def active_for_asset(self, asset_id: str) -> OfflineCaptionJobRecord | None:
        for record in sorted(self._jobs.values(), key=lambda row: row.created_at):
            if record.asset_id == asset_id and record.state in OFFLINE_CAPTION_JOB_ACTIVE_STATES:
                return record.model_copy(deep=True)
        return None

    def due(
        self,
        *,
        now: datetime,
        states: Sequence[str] = OFFLINE_CAPTION_JOB_ACTIVE_STATES,
    ) -> list[OfflineCaptionJobRecord]:
        rows = [
            record
            for record in self._jobs.values()
            if record.state in states
            and (record.next_attempt_at is None or record.next_attempt_at <= now)
        ]
        return [
            record.model_copy(deep=True)
            for record in sorted(rows, key=lambda row: (row.created_at, row.job_id))
        ]

    def list(
        self,
        *,
        asset_id: str | None = None,
        state: str | None = None,
    ) -> list[OfflineCaptionJobRecord]:
        rows = list(self._jobs.values())
        if asset_id is not None:
            rows = [row for row in rows if row.asset_id == asset_id]
        if state is not None:
            rows = [row for row in rows if row.state == state]
        return [
            row.model_copy(deep=True)
            for row in sorted(rows, key=lambda row: (row.created_at, row.job_id))
        ]


class OfflineCaptionJobSettings(RecordedCaptionSettings):
    """Station policy plus worker timing for the offline caption job."""

    poll_seconds: float = Field(default=60.0, gt=0)
    backoff_seconds: float = Field(default=300.0, gt=0)
    max_attempts: int = Field(default=4, gt=0)
    chunk_seconds: float = Field(default=OFFLINE_CAPTION_CHUNK_SECONDS, gt=0)

    @property
    def mode(self) -> str:
        """Compatibility view for existing startup diagnostics."""

        return OFFLINE_CAPTION_JOB_MODE_INLINE if self.enabled else OFFLINE_CAPTION_JOB_MODE_OFF

    @property
    def spanish_enabled(self) -> bool:
        """Whether Spanish is selected for this station."""

        return "es" in self.languages

    @property
    def spanish_target_language(self) -> str:
        return SPANISH_CAPTION_LANGUAGE

    @classmethod
    def from_env(cls) -> OfflineCaptionJobSettings:
        mode = (
            os.environ.get("CIVICCAST_OFFLINE_CAPTION_JOB", OFFLINE_CAPTION_JOB_MODE_INLINE)
            .strip()
            .lower()
        )
        if mode not in _OFFLINE_CAPTION_JOB_MODES:
            raise ValueError(
                f"CIVICCAST_OFFLINE_CAPTION_JOB must be one of "
                f"{', '.join(_OFFLINE_CAPTION_JOB_MODES)}; got {mode!r}."
            )
        defaults = cls()
        station_defaults = RecordedCaptionSettings.from_env()
        return cls(
            enabled=(mode == OFFLINE_CAPTION_JOB_MODE_INLINE) and station_defaults.enabled,
            languages=station_defaults.languages,
            completion_requirement=station_defaults.completion_requirement,
            poll_seconds=_env_positive_float(
                "CIVICCAST_OFFLINE_CAPTION_POLL_SECONDS", defaults.poll_seconds
            ),
            backoff_seconds=_env_positive_float(
                "CIVICCAST_OFFLINE_CAPTION_BACKOFF_SECONDS", defaults.backoff_seconds
            ),
            max_attempts=_env_positive_int(
                "CIVICCAST_OFFLINE_CAPTION_MAX_ATTEMPTS", defaults.max_attempts
            ),
            chunk_seconds=_env_positive_float(
                "CIVICCAST_OFFLINE_CAPTION_CHUNK_SECONDS", defaults.chunk_seconds
            ),
        )


def _env_positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number; got {raw!r}.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero; got {raw!r}.")
    return value


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero; got {raw!r}.")
    return value


def enqueue_offline_caption_job(
    store: OfflineCaptionJobStore,
    *,
    asset_id: str,
    source_path: Path,
    package_dir: Path,
    now: datetime | None = None,
) -> OfflineCaptionJobRecord:
    """Queue offline captioning for a published recording.

    Idempotent per asset: publishing (or re-publishing) an asset that is
    already queued or already awaiting review returns the existing job
    instead of re-transcribing the same meeting and re-queueing cues an
    operator may already be working through.

    The idempotency check is check-then-insert (a plain SELECT, no
    row lock), so two concurrent calls for the same asset -- a publish
    approval racing a retry, or two operators approving at once -- can both
    pass it before either has inserted. The DB-level partial-unique index
    (``ix_offline_caption_jobs_one_active_per_asset``) is the real guard:
    when this loses that race, the losing insert raises
    :class:`OfflineCaptionJobConflictError`, and that's caught here to
    return the winner instead of a raw DB error or a duplicate job.
    """

    existing = store.active_for_asset(asset_id)
    if existing is not None:
        return existing
    resolved_now = now or datetime.now(UTC)
    record = OfflineCaptionJobRecord(
        job_id=new_offline_caption_job_id(),
        asset_id=asset_id,
        source_path=str(source_path),
        package_dir=str(package_dir),
        state=OFFLINE_CAPTION_JOB_STATE_PENDING,
        attempts=0,
        next_attempt_at=resolved_now,
        created_at=resolved_now,
        updated_at=resolved_now,
    )
    try:
        enqueued = store.enqueue(record)
    except OfflineCaptionJobConflictError:
        # Lost the DB-level race against a concurrent enqueue for this asset
        # (the check above raced past another caller's insert). Re-fetch
        # and return the winner instead of queueing a second full
        # transcription pass -- or a raw integrity error -- for one asset.
        winner = store.active_for_asset(asset_id)
        if winner is None:
            # The winner must have already finished (or itself failed)
            # between the conflict and this re-fetch; nothing active is
            # left to return, so surface the conflict as-is.
            raise
        _LOG.info(
            "Offline caption enqueue for asset %s lost a concurrent race; reusing job %s.",
            asset_id,
            winner.job_id,
        )
        return winner
    _LOG.info("Queued offline captioning for published asset %s.", asset_id)
    return enqueued


class OfflineCaptionJobWorker:
    """Drive queued assets from published-but-uncaptioned to captioned.

    ``run_once`` is the testable unit; ``run_forever`` is the supervised
    loop that survives and logs scan exceptions -- the same split every
    other CivicCast background worker uses.

    The caption runtime is built lazily, through ``runtime_factory``, so a
    station with the worker enabled but nothing queued never loads a
    multi-gigabyte model into memory. The factory is the app's existing
    :func:`civiccast.ai_models.runtime.build_caption_runtime` seam, which
    resolves the operator-selected tier and inherits the hardware-adaptive
    device/compute-type the native station runtime published into the
    environment -- this worker never decides ``cpu`` versus ``cuda`` itself.

    Every tick also runs the retained-audio-evidence retention sweep
    (audit finding, MAJOR). ``CaptionEvidenceRetentionPolicy``
    (civiccast/captions/retention.py) is the owner-approved 90-day/
    free-space lifecycle for review-evidence WAVs, but its only caller used
    to be the live channel readiness tick in
    :meth:`civiccast.captions.tap_worker.CaptionTapWorker.run_once`. A
    station doing offline/VOD captioning with no airing live channel never
    ran that tick, so its evidence WAVs (written under each asset's package
    directory -- see ``_offline_audio_evidence_factory`` in
    :mod:`civiccast.captions.vod`) grew unbounded. ``run_once`` here now
    triggers the same policy class -- ``CaptionEvidenceRetentionPolicy``,
    same ``enforce_discovered`` call shape as ``CaptionTapWorker`` uses,
    just with ``tap_root`` absent, since this path has no live tap
    directory to also sweep. The *storage root* is not shared with
    ``CaptionTapWorker`` (audit finding, P1): that worker measures free
    space and the storage cap against the live egress work directory
    (``default_egress_work_dir()``), because that is where its tap evidence
    lives. This worker's evidence lives under the VOD package tree instead
    (``<package_dir>/captions/evidence/*.wav``), which can be a different
    filesystem entirely, so ``_retention_policy_instance`` builds its
    default policy against the VOD package root
    (:func:`civiccast.schedule.paths.resolve_vod_package_root`) instead --
    see that method for the resolution. Two more consequences of routing
    through the *same* policy class rather than a forked one: (1) the
    result is no longer discarded (audit finding, P1) -- ``run_once`` reads
    ``CaptionRetentionResult.ready`` and skips processing every due job
    this tick when the sweep refuses storage, exactly like
    ``CaptionTapWorker.run_once`` skips its channel scan; and (2) a sweep
    *exception* (as opposed to a clean not-ready result) still never blocks
    job processing -- see ``_sweep_retention``. Unlike ``CaptionTapWorker``
    (whose ``caption_work_dir`` is a required constructor argument, so its
    default policy is already hermetic per caller), this worker is built
    unconditionally in app.py regardless of
    ``OfflineCaptionJobSettings.mode``; the default policy is therefore
    built lazily on first use (see ``_retention_policy_instance``) rather
    than in ``__init__``, so constructing the worker itself never touches
    the filesystem.
    """

    def __init__(
        self,
        store: OfflineCaptionJobStore,
        review_store: CaptionReviewStore,
        *,
        runtime_factory: CaptionRuntimeFactory,
        settings: OfflineCaptionJobSettings,
        settings_provider: Callable[[], RecordedCaptionSettings] | None = None,
        translation_provider_factory: TranslationProviderFactory | None = None,
        cdn_republisher: CaptionPackageCdnRepublisher | None = None,
        retention_policy: CaptionEvidenceRetentionPolicy | None = None,
    ) -> None:
        self._store = store
        self._review_store = review_store
        self._runtime_factory = runtime_factory
        self._settings = settings
        self._settings_provider = settings_provider
        # Built lazily on first Spanish translation, same reason as
        # ``_runtime`` -- a station with nothing to translate never loads
        # the translation model. ``None`` means "no translation runtime
        # configured", which records MISSING_TRANSLATOR_REMEDIATION when a
        # selected Spanish track cannot be generated. In english_reviewed
        # mode the reviewed English track can already be attached. app.py supplies the
        # factory in production.
        self._translation_provider_factory = translation_provider_factory
        self._translation_provider: TranslationProvider | None = None
        # Optional: re-publishes the rewritten manifest and the new caption
        # files to the CDN this asset's package is actually served from
        # (see civiccast.captions.cdn_republish). ``None`` means no CDN is
        # configured, so the local rewrite IS the published state.
        self._cdn_republisher = cdn_republisher
        self._runtime: CaptionRuntime | None = None
        # Built lazily, same reason as ``_runtime_instance`` below: this
        # worker is constructed unconditionally in app.py regardless of
        # ``OfflineCaptionJobSettings.mode``, so touching the real
        # filesystem (``mkdir`` + ``shutil.disk_usage`` inside
        # ``CaptionEvidenceRetentionPolicy.from_system``) here in
        # ``__init__`` would run on every app startup -- including every
        # test that builds the app -- rather than only when a tick
        # actually runs.
        self._retention_policy = retention_policy

    def run_forever(
        self,
        *,
        poll_seconds: float = 60.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Run the caption-job loop until ``stop_event`` is set."""

        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.exception("Offline caption scan failed; retrying on the next poll interval.")
            if stop_event is not None:
                stop_event.wait(poll_seconds)
            else:  # pragma: no cover - only reachable outside a supervisor
                time.sleep(poll_seconds)

    def run_once(self, *, now: datetime | None = None) -> list[OfflineCaptionJobRecord]:
        """Advance every due job once; return the rows that were touched.

        The retention sweep (see ``_sweep_retention``) always runs first.
        When it returns a clean *not-ready* result -- the free-space
        reserve would be breached, or retained evidence is over the
        storage cap, even after pruning everything eligible -- no due job
        is processed this tick, mirroring
        :meth:`civiccast.captions.tap_worker.CaptionTapWorker.run_once`'s
        own predicate (``if not retention.ready: ...`` before any channel
        work) exactly: this worker must not keep writing new evidence WAVs
        (stage one, ``_transcribe``) or new package artifacts (stage two,
        ``_publish_if_reviewed``) onto a volume the policy just refused
        (audit finding, P1 -- the result used to be discarded and every due
        job ran regardless). A sweep *exception* is different from a clean
        not-ready result and is never treated as a refusal here -- see
        ``_sweep_retention``.
        """

        if self._settings_provider is not None:
            station_settings = self._settings_provider()
            self._settings = OfflineCaptionJobSettings.model_validate(
                {
                    **self._settings.model_dump(),
                    **station_settings.model_dump(),
                }
            )
        if not self._settings.enabled:
            return []

        retention = self._sweep_retention()
        if retention is not None and not retention.ready:
            _LOG.warning(
                "Offline caption evidence retention refused storage (%s); skipping "
                "job processing this tick.",
                retention.refusal_reason,
            )
            return []
        resolved_now = now or datetime.now(UTC)
        processed: list[OfflineCaptionJobRecord] = []
        for row in self._store.due(now=resolved_now):
            if self._cancelled(row.job_id):
                continue
            if row.state == OFFLINE_CAPTION_JOB_STATE_PENDING:
                processed.append(self._transcribe(row, now=resolved_now))
            elif row.state == OFFLINE_CAPTION_JOB_STATE_AWAITING_REVIEW:
                processed.append(self._publish_if_reviewed(row, now=resolved_now))
        return processed

    def _save(self, row: OfflineCaptionJobRecord) -> OfflineCaptionJobRecord:
        return self._store.save(row, preserve_cancelled=True)

    def _cancelled(self, job_id: str) -> bool:
        current = self._store.get(job_id)
        return current is None or current.state == "cancelled"

    def _sweep_retention(self) -> CaptionRetentionResult | None:
        """Prune expired retained caption evidence once per tick.

        Runs every ``run_once`` regardless of whether any job is due, same
        cadence as :meth:`civiccast.captions.tap_worker.CaptionTapWorker
        .run_once`'s own unconditional call. ``tap_root=None`` because this
        worker has no live tap directory -- ``enforce_discovered`` already
        treats that as "skip the raw-chunk sweep, still prune resolved
        review-evidence WAVs" (see ``_discover_candidates`` in retention.py).

        Returns the sweep's :class:`~civiccast.captions.retention
        .CaptionRetentionResult` so ``run_once`` can honor a not-ready
        refusal the same way the live tap does, or ``None`` when the sweep
        raised. A retention *failure* -- including building the default
        policy the first time -- must never fail the caption job it happens
        to run alongside, so it is logged and treated as "no opinion this
        tick" (``run_once`` proceeds with job processing), not surfaced as
        a refusal.
        """

        try:
            return self._retention_policy_instance().enforce_discovered(
                tap_root=None,
                review_store=self._review_store,
                # Only consulted for raw-chunk (live tap) candidates, which
                # cannot exist when tap_root is None -- kept for structural
                # parity with the tap-worker call this mirrors.
                segment_seconds=self._settings.chunk_seconds,
            )
        except Exception:
            _LOG.exception(
                "Offline caption evidence retention sweep failed; continuing without "
                "pruning this tick."
            )
            return None

    def _retention_policy_instance(self) -> CaptionEvidenceRetentionPolicy:
        if self._retention_policy is None:
            # Offline evidence WAVs land under the VOD package tree, not
            # the live egress work directory CaptionTapWorker measures
            # (audit finding, P1) -- see the class docstring. Resolved the
            # same way every queued job's own ``package_dir`` was
            # (civiccast.publish.router._queue_offline_captions, via
            # resolve_vod_package_dir): CIVICCAST_UPLOAD_DIR is the
            # required base, optionally overridden by
            # CIVICCAST_VOD_PACKAGE_DIR. Lazy import mirrors
            # civiccast.captions.tap_worker.build_tap_worker's own default
            # resolution -- neither seam is needed eagerly at module load
            # time.
            from civiccast.schedule.paths import resolve_upload_root, resolve_vod_package_root

            upload_root = resolve_upload_root()
            if upload_root is None:
                # A job can only ever have been enqueued with a real
                # package_dir when CIVICCAST_UPLOAD_DIR was set (see
                # _queue_offline_captions, which skips enqueueing
                # otherwise) -- so in real deployments this only fires when
                # nothing is queued yet. Raising here (rather than
                # guessing a root) is caught by _sweep_retention's
                # try/except and logged, same as any other sweep failure.
                raise ValueError(
                    "CIVICCAST_UPLOAD_DIR must be set to resolve the offline "
                    "caption evidence volume for the retention sweep."
                )
            self._retention_policy = CaptionEvidenceRetentionPolicy.from_system(
                storage_root=resolve_vod_package_root(upload_root)
            )
        return self._retention_policy

    # -- stage one ---------------------------------------------------------

    def _transcribe(
        self,
        row: OfflineCaptionJobRecord,
        *,
        now: datetime,
    ) -> OfflineCaptionJobRecord:
        try:
            transcription = transcribe_asset_captions(
                self._runtime_instance(),
                self._review_store,
                asset_id=row.asset_id,
                source_path=Path(row.source_path),
                package_dir=Path(row.package_dir),
                chunk_seconds=self._settings.chunk_seconds,
                vocabulary=self._settings.vocabulary,
                cancelled=lambda: self._cancelled(row.job_id),
            )
        except Exception as exc:
            if self._cancelled(row.job_id):
                return self._store.get(row.job_id) or row
            return self._record_failure(row, now=now, error=str(exc), stage="transcription")

        cue_count = len(transcription.cues)
        if cue_count == 0:
            _LOG.info(
                "Offline captioning found no speech in asset %s; nothing to review.",
                row.asset_id,
            )
            return self._save(
                row.model_copy(
                    update={
                        "state": OFFLINE_CAPTION_JOB_STATE_COMPLETE,
                        "attempts": row.attempts + 1,
                        "next_attempt_at": None,
                        "cue_count": 0,
                        "last_error": "",
                        "updated_at": now,
                    }
                )
            )
        _LOG.info(
            "Offline captioning queued %d cue(s) for review on asset %s.",
            cue_count,
            row.asset_id,
        )
        return self._save(
            row.model_copy(
                update={
                    "state": OFFLINE_CAPTION_JOB_STATE_AWAITING_REVIEW,
                    # Each stage gets its own attempt budget: a transcription
                    # that needed two tries must not leave the attach stage
                    # with fewer retries than a first-try transcription did.
                    "attempts": 0,
                    # Re-checked on the normal poll cadence; the operator
                    # decides when, not the worker.
                    "next_attempt_at": now,
                    "cue_count": cue_count,
                    "last_error": "",
                    "updated_at": now,
                }
            )
        )

    # -- stage two ---------------------------------------------------------

    def _publish_if_reviewed(
        self,
        row: OfflineCaptionJobRecord,
        *,
        now: datetime,
    ) -> OfflineCaptionJobRecord:
        """Two-phase publish gate (spec §4.2, operator review before publish).

        English is always the transcription/source language. Selected
        Spanish cues are translated from reviewed English and receive their
        own review pass. Under ``all_selected_languages`` neither track is
        attached until both passes are ready. Under ``english_reviewed`` the
        reviewed English track can attach first while the active job keeps
        polling for Spanish; no unreviewed cue is published in either mode.
        """

        english = reviewed_caption_cues(
            self._review_store, row.asset_id, language=OFFLINE_CAPTION_LANGUAGE
        )
        if english.pending:
            # Still the operator's move on the English pass. Not an attempt,
            # not a failure -- just poll again without burning the budget.
            return self._poll_again(row, now=now)
        if not english.cues:
            # Every English cue was rejected. Completing here would mark a
            # recording "captioned" while it carries no caption track.
            # Keep the job actionable instead.
            return self._hold_for_review(
                row,
                now=now,
                reason=ALL_ENGLISH_REJECTED_REMEDIATION,
                log_message=(
                    "Every English caption cue for asset %s was rejected; holding the "
                    "recording rather than marking it captioned with no track at all."
                ),
            )

        # English is approved. Required mode keeps the historical atomic
        # two-track attach; english_reviewed may publish English while the
        # selected Spanish pass continues in this active job.
        translations: dict[str, list[CaptionCue]] = {}
        for target_language in self._settings.languages:
            if target_language == "en":
                continue
            label = RECORDED_TRANSLATION_LANGUAGES[target_language]
            if self._translation_provider_factory is None:
                if self._settings.completion_requirement == "english_reviewed":
                    return self._publish_english_while_spanish_waits(
                        row,
                        english.cues,
                        now=now,
                        reason=MISSING_TRANSLATOR_REMEDIATION.replace("Spanish", label),
                    )
                return self._record_failure(
                    row,
                    now=now,
                    error=MISSING_TRANSLATOR_REMEDIATION.replace("Spanish", label),
                    stage="caption translation",
                )
            try:
                spanish = self._resolve_spanish_review(
                    row.asset_id, english.cues, target_language=target_language
                )
            except Exception as exc:
                if self._settings.completion_requirement == "english_reviewed":
                    return self._publish_english_while_spanish_waits(
                        row,
                        english.cues,
                        now=now,
                        reason=f"{label} translation is waiting to retry: {exc}",
                    )
                return self._record_failure(
                    row, now=now, error=str(exc), stage="caption translation"
                )
            if spanish is None:
                if self._settings.completion_requirement == "english_reviewed":
                    return self._publish_english_while_spanish_waits(
                        row,
                        english.cues,
                        now=now,
                        reason=SPANISH_REVIEW_PENDING_REMEDIATION.replace("Spanish", label),
                    )
                return self._poll_again(row, now=now)
            if not spanish.cues:
                if self._settings.completion_requirement == "english_reviewed":
                    return self._publish_english_while_spanish_waits(
                        row,
                        english.cues,
                        now=now,
                        reason=ALL_SPANISH_REJECTED_REMEDIATION.replace("Spanish", label),
                    )
                return self._hold_for_review(
                    row,
                    now=now,
                    reason=ALL_SPANISH_REJECTED_REMEDIATION.replace("Spanish", label),
                    log_message=(
                        "Every Spanish caption cue for asset %s was rejected; holding the "
                        "recording rather than publishing it in English only."
                    ),
                )
            translations[target_language] = spanish.cues

        if self._cancelled(row.job_id):
            return self._store.get(row.job_id) or row
        try:
            translation_arguments: _TranslationCaptionArguments = {}
            if any(code != "es" for code in translations):
                translation_arguments["translated_cues"] = {
                    code: cues for code, cues in translations.items() if code != "es"
                }
            attached = attach_reviewed_captions(
                Path(row.package_dir),
                english.cues,
                spanish_cues=translations.get("es"),
                **translation_arguments,
            )
        except Exception as exc:
            return self._record_failure(row, now=now, error=str(exc), stage="caption attach")
        if self._cancelled(row.job_id):
            return self._store.get(row.job_id) or row
        if self._cdn_republisher is not None:
            # attach_reviewed_captions rewrote the LOCAL manifest and wrote
            # the LOCAL caption files. If this package is served to residents
            # through a CDN, that copy is still the pre-caption one, and
            # calling the job complete would claim a captioned recording the
            # public cannot get. Re-publish before completing; a failure
            # fails the job (with the provider's message on the row) instead.
            # Safe to retry: attach is idempotent, so a re-run rewrites the
            # same files from the same decisions and re-uploads them.
            try:
                self._cdn_republisher.republish(
                    asset_id=row.asset_id,
                    package_dir=Path(row.package_dir),
                    attached=attached,
                )
            except Exception as exc:
                return self._record_failure(
                    row, now=now, error=str(exc), stage="caption CDN republish"
                )
        _LOG.info(
            "Published %d English + %d Spanish reviewed caption cue(s) onto asset %s (%s).",
            attached.cue_count,
            attached.spanish_cue_count,
            row.asset_id,
            attached.sidecar_path,
        )
        return self._save(
            row.model_copy(
                update={
                    "state": OFFLINE_CAPTION_JOB_STATE_COMPLETE,
                    "next_attempt_at": None,
                    "published_cue_count": attached.cue_count,
                    "last_error": "",
                    "updated_at": now,
                }
            )
        )

    def _publish_english_while_spanish_waits(
        self,
        row: OfflineCaptionJobRecord,
        english_cues: list[CaptionCue],
        *,
        now: datetime,
        reason: str,
    ) -> OfflineCaptionJobRecord:
        """Attach reviewed English once, then keep the job active for Spanish."""

        sidecar = published_caption_sidecar(Path(row.package_dir))
        try:
            playback_cues = caption_cues_for_package(Path(row.package_dir), english_cues)
        except Exception as exc:
            return self._record_failure(row, now=now, error=str(exc), stage="caption attach")
        english_vtt = render_webvtt(playback_cues)
        try:
            english_is_current = (
                row.published_cue_count == len(playback_cues)
                and sidecar.is_file()
                and sidecar.read_text(encoding="utf-8") == english_vtt
            )
        except OSError:
            english_is_current = False

        # The local sidecar can already match after an attach succeeded but
        # its CDN upload failed. Preserve that retry obligation in last_error
        # so the next poll does not mistake local equality for public delivery.
        cdn_republish_retry_pending = (
            self._cdn_republisher is not None
            and row.last_error.startswith(_CAPTION_CDN_REPUBLISH_ERROR_PREFIX)
        )
        published_cue_count = row.published_cue_count
        if not english_is_current or cdn_republish_retry_pending:
            if self._cancelled(row.job_id):
                return self._store.get(row.job_id) or row
            try:
                attached = attach_reviewed_captions(Path(row.package_dir), english_cues)
            except Exception as exc:
                return self._record_failure(row, now=now, error=str(exc), stage="caption attach")
            if self._cancelled(row.job_id):
                return self._store.get(row.job_id) or row
            if self._cdn_republisher is not None:
                try:
                    self._cdn_republisher.republish(
                        asset_id=row.asset_id,
                        package_dir=Path(row.package_dir),
                        attached=attached,
                    )
                except Exception as exc:
                    return self._record_failure(
                        row,
                        now=now,
                        error=f"{_CAPTION_CDN_REPUBLISH_ERROR_PREFIX}{exc}",
                        stage="caption CDN republish",
                    )
            published_cue_count = attached.cue_count

        if row.last_error == reason and row.published_cue_count == published_cue_count:
            return row
        return self._save(
            row.model_copy(
                update={
                    "state": OFFLINE_CAPTION_JOB_STATE_AWAITING_REVIEW,
                    "next_attempt_at": now,
                    "published_cue_count": published_cue_count,
                    "last_error": reason,
                    "updated_at": now,
                }
            )
        )

    def _poll_again(
        self, row: OfflineCaptionJobRecord, *, now: datetime
    ) -> OfflineCaptionJobRecord:
        """Re-check on the next poll without consuming the retry budget."""

        return self._save(row.model_copy(update={"next_attempt_at": now, "updated_at": now}))

    def _hold_for_review(
        self,
        row: OfflineCaptionJobRecord,
        *,
        now: datetime,
        reason: str,
        log_message: str,
    ) -> OfflineCaptionJobRecord:
        """Hold a job open on an operator decision, with the next move on the row.

        Stays in ``awaiting_review`` and does not consume the retry budget --
        the block is a decision waiting on a human, not a transient fault --
        but unlike :meth:`_poll_again` it puts a reason on the row, because
        an otherwise-silent job that never completes is the thing an operator
        cannot act on.

        Writes and logs **only on a state change.** This runs on every poll
        (60s by default) for as long as the operator takes to act, so the
        earlier version rewrote an identical row and emitted an identical
        WARNING every minute -- filling the log with one recurring condition
        and burning the signal that a warning is supposed to carry, while
        moving ``updated_at`` so the staff view could not tell when the job
        actually last changed. When the reason already on the row is the one
        we are about to write, this returns the row untouched.
        """

        if row.last_error == reason:
            return row
        _LOG.warning(log_message, row.asset_id)
        return self._save(
            row.model_copy(
                update={
                    "next_attempt_at": now,
                    "last_error": reason,
                    "updated_at": now,
                }
            )
        )

    def _resolve_spanish_review(
        self,
        asset_id: str,
        english_cues: list[CaptionCue],
        *,
        target_language: str = "es",
    ) -> ReviewedCaptions | None:
        """Ensure EVERY approved English cue has a Spanish row, then gate on review.

        Returns the reviewed Spanish cues once every expected Spanish row has
        an operator decision (an empty ``cues`` list means all were rejected),
        or ``None`` while any is still pending.

        The gate is **completeness of the queue, not emptiness of it.** The
        earlier version asked ``spanish.total == 0`` and translated only when
        the asset had no Spanish rows at all. A translation pass that failed
        partway -- ``queue_translated_captions`` writes one review row per cue,
        so a store error on cue 4 of 6 leaves 3 rows behind -- came back to a
        non-zero total on the retry, never queued the rest, and then, once an
        operator decided those 3, reported ``pending == 0`` and published a
            3-of-6 Spanish track as ``complete``. Silent truncation of a
            selected caption track, with nothing on the row to show it.

        So the expected id set is derived up front from the approved English
        cues (:func:`~civiccast.translate.service.translated_cue_id`, the same
        function that mints the ids), compared against what is actually
        stored, and anything missing is translated and queued -- passing only
        the missing English cues, so a re-poll never re-invokes the model for
        rows that are already there, and ``queue_translated_captions``'
        id-derived idempotency is the second line of defense. If rows are
        still missing after that attempt, this raises rather than returning a
        short set: the caller records it as a job failure with
        :data:`INCOMPLETE_TRANSLATION_REMEDIATION`, and no partial track is
        ever attached.

        The expected id binds the **source wording**, not just the source cue
        (``source_text=`` on ``translated_cue_id``). A cue id alone says which
        cue a translation came from, not which *version* of it, so an operator
        who corrects an English cue after the Spanish pass has already run
        leaves a Spanish row that still says the old thing -- and an id-only
        match accepts it as present and publishes it. Reviewer-proven: "the
        motion carries", corrected in English to "the motion FAILS", shipped
        as "la moción se aprueba" in Spanish with the job green. With the
        source text in the id, an edited English cue produces an id no stored
        row carries, so its translation is seen as missing and re-queued
        through the ordinary path above -- idempotently, since an unedited cue
        keeps its id and stays a duplicate.

        The tally is scoped to the expected ids for the same reason (see
        ``cue_ids`` on :func:`~civiccast.captions.vod.reviewed_caption_cues`):
        a Spanish row is an orphan the moment its English source is rejected
        *or revised*, and an orphan must neither gate publication nor reach
        the track. The superseded row stays in the review queue and simply is
        not one of the ids the publisher asks for.

        Note what is deliberately NOT treated as truncation: an operator
        *rejecting* some Spanish cues. That is an editorial decision on a row
        that exists, exactly as it is in English, and it legitimately produces
        a shorter Spanish track. What must never happen is a row that was
        never created -- or one that no longer matches its source -- going
        unnoticed.
        """

        expected_ids = {
            recorded_translation_cue_id(self._review_store, asset_id, cue, target_language): cue
            for cue in english_cues
        }
        stored_ids = {
            item.cue.cue_id
            for item in self._review_store.list(asset_id=asset_id, language=target_language)
        }
        missing = [cue for cue_id, cue in expected_ids.items() if cue_id not in stored_ids]
        if missing:
            cause: Exception | None = None
            try:
                queued = queue_translated_captions(
                    self._review_store,
                    asset_id=asset_id,
                    cues=missing,
                    provider=self._translation_provider_instance(),
                    target=TranslationTarget(
                        target_language=target_language,
                        target_name=RECORDED_TRANSLATION_LANGUAGES[target_language],
                    ),
                )
            except Exception as exc:
                # A mid-batch failure is the reviewer's reproduction: rows
                # written before the error are already durable, so this is a
                # SHORT queue, not a no-op. Hold the cause and fall through to
                # the completeness check, which turns it into the operator's
                # remediation ("3 of 6 ... here is what to do") rather than a
                # bare store-error string on the job row. Re-raised below with
                # the cause attached if rows really are missing; if the error
                # happened to land after the last row, the check passes and the
                # job carries on, which is the correct outcome.
                cause = exc
            else:
                _LOG.info(
                    "Queued %d Spanish caption cue(s) for review on asset %s "
                    "(%d already queued; %d of %d English cues needed one).",
                    len(queued.created_review_item_ids),
                    asset_id,
                    len(queued.duplicate_review_item_ids),
                    len(missing),
                    len(expected_ids),
                )
            stored_ids = {
                item.cue.cue_id
                for item in self._review_store.list(asset_id=asset_id, language=target_language)
            }
            still_missing = set(expected_ids) - stored_ids
            if still_missing:
                detail = INCOMPLETE_TRANSLATION_REMEDIATION.format(
                    queued=len(expected_ids) - len(still_missing),
                    expected=len(expected_ids),
                )
                if cause is not None:
                    detail = f"{detail} (Underlying error: {cause})"
                raise OfflineCaptionTranslationError(
                    detail.replace("Spanish", RECORDED_TRANSLATION_LANGUAGES[target_language])
                ) from cause
        return_scope = set(expected_ids)
        spanish = reviewed_caption_cues(
            self._review_store,
            asset_id,
            language=target_language,
            cue_ids=return_scope,
        )
        if spanish.pending:
            return None
        return spanish

    # -- shared ------------------------------------------------------------

    def _runtime_instance(self) -> CaptionRuntime:
        if self._runtime is None:
            self._runtime = self._runtime_factory()
        return self._runtime

    def _translation_provider_instance(self) -> TranslationProvider:
        if self._translation_provider is None:
            if self._translation_provider_factory is None:  # pragma: no cover - guarded by caller
                raise RuntimeError(
                    "No translation provider factory configured for the offline caption "
                    "worker; the recorded-Spanish leg cannot run."
                )
            self._translation_provider = self._translation_provider_factory()
        return self._translation_provider

    def _record_failure(
        self,
        row: OfflineCaptionJobRecord,
        *,
        now: datetime,
        error: str,
        stage: str,
    ) -> OfflineCaptionJobRecord:
        attempts = row.attempts + 1
        if attempts >= self._settings.max_attempts:
            _LOG.error(
                "Offline caption %s for asset %s failed %d time(s); giving up: %s",
                stage,
                row.asset_id,
                attempts,
                error,
            )
            return self._save(
                row.model_copy(
                    update={
                        "state": OFFLINE_CAPTION_JOB_STATE_FAILED,
                        "attempts": attempts,
                        "next_attempt_at": None,
                        "last_error": error,
                        "updated_at": now,
                    }
                )
            )
        delay = self._settings.backoff_seconds * (2 ** max(attempts - 1, 0))
        _LOG.warning(
            "Offline caption %s for asset %s failed (attempt %d/%d); next try in %.0fs: %s",
            stage,
            row.asset_id,
            attempts,
            self._settings.max_attempts,
            delay,
            error,
        )
        return self._save(
            row.model_copy(
                update={
                    "attempts": attempts,
                    "next_attempt_at": now + timedelta(seconds=delay),
                    "last_error": error,
                    "updated_at": now,
                }
            )
        )
