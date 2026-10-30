# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Fail-closed retention policy for local caption evidence.

Discovery here is EXPENSIVE and therefore never runs on a channel-start path:
MEASURED LIVE 2026-09-24 (installed station) one ``enforce_discovered`` over
15,045 processed public chunks took 60.5 s -- a ``resolve()``, a ``stat()``, a
``_probe_wav_duration()`` and a full-read ``_sha256()`` per chunk -- and the
egress readiness gate ran it synchronously on the automation thread, holding
every other channel's start behind it (the automation watchdog fires at 30 s).
The verdict is now produced by :class:`CaptionRetentionVerdictSource` on a
background thread and the start path only READS it; see that class for the
freshness/pending contract, and ``check_storage_divergence`` for the one
refusal that stays synchronous (it costs three syscalls, not a scan).

A sweep still runs every ``RETENTION_SWEEP_SECONDS`` forever, so its STEADY
STATE cost matters as much as its latency: MEASURED 2026-09-24 on a synthetic
9,996-file archive shaped like the live station (5,000 chunks at the live
160,044-byte size, 4,998 matching evidence WAVs) one warm sweep took
11.3-12.1 s, of which SHA-256 was 6.8 s and the WAV duration 1.0 s -- both of
them pure functions of file CONTENT that the tap never rewrites.  They are
reused while a file's size and mtime are unchanged
(:class:`_ReusableFileFacts`), which takes the steady state to roughly the
cost of stat-ing the archive.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
import wave
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from stat import S_ISREG
from typing import cast

from civiccast.captions.review import CaptionReviewAudioEvidence, CaptionReviewStore
from civiccast.captions.review_media import (
    CaptionReviewClipError,
    verify_caption_review_audio_evidence,
)

_LOG = logging.getLogger(__name__)

_RAW_CHUNK_MAX_AGE = timedelta(hours=24)
_RESOLVED_EVIDENCE_MAX_AGE = timedelta(days=90)
_AUDIT_LOCKS: dict[Path, threading.RLock] = {}
_AUDIT_LOCKS_GUARD = threading.Lock()

#: How often a retention sweep may run, independent of the scan interval.
#: ``enforce_discovered`` lists review rows from the database and SHA-256s
#: every chunk it considers; at the 2-second scan cadence that is a database
#: query and a pass over the recorded audio 30 times a minute, forever, to
#: enforce a schedule measured in days. Retention correctness does not depend
#: on the interval -- only on running often enough that the schedule is
#: honoured -- so it gets its own, much slower clock. The first scan after
#: startup always sweeps. ONE number for both sweeps in a process: the caption
#: tap's own sweep and the egress readiness sweep below.
RETENTION_SWEEP_SECONDS = 60.0

#: How old the shared readiness verdict may be before a channel start stops
#: trusting it: three sweep intervals, so a start after two consecutive
#: cadences still gets a real verdict rather than the pending answer.
RETENTION_VERDICT_FRESHNESS_SECONDS = 3 * RETENTION_SWEEP_SECONDS

#: One full discovery at a time, PROCESS-wide. Two sweeps of this archive can
#: otherwise overlap and SHA-256 the same bytes concurrently -- the caption
#: tap's own sweep (``CaptionTapWorker._run_retention_sweep``) and
#: :class:`CaptionRetentionVerdictSource`'s -- which would double the disk load
#: on a station whose whole defect is disk contention. Held around the heavy
#: half only: a divergence refusal never queues behind a running scan.
_DISCOVERY_LOCK = threading.RLock()


@dataclass(frozen=True)
class CaptionRetentionResult:
    """The deletion receipt and the readiness decision consumed by egress."""

    ready: bool
    refusal_reason: str | None
    requires_fallback_slate: bool
    deleted_paths: tuple[Path, ...] = ()
    protected_paths: tuple[Path, ...] = ()
    audit_records: tuple[dict[str, object], ...] = ()


#: What a channel start sees when no fresh verdict exists. NOT a refusal --
#: the program starts, and WAV retention stays off through the caption tap's
#: own pending rule (``ReviewPersistenceMode`` ``"text-only"``,
#: ``civiccast/captions/tap_worker.py::_review_persistence_guard``), which
#: reads the tap's state and never this value. Shared: the dataclass is frozen.
_PENDING_VERDICT = CaptionRetentionResult(
    ready=True,
    refusal_reason=None,
    requires_fallback_slate=False,
)


@dataclass(frozen=True)
class _Verdict:
    """One published verdict and the monotonic instant it was published."""

    result: CaptionRetentionResult
    published_at: float


@dataclass(frozen=True)
class _Candidate:
    path: Path
    kind: str
    review_status: str
    low_confidence: bool
    created_at: datetime
    resolved_at: datetime | None
    sha256: str
    bytes: int
    derived_evidence_verified: bool
    evidence_pending: bool = True


class _ReusableFileFacts:
    """One policy's reuse of a file's digest and WAV duration while it is unchanged.

    MEASURED 2026-09-24 (synthetic 9,996-file archive shaped like the live
    station: 5,000 chunks at the live measured 160,044-byte size plus 4,998
    matching evidence WAVs, every phase of ``_discover_candidates``
    instrumented): a warm steady-state sweep cost 11.3-12.1 s, of which SHA-256
    was 6.8 s and the WAV duration 1.0 s.  Both are pure functions of a file's
    CONTENT, and the files under them are write-once -- the tap parks a settled
    chunk in ``processed/`` and the evidence writer does an ``os.replace`` --
    so re-deriving them on every sweep is a full read of the recorded audio
    once a minute, forever, on the volume whose contention is the standing
    defect (the whole sweep measured 60.5 s live over 21,153 files).

    The key is ``(size, mtime_ns)``, and it is checked on EVERY use: a digest is
    reused only while both are unchanged, so any ordinary write -- which moves
    mtime -- is re-read.  What this is NOT is a tamper detector, and
    ``_discover_candidates`` deliberately does not use it for the one digest
    that is: ``review_media.verify_caption_review_audio_evidence`` compares a
    fresh digest against the digest recorded in the review row, which is how an
    operator's approval of a low-confidence cue is blocked when the retained
    audio no longer matches the cue.  That read stays a read of the CURRENT
    bytes, once per sweep per file (``_verify_evidence_once``).

    Bounded by construction: ``begin_pass``/``end_pass`` wrap one discovery and
    a pass DROPS every entry whose file it did not see, so a deleted file's
    digest can never be served to a different file that later reuses its path.
    An entry is only written after a successful read, so a transient failure (a
    locked file, a partial write) is retried next sweep rather than cached as an
    answer.

    One instance per policy object: every caller of the same policy shares it,
    and a new policy starts cold.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._digests: dict[Path, tuple[int, int, str]] = {}
        self._durations: dict[Path, tuple[int, int, float]] = {}
        self._visited: set[Path] | None = None

    def begin_pass(self) -> None:
        with self._lock:
            self._visited = set()

    def end_pass(self) -> None:
        with self._lock:
            visited = self._visited
            self._visited = None
            if visited is None:
                return
            self._digests = {
                path: entry for path, entry in self._digests.items() if path in visited
            }
            self._durations = {
                path: entry for path, entry in self._durations.items() if path in visited
            }

    def digest(self, path: Path, stat_result: os.stat_result) -> str:
        key = (stat_result.st_size, stat_result.st_mtime_ns)
        with self._lock:
            self._note(path)
            cached = self._digests.get(path)
        if cached is not None and cached[:2] == key:
            return cached[2]
        value = _sha256(path)
        with self._lock:
            self._digests[path] = (key[0], key[1], value)
        return value

    def duration(self, path: Path, stat_result: os.stat_result) -> float:
        key = (stat_result.st_size, stat_result.st_mtime_ns)
        with self._lock:
            self._note(path)
            cached = self._durations.get(path)
        if cached is not None and cached[:2] == key:
            return cached[2]
        probed = _probe_wav_duration(path)
        if probed is None:
            # Unreadable right now. Cache nothing: the file did not give an
            # answer, and a stale 0.0 would silently change coverage math for
            # as long as the file stays unreadable.
            return 0.0
        with self._lock:
            self._durations[path] = (key[0], key[1], probed)
        return probed

    def _note(self, path: Path) -> None:
        """Record that this pass saw ``path``, so ``end_pass`` keeps its entry."""

        if self._visited is not None:
            self._visited.add(path)


class CaptionEvidenceRetentionPolicy:
    """Apply the owner-approved lifecycle without deleting protected evidence."""

    raw_chunk_max_age = _RAW_CHUNK_MAX_AGE
    resolved_evidence_max_age = _RESOLVED_EVIDENCE_MAX_AGE

    def __init__(
        self,
        *,
        volume_bytes: int,
        free_bytes: int,
        audit_path: Path | None = None,
        storage_root: Path | None = None,
    ) -> None:
        self.volume_bytes = volume_bytes
        self.free_bytes = free_bytes
        self._facts = _ReusableFileFacts()
        self._audit_path = audit_path.expanduser().resolve() if audit_path is not None else None
        self._storage_root = (
            storage_root.expanduser().resolve()
            if storage_root is not None
            else self._audit_path.parent
            if self._audit_path is not None
            else None
        )

    @classmethod
    def from_system(
        cls, *, storage_root: Path, audit_path: Path | None = None
    ) -> CaptionEvidenceRetentionPolicy:
        """Construct from the actual volume that holds the retained evidence."""

        root = storage_root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(root)
        return cls(
            volume_bytes=usage.total,
            free_bytes=usage.free,
            audit_path=audit_path or root / "caption-retention-audit.jsonl",
            storage_root=root,
        )

    def enforce(
        self,
        *,
        candidates: Iterable[Mapping[str, object]],
        now: datetime | None = None,
    ) -> CaptionRetentionResult:
        """Prune verified/expired evidence by age; no volume-relative space refusal."""

        observed_at = _utc(now or datetime.now(UTC))
        normalized = tuple(_normalize_candidate(candidate) for candidate in candidates)
        protected = tuple(
            candidate.path
            for candidate in normalized
            if candidate.kind == "review-evidence"
            and candidate.low_confidence
            and candidate.review_status == "pending"
        )
        eligible = sorted(
            (candidate for candidate in normalized if _is_eligible(candidate, observed_at)),
            key=lambda candidate: (
                0 if candidate.kind == "review-evidence" else 1,
                _utc(candidate.resolved_at or candidate.created_at),
                str(candidate.path),
            ),
        )
        deleted: list[Path] = []
        records: list[dict[str, object]] = []
        for candidate in eligible:
            if not candidate.path.is_file():
                continue
            try:
                candidate.path.unlink()
            except OSError:
                continue
            deleted.append(candidate.path)
            records.append(
                {
                    "outcome": "pruned",
                    "reason": _prune_reason(candidate),
                    "path": str(candidate.path),
                    "sha256": candidate.sha256,
                }
            )

        self._append_audit_records(records)
        # The volume-relative storage caps (the max-storage ceiling and the
        # free-space reserve) were removed by owner decision 2026-09-20: caption
        # evidence is small text/audio, and a station must not stop captioning
        # because the drive is full. Pruning is now purely age-based; a full or
        # near-full volume is no longer a refusal condition here. The
        # cross-volume misconfiguration check (``caption-storage-volumes-diverge``
        # in ``enforce_discovered``) still refuses readiness -- that is a real
        # routing hazard, not a space panic.
        return CaptionRetentionResult(
            ready=True,
            refusal_reason=None,
            requires_fallback_slate=False,
            deleted_paths=tuple(deleted),
            protected_paths=protected,
            audit_records=tuple(records),
        )

    def check_storage_divergence(self, *, tap_root: Path | None) -> CaptionRetentionResult | None:
        """The one refusal this policy still makes, and it is CHEAP.

        A tap directory and an evidence root on different volumes is a real
        routing hazard, so it refuses readiness -- but it is three syscalls
        (``resolve``, ``stat``, ``stat``), not a scan. It is deliberately
        separate from the heavy half: the egress start path runs this
        synchronously and must never have to wait for a running discovery.
        Returns ``None`` when the two are on the same volume.
        """

        if (
            tap_root is not None
            and tap_root.is_dir()
            and self._storage_root is not None
            and self._storage_root.is_dir()
            and tap_root.resolve().stat().st_dev != self._storage_root.stat().st_dev
        ):
            # dict[str, object] explicitly: inferred from this all-str literal
            # it would be dict[str, str], which _append_audit_records and
            # CaptionRetentionResult.audit_records both reject -- dict is
            # invariant, so a narrower value type is not a subtype.
            record: dict[str, object] = {
                "outcome": "storage-refused",
                "reason": "caption-storage-volumes-diverge",
                "path": "",
                "sha256": "",
            }
            self._append_audit_records((record,))
            return CaptionRetentionResult(
                ready=False,
                refusal_reason="caption-storage-volumes-diverge",
                requires_fallback_slate=True,
                audit_records=(record,),
            )
        return None

    def enforce_discovered(
        self,
        *,
        tap_root: Path | None,
        review_store: CaptionReviewStore,
        segment_seconds: float,
    ) -> CaptionRetentionResult:
        """Classify real persisted review/evidence state on the local volume."""

        self._refresh_system_capacity()
        divergence = self.check_storage_divergence(tap_root=tap_root)
        if divergence is not None:
            return divergence
        # Serialized across the whole process: see _DISCOVERY_LOCK. Everything
        # below here walks and hashes the archive.
        with _DISCOVERY_LOCK:
            candidates = self._discover_candidates(
                tap_root=tap_root,
                review_store=review_store,
                segment_seconds=segment_seconds,
            )
            return self.enforce(candidates=candidates)

    def _refresh_system_capacity(self) -> None:
        if self._storage_root is None:
            return
        usage = shutil.disk_usage(self._storage_root)
        self.volume_bytes = usage.total
        self.free_bytes = usage.free

    def record_event(self, *, outcome: str, reason: str, path: Path, sha256: str) -> None:
        """Record non-destructive retention decisions such as segment collisions."""

        self._append_audit_records(
            (
                {
                    "outcome": outcome,
                    "reason": reason,
                    "path": str(path.expanduser().resolve()),
                    "sha256": sha256,
                },
            )
        )

    def _discover_candidates(
        self,
        *,
        tap_root: Path | None,
        review_store: CaptionReviewStore,
        segment_seconds: float,
    ) -> list[dict[str, object]]:
        evidence_by_path: dict[Path, dict[str, object]] = {}
        verified_windows: dict[str, list[tuple[float, float]]] = {}
        #: Evidence verification for THIS pass only, keyed by the row's recorded
        #: identity for the file. See _verify_evidence_once.
        checks: dict[tuple[str, str, int], Path | None] = {}
        bulk_reader = getattr(review_store, "list_with_audio_evidence", None)
        if callable(bulk_reader):
            review_items = bulk_reader()
        else:
            # Compatibility for small third-party/test stores that implement
            # only the original protocol. The durable production store has the
            # bulk reader above, so its startup path is one DB query, not N+1.
            review_items = [
                (item, review_store.get_audio_evidence(item.review_item_id))
                for item in review_store.list()
            ]
        # Everything below the pass marker reuses digests and durations for
        # files it has already seen unchanged; end_pass drops what it did not.
        self._facts.begin_pass()
        try:
            for item, evidence in review_items:
                if evidence is None:
                    continue
                evidence_path = self._verify_evidence_once(evidence, checks)
                if evidence_path is None:
                    continue
                duration = self._file_duration(evidence_path)
                item_resolved_at = item.updated_at if item.status != "pending" else None
                candidate = evidence_by_path.get(evidence_path)
                if candidate is None:
                    candidate = {
                        "path": evidence_path,
                        "kind": "review-evidence",
                        "review_status": item.status,
                        "resolved_at": item_resolved_at,
                        "created_at": item.created_at,
                        "sha256": evidence.source_sha256,
                        "bytes": evidence.source_bytes,
                        "low_confidence": item.low_confidence,
                    }
                    evidence_by_path[evidence_path] = candidate
                else:
                    candidate["low_confidence"] = (
                        bool(candidate["low_confidence"]) or item.low_confidence
                    )
                    if item.status == "pending":
                        candidate["review_status"] = "pending"
                        candidate["resolved_at"] = None
                    elif candidate["review_status"] != "pending":
                        # One offline ASR chunk (or one live tap segment) can
                        # attach the SAME evidence WAV to several cues
                        # (_offline_audio_evidence_factory /
                        # CaptionTapWorker._audio_evidence_factory each write
                        # the file once and reuse it across every cue from that
                        # window). Coalescing by path must not let an early
                        # decision on one of those cues start the 90-day clock
                        # while a later cue sharing the same file is still
                        # unresolved-at-merge-time or was resolved after it
                        # (audit finding, P2): keep the LATEST resolution
                        # timestamp seen across every row sharing this path,
                        # not the first one processed -- order-independent,
                        # since every row is visited regardless of
                        # review_store.list()'s ordering.
                        existing_resolved_at = cast("datetime | None", candidate["resolved_at"])
                        if item_resolved_at is not None and (
                            existing_resolved_at is None or item_resolved_at > existing_resolved_at
                        ):
                            candidate["resolved_at"] = item_resolved_at
                verified_windows.setdefault(item.asset_id, []).append(
                    (evidence.source_start_seconds, evidence.source_start_seconds + duration)
                )

            candidates = list(evidence_by_path.values())
            known_evidence_paths = set(evidence_by_path)
            if self._storage_root is not None and self._storage_root.is_dir():
                for evidence_path in sorted(self._storage_root.glob("*/captions/evidence/*.wav")):
                    resolved_path = evidence_path.resolve()
                    if resolved_path in known_evidence_paths:
                        continue
                    stats = _stat_regular_file(resolved_path)
                    if stats is None:
                        continue
                    candidates.append(
                        {
                            "path": resolved_path,
                            "kind": "unclassified-evidence",
                            "review_status": "pending",
                            "resolved_at": None,
                            "created_at": datetime.fromtimestamp(stats.st_mtime, UTC),
                            "sha256": self._file_digest(resolved_path, stats),
                            "bytes": stats.st_size,
                            "low_confidence": False,
                        }
                    )
            if tap_root is None or not tap_root.is_dir():
                return candidates
            for channel_dir in sorted(path for path in tap_root.iterdir() if path.is_dir()):
                for raw_path in sorted((channel_dir / "processed").glob("chunk-*.wav")):
                    index = _chunk_index(raw_path)
                    if index is None:
                        continue
                    stats = _stat_regular_file(raw_path)
                    if stats is None:
                        continue
                    start = index * segment_seconds
                    duration = self._file_duration(raw_path, stats)
                    windows = verified_windows.get(channel_dir.name, ())
                    covering = tuple(
                        (evidence_start, evidence_end)
                        for evidence_start, evidence_end in windows
                        if evidence_start <= start and start + duration <= evidence_end
                    )
                    verified = bool(covering)
                    candidates.append(
                        {
                            "path": raw_path.resolve(),
                            "kind": "raw-chunk",
                            "review_status": "pending",
                            "resolved_at": None,
                            "created_at": datetime.fromtimestamp(stats.st_mtime, UTC),
                            "sha256": self._file_digest(raw_path, stats),
                            "bytes": stats.st_size,
                            "low_confidence": False,
                            "derived_evidence_verified": verified,
                            # Evidence for this window is still expected whenever a
                            # review row can still cover it.  A chunk with NO covering
                            # evidence window at all can never become verified, so it
                            # is retired on the age cap instead of being retained
                            # forever.  MEASURED LIVE 2026-09-17 (Blackwell station):
                            # public/processed reached 12,980 files / 1,980.8 MB
                            # (indices 0..13,857) while the retention audit's last
                            # prune was index 569 -- 12,737 permanently unprunable
                            # chunks, which then trips the max-2 backlog gate and
                            # pauses live captions for 120 s on every start.
                            "evidence_pending": bool(windows),
                        }
                    )
            return candidates
        finally:
            self._facts.end_pass()

    def _verify_evidence_once(
        self,
        evidence: CaptionReviewAudioEvidence,
        checks: dict[tuple[str, str, int], Path | None],
    ) -> Path | None:
        """``verify_caption_review_audio_evidence``, at most once per file per sweep.

        That call is a TAMPER CHECK -- it hashes the file's current bytes and
        compares them with the digest recorded when the evidence was written --
        so its result is deliberately not reused across sweeps (a digest cache
        keyed on size and mtime cannot see a same-size, same-mtime
        substitution; see :class:`_ReusableFileFacts`).  What it is not allowed
        to do is read the SAME file once per review row that shares it: one tap
        segment or one offline ASR window writes one evidence WAV and attaches
        it to every cue from that window, so a station with k cues per window
        was hashing that window's audio k times per sweep.

        The memo key carries the row's RECORDED identity (path, digest, size)
        as well as the file, so two rows that disagree about what the file
        should be each get their own check and the stale one is still skipped.
        Scope is one sweep, so a change is always seen by the next one.
        """

        key = (evidence.source_path, evidence.source_sha256, evidence.source_bytes)
        if key in checks:
            return checks[key]
        try:
            verified: Path | None = verify_caption_review_audio_evidence(evidence)
        except CaptionReviewClipError:
            verified = None
        checks[key] = verified
        return verified

    def _file_digest(self, path: Path, stat_result: os.stat_result | None = None) -> str:
        """``_sha256``, reused while the file's size and mtime are unchanged."""

        if stat_result is None:
            stat_result = path.stat()
        return self._facts.digest(path, stat_result)

    def _file_duration(self, path: Path, stat_result: os.stat_result | None = None) -> float:
        """``_wav_duration``, reused while the file's size and mtime are unchanged."""

        try:
            if stat_result is None:
                stat_result = path.stat()
        except OSError:
            # The historical contract of _wav_duration: an unreadable file is
            # 0.0 seconds, not an exception.
            return 0.0
        return self._facts.duration(path, stat_result)

    def _append_audit_records(self, records: Iterable[dict[str, object]]) -> None:
        if self._audit_path is None:
            return
        payload = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
        if not payload:
            return
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        lock = _audit_lock(self._audit_path)
        with lock:
            descriptor = os.open(self._audit_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY)
            try:
                os.write(descriptor, payload.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


class CaptionRetentionVerdictSource:
    """The one retention verdict per process, produced OFF the start path.

    The egress daemon calls this (``(channel_id) -> CaptionRetentionResult``)
    before selecting a program source. What it must NOT do is the work: MEASURED
    LIVE 2026-09-24, that call ran a 60.5 s discovery over 15,045 public chunks
    inline on the automation thread under the daemon-wide ``_preparation_guard``,
    so every other channel's start waited behind one channel's scan and missed
    its own handover. So the answer is:

    * a FRESH verdict -- one published by the background sweep within
      ``freshness_seconds`` -- is returned as-is, refusals included, and a
      refusal still publishes the ``storage-refused`` sidecar exactly as the
      synchronous path always did;
    * with NO verdict, or a STALE one, the sweep is dispatched (at most one in
      flight, never more often than the shared cadence) and the caller gets
      :data:`_PENDING_VERDICT` immediately. Pending is not a refusal: the
      program starts, and WAV retention stays off because the caption tap
      applies its own pending rule (:class:`ReviewPersistenceMode`
      ``"text-only"``). Nothing here reads or sets the tap's state.

    Thread safety: the verdict slot, the in-flight flag and the cadence stamp
    live behind one lock; the sweep runs on a daemon thread and NEVER takes a
    daemon lock, so a start can be blocked by neither a slow scan nor a wedged
    one. The heavy discovery is serialized process-wide by
    :data:`_DISCOVERY_LOCK`, so this sweep and the caption tap's own sweep can
    never hash the archive at the same time.

    The verdict is kept fresh by its OWN cadence, not by starts: the first
    ``__call__`` arms a daemon refresh thread that dispatches a sweep every
    ``sweep_interval_seconds`` from then on (``_arm_background_refresh``).
    As first built, a sweep was dispatched only when a start found no fresh
    verdict -- and channel starts are normally further apart than one interval,
    so nearly every start found a stale verdict, returned "pending", and
    dispatched the scan it would have dispatched anyway. The consequence was
    that a background refusal could essentially never gate a start: a refusal
    published 60 s after the last start was already past
    ``RETENTION_VERDICT_FRESHNESS_SECONDS`` (180 s) by the time the next start
    arrived. Arming on the first call rather than in ``__init__`` keeps
    "building the provider starts no thread" true.
    """

    def __init__(
        self,
        *,
        policy: CaptionEvidenceRetentionPolicy,
        tap_root: Path | None,
        review_store: CaptionReviewStore,
        segment_seconds: float,
        storage_root: Path,
        monotonic: Callable[[], float] = time.monotonic,
        freshness_seconds: float = RETENTION_VERDICT_FRESHNESS_SECONDS,
        sweep_interval_seconds: float = RETENTION_SWEEP_SECONDS,
    ) -> None:
        self.policy = policy
        self._tap_root = tap_root
        self._review_store = review_store
        self._segment_seconds = segment_seconds
        self._storage_root = storage_root
        self._monotonic = monotonic
        self._freshness_seconds = freshness_seconds
        self._sweep_interval_seconds = sweep_interval_seconds
        self._lock = threading.RLock()
        self._verdict: _Verdict | None = None
        self._in_flight = False
        self._last_sweep_started_at: float | None = None
        self._thread: threading.Thread | None = None
        self._refresh_thread: threading.Thread | None = None
        self._stopped = threading.Event()
        #: Sweeps dispatched, for tests and for the report's sweeps-per-minute.
        self._sweeps_started = 0

    def __call__(self, channel_id: str) -> CaptionRetentionResult:
        # 1. Cheap, synchronous, unchanged: the divergence refusal. Everything
        #    below this line is a read of a value somebody else computed.
        divergence = self.policy.check_storage_divergence(tap_root=self._tap_root)
        if divergence is not None:
            self._publish_refusal(channel_id, divergence)
            return divergence
        # 2. From here on this provider keeps its own verdict fresh; a start is
        #    no longer the only thing that re-arms a sweep.
        self._arm_background_refresh()
        # 3. A fresh verdict is the real answer.
        verdict = self._fresh_verdict()
        if verdict is not None:
            if not verdict.ready:
                self._publish_refusal(channel_id, verdict)
            return verdict
        # 4. No fresh verdict: dispatch the sweep and start anyway. ONE line
        #    per start, so a station that is permanently pending is visible.
        self._dispatch_sweep()
        with self._lock:
            in_flight = self._in_flight
            age = None if self._verdict is None else self._monotonic() - self._verdict.published_at
        _LOG.info(
            "channel %s: caption retention verdict pending (%s); start proceeds, "
            "divergence check passed; background sweep in flight=%s",
            channel_id,
            "none yet" if age is None else f"stale {age:.0f}s",
            in_flight,
        )
        return _PENDING_VERDICT

    def stop(self, timeout: float = 5.0) -> bool:
        """End the background refresh. Returns True when no refresh thread remains.

        Production never calls this -- the provider lives as long as the
        process. It exists so a test (or an orderly shutdown) can prove that
        the thread named ``civiccast-caption-readiness-retention-timer`` ends,
        and so no test leaks a timer into the next one. An in-flight sweep is
        joined too, bounded by ``timeout``; one that outruns the timeout is
        left running, which is harmless -- it publishes its verdict and exits.
        """

        self._stopped.set()
        with self._lock:
            refresh, sweep = self._refresh_thread, self._thread
        current = threading.current_thread()
        for thread in (refresh, sweep):
            if thread is not None and thread is not current:
                thread.join(timeout)
        return refresh is None or not refresh.is_alive()

    def wait_for_sweep(self, timeout: float = 30.0) -> bool:
        """Block until any in-flight background sweep finishes. Returns True if idle.

        Production never calls this -- a start must not wait for a sweep. It
        exists so tests can observe the sweep deterministically instead of
        racing it, exactly like ``CaptionTapWorker.wait_for_retention_sweep``.
        """

        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _arm_background_refresh(self) -> None:
        """Start the self-scheduling refresh thread, once, on first use."""

        with self._lock:
            if self._refresh_thread is not None:
                return
            thread = threading.Thread(
                target=self._refresh_loop,
                name="civiccast-caption-readiness-retention-timer",
                daemon=True,
            )
            self._refresh_thread = thread
        thread.start()

    def _refresh_loop(self) -> None:
        """Dispatch a sweep every cadence until ``stop()``. Never exits by raising."""

        while not self._stopped.wait(self._sweep_interval_seconds):
            try:
                self._dispatch_sweep()
            except Exception:
                # The loop outlives any single sweep: a bad cadence must not
                # silently end the freshness guarantee for the rest of the
                # process's life.
                _LOG.exception(
                    "Caption retention refresh could not dispatch a sweep; "
                    "the refresh continues on the next cadence."
                )

    def _fresh_verdict(self) -> CaptionRetentionResult | None:
        with self._lock:
            verdict = self._verdict
            if verdict is None:
                return None
            if self._monotonic() - verdict.published_at > self._freshness_seconds:
                return None
            return verdict.result

    def _dispatch_sweep(self) -> None:
        """Start a background sweep unless one is in flight or the cadence forbids it."""

        with self._lock:
            if self._in_flight:
                return
            now = self._monotonic()
            if (
                self._last_sweep_started_at is not None
                and now - self._last_sweep_started_at < self._sweep_interval_seconds
            ):
                # Same cadence rule as the tap worker's sweep: a start never
                # stacks overlapping scans onto the disk. A sweep that FAILED
                # (so published no verdict) is therefore retried on the next
                # cadence, not on every start.
                return
            self._in_flight = True
            self._last_sweep_started_at = now
            self._sweeps_started += 1
            thread = threading.Thread(
                target=self._run_sweep,
                name="civiccast-caption-readiness-retention",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def _run_sweep(self) -> None:
        """The blocking half, on the sweep thread. Never raises to the caller."""

        try:
            result = self.policy.enforce_discovered(
                tap_root=self._tap_root,
                review_store=self._review_store,
                segment_seconds=self._segment_seconds,
            )
        except Exception:
            # "Cannot verify" produced no verdict, and it must NOT become a
            # refusal here: a start is answered by the pending verdict either
            # way, and inventing a refusal for a storage error would take a
            # station off the air over a transient database fault. The previous
            # verdict therefore stands until it ages out of the freshness
            # window. The SAFETY half is unaffected -- WAV retention is the
            # caption tap's own fail-closed decision, and a tap whose sweep
            # raises still publishes no ASR evidence.
            _LOG.exception(
                "Caption retention background sweep failed; the previous verdict stands."
            )
            with self._lock:
                self._in_flight = False
            return
        with self._lock:
            self._verdict = _Verdict(result=result, published_at=self._monotonic())
            self._in_flight = False
        if result.ready:
            return
        # A background refusal has no channel in hand, so it is published for
        # every channel that is publishing caption status -- the same payload
        # the synchronous path writes for the channel it is starting. Cleared by
        # the CHANNEL SET, not by tap-directory presence, matching
        # ``reset_existing_live_sidecars``: a channel whose tap directory was
        # already swept can still be serving a stale sidecar.
        for channel_id in self._published_channel_ids():
            self._publish_refusal(channel_id, result)

    def _published_channel_ids(self) -> tuple[str, ...]:
        root = self._storage_root
        if not root.is_dir():
            return ()
        return tuple(
            sorted(path.parent.parent.name for path in root.glob("*/captions/runtime-status.json"))
        )

    def _publish_refusal(self, channel_id: str, result: CaptionRetentionResult) -> None:
        # The egress gate can run before the next tap-worker poll. Clear the
        # published sidecar at this same refusal boundary, never after a
        # program encoder has already been allowed to start.
        from civiccast.captions.live_sidecar import publish_caption_runtime_status

        publish_caption_runtime_status(
            self._storage_root,
            channel_id,
            state="storage-refused",
            backlog_segments=0,
            max_backlog_segments=0,
            refusal_reason=result.refusal_reason,
        )


def build_caption_readiness_provider(
    *,
    tap_root: Path | None,
    review_store: CaptionReviewStore,
    storage_root: Path,
    segment_seconds: float = 5.0,
) -> CaptionRetentionVerdictSource:
    """Build the real storage/readiness provider used before egress starts.

    The returned object is both the ``(channel_id) -> CaptionRetentionResult``
    callable the daemon invokes and the owner of the background sweep that
    keeps that verdict fresh. It is built here rather than reusing the caption
    tap's sweep because the two are constructed by different code paths
    (``civiccast/app.py`` for the tap, ``build_channel_automation`` here) and
    the tap does not exist at all on a station that did not opt into live
    captioning -- there, this is the only sweep in the process. When both
    exist, ``_DISCOVERY_LOCK`` keeps them from scanning at the same time.
    """

    return CaptionRetentionVerdictSource(
        policy=CaptionEvidenceRetentionPolicy.from_system(storage_root=storage_root),
        tap_root=tap_root,
        review_store=review_store,
        segment_seconds=segment_seconds,
        storage_root=storage_root,
    )


def _normalize_candidate(candidate: Mapping[str, object]) -> _Candidate:
    path = Path(str(candidate["path"])).expanduser().resolve()
    return _Candidate(
        path=path,
        kind=str(candidate["kind"]),
        review_status=str(candidate["review_status"]),
        low_confidence=bool(candidate.get("low_confidence", False)),
        created_at=_utc(candidate["created_at"]),
        resolved_at=_utc(candidate["resolved_at"]) if candidate.get("resolved_at") else None,
        sha256=str(candidate["sha256"]),
        bytes=int(str(candidate["bytes"])),
        derived_evidence_verified=bool(candidate.get("derived_evidence_verified", False)),
        # Conservative default: callers that build candidates directly (the
        # owner-approved unit contracts) keep the original protect-until-
        # verified behavior.  Only ``_discover_candidates``, which can see the
        # real evidence windows, ever sets this to False.
        evidence_pending=bool(candidate.get("evidence_pending", True)),
    )


def _is_eligible(candidate: _Candidate, now: datetime) -> bool:
    if candidate.kind == "raw-chunk":
        # Raw tap audio backs operator review of a live cue; it is not itself
        # evidence, so it is retained only while it can still serve that review.
        # It becomes eligible on the age cap once EITHER:
        #   (a) the derived evidence covering it has been verified (normal path);
        #   (b) no evidence window can ever cover it, so waiting is pointless.
        # Case (b) is the live leak.  The tap parks a consumed chunk in
        # processed/ even when the evidence factory did not run (retention
        # verdict pending -> "text-only") or when the evidence path has stalled
        # for that channel.  Such a chunk can never flip to verified, so the old
        # ``verified and aged`` conjunction retained it FOREVER.  MEASURED LIVE
        # 2026-09-17: public/processed hit 12,980 files / 1,980.8 MB over three
        # days while pruning stopped at index 569.  The resulting backlog makes
        # the max-2 gate fail closed and pause live captions for 120 s on every
        # service start (reproduced 14:08:12 MT, 16.6 s after tap start).
        # Chunks whose evidence is still pending stay protected at any age
        # (pinned by test_keeps_raw_chunk_until_derived_evidence_is_verified).
        if candidate.evidence_pending and not candidate.derived_evidence_verified:
            return False
        return now - _utc(candidate.created_at) >= _RAW_CHUNK_MAX_AGE
    return (
        candidate.kind == "review-evidence"
        and candidate.review_status != "pending"
        and now - _utc(candidate.resolved_at or candidate.created_at) >= _RESOLVED_EVIDENCE_MAX_AGE
    )


def _prune_reason(candidate: _Candidate) -> str:
    if candidate.kind == "raw-chunk":
        return "raw-chunk-expired-after-derived-evidence"
    return "resolved-evidence-expired"


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("caption retention timestamps must be datetimes")
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _chunk_index(path: Path) -> int | None:
    stem = path.stem
    prefix = "chunk-"
    if not stem.startswith(prefix) or not stem[len(prefix) :].isdigit():
        return None
    return int(stem[len(prefix) :])


def _probe_wav_duration(path: Path) -> float | None:
    """The WAV's duration in seconds, or ``None`` when it cannot be read now.

    ``None`` rather than the historical ``0.0`` so a caller that caches the
    answer can tell "this file is zero seconds long" from "this file did not
    answer", and declines to cache the second.
    """

    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else 0.0
    except (OSError, EOFError, wave.Error):
        return None


def _stat_regular_file(path: Path) -> os.stat_result | None:
    """One ``stat()`` replacing ``is_file()`` + ``stat()`` + ``stat()``.

    Returns ``None`` for anything that is not a regular file (or is gone), which
    is exactly the set ``Path.is_file()`` rejects, but costs one syscall instead
    of the three the chunk loop used to spend per chunk -- MEASURED 2026-09-24,
    the archive's stat/resolve phase was 64,983 calls and 3.9-4.7 s of a
    11.3-12.1 s warm sweep, of which roughly two calls per chunk were this.
    """

    try:
        stats = path.stat()
    except OSError:
        return None
    return stats if S_ISREG(stats.st_mode) else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _audit_lock(path: Path) -> threading.RLock:
    with _AUDIT_LOCKS_GUARD:
        return _AUDIT_LOCKS.setdefault(path, threading.RLock())


__all__ = [
    "RETENTION_SWEEP_SECONDS",
    "RETENTION_VERDICT_FRESHNESS_SECONDS",
    "CaptionEvidenceRetentionPolicy",
    "CaptionRetentionResult",
    "CaptionRetentionVerdictSource",
    "build_caption_readiness_provider",
]
