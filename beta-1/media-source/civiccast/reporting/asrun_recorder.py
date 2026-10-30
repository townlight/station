# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Store-backed as-run recorder (S23 §6.1) — the concrete engine seam impl.

The egress engine emits a proof event at every ACTUAL source transition; this
recorder turns each transition into an append-only ``AsRunLogEntry`` on the
durable franchise-compliance ledger (``as_run_log``, migration 0055). It is the
``civiccast.egress.asrun.AsRunRecorder`` protocol implementation, wired into the
daemon in ``build_channel_automation`` so the engine never imports the reporting
package directly.

Two responsibilities the engine cannot own (the engine emits one event per
*going-on-air* transition and has no notion of "the previous segment ended"):

1. **Open/close stitch.** A segment's ``actual_end`` is the ``actual_start`` of
   the next transition on that channel (or a terminal stop/error/drain). The
   recorder tracks the per-channel open ``entry_id`` and closes it (writing
   ``actual_end`` + ``duration_s``) when the next transition — or ``close_open``
   — arrives.
2. **station_id resolution.** The proof event / egress config carry only
   ``channel_id``; the as-run ledger is station-scoped. The recorder resolves
   the active station id the same way the metadata router does
   (``CIVICCAST_STATION_ID`` env, else the single-station default).

Two distinct failure modes are split (E-1 fix):

* **Schema drift** — an upstream ``channel_id`` / ``asset_id`` / station_id env
  value that fails the ``Slug`` pattern is raised as
  :class:`AsRunCaptureSchemaError` so the daemon can flag a degraded-mode
  marker. Silent swallow of these would erase the franchise-compliance ledger
  while playout continued, the exact "silent loss of as-aired log" failure the
  S23 slice exists to prevent.
* **Transport / DB failures** — connection drops, disk full, etc. — BUG C2
  fix: no longer swallowed. Every write goes through the durable
  :class:`~civiccast.reporting.asrun_outbox.AsRunOutbox` (journal-first,
  fsync'd local SQLite, then a drain to the real store with retries and a
  visible ``asrun-outbox-degraded`` health condition on failure — see that
  module's docstring for the full design). A DB hiccup during playout no
  longer drops the row: it stays journaled and drains once the DB returns.
  The ONLY remaining swallow-and-log path is
  :class:`~civiccast.reporting.asrun_outbox.AsRunOutboxJournalError` — the
  true last resort where even the local durable journal could not accept
  the write (e.g. local disk full) — and that path now also raises the same
  degraded health condition rather than logging silently.

``verified=True`` always: the entry only exists because a proof event fired
(the encoder actually started).
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from civiccast.egress.asrun import AsRunCaptureSchemaError
from civiccast.reporting.asrun_outbox import (
    AsRunOutbox,
    AsRunOutboxJournalError,
    ephemeral_outbox_path,
    make_append_op,
    make_close_op,
)
from civiccast.reporting.models import AsRunLogEntry, Slug
from civiccast.reporting.store import ReportingStore

if TYPE_CHECKING:
    from civiccast.underwriting.store import UnderwritingStore

_LOG = logging.getLogger(__name__)

# The single-station default (matches the metadata router / app-platform default).
_DEFAULT_STATION_ID = "civiccast-station"

# Validator for the station_id env (raises ValidationError on pattern miss).
_SLUG_ADAPTER: TypeAdapter[str] = TypeAdapter(Slug)


def resolve_station_id() -> str:
    """The active station id (single-station deployment; env-overridable).

    Validates the resolved id against the ``Slug`` pattern. A
    ``CIVICCAST_STATION_ID`` that fails the pattern raises
    :class:`AsRunCaptureSchemaError` at boot so the operator catches the
    typo before any as-run row is silently dropped.
    """
    station_id = os.environ.get("CIVICCAST_STATION_ID") or _DEFAULT_STATION_ID
    try:
        _SLUG_ADAPTER.validate_python(station_id)
    except ValidationError as exc:  # fail-closed at startup — loud, not silent
        raise AsRunCaptureSchemaError(
            f"CIVICCAST_STATION_ID is not a valid Slug: {station_id!r}"
        ) from exc
    return station_id


def _now() -> datetime:
    return datetime.now(UTC)


class StoreAsRunRecorder:
    """Append as-run entries to the durable ledger at each source transition.

    Implements ``civiccast.egress.asrun.AsRunRecorder``. Thread-safe per the
    in-process open-row map (the automation pass is single-threaded per channel,
    but a lock keeps the stitch correct if a future driver fans out).

    Every store write is routed through an :class:`AsRunOutbox` (BUG C2 fix):
    journaled locally first, then opportunistically drained to *store*. In
    the common case (store reachable) this is transparent — the row is in
    *store* before ``record_transition``/``close_open`` returns, exactly as
    before. Pass an explicit *outbox* (as ``build_channel_automation`` does)
    to share one journal + one alert wiring across the process; the default
    (used by ad-hoc callers and the existing unit tests) builds an
    unwired, ephemeral-path outbox so constructing a recorder standalone
    stays side-effect-free.
    """

    def __init__(
        self,
        store: ReportingStore,
        *,
        station_id: str | None = None,
        outbox: AsRunOutbox | None = None,
        underwriting_store: UnderwritingStore | None = None,
    ) -> None:
        self._store = store
        self._station_id = station_id or resolve_station_id()
        # channel_id -> (open entry_id, its actual_start) awaiting an actual_end.
        self._open: dict[str, tuple[str, datetime]] = {}
        self._lock = threading.Lock()
        self._outbox = outbox or AsRunOutbox(store, db_path=ephemeral_outbox_path())
        self._underwriting = underwriting_store

    def record_transition(
        self,
        *,
        channel_id: str,
        source_kind: str,
        asset_id: str | None,
        source_label: str,
        actual_start: datetime,
        proof_event_id: str,
        schedule_item_id: str | None = None,
    ) -> None:
        try:
            with self._lock:
                # Close the previous open row for this channel: its actual_end is
                # exactly when this new source took over.
                self._close_locked(channel_id, actual_start)
                entry_id = f"asrun-{uuid.uuid4()}"
                # Open the new row. actual_end == actual_start for now (zero
                # duration); it is rewritten when the next transition closes it.
                entry = AsRunLogEntry(
                    entry_id=entry_id,
                    station_id=self._station_id,
                    channel_id=channel_id,
                    asset_id=asset_id,
                    schedule_item_id=schedule_item_id,
                    actual_start=actual_start,
                    actual_end=actual_start,
                    duration_s=0,
                    source_kind=self._airing_kind(
                        source_kind, schedule_item_id, channel_id, asset_id
                    ),  # type: ignore[arg-type]
                    verified=True,
                )
                # BUG C2 fix: journal-first, then drain — never a bare direct
                # store write. See civiccast.reporting.asrun_outbox.
                self._outbox.append_and_drain(make_append_op(entry))
                self._open[channel_id] = (entry_id, actual_start)
        except ValidationError as exc:
            # Schema drift — an upstream id violates the Slug pattern. Loud, not
            # silent: surface via a typed exception so the daemon can mark
            # degraded mode (the S23 §6.1 anti-silent-drop guard).
            _LOG.error(
                "As-run schema drift on channel=%s source_kind=%s asset_id=%s: %s",
                channel_id,
                source_kind,
                asset_id,
                exc,
            )
            raise AsRunCaptureSchemaError(
                f"As-run schema drift on channel={channel_id!r} source_kind={source_kind!r}: {exc}"
            ) from exc
        except AsRunOutboxJournalError:
            # True last resort (BUG C2 fix): even the local durable journal
            # could not accept the write (e.g. local disk full). Still must
            # never break playout, but this is no longer a silent swallow —
            # CRITICAL, not a routine exception log, because it means the
            # durability guarantee itself just failed for this transition.
            _LOG.critical(
                "As-run journal write failed for channel %s (%s); this transition "
                "may not be captured in the as-aired ledger. Playout is unaffected.",
                channel_id,
                source_kind,
                exc_info=True,
            )

    def close_open(self, *, channel_id: str, actual_end: datetime) -> None:
        try:
            with self._lock:
                self._close_locked(channel_id, actual_end)
        except ValidationError as exc:
            # Schema drift on close — same loud-not-silent contract as the
            # record path.
            _LOG.error(
                "As-run schema drift closing channel=%s: %s",
                channel_id,
                exc,
            )
            raise AsRunCaptureSchemaError(
                f"As-run schema drift closing channel={channel_id!r}: {exc}"
            ) from exc
        except AsRunOutboxJournalError:
            # True last resort — see record_transition's matching comment.
            _LOG.critical(
                "As-run journal write failed closing channel %s; this segment's "
                "actual_end may not be captured. Playout is unaffected.",
                channel_id,
                exc_info=True,
            )

    # --- internals (caller holds the lock) ------------------------------

    def _airing_kind(
        self, source_kind: str, schedule_item_id: str | None, channel_id: str, asset_id: str | None
    ) -> str:
        # A sponsor asset aired as an ordinary program is not proof of a
        # trafficked spot. Match the exact occurrence AND the chosen creative.
        if source_kind != "program" or not schedule_item_id or self._underwriting is None:
            return source_kind
        try:
            placement = self._underwriting.get_placement(f"pl-{schedule_item_id}")
            if placement is None or placement.channel_id != channel_id:
                return source_kind
            flight = self._underwriting.get_flight(placement.flight_id)
            spot = self._underwriting.get_spot(flight.spot_id) if flight else None
            if spot and spot.station_id == self._station_id and spot.asset_id == asset_id:
                return "spot"
        except Exception:
            # Preserve the verified airing even if optional attribution cannot
            # be resolved. Never invent a billable spot on a lookup failure.
            _LOG.exception("Underwriting attribution unavailable for schedule %s", schedule_item_id)
        return source_kind

    def _close_locked(self, channel_id: str, actual_end: datetime) -> None:
        open_row = self._open.get(channel_id)
        if open_row is None:
            return
        entry_id, actual_start = open_row
        if actual_end <= actual_start:
            # A close at or before the open instant (clock skew / immediate
            # re-transition): keep zero duration, do not go negative.
            actual_end = actual_start
            duration_s = 0
        else:
            duration_s = int((actual_end - actual_start).total_seconds())
        # Pop BEFORE the durable write (E-4 fix): a post-write teardown
        # failure must not leave a stale handle that the next transition
        # would re-close, overwriting an already-recorded actual_end.
        self._open.pop(channel_id, None)
        # BUG C2 fix: journal-first, then drain (idempotent — see
        # civiccast.reporting.asrun_outbox's exactly-once contract and
        # ReportingStore.close_entry's own "guarded UPDATE" docstring), not
        # a bare direct store write.
        self._outbox.append_and_drain(
            make_close_op(
                channel_id=channel_id,
                entry_id=entry_id,
                actual_end=actual_end,
                duration_s=duration_s,
            )
        )


__all__ = [
    "AsRunCaptureSchemaError",
    "StoreAsRunRecorder",
    "resolve_station_id",
]
