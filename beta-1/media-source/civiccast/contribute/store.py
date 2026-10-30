# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Contributor submission store used by public and staff routes."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import stat
import tempfile
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import threading

    from civiccast.contribute.router import ContributorUploadByteBudget

from civiccast.contribute.models import (
    ContributorNotificationOutbox,
    ContributorReviewQueue,
    ContributorReviewRequest,
    ContributorSubmission,
    ContributorSubmissionCreate,
    ContributorSubmissionReceipt,
    ContributorSubmissionState,
    ProducerActivityReport,
    ProducerActivityReportRow,
    PublicSubmissionStatus,
    ReviewAction,
    SubmissionAgreementCatalog,
    SubmissionMediaReference,
    SubmissionNotificationPreference,
    SubmissionStatusNotification,
    utc_now,
)
from civiccast.installer.storage import default_storage_dir

_LOG = logging.getLogger(__name__)

_STORE_FILE_NAME = "contributor-submissions.json"
_CURRENT_AGREEMENT = SubmissionAgreementCatalog(
    agreement_id="community-media-submission",
    version="2026-05-31",
    title="Community media submission agreement",
    summary=(
        "Contributors confirm they have rights to submit the program, consent to "
        "operator review, and understand that operators decide what airs or publishes."
    ),
    effective_at=utc_now(),
)


class ContributorSubmissionNotFoundError(KeyError):
    """Raised when a contributor submission cannot be found."""


class ContributorReceiptTokenError(PermissionError):
    """Raised when public status lookup uses the wrong receipt token."""


class ContributorStoreError(RuntimeError):
    """Raised when contributor workflow state cannot be persisted."""


class ContributorUploadAlreadyUsedError(Exception):
    """Raised when a submission reuses an ``upload_ref`` another submission already holds.

    An uploaded file must back at most one submission (rc18 re-gate PE-1). It is a
    distinct type -- not a ``ValueError`` -- so the router maps it to 409 Conflict
    (the resource state conflicts with the request) rather than 422 (the request
    body is malformed); the body is well-formed, the upload is simply spent.
    """


class ContributorReviewConflictError(RuntimeError):
    """Raised when a review action is not valid for a submission's current state.

    CRITICAL/MAJOR field evidence: ``accept``/``schedule`` used to be guarded
    only by a plain state read the router took OUTSIDE the store's lock
    (TOCTOU), and ``mark_under_review``/``request_changes`` had no guard at
    all -- either could revert an already-scheduled/published submission
    while the real schedule item stayed live. This is a distinct type from
    ``ValueError`` (which ``_apply_review`` still raises for payload-shape
    problems like a missing media-gate pass) so the router maps it to 409
    Conflict -- the resource's state conflicts with the request, the
    request body itself is well-formed.
    """


class ContributorSubmissionStore:
    """Thread-safe JSON-backed submission queue for external producers."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = Lock()
        self._submissions = self._load()
        # CRITICAL race fix: in-process reservation ledger keyed by
        # submission_id -> the ReviewAction currently claiming it. Every
        # review action (accept/schedule/decline/mark_under_review/
        # request_changes) takes this reservation via
        # ``reserve_review_action`` before it does anything else -- not just
        # accept/schedule. accept/schedule each trigger a REAL external side
        # effect (ffprobe ingest into the asset library / a real
        # civiccast.schedule_items row) BEFORE this store is asked to
        # persist anything, so a state guard applied only at persist time is
        # too late: two concurrent accepts would already have created two
        # real assets before either write landed. BLOCKER field evidence
        # showed the same is true of decline even though it has no side
        # effect of its own: an unreserved decline could interleave between
        # a concurrent schedule's real-row creation and that schedule's
        # persist, leaving the real schedule row live behind a submission
        # marked declined. Reserving every action first closes all of this
        # off -- the second concurrent/interleaved request of any kind is
        # rejected before it can touch the asset/schedule store, or the
        # persisted state, at all. In-memory and per-process by design (same
        # trade-off as
        # ``civiccast.auth.rate_limit.AuthRateLimiter``): it only needs to
        # arbitrate requests racing within this process, which is exactly
        # where the race lives (both requests go through the same FastAPI
        # app/threadpool).
        self._in_flight: dict[str, str] = {}

    def current_agreement(self) -> SubmissionAgreementCatalog:
        return _CURRENT_AGREEMENT.model_copy(deep=True)

    def create_submission(
        self, payload: ContributorSubmissionCreate
    ) -> ContributorSubmissionReceipt:
        # QA-3: upload_ref/sha256 arrive as client-supplied JSON claims, not
        # proof of a real upload. Resolve + hash the real file BEFORE taking
        # the lock (file IO shouldn't hold up other store operations); a bad
        # claim raises ValueError, which router.py maps to 422.
        media = _verified_media_reference(payload.media)
        with self._lock:
            # PE-1: an upload is single-use. QA-3 already proved the ref points at
            # a real file inside the upload directory; here we reject it if ANY
            # existing submission (any state) already holds that same resolved
            # path. The submission store IS the durable ledger, so this survives a
            # restart without new state, and the check + insert share one lock so
            # two concurrent creates cannot both claim the same upload.
            try:
                resolved_ref = str(resolve_contributor_upload_path(media.upload_ref))
            except (
                OSError
            ) as exc:  # pragma: no cover - _verified_media_reference resolved it already
                _LOG.warning("could not re-resolve a just-verified upload_ref: %s", exc)
                raise ValueError("submission media upload_ref is not a usable reference") from exc
            if resolved_ref in self._referenced_upload_paths_locked():
                raise ContributorUploadAlreadyUsedError(
                    "This uploaded file is already attached to another submission. "
                    "Upload the program again to submit it a second time."
                )
            now = utc_now()
            submission_id = _unique_submission_id(payload.title, self._submissions)
            receipt_token = secrets.token_urlsafe(32)
            submission = ContributorSubmission(
                submission_id=submission_id,
                receipt_token=receipt_token,
                contributor=payload.contributor,
                channel_id=payload.channel_id,
                title=payload.title,
                description=payload.description,
                tags=payload.tags,
                producer_name=payload.producer_name,
                requested_air_date=payload.requested_air_date,
                media=media,
                agreements=payload.agreements,
                notifications=payload.notifications,
                state="submitted",
                created_at=now,
                updated_at=now,
                status_history=["submitted: received from contributor portal"],
                notifications_sent=_status_notifications(
                    payload.notifications,
                    state="submitted",
                    queued_at=now,
                ),
            )
            self._submissions[submission_id] = submission
            self._persist_locked()
        return ContributorSubmissionReceipt(
            submission_id=submission_id,
            receipt_token=receipt_token,
            state="submitted",
            status_url=f"/api/public/contribute/submissions/{submission_id}/status",
            proof_boundary="contributor-submission-to-operator-review-queue",
        )

    def public_status(self, submission_id: str, receipt_token: str) -> PublicSubmissionStatus:
        submission = self.get_submission(submission_id)
        if not secrets.compare_digest(submission.receipt_token, receipt_token):
            raise ContributorReceiptTokenError(submission_id)
        return PublicSubmissionStatus(
            submission_id=submission.submission_id,
            title=submission.title,
            state=submission.state,
            producer_name=submission.producer_name,
            updated_at=submission.updated_at,
            status_message=_status_message(submission.state),
            decline_reason=submission.decline_reason if submission.state == "declined" else None,
            schedule_handoff=(
                submission.schedule_handoff
                if submission.state in {"scheduled", "published"}
                else None
            ),
        )

    def list_queue(self) -> ContributorReviewQueue:
        submissions = sorted(
            self._submissions.values(),
            key=lambda item: (item.created_at, item.submission_id),
        )
        needs_action = sum(
            1
            for submission in submissions
            if submission.state in {"submitted", "under_review", "needs_changes"}
        )
        return ContributorReviewQueue(
            generated_at=utc_now(),
            submissions=[submission.model_copy(deep=True) for submission in submissions],
            needs_operator_action=needs_action,
            proof_boundary="contributor-queue-to-operator-final-control",
        )

    def known_producer_ids(self) -> set[str]:
        """The durable set of producer (contributor-account) identities the station knows.

        A producer's identity is its :class:`ContributorAccount` ``account_id``. The
        producer-identity ledger is the set of distinct account ids recorded across ALL
        submissions in ANY state — the same source ``producer_report`` aggregates. This is
        the durable identity source for ``producer_ref`` resolution: a producer resolves
        because the station has an account record of them, not because they currently have a
        submission sitting in the operator review queue (so a producer whose submissions are
        all published/declined still resolves).
        """
        return {submission.contributor.account_id for submission in self._submissions.values()}

    def referenced_upload_paths(self) -> set[str]:
        """Resolved ``media.upload_ref`` paths for EVERY submission, any state.

        QA-2: the contributor-upload reaper treats this set as untouchable —
        a pending, under-review, accepted, or already-scheduled/published
        contributor's file must never be deleted just because it looks old.
        """
        with self._lock:
            return self._referenced_upload_paths_locked()

    def _referenced_upload_paths_locked(self) -> set[str]:
        """Resolved ``media.upload_ref`` for every submission; caller holds ``self._lock``.

        Shared by :meth:`referenced_upload_paths` (which takes the lock) and
        :meth:`create_submission` (which already holds it) -- ``self._lock`` is a
        plain, non-reentrant ``Lock``, so the create path must not re-acquire it.
        """
        paths: set[str] = set()
        for submission in self._submissions.values():
            try:
                paths.add(str(resolve_contributor_upload_path(submission.media.upload_ref)))
            except OSError:
                continue
        return paths

    def get_submission(self, submission_id: str) -> ContributorSubmission:
        with self._lock:
            submission = self._submissions.get(submission_id)
            if submission is None:
                raise ContributorSubmissionNotFoundError(submission_id)
            return submission.model_copy(deep=True)

    def reserve_review_action(
        self, submission_id: str, action: ReviewAction
    ) -> ContributorSubmission:
        """Atomically claim ``action`` for ``submission_id`` before its real side effect runs.

        CRITICAL fix: the router must call this BEFORE it runs
        ``_ingest_accepted_media`` / ``_create_real_schedule_item`` for
        ``accept``/``schedule`` -- both create a REAL external resource (a
        library asset; a ``civiccast.schedule_items`` row) that a later,
        persist-time-only guard cannot un-create. Under one lock this
        (a) proves ``submission_id`` exists, (b) proves no OTHER review
        action is already in flight for it, and (c) proves the submission's
        CURRENT state actually allows ``action`` (the same check
        :func:`_ensure_review_action_allowed` applies at persist time) --
        then claims the reservation, so a second concurrent/duplicate
        request is rejected here, cleanly, before it can touch the asset or
        schedule store at all.

        The caller MUST release the reservation exactly once, on every
        path -- success or failure -- via :meth:`release_reservation`
        (``review_contributor_submission`` does this in a ``finally``).

        Raises:
            ContributorSubmissionNotFoundError: no such submission.
            ContributorReviewConflictError: another review action is
                already in flight for this submission, or the submission's
                current state does not allow ``action``.
        """
        with self._lock:
            submission = self._submissions.get(submission_id)
            if submission is None:
                raise ContributorSubmissionNotFoundError(submission_id)
            in_flight = self._in_flight.get(submission_id)
            if in_flight is not None:
                raise ContributorReviewConflictError(
                    f"Another review action ({in_flight!r}) is already being processed "
                    "for this submission. Try again once it completes."
                )
            _ensure_review_action_allowed(submission, action)
            self._in_flight[submission_id] = action
            return submission.model_copy(deep=True)

    def release_reservation(self, submission_id: str) -> None:
        """Release a reservation :meth:`reserve_review_action` claimed.

        Idempotent -- releasing a submission with no active reservation (it
        was never reserved, or was already released) is a no-op, so the
        router's ``finally`` can call this unconditionally.
        """
        with self._lock:
            self._in_flight.pop(submission_id, None)

    def review_submission(
        self,
        submission_id: str,
        request: ContributorReviewRequest,
        *,
        ingested_asset_id: str | None = None,
        created_schedule_item_id: str | None = None,
    ) -> ContributorSubmission:
        """Apply an operator review decision.

        ``ingested_asset_id`` / ``created_schedule_item_id`` are supplied by
        the router AFTER it has actually performed the corresponding side
        effect (ffprobe ingest into the asset library / creation of a real
        schedule item) -- this method never fabricates either. ``accept``
        requires ``ingested_asset_id``; ``schedule`` requires the submission
        to already carry an ``asset_id`` (from a prior accept) plus
        ``created_schedule_item_id``. See :func:`_apply_review`.
        """
        with self._lock:
            submission = self._submissions.get(submission_id)
            if submission is None:
                raise ContributorSubmissionNotFoundError(submission_id)
            updated = _apply_review(
                submission,
                request,
                ingested_asset_id=ingested_asset_id,
                created_schedule_item_id=created_schedule_item_id,
            )
            self._submissions[submission_id] = updated
            self._persist_locked()
            return updated.model_copy(deep=True)

    def producer_report(self) -> ProducerActivityReport:
        rows_by_contributor: dict[str, ProducerActivityReportRow] = {}
        for submission in self._submissions.values():
            existing = rows_by_contributor.get(submission.contributor.account_id)
            accepted_increment = (
                1 if submission.state in {"accepted", "scheduled", "published"} else 0
            )
            scheduled_increment = 1 if submission.state in {"scheduled", "published"} else 0
            declined_increment = 1 if submission.state == "declined" else 0
            if existing is None:
                rows_by_contributor[submission.contributor.account_id] = ProducerActivityReportRow(
                    contributor_id=submission.contributor.account_id,
                    producer_name=submission.producer_name,
                    submitted_count=1,
                    accepted_count=accepted_increment,
                    scheduled_count=scheduled_increment,
                    declined_count=declined_increment,
                    latest_submission_at=submission.created_at,
                )
                continue
            rows_by_contributor[submission.contributor.account_id] = ProducerActivityReportRow(
                contributor_id=existing.contributor_id,
                producer_name=submission.producer_name,
                submitted_count=existing.submitted_count + 1,
                accepted_count=existing.accepted_count + accepted_increment,
                scheduled_count=existing.scheduled_count + scheduled_increment,
                declined_count=existing.declined_count + declined_increment,
                latest_submission_at=max(existing.latest_submission_at, submission.created_at),
            )
        return ProducerActivityReport(
            generated_at=utc_now(),
            rows=sorted(rows_by_contributor.values(), key=lambda row: row.producer_name.casefold()),
            proof_boundary="producer-submission-ledger-to-station-reporting",
        )

    def notification_outbox(self) -> ContributorNotificationOutbox:
        notifications = [
            notification
            for submission in self._submissions.values()
            for notification in submission.notifications_sent
        ]
        return ContributorNotificationOutbox(
            generated_at=utc_now(),
            notifications=[notification.model_copy(deep=True) for notification in notifications],
            proof_boundary="contributor-status-change-to-notification-outbox",
        )

    def _load(self) -> dict[str, ContributorSubmission]:
        if self._path is None or not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            return {
                item["submission_id"]: ContributorSubmission.model_validate(item)
                for item in payload.get("submissions", [])
            }
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ContributorStoreError(f"Could not read contributor submissions: {exc}") from exc

    def _persist_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(
                    {
                        "submissions": [
                            submission.model_dump(mode="json")
                            for submission in self._submissions.values()
                        ]
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            if os.name != "nt":
                tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            tmp_path.replace(self._path)
            if os.name != "nt":
                self._path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            raise ContributorStoreError(f"Could not write contributor submissions: {exc}") from exc


def default_contributor_store_path() -> Path | None:
    configured = os.environ.get("CIVICCAST_CONTRIBUTOR_STORE_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.environ.get("CIVICCAST_ALLOW_EPHEMERAL_STORES") == "1":
        return None
    return (default_storage_dir() / _STORE_FILE_NAME).expanduser().resolve()


def default_contributor_upload_dir() -> Path:
    configured = os.environ.get("CIVICCAST_CONTRIBUTOR_UPLOAD_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.environ.get("CIVICCAST_ALLOW_EPHEMERAL_STORES") == "1":
        return Path(tempfile.gettempdir()) / "civiccast-contributor-uploads"
    return (default_storage_dir() / "contributor-uploads").expanduser().resolve()


def resolve_contributor_upload_path(upload_ref: str, *, upload_dir: Path | None = None) -> Path:
    """Resolve a public ``upload_ref`` to the real file it names.

    Field evidence / GauntletGate finding: ``upload_ref`` used to BE the
    server's absolute filesystem path, returned verbatim to an anonymous
    public caller of ``POST /api/public/contribute/uploads`` -- a privacy
    leak (reveals the service account's profile layout) that also, more
    seriously, meant the "reference" a submission attaches to was just the
    contributor's own sanitized filename plus a numeric collision suffix
    (see ``router._unique_upload_path``): guessable for anything with a
    predictable name ("june-council-meeting.mp4"), and ``sha256`` is
    optional in the public payload, so a second anonymous caller who
    guessed it could attach someone else's pending upload to their own
    submission with no proof of anything.

    The fix issues an opaque, unguessable filename at upload time (a
    ``uuid4`` hex token) and returns ONLY that filename -- no directory
    component -- as ``upload_ref``. This is the single place every other
    module resolves that filename back to a real path, joined onto the
    contributor upload directory: no second storage/lookup mechanism, the
    filesystem directory this module already owns via
    :func:`default_contributor_upload_dir` IS the store.

    ``Path.__truediv__`` leaves an anchored (absolute) right-hand operand
    unchanged, so a submission recorded before this fix -- when
    ``upload_ref`` was still a full absolute path -- resolves to the exact
    same file it always did; no data migration needed for a station's
    existing ``contributor-submissions.json``.

    Does NOT itself enforce that the result stays inside ``upload_dir`` --
    callers that treat ``upload_ref`` as an untrusted, freshly-submitted
    client claim (see :func:`_verified_media_reference`) still check with
    ``relative_to`` after calling this.
    """
    base = upload_dir if upload_dir is not None else default_contributor_upload_dir()
    return (base / upload_ref).resolve()


def default_contributor_upload_reap_max_age_hours() -> float:
    """Env-tunable reap age threshold in hours (QA-2), default 48h.

    A file is only ever reaped once it is BOTH older than this window AND
    unreferenced by any submission in any state — see
    :func:`reap_unreferenced_contributor_uploads`.
    """
    raw = os.environ.get("CIVICCAST_CONTRIBUTOR_UPLOAD_MAX_AGE_HOURS", "").strip()
    if not raw:
        return 48.0
    try:
        value = float(raw)
    except ValueError:
        return 48.0
    return value if value > 0 else 48.0


def reap_unreferenced_contributor_uploads(
    store: ContributorSubmissionStore,
    *,
    upload_dir: Path | None = None,
    max_age_hours: float | None = None,
    now: float | None = None,
    byte_budget: ContributorUploadByteBudget | None = None,
) -> list[Path]:
    """Delete stale contributor upload files nothing references (QA-2, Critical).

    The per-request ceiling, the per-IP upload budget, and the aggregate
    directory ceiling all bound how fast the contributor upload directory
    can fill — none of them ever reclaim space, so an anonymous upload sat
    on disk forever even after its submission was declined or published. A
    file is deleted only when BOTH hold:

    * it is older than ``max_age_hours`` (env
      ``CIVICCAST_CONTRIBUTOR_UPLOAD_MAX_AGE_HOURS``, default 48h), and
    * its resolved path is not the ``media.upload_ref`` of ANY submission in
      *any* state (:meth:`ContributorSubmissionStore.referenced_upload_paths`)
      — so a pending contributor's not-yet-submitted-for-review file, or an
      already-accepted/scheduled one, is never touched.

    Returns the list of paths actually deleted.
    """
    resolved_dir = upload_dir if upload_dir is not None else default_contributor_upload_dir()
    if not resolved_dir.exists():
        return []
    age_hours = (
        max_age_hours
        if max_age_hours is not None
        else default_contributor_upload_reap_max_age_hours()
    )
    cutoff = (now if now is not None else time.time()) - (age_hours * 3600)
    referenced = store.referenced_upload_paths()
    deleted: list[Path] = []
    for entry in resolved_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            resolved_path = str(entry.resolve())
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if resolved_path in referenced:
            continue
        if mtime >= cutoff:
            continue
        try:
            entry.unlink()
        except OSError:
            continue
        deleted.append(entry)
        # rc18 re-gate TE-3: return the deleted file's bytes to the address that
        # uploaded it. Without this the in-memory per-IP budget never learns the
        # file is gone, so a reaped uploader stays locked out until restart.
        # Idempotent for files this process never tracked (a previous run's, or
        # a pending contributor's), so it is safe to call for every deletion.
        if byte_budget is not None:
            byte_budget.release_path(resolved_path)
    return deleted


class ContributorUploadReapWorker:
    """Periodic sweep wiring for :func:`reap_unreferenced_contributor_uploads`.

    Rebuilds a fresh :class:`ContributorSubmissionStore` every tick (rather
    than holding one open) so the referenced-path set always reflects
    submissions written since the previous sweep, including by another
    worker thread or process. Same ``run_forever(poll_seconds, stop_event)``
    shape as every other Stage F background worker — see
    ``civiccast.platform.worker_runtime.ThreadSupervisor``.
    """

    def __init__(
        self,
        *,
        store_path: Path | None = None,
        upload_dir: Path | None = None,
        max_age_hours: float | None = None,
        byte_budget: ContributorUploadByteBudget | None = None,
    ) -> None:
        self._store_path = store_path
        self._upload_dir = upload_dir
        self._max_age_hours = max_age_hours
        # rc18 re-gate TE-3: the app's per-app upload byte budget, so each sweep
        # returns a reaped file's bytes to the address that uploaded it. None
        # when the worker runs outside an app (the reap still works; only the
        # in-memory per-IP budget is not credited, which no longer matters
        # because there is no live budget to credit).
        self._byte_budget = byte_budget

    def tick(self) -> list[Path]:
        path = (
            self._store_path if self._store_path is not None else default_contributor_store_path()
        )
        store = ContributorSubmissionStore(path)
        return reap_unreferenced_contributor_uploads(
            store,
            upload_dir=self._upload_dir,
            max_age_hours=self._max_age_hours,
            byte_budget=self._byte_budget,
        )

    def run_forever(self, *, poll_seconds: float, stop_event: threading.Event) -> None:
        """ThreadSupervisor entry point — sweep until stopped.

        The first sweep runs immediately (before the first wait), so wiring
        this worker at app startup also satisfies "reap at startup" without
        a separate one-off call.
        """
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:
                _LOG.exception("contributor upload reap sweep failed")
            stop_event.wait(poll_seconds)


def _verified_media_reference(
    media: SubmissionMediaReference,
) -> SubmissionMediaReference:
    """Recompute the upload's real sha256/size server-side (QA-3, Major).

    ``upload_ref`` and ``sha256`` arrive as claims in client JSON, not proof
    of a real upload — a contributor (or an attacker) can name any path or
    hash. This resolves the ref, requires it to live inside the contributor
    upload directory the station actually writes to, requires it to be a
    real file, and OVERWRITES ``sha256``/``size_bytes`` with values
    recomputed by hashing the file on disk. A client-claimed ``sha256`` that
    disagrees with the recomputed digest is rejected outright rather than
    silently corrected — a mismatch means the client is describing a
    DIFFERENT file than the one its ``upload_ref`` points at.
    """
    upload_dir = default_contributor_upload_dir().resolve()
    try:
        resolved = resolve_contributor_upload_path(media.upload_ref, upload_dir=upload_dir)
    except OSError as exc:
        # The raw OSError text can carry a filesystem path -- never echo it to
        # the anonymous public caller (same class of leak this function
        # exists to close). Detail server-side only.
        _LOG.warning("submission media upload_ref did not resolve: %s", exc)
        raise ValueError("submission media upload_ref is not a usable reference") from exc
    try:
        resolved.relative_to(upload_dir)
    except ValueError as exc:
        raise ValueError(
            "submission media upload_ref must point inside the contributor upload directory"
        ) from exc
    if not resolved.is_file():
        raise ValueError("submission media upload_ref does not reference a real uploaded file")

    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with resolved.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size_bytes += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        _LOG.warning("submission media upload_ref could not be read: %s", exc)
        raise ValueError("submission media upload_ref could not be read") from exc
    recomputed_sha256 = digest.hexdigest()
    if media.sha256 is not None and media.sha256.lower() != recomputed_sha256:
        raise ValueError("submission media sha256 does not match the uploaded file's real digest")
    return media.model_copy(update={"sha256": recomputed_sha256, "size_bytes": size_bytes})


# CRITICAL/MAJOR field evidence: a submission that is already ``scheduled``
# or ``published`` is a REAL, live booking on the schedule -- reverting it
# to ``under_review``/``needs_changes`` (or double-accepting/double-
# scheduling it) leaves that real booking dangling while the contributor
# portal and operator queue disagree about what state it is in. ``decline``
# is deliberately NOT blocked at ``scheduled`` here -- that is exactly the
# state the BLOCKER fix (pulling the real schedule item, see
# civiccast.contribute.router._cancel_schedule_item_for_decline) needs to
# reach; it IS blocked at ``published`` because a recording that has
# already aired/gone public cannot be un-published by a decline.
_LOCKED_FOR_REVERSION_STATES = frozenset({"scheduled", "published"})


def _ensure_review_action_allowed(submission: ContributorSubmission, action: ReviewAction) -> None:
    """Guard a review ``action`` against the submission's CURRENT state.

    Raises :class:`ContributorReviewConflictError` when ``action`` is not
    valid from ``submission.state``. Called from two places, both inside
    ``ContributorSubmissionStore._lock``, so the check is atomic with either
    the reservation (:meth:`ContributorSubmissionStore.reserve_review_action`)
    or the actual persisted mutation (:func:`_apply_review`) -- never a
    read that a concurrent request can invalidate before it is acted on.
    """
    state = submission.state
    if action == "accept":
        if state not in {"submitted", "under_review", "needs_changes"}:
            raise ContributorReviewConflictError(f"Cannot accept a submission in state {state!r}.")
    elif action == "schedule":
        if state in _LOCKED_FOR_REVERSION_STATES:
            raise ContributorReviewConflictError(
                "This submission has already been sent to the schedule."
            )
        if state != "accepted":
            raise ContributorReviewConflictError(
                "This submission has not been ingested into the asset library yet. "
                "Accept it before sending it to the schedule."
            )
    elif action in {"mark_under_review", "request_changes"}:
        if state in _LOCKED_FOR_REVERSION_STATES:
            raise ContributorReviewConflictError(
                f"Cannot move a {state!r} submission back into review -- it is already "
                "a live booking on the schedule."
            )
    elif action == "decline" and state == "published":
        raise ContributorReviewConflictError(
            "Cannot decline a submission that has already been published."
        )


def _apply_review(
    submission: ContributorSubmission,
    request: ContributorReviewRequest,
    *,
    ingested_asset_id: str | None = None,
    created_schedule_item_id: str | None = None,
) -> ContributorSubmission:
    _ensure_review_action_allowed(submission, request.action)
    values = submission.model_dump()
    if request.metadata_patch is not None:
        values.update(request.metadata_patch.model_dump(exclude_none=True))
    if request.broken_media_gate is not None:
        values["broken_media_gate"] = request.broken_media_gate.model_dump()
    if request.operator_notes is not None:
        values["operator_notes"] = request.operator_notes
    values["updated_at"] = utc_now()
    values["status_history"] = [
        *submission.status_history,
        f"{request.action}: operator review updated submission",
    ]

    if request.action == "mark_under_review":
        values["state"] = "under_review"
    elif request.action == "request_changes":
        values["state"] = "needs_changes"
    elif request.action == "decline":
        values["state"] = "declined"
        values["decline_reason"] = request.decline_reason
    elif request.action == "accept":
        gate = request.broken_media_gate or submission.broken_media_gate
        if gate.state not in {"passed", "override_accepted"}:
            raise ValueError(
                "accepted submissions require a passed or operator-overridden media gate"
            )
        # TASK A: acceptance is only real once the contributor's file has
        # actually been ingested into the asset library -- the router runs
        # that ingest (reusing the same ffprobe pipeline the staff upload
        # and watch-folder paths use) BEFORE calling here and passes the
        # resulting asset_id. Without a real asset_id, "accepted" would be
        # exactly the false promise the field evidence flagged: a state
        # flip with no airable media behind it.
        if ingested_asset_id is None:
            raise ValueError(
                "accepted submissions require the contributor's media to be ingested "
                "into the asset library first (no asset_id was produced)"
            )
        values["state"] = "accepted"
        values["asset_id"] = ingested_asset_id
    elif request.action == "schedule":
        gate = request.broken_media_gate or submission.broken_media_gate
        if gate.state not in {"passed", "override_accepted"}:
            raise ValueError(
                "scheduled submissions require a passed or operator-overridden media gate"
            )
        if submission.asset_id is None:
            raise ValueError(
                "submissions must be accepted (ingested into the asset library) before "
                "they can be sent to the schedule"
            )
        # TASK A: schedule_item_id must never come back null on a success
        # response -- the router creates a real civiccast.schedule_items row
        # BEFORE calling here and passes back its id. A missing id is a bug,
        # not a valid "scheduled" outcome, so this refuses to fabricate one.
        if created_schedule_item_id is None:
            raise ValueError(
                "send-to-schedule requires a real schedule item to be created first "
                "(no schedule_item_id was produced)"
            )
        assert request.schedule_handoff is not None  # enforced by the request validator
        values["state"] = "scheduled"
        values["schedule_handoff"] = {
            **request.schedule_handoff.model_dump(),
            "schedule_item_id": created_schedule_item_id,
        }

    next_state = cast(ContributorSubmissionState, values["state"])
    if next_state != submission.state:
        values["notifications_sent"] = [
            *submission.notifications_sent,
            *_status_notifications(
                submission.notifications,
                state=next_state,
                queued_at=values["updated_at"],
            ),
        ]

    return ContributorSubmission.model_validate(values)


def _unique_submission_id(
    title: str,
    existing: dict[str, ContributorSubmission],
) -> str:
    base = "".join(char.lower() if char.isalnum() else "-" for char in title).strip("-")
    base = "-".join(part for part in base.split("-") if part)[:80] or "submission"
    candidate = base
    suffix = 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _status_message(state: str) -> str:
    messages = {
        "submitted": "Your program has been received and is waiting for operator review.",
        "under_review": "An operator is reviewing your program.",
        "needs_changes": "The operator needs changes before this program can move forward.",
        "accepted": "Your program has been accepted and is waiting for scheduling.",
        "declined": "Your program was declined by the operator.",
        # TASK B: "handed off to the schedule" used to be shown while
        # schedule_item_id was null and no civiccast.schedule_items row
        # existed -- an outright false promise (field evidence candidate
        # #17). By the time this state is ever reached now, the router has
        # already created a real schedule item (see
        # civiccast.contribute.router._create_real_schedule_item), so this
        # message describes something that is actually true: the program is
        # a real, airable entry on the schedule, not a placeholder.
        "scheduled": "Your program has a real spot on the schedule and will air automatically.",
        "published": "Your program has been published.",
    }
    return messages.get(state, "Submission status updated.")


def _status_notifications(
    preferences: list[SubmissionNotificationPreference],
    *,
    state: ContributorSubmissionState,
    queued_at: datetime,
) -> list[SubmissionStatusNotification]:
    return [
        SubmissionStatusNotification(
            kind=preference.kind,
            target=preference.target,
            state=state,
            queued_at=queued_at,
            message=_status_message(state),
        )
        for preference in preferences
        if preference.enabled
    ]
