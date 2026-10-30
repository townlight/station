# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable store for as-run entries + EPG export configs (S23).

Per-request store over the single global session factory (same lazy posture as
the eas / ai_models / metadata stores). The as-run ledger is **append-only**
proof-of-performance (a permanent franchise-compliance record); it is written by
the playout engine at actual air time, NOT derived from the trimmed egress
proof-event ring buffer.

* ``append_as_run`` — write (or idempotently update by ``entry_id``) one
  as-aired record.
* ``list_as_run`` — query the ledger by station, with optional channel filter and
  a half-open ``[from_ts, to_ts)`` window on ``actual_start`` (the engine-verified
  actual time, per the S23 key claim that as-run = what *aired*).
* ``upsert_config`` / ``get_config`` / ``list_configs`` / ``delete_config`` — CRUD
  for EPG export profiles.

All comparison values are bound parameters (no string interpolation): the
station/channel/window filters ride the ``(channel_id, actual_start)`` /
``(station_id, actual_start)`` indexes, mirroring the metadata store's
index-path contract.

``station_id`` / ``channel_id`` / ``asset_id`` are loose string columns (no DB
FK), matching the eas / ai_models / metadata convention.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from civiccast.reporting.models import (
    AsRunLogEntry,
    AsRunLogEntryDb,
    EpgExportConfig,
    EpgExportConfigDb,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


class ReportingStoreError(RuntimeError):
    """Base error for reporting persistence failures."""


class EpgConfigNotFoundError(ReportingStoreError):
    """Raised when a config_id does not resolve (delete/patch of a missing row)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a possibly-naive datetime (SQLite drops tz) to UTC-aware."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class ReportingStore:
    """Append-only as-run ledger + EPG-config CRUD."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def _session(self) -> AbstractContextManager[Session]:
        return self._session_factory()

    # --- as-run ledger ---------------------------------------------------

    def append_as_run(self, entry: AsRunLogEntry) -> AsRunLogEntry:
        """Write one as-aired record. Idempotent on ``entry_id`` (re-append updates
        in place — the engine may close a row's ``actual_end`` after first write)."""
        with self._session() as session:
            row = session.get(AsRunLogEntryDb, entry.entry_id)
            if row is None:
                row = AsRunLogEntryDb(entry_id=entry.entry_id, created_at=entry.created_at)
                session.add(row)
            row.station_id = entry.station_id
            row.channel_id = entry.channel_id
            row.schedule_item_id = entry.schedule_item_id
            row.asset_id = entry.asset_id
            row.scheduled_start = entry.scheduled_start
            row.actual_start = entry.actual_start
            row.actual_end = entry.actual_end
            row.duration_s = entry.duration_s
            row.source_kind = entry.source_kind
            row.verified = entry.verified
            row.updated_at = _now()
            session.commit()
            return _entry_to_model(row)

    def get_entry(self, entry_id: str) -> AsRunLogEntry | None:
        """Read one as-run entry by id (used by the recorder to close an open row)."""
        with self._session() as session:
            row = session.get(AsRunLogEntryDb, entry_id)
            return _entry_to_model(row) if row is not None else None

    def close_entry(
        self,
        *,
        entry_id: str,
        actual_end: datetime,
        duration_s: int,
    ) -> None:
        """Idempotent close of an open as-run row.

        Single ``UPDATE`` that sets ``actual_end`` + ``duration_s`` only on the
        still-open row (``duration_s == 0``). A second close (retry, raced
        teardown) is a no-op rather than a mutation that would overwrite the
        previously-recorded ``actual_end`` (E-3 + E-4 fix). Replaces the prior
        ``get_entry`` + ``append_as_run`` pair: one round-trip instead of two,
        and the immutability of an already-closed row is enforced in SQL.
        """
        with self._session() as session:
            session.execute(
                update(AsRunLogEntryDb)
                .where(AsRunLogEntryDb.entry_id == entry_id)
                .where(AsRunLogEntryDb.duration_s == 0)
                .values(actual_end=actual_end, duration_s=duration_s, updated_at=_now())
            )
            session.commit()

    def list_as_run(
        self,
        station_id: str,
        *,
        channel_id: str | None = None,
        source_kind: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
    ) -> list[AsRunLogEntry]:
        """As-aired records for a station, ordered by ``actual_start``.

        Optional ``channel_id`` filter, ``source_kind`` filter (e.g. ``"spot"``
        for the affidavit-billing hot path — E-5), and a half-open
        ``[from_ts, to_ts)`` window on ``actual_start``. All bound parameters
        (index-path scan)."""
        with self._session() as session:
            stmt = (
                select(AsRunLogEntryDb)
                .where(AsRunLogEntryDb.station_id == station_id)
                .order_by(AsRunLogEntryDb.actual_start, AsRunLogEntryDb.entry_id)
            )
            if channel_id is not None:
                stmt = stmt.where(AsRunLogEntryDb.channel_id == channel_id)
            if source_kind is not None:
                stmt = stmt.where(AsRunLogEntryDb.source_kind == source_kind)
            if from_ts is not None:
                stmt = stmt.where(AsRunLogEntryDb.actual_start >= from_ts)
            if to_ts is not None:
                stmt = stmt.where(AsRunLogEntryDb.actual_start < to_ts)
            return [_entry_to_model(r) for r in session.execute(stmt).scalars().all()]

    # --- EPG configs -----------------------------------------------------

    def upsert_config(self, config: EpgExportConfig) -> EpgExportConfig:
        """Create or update an EPG export config (keyed by ``config_id``)."""
        with self._session() as session:
            row = session.get(EpgExportConfigDb, config.config_id)
            if row is None:
                row = EpgExportConfigDb(config_id=config.config_id, created_at=config.created_at)
                session.add(row)
            row.station_id = config.station_id
            row.channel_id = config.channel_id
            row.format = config.format
            row.horizon_days = config.horizon_days
            row.endpoint = config.endpoint
            row.field_map = dict(config.field_map)
            row.updated_at = _now()
            session.commit()
            return _config_to_model(row)

    def get_config(self, config_id: str) -> EpgExportConfig | None:
        with self._session() as session:
            row = session.get(EpgExportConfigDb, config_id)
            return _config_to_model(row) if row is not None else None

    def list_configs(self, station_id: str) -> list[EpgExportConfig]:
        with self._session() as session:
            stmt = (
                select(EpgExportConfigDb)
                .where(EpgExportConfigDb.station_id == station_id)
                .order_by(EpgExportConfigDb.config_id)
            )
            return [_config_to_model(r) for r in session.execute(stmt).scalars().all()]

    def delete_config(self, config_id: str) -> None:
        with self._session() as session:
            row = session.get(EpgExportConfigDb, config_id)
            if row is None:
                raise EpgConfigNotFoundError(f"EPG config {config_id!r} not found.")
            session.delete(row)
            session.commit()


# --- row → model converters ----------------------------------------------------


def _entry_to_model(row: AsRunLogEntryDb) -> AsRunLogEntry:
    return AsRunLogEntry(
        entry_id=row.entry_id,
        station_id=row.station_id,
        channel_id=row.channel_id,
        schedule_item_id=row.schedule_item_id,
        asset_id=row.asset_id,
        scheduled_start=_as_utc(row.scheduled_start),
        actual_start=_as_utc(row.actual_start),  # type: ignore[arg-type]
        actual_end=_as_utc(row.actual_end),  # type: ignore[arg-type]
        duration_s=row.duration_s,
        source_kind=row.source_kind,  # type: ignore[arg-type]
        verified=row.verified,
        created_at=_as_utc(row.created_at),  # type: ignore[arg-type]
        updated_at=_as_utc(row.updated_at),  # type: ignore[arg-type]
    )


def _config_to_model(row: EpgExportConfigDb) -> EpgExportConfig:
    return EpgExportConfig(
        config_id=row.config_id,
        station_id=row.station_id,
        channel_id=row.channel_id,
        format=row.format,  # type: ignore[arg-type]
        horizon_days=row.horizon_days,
        endpoint=row.endpoint,
        field_map=dict(row.field_map or {}),
        created_at=_as_utc(row.created_at),  # type: ignore[arg-type]
        updated_at=_as_utc(row.updated_at),  # type: ignore[arg-type]
    )


__all__ = [
    "EpgConfigNotFoundError",
    "ReportingStore",
    "ReportingStoreError",
    "SessionFactory",
]
