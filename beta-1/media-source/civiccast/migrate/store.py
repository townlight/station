# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Persistence for the import batch/provenance ledger.

This store owns exactly two tables — ``import_batches`` and
``import_batch_items`` (migration ``0068_migrate_batches``). It does NOT
touch ``civiccast.assets`` or ``civiccast.schedule_items`` — those real
stores are written directly by :mod:`civiccast.migrate.service` (via
:class:`civiccast.schedule.store.PostgresScheduleStore` for schedule items,
and direct :class:`civiccast.schedule.models.Asset` rows for shows, since
the schedule module's own ``PostgresAssetStore.create`` requires a packaged
HLS ``manifest_url`` that an import — metadata + a pointer, not copied media
— does not have yet). This store is only the ledger of what happened.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.migrate.models import (
    ImportBatch,
    ImportBatchDb,
    ImportBatchItem,
    ImportBatchItemDb,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


class MigrationStoreError(RuntimeError):
    """Base error for the migration ledger."""


class BatchNotFoundError(MigrationStoreError):
    """Raised when ``import_batch_id`` does not resolve to a ledger row."""


class BatchAlreadyRolledBackError(MigrationStoreError):
    """Raised when rollback is requested twice for the same batch."""


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class MigrationStore:
    """CRUD over the two ledger tables."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def create_batch(self, import_batch_id: str, source_system: str) -> ImportBatch:
        with self._session_factory() as session:
            row = ImportBatchDb(
                import_batch_id=import_batch_id,
                source_system=source_system,
                status="applied",
            )
            session.add(row)
            session.commit()
            return _batch_to_model(row, shows_created=0, schedule_items_created=0)

    def add_item(
        self,
        *,
        import_batch_id: str,
        entity_type: str,
        entity_id: str,
        source_ref: str,
    ) -> None:
        with self._session_factory() as session:
            session.add(
                ImportBatchItemDb(
                    import_batch_id=import_batch_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    source_ref=source_ref,
                )
            )
            session.commit()

    def get_batch(self, import_batch_id: str) -> ImportBatch | None:
        with self._session_factory() as session:
            row = session.get(ImportBatchDb, import_batch_id)
            if row is None:
                return None
            return _batch_to_model(row, **self._counts(session, import_batch_id))

    def list_batches(self) -> list[ImportBatch]:
        with self._session_factory() as session:
            rows = (
                session.execute(select(ImportBatchDb).order_by(ImportBatchDb.created_at.desc()))
                .scalars()
                .all()
            )
            return [
                _batch_to_model(row, **self._counts(session, row.import_batch_id)) for row in rows
            ]

    def list_items(self, import_batch_id: str) -> list[ImportBatchItem]:
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(ImportBatchItemDb).where(
                        ImportBatchItemDb.import_batch_id == import_batch_id
                    )
                )
                .scalars()
                .all()
            )
            return [_item_to_model(row) for row in rows]

    def mark_rolled_back(self, import_batch_id: str) -> ImportBatch:
        with self._session_factory() as session:
            row = session.get(ImportBatchDb, import_batch_id)
            if row is None:
                raise BatchNotFoundError(f"Import batch {import_batch_id!r} not found.")
            if row.status == "rolled_back":
                raise BatchAlreadyRolledBackError(
                    f"Import batch {import_batch_id!r} was already rolled back."
                )
            row.status = "rolled_back"
            row.rolled_back_at = _now()
            session.commit()
            return _batch_to_model(row, **self._counts(session, import_batch_id))

    def _counts(self, session: Session, import_batch_id: str) -> dict[str, int]:
        rows = (
            session.execute(
                select(ImportBatchItemDb.entity_type).where(
                    ImportBatchItemDb.import_batch_id == import_batch_id
                )
            )
            .scalars()
            .all()
        )
        return {
            "shows_created": sum(1 for t in rows if t == "asset"),
            "schedule_items_created": sum(1 for t in rows if t == "schedule_item"),
        }


def _batch_to_model(
    row: ImportBatchDb, *, shows_created: int, schedule_items_created: int
) -> ImportBatch:
    return ImportBatch(
        import_batch_id=row.import_batch_id,
        source_system=row.source_system,
        status=row.status,  # type: ignore[arg-type]
        shows_created=shows_created,
        schedule_items_created=schedule_items_created,
        created_at=_as_utc(row.created_at),  # type: ignore[arg-type]
        rolled_back_at=_as_utc(row.rolled_back_at),
    )


def _item_to_model(row: ImportBatchItemDb) -> ImportBatchItem:
    return ImportBatchItem(
        import_batch_id=row.import_batch_id,
        entity_type=row.entity_type,  # type: ignore[arg-type]
        entity_id=row.entity_id,
        source_ref=row.source_ref,
        created_at=_as_utc(row.created_at),  # type: ignore[arg-type]
    )


__all__ = [
    "BatchAlreadyRolledBackError",
    "BatchNotFoundError",
    "MigrationStore",
    "MigrationStoreError",
    "SessionFactory",
]
