# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Program-log materializer (cable automation CA-1).

Turns enabled program slots into real premiere ``schedule_items`` over a
rolling horizon so the existing schedule → source-plan → playout path plays
a 24/7 program log unchanged. Idempotency comes from the occurrence table:
one row per (slot, occurrence_start), recorded whether the materialization
succeeded (``scheduled``) or was skipped with an honest reason
(``skipped_conflict`` / ``skipped_asset``). Skips are never retried
automatically — the guide editor surfaces them for the operator to resolve.

Deployment shape mirrors the other lifespan workers: ``run_once`` is the
testable unit, ``run_forever`` survives and logs scan exceptions, and the
app lifespan runs the loop on a thread when ``CIVICCAST_PROGRAM_LOG_WORKER``
is ``inline`` (the default) and durable storage is active.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, Protocol

from civiccast.programlog.models import ProgramSlot, SlotOccurrence
from civiccast.programlog.occurrences import compute_occurrences
from civiccast.programlog.store import PostgresProgramLogStore
from civiccast.schedule.models import ScheduleItemCreate

_LOG = logging.getLogger(__name__)
_MATERIALIZE_LOCK = threading.RLock()

WORKER_MODE_INLINE = "inline"
WORKER_MODE_OFF = "off"
_WORKER_MODES = (WORKER_MODE_INLINE, WORKER_MODE_OFF)

_PLAYABLE_ASSET_STATES = ("validated", "recorded")

__all__ = ["ProgramLogMaterializer", "ProgramLogSettings"]


class AssetResolver(Protocol):
    """Resolve an asset id to its staff row (or None when unknown)."""

    def __call__(self, asset_id: str) -> Any: ...


@dataclass(frozen=True)
class ProgramLogSettings:
    """Deployment configuration for the program-log materializer."""

    mode: str = WORKER_MODE_INLINE
    poll_seconds: float = 300.0
    horizon_hours: float = 72.0

    @classmethod
    def from_env(cls) -> ProgramLogSettings:
        mode = os.environ.get("CIVICCAST_PROGRAM_LOG_WORKER", WORKER_MODE_INLINE).strip().lower()
        if mode not in _WORKER_MODES:
            raise ValueError(
                f"CIVICCAST_PROGRAM_LOG_WORKER must be one of {', '.join(_WORKER_MODES)}; "
                f"got {mode!r}."
            )
        defaults = cls()
        return cls(
            mode=mode,
            poll_seconds=_env_float("CIVICCAST_PROGRAM_LOG_POLL_SECONDS", defaults.poll_seconds),
            horizon_hours=_env_float("CIVICCAST_PROGRAM_LOG_HORIZON_HOURS", defaults.horizon_hours),
        )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number; got {raw!r}.") from exc


def _new_occurrence_id(day: date) -> str:
    # Keep the logical calendar day durable without a schema change. Legacy
    # ids predate station-local recurrence and therefore identify UTC days.
    token = secrets.token_urlsafe(16).replace("-", "").replace("_", "")
    return f"cpo_{token}_{day:%Y%m%d}"


def _occurrence_day(occurrence: SlotOccurrence) -> date:
    try:
        return datetime.strptime(occurrence.occurrence_id.rsplit("_", 1)[-1], "%Y%m%d").date()
    except ValueError:
        return occurrence.occurrence_start.astimezone(UTC).date()


class ProgramLogMaterializer:
    """Materializes slot occurrences into premiere schedule items."""

    def __init__(
        self,
        store: PostgresProgramLogStore,
        schedule_store: Any,
        asset_resolver: AssetResolver,
        *,
        settings: ProgramLogSettings,
        tz: tzinfo | Callable[[], tzinfo] = UTC,
    ) -> None:
        self._store = store
        self._schedule_store = schedule_store
        self._asset_resolver = asset_resolver
        self._settings = settings
        self._tz = tz

    def run_forever(
        self,
        *,
        poll_seconds: float = 300.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Run the materialization loop until stopped; survive scan errors."""

        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.exception("Program-log scan failed; retrying on the next poll interval.")
            if stop_event is not None:
                stop_event.wait(poll_seconds)
            else:
                time.sleep(poll_seconds)

    def run_once(self, *, now: datetime | None = None) -> list[SlotOccurrence]:
        """Materialize every due occurrence once; return newly recorded rows."""
        # API instances and the background worker share the entire read/write
        # boundary, including the current timezone snapshot.
        with _MATERIALIZE_LOCK:
            return self._run_once(now=now)

    def local_anchor(self, first_start: datetime) -> datetime:
        """Capture a slot's calendar/clock anchor in the current station zone."""
        with _MATERIALIZE_LOCK:
            tz = self._tz() if callable(self._tz) else self._tz
            return first_start.astimezone(tz).replace(tzinfo=None)

    def pin_legacy_anchors(self) -> None:
        """Adopt the current zone for legacy slots before a profile zone edit.

        Prior station timezone history was not stored and cannot be recovered.
        This only pins intent, including disabled slots; it creates no airings.
        """
        with _MATERIALIZE_LOCK:
            tz = self._tz() if callable(self._tz) else self._tz
            for slot in self._store.list_slots():
                self._pin_anchor(slot, tz)

    def _pin_anchor(self, slot: ProgramSlot, tz: tzinfo) -> ProgramSlot:
        if slot.recurrence_anchor_local is not None:
            return slot
        return self._store.update_slot(
            slot.model_copy(
                update={
                    "recurrence_anchor_local": slot.first_start_at.astimezone(tz).replace(
                        tzinfo=None
                    ),
                }
            )
        )

    def _run_once(self, *, now: datetime | None = None) -> list[SlotOccurrence]:

        resolved_now = now or datetime.now(UTC)
        window_end = resolved_now + timedelta(hours=self._settings.horizon_hours)
        tz = self._tz() if callable(self._tz) else self._tz
        recorded: list[SlotOccurrence] = []
        for slot in self._store.list_slots():
            slot = self._pin_anchor(slot, tz)
            if not slot.enabled:
                continue
            occurrences = self._store.list_occurrences(slot_id=slot.slot_id)
            existing = {occurrence.occurrence_start for occurrence in occurrences}
            protected_through = max(
                (
                    max(_occurrence_day(row), row.occurrence_start.astimezone(tz).date())
                    for row in occurrences
                ),
                default=None,
            )
            for start in compute_occurrences(
                slot,
                window_start=resolved_now,
                window_end=window_end,
                tz=tz,
            ):
                if start in existing:
                    continue
                day = start.astimezone(tz).date()
                # A zone/rule edit applies beyond the materialized horizon;
                # preserve earlier commitments, including skipped/cancelled rows.
                if (
                    slot.recurrence != "once"
                    and protected_through is not None
                    and day <= protected_through
                ):
                    continue
                recorded.append(self._materialize(slot, start, now=resolved_now, day=day))
        return recorded

    def disable_slot(self, slot_id: str, *, now: datetime | None = None) -> list[SlotOccurrence]:
        """Disable a slot and cancel its FUTURE materialized schedule items.

        Past occurrences are untouched — what aired, aired. Returns the
        occurrences that were cancelled.
        """

        resolved_now = now or datetime.now(UTC)
        slot = self._store.get_slot(slot_id)
        if slot is None:
            return []
        if slot.enabled:
            self._store.update_slot(
                slot.model_copy(update={"enabled": False, "updated_at": resolved_now})
            )
        cancelled: list[SlotOccurrence] = []
        for occurrence in self._store.list_occurrences(slot_id=slot_id, start_from=resolved_now):
            if occurrence.status != "scheduled" or occurrence.schedule_item_id is None:
                continue
            try:
                self._schedule_store.cancel(occurrence.schedule_item_id)
            except Exception as exc:
                _LOG.warning(
                    "Could not cancel schedule item %s for slot %s: %s",
                    occurrence.schedule_item_id,
                    slot_id,
                    exc,
                )
                continue
            updated = occurrence.model_copy(
                update={"status": "cancelled", "detail": "slot disabled"}
            )
            cancelled.append(self._store.update_occurrence(updated))
        return cancelled

    def _materialize(
        self, slot: ProgramSlot, start: datetime, *, now: datetime, day: date
    ) -> SlotOccurrence:
        from civiccast.schedule.store import AssetNotFoundError, ScheduleConflictError

        base = SlotOccurrence(
            occurrence_id=_new_occurrence_id(day),
            slot_id=slot.slot_id,
            occurrence_start=start,
            schedule_item_id=None,
            status="scheduled",
            detail="",
            created_at=now,
        )
        asset = self._asset_resolver(slot.asset_id)
        duration = slot.duration_seconds or (
            getattr(asset, "duration_seconds", None) if asset is not None else None
        )
        if (
            asset is None
            or getattr(asset, "state", None) not in _PLAYABLE_ASSET_STATES
            or not getattr(asset, "file_path", None)
            or not duration
        ):
            reason = (
                f"asset {slot.asset_id!r} is not playable "
                "(missing, unpackaged, no local media, or unknown duration)"
            )
            _LOG.warning("Program slot %s skipped at %s: %s", slot.slot_id, start, reason)
            return self._store.record_occurrence(
                base.model_copy(update={"status": "skipped_asset", "detail": reason})
            )
        try:
            created = self._schedule_store.create(
                ScheduleItemCreate(
                    asset_id=slot.asset_id,
                    channel_id=slot.channel_id,
                    mode="premiere",
                    scheduled_at=start,
                    duration_seconds=int(duration),
                    notes=f"program-log slot {slot.slot_id}",
                )
            )
        except ScheduleConflictError as exc:
            _LOG.warning("Program slot %s occurrence at %s skipped: %s", slot.slot_id, start, exc)
            return self._store.record_occurrence(
                base.model_copy(update={"status": "skipped_conflict", "detail": str(exc)})
            )
        except AssetNotFoundError as exc:
            return self._store.record_occurrence(
                base.model_copy(update={"status": "skipped_asset", "detail": str(exc)})
            )
        return self._store.record_occurrence(
            base.model_copy(update={"schedule_item_id": str(created.id)})
        )
