# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The summary generation job -- async summary generation, off the request cycle.

Field evidence (candidate #17, AMD Ryzen 7 8745HS / 32GB RAM / CPU-only inference,
Windows 11): ``POST /api/staff/summaries/generate`` 503'd at ~120s even on a *warm*
model, because the control plane's synchronous request/response cycle could not
survive a legitimate CPU-only generation (measured 94-366s+ on the same hardware
class -- see ``civiccast/ai_models/models.py``). Worse: Ollama's own completion
succeeded server-side and was thrown away, because the HTTP client had already given
up. Making the client-side socket timeout longer (``ai_runtime/ollama_client.py``)
fixes the immediate cause, but an operator still should not have to hold a browser tab
open against a multi-minute blocking request. This module is the product-level fix:
summary generation becomes a durable queued job with visible state, on the SAME
background-job shape the offline caption job (``civiccast/captions/vod_job.py``, K3)
already established and the whole codebase treats as the pattern for "AI work that can
legitimately take minutes" -- durable queue row + ``run_once``/``run_forever`` worker
under :class:`~civiccast.platform.worker_runtime.ThreadSupervisor`, bounded retry with
exponential backoff, and a manual retry action once the budget is spent.

One job row per meeting, three live states plus two terminal ones:

``pending``
    Queued; a worker tick has not picked it up yet.

``running``
    A worker is actively generating (the model call is in flight). Distinct from
    ``pending`` so the operator console can show real progress instead of a static
    spinner over an unknown wait -- the whole point of building this job instead of
    just raising the HTTP timeout.

``complete``
    The generation pipeline ran to completion and a :class:`~civiccast.summary.models
    .SummaryDraft` was persisted to the summary review store -- REGARDLESS of whether
    that draft's own status is ``pending_review`` (claims cited to transcript
    evidence) or ``refused`` (the pipeline's own evidence-citation gate declined to
    publish an unsupported claim, spec §4.2). Both outcomes are the pipeline doing its
    job correctly; ``job.state == "complete"`` means "no error", not "approved".
    ``summary_id`` on the row points at the resulting draft either way, so the
    operator console can link straight to Summary review.

``failed``
    The model/runtime raised (Ollama unreachable, the model failed to load, a crash)
    rather than returning a result the pipeline could evaluate. Retried with bounded
    exponential backoff up to the settings' attempt budget, then a manual retry.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from civiccast.ai_runtime.ollama_client import OllamaRuntimeUnavailableError
from civiccast.captions import CaptionCue
from civiccast.summary.generate import SummaryGenerationPipeline, SummaryModel
from civiccast.summary.store import SummaryStore, SummaryStoreConflictError

_LOG = logging.getLogger(__name__)

SummaryGenerationJobState = Literal["pending", "running", "complete", "failed"]

SUMMARY_JOB_STATE_PENDING: SummaryGenerationJobState = "pending"
SUMMARY_JOB_STATE_RUNNING: SummaryGenerationJobState = "running"
SUMMARY_JOB_STATE_COMPLETE: SummaryGenerationJobState = "complete"
SUMMARY_JOB_STATE_FAILED: SummaryGenerationJobState = "failed"
SUMMARY_JOB_STATES = (
    SUMMARY_JOB_STATE_PENDING,
    SUMMARY_JOB_STATE_RUNNING,
    SUMMARY_JOB_STATE_COMPLETE,
    SUMMARY_JOB_STATE_FAILED,
)
#: States the worker still has work to do in (mirrors OFFLINE_CAPTION_JOB_ACTIVE_STATES;
#: also the states ``enqueue_summary_job`` treats as "already active for this meeting").
SUMMARY_JOB_ACTIVE_STATES = (SUMMARY_JOB_STATE_PENDING, SUMMARY_JOB_STATE_RUNNING)

SUMMARY_JOB_MODE_INLINE = "inline"
SUMMARY_JOB_MODE_OFF = "off"
_SUMMARY_JOB_MODES = (SUMMARY_JOB_MODE_INLINE, SUMMARY_JOB_MODE_OFF)

SummaryModelFactory = Callable[[], SummaryModel]

__all__ = [
    "SUMMARY_JOB_ACTIVE_STATES",
    "SUMMARY_JOB_MODE_INLINE",
    "SUMMARY_JOB_MODE_OFF",
    "SUMMARY_JOB_STATES",
    "SUMMARY_JOB_STATE_COMPLETE",
    "SUMMARY_JOB_STATE_FAILED",
    "SUMMARY_JOB_STATE_PENDING",
    "SUMMARY_JOB_STATE_RUNNING",
    "InMemorySummaryGenerationJobStore",
    "SummaryGenerationJobConflictError",
    "SummaryGenerationJobRecord",
    "SummaryGenerationJobSettings",
    "SummaryGenerationJobState",
    "SummaryGenerationJobStore",
    "SummaryGenerationJobWorker",
    "enqueue_summary_job",
    "new_summary_job_id",
]


class SummaryGenerationJobConflictError(Exception):
    """Raised when a concurrent enqueue lost the race for a meeting's active job.

    Mirrors :class:`~civiccast.captions.vod_job.OfflineCaptionJobConflictError`: a
    DB-level partial-unique index is the real guard against two active jobs for one
    meeting; a store raises this from ``enqueue`` when it loses that race so the
    caller can recover the winning job instead of surfacing a raw integrity error.
    """

    def __init__(self, meeting_id: str) -> None:
        self.meeting_id = meeting_id
        super().__init__(f"Summary generation job already active for meeting {meeting_id!r}")


def new_summary_job_id() -> str:
    """Return a fresh opaque job id (same shape as the other queue ids)."""

    return "sgj_" + secrets.token_urlsafe(16).replace("-", "").replace("_", "")


class SummaryGenerationJobRecord(BaseModel):
    """One meeting's summary generation job."""

    model_config = ConfigDict(extra="forbid")

    job_id: Annotated[str, Field(min_length=1, max_length=120)]
    meeting_id: Annotated[str, Field(min_length=1, max_length=160)]
    #: The committed transcript cues to summarize (the same shape the synchronous
    #: ``POST /api/staff/summaries/generate`` endpoint already accepted directly).
    cues: list[CaptionCue] = Field(default_factory=list)
    state: SummaryGenerationJobState
    attempts: int = 0
    next_attempt_at: datetime | None = None
    #: Set once a SummaryDraft exists for this job (state == "complete"), whatever
    #: that draft's own status (pending_review or refused) -- see module docstring.
    summary_id: str | None = None
    last_error: str = ""
    created_at: datetime
    updated_at: datetime


class SummaryGenerationJobStore(Protocol):
    """Storage contract for the summary generation job queue."""

    def enqueue(self, record: SummaryGenerationJobRecord) -> SummaryGenerationJobRecord:
        """Persist a new job row.

        Raises :class:`SummaryGenerationJobConflictError` when ``record`` is in an
        active state and another active job already exists for its meeting.
        """

    def save(self, record: SummaryGenerationJobRecord) -> SummaryGenerationJobRecord:
        """Persist an updated job row."""

    def get(self, job_id: str) -> SummaryGenerationJobRecord | None:
        """Return one job row when present."""

    def active_for_meeting(self, meeting_id: str) -> SummaryGenerationJobRecord | None:
        """Return the meeting's non-terminal job, when one exists."""

    def due(
        self,
        *,
        now: datetime,
        states: Sequence[str] = SUMMARY_JOB_ACTIVE_STATES,
    ) -> list[SummaryGenerationJobRecord]:
        """Return job rows in ``states`` whose next attempt is due."""

    def list(
        self,
        *,
        meeting_id: str | None = None,
        state: str | None = None,
    ) -> list[SummaryGenerationJobRecord]:
        """Return job rows for operator visibility, optionally filtered.

        Unlike :meth:`due`, this is not restricted to active states -- it is the read
        path for the operator's "what's stuck and why" view, so a ``failed`` or
        ``complete`` row must come back too.
        """


class InMemorySummaryGenerationJobStore:
    """In-memory job queue for tests and non-DB development."""

    def __init__(self) -> None:
        self._jobs: dict[str, SummaryGenerationJobRecord] = {}

    def enqueue(self, record: SummaryGenerationJobRecord) -> SummaryGenerationJobRecord:
        if record.state in SUMMARY_JOB_ACTIVE_STATES:
            conflict = next(
                (
                    row
                    for row in self._jobs.values()
                    if row.meeting_id == record.meeting_id
                    and row.state in SUMMARY_JOB_ACTIVE_STATES
                ),
                None,
            )
            if conflict is not None:
                raise SummaryGenerationJobConflictError(record.meeting_id)
        self._jobs[record.job_id] = record
        return record.model_copy(deep=True)

    def save(self, record: SummaryGenerationJobRecord) -> SummaryGenerationJobRecord:
        self._jobs[record.job_id] = record
        return record.model_copy(deep=True)

    def get(self, job_id: str) -> SummaryGenerationJobRecord | None:
        record = self._jobs.get(job_id)
        return record.model_copy(deep=True) if record is not None else None

    def active_for_meeting(self, meeting_id: str) -> SummaryGenerationJobRecord | None:
        for record in sorted(self._jobs.values(), key=lambda row: row.created_at):
            if record.meeting_id == meeting_id and record.state in SUMMARY_JOB_ACTIVE_STATES:
                return record.model_copy(deep=True)
        return None

    def due(
        self,
        *,
        now: datetime,
        states: Sequence[str] = SUMMARY_JOB_ACTIVE_STATES,
    ) -> list[SummaryGenerationJobRecord]:
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
        meeting_id: str | None = None,
        state: str | None = None,
    ) -> list[SummaryGenerationJobRecord]:
        rows = list(self._jobs.values())
        if meeting_id is not None:
            rows = [row for row in rows if row.meeting_id == meeting_id]
        if state is not None:
            rows = [row for row in rows if row.state == state]
        return [
            row.model_copy(deep=True)
            for row in sorted(rows, key=lambda row: (row.created_at, row.job_id))
        ]


@dataclass(frozen=True)
class SummaryGenerationJobSettings:
    """Deployment configuration for the summary generation job worker."""

    mode: str = SUMMARY_JOB_MODE_INLINE
    poll_seconds: float = 15.0
    #: Longer than the offline caption job's 300s default: a summary retry re-runs a
    #: multi-minute CPU generation, so hammering it on a short cadence just burns CPU
    #: the box needs to finish the FIRST attempt.
    backoff_seconds: float = 120.0
    #: Lower than the offline caption job's 4: each attempt here can cost several
    #: minutes of CPU time (measured up to 366s+ for 12B); a smaller budget still
    #: absorbs one transient Ollama hiccup without letting a truly broken box retry
    #: for the better part of an hour before an operator sees "failed" and can act.
    max_attempts: int = 3

    @classmethod
    def from_env(cls) -> SummaryGenerationJobSettings:
        mode = os.environ.get("CIVICCAST_SUMMARY_JOB", SUMMARY_JOB_MODE_INLINE).strip().lower()
        if mode not in _SUMMARY_JOB_MODES:
            raise ValueError(
                f"CIVICCAST_SUMMARY_JOB must be one of {', '.join(_SUMMARY_JOB_MODES)}; "
                f"got {mode!r}."
            )
        defaults = cls()
        return cls(
            mode=mode,
            poll_seconds=_env_positive_float(
                "CIVICCAST_SUMMARY_JOB_POLL_SECONDS", defaults.poll_seconds
            ),
            backoff_seconds=_env_positive_float(
                "CIVICCAST_SUMMARY_JOB_BACKOFF_SECONDS", defaults.backoff_seconds
            ),
            max_attempts=_env_positive_int(
                "CIVICCAST_SUMMARY_JOB_MAX_ATTEMPTS", defaults.max_attempts
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


def enqueue_summary_job(
    store: SummaryGenerationJobStore,
    *,
    meeting_id: str,
    cues: list[CaptionCue],
    now: datetime | None = None,
) -> SummaryGenerationJobRecord:
    """Queue summary generation for a meeting's committed transcript cues.

    Idempotent per meeting: a meeting that already has a pending or running job
    returns the existing job instead of starting a second multi-minute CPU
    generation for the same meeting.

    Same check-then-insert race shape as ``enqueue_offline_caption_job`` (a plain
    SELECT, no row lock): two concurrent enqueues can both pass the pre-check before
    either inserts. The DB-level partial-unique index is the real guard; a
    :class:`SummaryGenerationJobConflictError` from ``store.enqueue`` here is caught
    and the winner is returned instead of a raw DB error or a duplicate job.
    """

    existing = store.active_for_meeting(meeting_id)
    if existing is not None:
        return existing
    resolved_now = now or datetime.now(UTC)
    record = SummaryGenerationJobRecord(
        job_id=new_summary_job_id(),
        meeting_id=meeting_id,
        cues=list(cues),
        state=SUMMARY_JOB_STATE_PENDING,
        attempts=0,
        next_attempt_at=resolved_now,
        created_at=resolved_now,
        updated_at=resolved_now,
    )
    try:
        enqueued = store.enqueue(record)
    except SummaryGenerationJobConflictError:
        winner = store.active_for_meeting(meeting_id)
        if winner is None:
            raise
        _LOG.info(
            "Summary generation enqueue for meeting %s lost a concurrent race; reusing job %s.",
            meeting_id,
            winner.job_id,
        )
        return winner
    _LOG.info("Queued summary generation for meeting %s.", meeting_id)
    return enqueued


class SummaryGenerationJobWorker:
    """Drive queued meetings from pending transcript cues to a stored summary draft.

    ``run_once`` is the testable unit; ``run_forever`` is the supervised loop that
    survives and logs scan exceptions -- the same split every other CivicCast
    background worker (including :class:`~civiccast.captions.vod_job
    .OfflineCaptionJobWorker`, which this mirrors) uses.

    The summary model is built lazily, through ``model_factory``, so a station with
    the worker enabled but nothing queued never loads a multi-gigabyte local model
    into memory, and so each attempt picks up the operator's CURRENT model selection
    rather than one captured at worker-construction time.
    """

    def __init__(
        self,
        store: SummaryGenerationJobStore,
        summary_store: SummaryStore,
        *,
        model_factory: SummaryModelFactory,
        settings: SummaryGenerationJobSettings,
    ) -> None:
        self._store = store
        self._summary_store = summary_store
        self._model_factory = model_factory
        self._settings = settings

    def run_forever(
        self,
        *,
        poll_seconds: float = 15.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Run the summary-job loop until ``stop_event`` is set."""

        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.exception("Summary generation scan failed; retrying on the next poll.")
            if stop_event is not None:
                stop_event.wait(poll_seconds)
            else:  # pragma: no cover - only reachable outside a supervisor
                time.sleep(poll_seconds)

    def run_once(self, *, now: datetime | None = None) -> list[SummaryGenerationJobRecord]:
        """Advance every due job once; return the rows that were touched."""

        resolved_now = now or datetime.now(UTC)
        processed: list[SummaryGenerationJobRecord] = []
        for row in self._store.due(now=resolved_now):
            processed.append(self._generate(row, now=resolved_now))
        return processed

    def _generate(
        self, row: SummaryGenerationJobRecord, *, now: datetime
    ) -> SummaryGenerationJobRecord:
        # Mark running BEFORE the (potentially multi-minute) model call, and save
        # immediately, so a concurrent status poll sees real progress instead of a
        # stale "pending" for the whole duration -- this state transition is the
        # entire point of the job existing instead of just raising the HTTP timeout.
        running = self._store.save(
            row.model_copy(update={"state": SUMMARY_JOB_STATE_RUNNING, "updated_at": now})
        )
        try:
            pipeline = SummaryGenerationPipeline(model=self._model_factory())
            draft = pipeline.generate(meeting_id=running.meeting_id, cues=running.cues)
        except OllamaRuntimeUnavailableError as exc:
            return self._record_failure(running, now=now, error=str(exc))
        except Exception as exc:
            return self._record_failure(running, now=now, error=str(exc))

        try:
            stored = self._summary_store.create_summary(draft)
        except SummaryStoreConflictError:
            # A prior attempt's draft already landed (e.g. a retry re-ran after the
            # store write succeeded but this process died before marking the job
            # complete) -- the summary_id is still the fingerprint-stable id the
            # pipeline would have produced, so this is recovery, not a new failure.
            stored = draft
        _LOG.info(
            "Summary generation complete for meeting %s (job %s): draft %s, status %s.",
            running.meeting_id,
            running.job_id,
            stored.summary_id,
            stored.status,
        )
        return self._store.save(
            running.model_copy(
                update={
                    "state": SUMMARY_JOB_STATE_COMPLETE,
                    "attempts": running.attempts + 1,
                    "next_attempt_at": None,
                    "summary_id": stored.summary_id,
                    "last_error": "",
                    "updated_at": now,
                }
            )
        )

    def _record_failure(
        self, row: SummaryGenerationJobRecord, *, now: datetime, error: str
    ) -> SummaryGenerationJobRecord:
        attempts = row.attempts + 1
        if attempts >= self._settings.max_attempts:
            _LOG.error(
                "Summary generation for meeting %s (job %s) failed %d time(s); giving up: %s",
                row.meeting_id,
                row.job_id,
                attempts,
                error,
            )
            return self._store.save(
                row.model_copy(
                    update={
                        "state": SUMMARY_JOB_STATE_FAILED,
                        "attempts": attempts,
                        "next_attempt_at": None,
                        "last_error": error,
                        "updated_at": now,
                    }
                )
            )
        delay = self._settings.backoff_seconds * (2 ** max(attempts - 1, 0))
        _LOG.warning(
            "Summary generation for meeting %s (job %s) failed (attempt %d/%d); "
            "next try in %.0fs: %s",
            row.meeting_id,
            row.job_id,
            attempts,
            self._settings.max_attempts,
            delay,
            error,
        )
        return self._store.save(
            row.model_copy(
                update={
                    "state": SUMMARY_JOB_STATE_PENDING,
                    "attempts": attempts,
                    "next_attempt_at": now + timedelta(seconds=delay),
                    "last_error": error,
                    "updated_at": now,
                }
            )
        )
