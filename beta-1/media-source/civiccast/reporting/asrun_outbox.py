# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable transactional outbox for the as-run ledger (BUG C2 fix, S23 §6.1).

The problem this fixes: :class:`~civiccast.reporting.asrun_recorder.
StoreAsRunRecorder` used to write straight to the durable (Postgres/SQLite)
``ReportingStore`` and swallow the write's exception on failure
(``except Exception: log and continue``). A DB hiccup during playout — the
exact moment the as-run ledger matters most — silently dropped an aired
item from the station's legal record, with nobody told. The 3.0 MASTER spec
(§6/§12, S23 §3) treats the as-run log as a durable public record; silent
loss is the one failure mode the module exists to prevent.

The fix is a transactional outbox:

1. **Journal first.** Every as-run write (an "open" for a new transition, a
   "close" for the previous one) is durably appended to a local,
   process-independent SQLite journal *before* anything touches the
   franchise-compliance DB. The journal lives on local disk in the station
   data dir (see :func:`default_asrun_outbox_path`), fsync'd on every write
   (``PRAGMA synchronous=FULL``) so a journal row that reports success
   really is on disk, survives a crash or power loss.
2. **Drain with retries.** Immediately after journaling, an opportunistic
   drain (:meth:`AsRunOutbox.drain_once`) applies pending rows to the real
   store, oldest first. In the common case (DB reachable) this makes the
   round trip through the outbox invisible — the row lands in the DB before
   ``record_transition``/``close_open`` returns, same as before this fix.
   When the DB is unreachable, the failing row stays journaled (nothing is
   lost) and the periodic channel-automation poll
   (:meth:`ChannelAutomationService.run_once` in ``civiccast.egress.
   automation``) retries the drain every tick until the DB comes back.
3. **Health, not silence.** A drain failure raises
   ``asrun-outbox-degraded`` on the existing alert hub
   (:func:`civiccast.alerting.store.record_alert_condition`) instead of only
   a log line — the operator's Alert Settings / safe-to-air surface sees a
   real, visible condition. The condition resolves itself once a drain call
   clears the backlog to zero.
4. **Exactly-once.** Every journaled row carries a stable ``event_id``
   (``asrun-open:<entry_id>`` / ``asrun-close:<entry_id>``, derived from the
   recorder's own per-transition UUID) as a ``UNIQUE`` journal-table column,
   so re-journaling the same op (e.g. a retried caller) is a no-op. Draining
   is *also* safe to repeat: ``ReportingStore.append_as_run`` upserts by
   ``entry_id`` and ``ReportingStore.close_entry`` is a guarded
   ``duration_s == 0`` UPDATE (idempotent by construction — see their own
   docstrings) — so replaying an already-drained row twice, which can
   legitimately happen across a crash between "DB commit" and "mark
   drained", writes the identical result rather than a duplicate or a
   clobber.
5. **Lazy startup replay.** :meth:`AsRunOutbox.replay_pending` drains every
   row left over from a prior process — a crash between journaling and
   marking a row drained loses nothing, because the row is still
   ``drained_at IS NULL`` when a new process opens the same journal file.
   Reached via :meth:`AsRunOutbox.ensure_started`, which runs it exactly
   once, on the first real drain attempt — NOT eagerly at construction.
   ``build_channel_automation`` (which constructs the outbox) runs
   synchronously inside ``civiccast.app.create_app()`` when
   ``DATABASE_URL`` is set at boot, and that path must never touch the
   database (see ``tests/schedule/test_app_wiring.py::
   TestAppFactorySetEnv::test_create_app_does_not_call_engine_connect``).
   The replay instead fires the first time the engine actually emits a
   proof event (the recorder's first opportunistic write) or the first
   channel-automation poll tick — whichever comes first — both of which
   only happen once the app is genuinely serving.

This module is reporting-owned and imports nothing from ``civiccast.egress``
(the engine-side seam stays dependency-free per ``civiccast/egress/asrun.py``'s
module docstring) — ``civiccast.alerting`` is imported lazily inside the
health-reporting method, mirroring ``civiccast.egress.automation``'s
``_raise_egress_degraded_alert`` pattern, so a reporting-only import never
pulls in the alerting package's dependency surface.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from civiccast.reporting.models import AsRunLogEntry

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from civiccast.alerting.models import AlertConditionKind
    from civiccast.reporting.store import ReportingStore

_LOG = logging.getLogger(__name__)

#: The outbox's own journal file name, sibling to the egress work dir's
#: filename conventions.
ASRUN_OUTBOX_FILENAME = "asrun_outbox.sqlite3"

#: The alert condition this module raises/resolves (added alongside every
#: other post-migration-0039 condition kind — see ``alerting/models.py``).
_ASRUN_OUTBOX_ALERT_KIND: AlertConditionKind = "asrun-outbox-degraded"

#: Stable dedupe key: one firing AlertEvent for the whole station's as-run
#: outbox, not one per failing op (``record_alert_condition`` dedupes per
#: (kind, resource_ref) pair — see its docstring).
_ASRUN_OUTBOX_RESOURCE_REF = "station:asrun-outbox"

AlertSessionFactory = Callable[[], "AbstractContextManager[Session]"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS asrun_outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    channel_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('append', 'close')),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    drained_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_asrun_outbox_pending
    ON asrun_outbox (seq) WHERE drained_at IS NULL;
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def default_reporting_work_dir() -> Path:
    """Default directory for reporting's local durable state (the as-run
    outbox journal). Mirrors ``civiccast.egress.automation.
    default_egress_work_dir``'s resolution order (env override, then Windows
    ``LOCALAPPDATA``, then an XDG-style POSIX fallback) so the two sibling
    on-disk conventions stay consistent."""

    configured = os.environ.get("CIVICCAST_REPORTING_WORK_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "CivicCast" / "reporting"
    return Path.home() / ".local" / "share" / "civiccast" / "reporting"


def default_asrun_outbox_path() -> Path:
    """The real, production journal file path (station data dir)."""

    return default_reporting_work_dir() / ASRUN_OUTBOX_FILENAME


def ephemeral_outbox_path() -> Path:
    """An isolated, per-instance journal file used ONLY when a caller builds
    :class:`~civiccast.reporting.asrun_recorder.StoreAsRunRecorder` without
    injecting a wired :class:`AsRunOutbox` — ad-hoc scripts and (importantly)
    the existing unit tests that construct ``StoreAsRunRecorder(reporting_store,
    ...)`` directly against a ``tmp_path`` SQLAlchemy engine, with no outbox
    of their own.

    Production wiring (``civiccast.egress.automation.build_channel_automation``)
    always constructs and injects an explicit :class:`AsRunOutbox` rooted at
    :func:`default_asrun_outbox_path`, shared for the process lifetime so
    startup replay and the periodic drain observe the same journal. This
    fallback exists purely so the recorder stays safe and side-effect-free
    to construct standalone — it must never resolve to a real, shared,
    persistent station-data-dir path, or every test process would race to
    create/lock the same file on the CI/dev machine.
    """

    return Path(tempfile.mkdtemp(prefix="civiccast-asrun-outbox-")) / ASRUN_OUTBOX_FILENAME


class AsRunOutboxError(RuntimeError):
    """Base error for the as-run durable outbox."""


class AsRunOutboxJournalError(AsRunOutboxError):
    """The local durable journal itself could not accept a write (disk full,
    permissions, a corrupt journal file, ...).

    This is the TRUE last resort: every ordinary DB-side failure (the
    playout-time DB hiccup this module exists to survive) is absorbed inside
    :meth:`AsRunOutbox.drain_once` and never raises. Only a failure to even
    durably record the event locally reaches the caller, so
    ``StoreAsRunRecorder`` can log it at CRITICAL and still guarantee it
    never breaks playout.
    """


@dataclass(frozen=True)
class _OutboxRow:
    event_id: str
    channel_id: str
    kind: Literal["append", "close"]
    payload_json: str


def make_append_op(entry: AsRunLogEntry) -> _OutboxRow:
    """The durable-journal row for opening a new as-run entry."""

    return _OutboxRow(
        event_id=f"asrun-open:{entry.entry_id}",
        channel_id=entry.channel_id,
        kind="append",
        payload_json=entry.model_dump_json(),
    )


def make_close_op(
    *, channel_id: str, entry_id: str, actual_end: datetime, duration_s: int
) -> _OutboxRow:
    """The durable-journal row for closing a previously-opened as-run entry."""

    return _OutboxRow(
        event_id=f"asrun-close:{entry_id}",
        channel_id=channel_id,
        kind="close",
        payload_json=json.dumps(
            {
                "entry_id": entry_id,
                "actual_end": actual_end.isoformat(),
                "duration_s": duration_s,
            }
        ),
    )


class AsRunOutbox:
    """Local durable journal + DB drain for the as-run ledger (BUG C2 fix).

    See the module docstring for the full design. In short: journal first
    (must succeed — raises :class:`AsRunOutboxJournalError` if even that
    fails), then drain to the real store with retries, raising a visible
    ``asrun-outbox-degraded`` health condition on a drain failure instead of
    swallowing it, and resolving that condition once the backlog clears.
    """

    def __init__(
        self,
        store: ReportingStore,
        *,
        db_path: Path | None = None,
        alert_session_factory: AlertSessionFactory | None = None,
    ) -> None:
        self._store = store
        self._db_path = db_path or default_asrun_outbox_path()
        self._alert_session_factory = alert_session_factory
        self._lock = threading.Lock()
        # Lazy startup replay (app-factory contract fix): AsRunOutbox is
        # constructed synchronously inside build_channel_automation, which
        # itself runs during create_app() (civiccast/app.py's
        # _wire_stage_f_workers, called from _wire_durable_stores /
        # _install_durable_store_wiring) -- a path that must NEVER touch the
        # SQLAlchemy-backed store (pinned by tests/schedule/test_app_wiring.py::
        # TestAppFactorySetEnv::test_create_app_does_not_call_engine_connect).
        # Opening THIS journal file below is fine (a local sqlite3 handle, not
        # the app's SQLAlchemy Engine) -- what must not happen at construction
        # is replay_pending()'s store/alert-hub reads. Deferred to the first
        # real drain attempt instead: see ensure_started().
        self._did_startup_replay = False
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = self._open_connection()
        except OSError as exc:
            raise AsRunOutboxJournalError(
                f"Could not open the as-run outbox journal at {self._db_path}: {exc}"
            ) from exc

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(_SCHEMA)
        return conn

    def close(self) -> None:
        """Release the journal's SQLite handle (tests / graceful shutdown)."""

        with self._lock:
            self._conn.close()

    # -- journal-first write (the durable path) ---------------------------

    def append_and_drain(self, op: _OutboxRow) -> None:
        """Durably journal *op*, then attempt an immediate opportunistic drain.

        Journaling is the ONLY step that must succeed for this call to
        return normally. A DB failure during the opportunistic drain is
        caught inside :meth:`drain_once`, logged, and surfaced via the
        degraded-health alert — it never raises here. Only a failure to even
        accept the journal write raises :class:`AsRunOutboxJournalError`;
        the caller (``StoreAsRunRecorder``) treats that as the true last
        resort that must still never break playout.
        """

        self._journal(op)
        try:
            self.ensure_started()
        except Exception:
            # ensure_started()/drain_once() already log + alert internally
            # for the DB failures they expect; a second, unexpected
            # exception class escaping it (a bug, not a DB error) must still
            # not reach the playout thread — the event is safely journaled
            # either way.
            _LOG.exception("Unexpected error during opportunistic as-run drain.")

    def _journal(self, op: _OutboxRow) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR IGNORE INTO asrun_outbox "
                    "(event_id, channel_id, kind, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (op.event_id, op.channel_id, op.kind, op.payload_json, _now_iso()),
                )
        except sqlite3.Error as exc:
            raise AsRunOutboxJournalError(
                f"Failed to journal as-run event {op.event_id!r} to {self._db_path}: {exc}"
            ) from exc

    # -- drain: journal -> durable store, with retries + health -----------

    def drain_once(self, *, max_batch: int = 500) -> int:
        """Apply pending journaled rows to the durable store, oldest first.

        Stops at the first failing row so per-channel open/close ordering is
        preserved (draining out of order could close a row before its open
        lands). Returns the count successfully drained. Never raises for a
        store failure: it is logged and surfaced via
        ``record_alert_condition``, and the failing row (plus everything
        behind it) stays pending for the next call.
        """

        with self._lock:
            pending = self._conn.execute(
                "SELECT seq, event_id, channel_id, kind, payload_json FROM asrun_outbox "
                "WHERE drained_at IS NULL ORDER BY seq LIMIT ?",
                (max_batch,),
            ).fetchall()

        drained = 0
        failure: Exception | None = None
        for seq, event_id, channel_id, kind, payload_json in pending:
            try:
                self._apply(kind, payload_json)
            except Exception as exc:  # the store/DB failure this module exists to survive
                failure = exc
                _LOG.warning(
                    "As-run outbox drain failed at event %s (channel=%s, kind=%s); "
                    "%d pending event(s) remain safely journaled, not lost: %s",
                    event_id,
                    channel_id,
                    kind,
                    len(pending) - drained,
                    exc,
                )
                break
            with self._lock:
                self._conn.execute(
                    "UPDATE asrun_outbox SET drained_at = ? WHERE seq = ?",
                    (_now_iso(), seq),
                )
            drained += 1

        remaining = self._pending_count()
        self._report_health(failure, remaining=remaining)
        return drained

    def _apply(self, kind: str, payload_json: str) -> None:
        payload = json.loads(payload_json)
        if kind == "append":
            entry = AsRunLogEntry.model_validate(payload)
            self._store.append_as_run(entry)
        elif kind == "close":
            self._store.close_entry(
                entry_id=payload["entry_id"],
                actual_end=datetime.fromisoformat(payload["actual_end"]),
                duration_s=payload["duration_s"],
            )
        else:  # pragma: no cover - the CHECK constraint prevents this
            raise AsRunOutboxError(f"Unknown outbox op kind: {kind!r}")

    def pending_count(self) -> int:
        """How many journaled rows are not yet durable in the real store."""

        return self._pending_count()

    def _pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM asrun_outbox WHERE drained_at IS NULL"
            ).fetchone()
        return int(row[0]) if row else 0

    # -- startup replay -----------------------------------------------------

    def replay_pending(self) -> int:
        """Drain every row left pending by a crash mid-drain, in order.

        A prior crash between journaling and the DB commit loses nothing,
        because the row is still ``drained_at IS NULL`` and gets re-applied
        — idempotently, per the module docstring's exactly-once contract.
        Terminates once a call makes zero progress (the backlog is empty, or
        the remaining rows are failing immediately) rather than spinning
        against an unreachable DB.

        Callable directly (e.g. a fresh process resuming the same journal
        file, as the crash-recovery tests do). Production callers should use
        :meth:`ensure_started` instead — see its docstring for why this
        method is never called eagerly from ``__init__``/
        ``build_channel_automation``.
        """

        total = 0
        while True:
            n = self.drain_once()
            total += n
            if n == 0:
                break
        return total

    def ensure_started(self) -> int:
        """Lazy startup replay: performs :meth:`replay_pending` on the FIRST
        call only, then behaves as an ordinary :meth:`drain_once` on every
        call after that.

        This is what makes the durability guarantee ("a crash mid-drain
        loses nothing") hold WITHOUT touching the database during
        ``AsRunOutbox.__init__``/``build_channel_automation`` — both of
        which run synchronously inside ``civiccast.app.create_app()`` (via
        ``_wire_stage_f_workers``) when ``DATABASE_URL`` is set at boot, a
        path that must never open a DB connection (see
        ``tests/schedule/test_app_wiring.py::TestAppFactorySetEnv::
        test_create_app_does_not_call_engine_connect``). The replay instead
        runs on the first REAL drain attempt — whichever comes first:
        ``StoreAsRunRecorder``'s first opportunistic write (``append_and_
        drain``, called only once the playout engine actually emits a proof
        event) or ``ChannelAutomationService.run_once``'s first poll tick
        (``_drain_as_run_outbox``, called only once the app lifespan
        actually starts the automation loop thread — never during a bare
        ``create_app()``). Both call sites route through this method rather
        than ``drain_once`` directly.
        """

        with self._lock:
            needs_replay = not self._did_startup_replay
            self._did_startup_replay = True
        if needs_replay:
            return self.replay_pending()
        return self.drain_once()

    # -- health/alert plumbing ----------------------------------------------

    def _report_health(self, failure: Exception | None, *, remaining: int) -> None:
        if self._alert_session_factory is None:
            return
        try:
            from civiccast.alerting.store import get_alert_events, record_alert_condition

            if failure is not None:
                with self._alert_session_factory() as session:
                    record_alert_condition(
                        session,
                        kind=_ASRUN_OUTBOX_ALERT_KIND,
                        resource_ref=_ASRUN_OUTBOX_RESOURCE_REF,
                        source_section="S23",
                        summary=(
                            f"As-run ledger drain failing; {remaining} as-aired event(s) "
                            "are journaled locally and not yet durable in the "
                            "franchise-compliance database."
                        ),
                        detail=str(failure),
                    )
                    session.commit()
            elif remaining == 0:
                # Only resolve if THIS condition is actually firing right now
                # — checked against the DB (not an in-process flag) so a
                # fresh process that replays a leftover backlog at startup
                # still clears a condition raised by a prior, now-dead
                # process. Without this check, every ordinary successful
                # drain (the DB was never down) would write a spurious
                # "pre-resolved" audit-trail row every poll tick — see
                # ``record_alert_condition``'s "nothing to resolve" branch.
                with self._alert_session_factory() as session:
                    firing = get_alert_events(session, state="firing")
                    is_firing = any(
                        e.condition == _ASRUN_OUTBOX_ALERT_KIND
                        and e.resource_ref == _ASRUN_OUTBOX_RESOURCE_REF
                        for e in firing
                    )
                    if is_firing:
                        record_alert_condition(
                            session,
                            kind=_ASRUN_OUTBOX_ALERT_KIND,
                            resource_ref=_ASRUN_OUTBOX_RESOURCE_REF,
                            source_section="S23",
                            summary="As-run ledger drain recovered; the local outbox is empty.",
                            resolved=True,
                        )
                        session.commit()
        except Exception:
            # The alert hub can share the very DB that is down (same class
            # of limitation as civiccast.egress.automation's
            # _raise_egress_degraded_alert). Never let a failure to ALSO
            # write the alert row mask the underlying drain failure, retry
            # it, or reach playout — the local journal already protects the
            # as-run event; this is best-effort visibility on top of that.
            _LOG.exception("Failed to record as-run outbox health condition.")


__all__ = [
    "ASRUN_OUTBOX_FILENAME",
    "AsRunOutbox",
    "AsRunOutboxError",
    "AsRunOutboxJournalError",
    "default_asrun_outbox_path",
    "default_reporting_work_dir",
    "ephemeral_outbox_path",
    "make_append_op",
    "make_close_op",
]
