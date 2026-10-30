# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable store for underwriting spots / flights / placements (S24 slice 1).

Per-request store over the single global session factory (same lazy posture as
eas / ai_models / metadata / reporting). All comparison values are bound
parameters (no string interpolation): per-station + per-channel + per-window
filters ride the indexes defined in migration ``0057_underwriting_spots``.

* ``upsert_spot`` / ``get_spot`` / ``list_spots`` / ``delete_spot`` —
  underwriting-spot CRUD (delete cascades the flights/placements that reference
  the spot via the store layer to keep referential cleanup transactional, since
  the loose-ref convention does not use DB foreign keys).
* ``upsert_flight`` / ``get_flight`` / ``list_flights`` / ``delete_flight`` —
  flight CRUD; ``list_flights`` supports ``spot_id`` and ``active_on``
  (a date) filters that the trafficking compiler (slice 2) calls.
* ``record_placement`` / ``get_placement`` / ``list_placements`` /
  ``delete_placements_for_flight`` — placement persistence written by the
  compiler; ``list_placements`` supports ``channel_id`` + a half-open
  ``[from_ts, to_ts)`` window on ``scheduled_at`` for the operator
  upcoming-and-aired view.

``station_id`` / ``channel_id`` / ``asset_id`` / ``schedule_item_id`` /
``daypart_block_id`` are loose string columns (no DB FK), matching the
codebase's existing convention.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from typing import cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from civiccast.underwriting.models import (
    SpotFlight,
    SpotFlightDb,
    SpotPlacement,
    SpotPlacementDb,
    UnderwritingSpot,
    UnderwritingSpotDb,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


class UnderwritingStoreError(RuntimeError):
    """Base error for underwriting persistence failures."""


class SpotNotFoundError(UnderwritingStoreError):
    """Raised when a ``spot_id`` does not resolve (delete/patch of a missing row)."""


class FlightNotFoundError(UnderwritingStoreError):
    """Raised when a ``flight_id`` does not resolve."""


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a possibly-naive datetime (SQLite drops tz) to UTC-aware."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _join_channels(channels: list[str]) -> str:
    """Serialize a channel slug list to the denormalized ``\\n``-joined column."""
    return "\n".join(channels)


def _split_channels(value: str | None) -> list[str]:
    """Deserialize the denormalized column to a channel slug list."""
    if not value:
        return []
    return [c for c in value.split("\n") if c]


class UnderwritingStore:
    """CRUD over the three S24 tables. Loose-ref convention; no DB FKs."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def _session(self) -> AbstractContextManager[Session]:
        return self._session_factory()

    # --- underwriting spots ---------------------------------------------

    def upsert_spot(self, spot: UnderwritingSpot) -> UnderwritingSpot:
        """Create or update a spot (keyed by ``spot_id``). Idempotent."""
        with self._session() as session:
            row = session.get(UnderwritingSpotDb, spot.spot_id)
            if row is None:
                row = UnderwritingSpotDb(spot_id=spot.spot_id, created_at=spot.created_at)
                session.add(row)
            row.station_id = spot.station_id
            row.underwriter = spot.underwriter
            row.asset_id = spot.asset_id
            row.fcc_compliant_ack = spot.fcc_compliant_ack
            row.review_notes = spot.review_notes
            row.updated_at = _now()
            session.commit()
            return _spot_to_model(row)

    def get_spot(self, spot_id: str) -> UnderwritingSpot | None:
        with self._session() as session:
            row = session.get(UnderwritingSpotDb, spot_id)
            return _spot_to_model(row) if row is not None else None

    def list_spots(
        self,
        station_id: str,
        *,
        underwriter: str | None = None,
    ) -> list[UnderwritingSpot]:
        """Spots for a station, optionally filtered to one underwriter."""
        with self._session() as session:
            stmt = (
                select(UnderwritingSpotDb)
                .where(UnderwritingSpotDb.station_id == station_id)
                .order_by(UnderwritingSpotDb.underwriter, UnderwritingSpotDb.spot_id)
            )
            if underwriter is not None:
                stmt = stmt.where(UnderwritingSpotDb.underwriter == underwriter)
            return [_spot_to_model(r) for r in session.execute(stmt).scalars().all()]

    def delete_spot(self, spot_id: str) -> None:
        """Delete a spot AND every flight + placement that references it.

        The loose-ref convention has no DB cascade, so the store performs the
        cascade transactionally — an orphan flight referencing a vanished spot
        would silently break the affidavit join (the spot row carries
        ``underwriter`` + ``asset_id`` that the affidavit groups by).
        """
        with self._session() as session:
            row = session.get(UnderwritingSpotDb, spot_id)
            if row is None:
                raise SpotNotFoundError(f"Underwriting spot {spot_id!r} not found.")
            # Cascade flights → placements.
            flight_ids = list(
                session.execute(
                    select(SpotFlightDb.flight_id).where(SpotFlightDb.spot_id == spot_id)
                )
                .scalars()
                .all()
            )
            for fid in flight_ids:
                session.execute(delete(SpotPlacementDb).where(SpotPlacementDb.flight_id == fid))
            session.execute(delete(SpotFlightDb).where(SpotFlightDb.spot_id == spot_id))
            session.delete(row)
            session.commit()

    # --- spot flights ---------------------------------------------------

    def upsert_flight(self, flight: SpotFlight) -> SpotFlight:
        """Create or update a flight (keyed by ``flight_id``)."""
        with self._session() as session:
            row = session.get(SpotFlightDb, flight.flight_id)
            if row is None:
                row = SpotFlightDb(flight_id=flight.flight_id, created_at=flight.created_at)
                session.add(row)
            row.spot_id = flight.spot_id
            row.start_date = flight.start_date
            row.end_date = flight.end_date
            row.frequency_cap_per_day = flight.frequency_cap_per_day
            row.daypart_block_id = flight.daypart_block_id
            row.channels = _join_channels(flight.channels)
            row.updated_at = _now()
            session.commit()
            return _flight_to_model(row)

    def get_flight(self, flight_id: str) -> SpotFlight | None:
        with self._session() as session:
            row = session.get(SpotFlightDb, flight_id)
            return _flight_to_model(row) if row is not None else None

    def list_flights(
        self,
        *,
        spot_id: str | None = None,
        active_on: date | None = None,
    ) -> list[SpotFlight]:
        """Flights, optionally narrowed to one spot and/or active on a date.

        ``active_on`` is the trafficking-compiler hot path: returns flights
        whose ``[start_date, end_date]`` window covers the date.
        """
        with self._session() as session:
            stmt = select(SpotFlightDb).order_by(SpotFlightDb.start_date, SpotFlightDb.flight_id)
            if spot_id is not None:
                stmt = stmt.where(SpotFlightDb.spot_id == spot_id)
            if active_on is not None:
                stmt = stmt.where(SpotFlightDb.start_date <= active_on).where(
                    SpotFlightDb.end_date >= active_on
                )
            return [_flight_to_model(r) for r in session.execute(stmt).scalars().all()]

    def delete_flight(self, flight_id: str) -> None:
        """Delete a flight AND every placement that references it."""
        with self._session() as session:
            row = session.get(SpotFlightDb, flight_id)
            if row is None:
                raise FlightNotFoundError(f"Spot flight {flight_id!r} not found.")
            session.execute(delete(SpotPlacementDb).where(SpotPlacementDb.flight_id == flight_id))
            session.delete(row)
            session.commit()

    # --- spot placements ------------------------------------------------

    def record_placement(self, placement: SpotPlacement) -> SpotPlacement:
        """Append-only insertion the trafficking compiler made. Idempotent
        on ``placement_id`` so a re-run of the compiler that re-derives the
        same placement does not duplicate the row."""
        with self._session() as session:
            row = session.get(SpotPlacementDb, placement.placement_id)
            if row is None:
                row = SpotPlacementDb(
                    placement_id=placement.placement_id, created_at=placement.created_at
                )
                session.add(row)
            row.flight_id = placement.flight_id
            row.channel_id = placement.channel_id
            row.scheduled_at = placement.scheduled_at
            row.schedule_item_id = placement.schedule_item_id
            session.commit()
            return _placement_to_model(row)

    def get_placement(self, placement_id: str) -> SpotPlacement | None:
        with self._session() as session:
            row = session.get(SpotPlacementDb, placement_id)
            return _placement_to_model(row) if row is not None else None

    def list_placements(
        self,
        *,
        channel_id: str | None = None,
        flight_id: str | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
    ) -> list[SpotPlacement]:
        """Placements, narrowed by channel and/or flight and/or a half-open
        ``[from_ts, to_ts)`` window on ``scheduled_at`` (the operator
        upcoming-and-aired view DC-1)."""
        with self._session() as session:
            stmt = select(SpotPlacementDb).order_by(
                SpotPlacementDb.scheduled_at, SpotPlacementDb.placement_id
            )
            if channel_id is not None:
                stmt = stmt.where(SpotPlacementDb.channel_id == channel_id)
            if flight_id is not None:
                stmt = stmt.where(SpotPlacementDb.flight_id == flight_id)
            if from_ts is not None:
                stmt = stmt.where(SpotPlacementDb.scheduled_at >= from_ts)
            if to_ts is not None:
                stmt = stmt.where(SpotPlacementDb.scheduled_at < to_ts)
            return [_placement_to_model(r) for r in session.execute(stmt).scalars().all()]

    def count_placements(
        self,
        *,
        channel_id: str | None = None,
        flight_id: str | None = None,
        flight_ids: list[str] | None = None,
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
    ) -> int:
        """Return the number of placements matching the filters (single ``SELECT COUNT(*)``).

        Used by :class:`TraffickingCompiler` on the cap-check hot path to avoid
        materializing the placement rows just to call ``len()`` on them.
        Pure aggregation; no Pydantic round-trip.
        """
        with self._session() as session:
            stmt = select(func.count()).select_from(SpotPlacementDb)
            if channel_id is not None:
                stmt = stmt.where(SpotPlacementDb.channel_id == channel_id)
            if flight_id is not None:
                stmt = stmt.where(SpotPlacementDb.flight_id == flight_id)
            if flight_ids is not None:
                stmt = stmt.where(SpotPlacementDb.flight_id.in_(flight_ids))
            if from_ts is not None:
                stmt = stmt.where(SpotPlacementDb.scheduled_at >= from_ts)
            if to_ts is not None:
                stmt = stmt.where(SpotPlacementDb.scheduled_at < to_ts)
            return int(session.execute(stmt).scalar_one() or 0)

    def count_placements_by_flight(
        self,
        *,
        flight_ids: list[str],
        from_ts: datetime | None = None,
        to_ts: datetime | None = None,
        channel_id: str | None = None,
    ) -> dict[str, int]:
        """Per-flight placement counts for a window (single grouped query).

        Returns a ``{flight_id: count}`` mapping; flight_ids with zero
        placements in the window are present in the result with a value of
        ``0`` so the caller does not need a separate ``.get(flight_id, 0)``.
        Pass ``channel_id=None`` to count across ALL channels the flight
        targets (the spec-correct per-day cap rule — see T-2).
        """
        result: dict[str, int] = dict.fromkeys(flight_ids, 0)
        if not flight_ids:
            return result
        with self._session() as session:
            stmt = (
                select(SpotPlacementDb.flight_id, func.count())
                .where(SpotPlacementDb.flight_id.in_(flight_ids))
                .group_by(SpotPlacementDb.flight_id)
            )
            if channel_id is not None:
                stmt = stmt.where(SpotPlacementDb.channel_id == channel_id)
            if from_ts is not None:
                stmt = stmt.where(SpotPlacementDb.scheduled_at >= from_ts)
            if to_ts is not None:
                stmt = stmt.where(SpotPlacementDb.scheduled_at < to_ts)
            for fid, count in session.execute(stmt).all():
                result[fid] = int(count)
            return result

    def delete_placements_for_flight(self, flight_id: str) -> int:
        """Drop every placement for a flight (used by the compiler when a flight
        is re-resolved). Returns the number of rows deleted."""
        with self._session() as session:
            result = session.execute(
                delete(SpotPlacementDb).where(SpotPlacementDb.flight_id == flight_id)
            )
            session.commit()
            return int(cast(CursorResult[object], result).rowcount or 0)


# --- row → model converters --------------------------------------------------


def _spot_to_model(row: UnderwritingSpotDb) -> UnderwritingSpot:
    return UnderwritingSpot(
        spot_id=row.spot_id,
        station_id=row.station_id,
        underwriter=row.underwriter,
        asset_id=row.asset_id,
        fcc_compliant_ack=bool(row.fcc_compliant_ack),
        review_notes=row.review_notes,
        created_at=_as_utc(row.created_at),  # type: ignore[arg-type]
        updated_at=_as_utc(row.updated_at),  # type: ignore[arg-type]
    )


def _flight_to_model(row: SpotFlightDb) -> SpotFlight:
    return SpotFlight(
        flight_id=row.flight_id,
        spot_id=row.spot_id,
        start_date=row.start_date,
        end_date=row.end_date,
        frequency_cap_per_day=row.frequency_cap_per_day,
        daypart_block_id=row.daypart_block_id,
        channels=_split_channels(row.channels),
        created_at=_as_utc(row.created_at),  # type: ignore[arg-type]
        updated_at=_as_utc(row.updated_at),  # type: ignore[arg-type]
    )


def _placement_to_model(row: SpotPlacementDb) -> SpotPlacement:
    return SpotPlacement(
        placement_id=row.placement_id,
        flight_id=row.flight_id,
        channel_id=row.channel_id,
        scheduled_at=_as_utc(row.scheduled_at),  # type: ignore[arg-type]
        schedule_item_id=row.schedule_item_id,
        created_at=_as_utc(row.created_at),  # type: ignore[arg-type]
    )


__all__ = [
    "FlightNotFoundError",
    "SessionFactory",
    "SpotNotFoundError",
    "UnderwritingStore",
    "UnderwritingStoreError",
]
