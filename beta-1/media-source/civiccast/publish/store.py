# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Persistence seam for v0.7 publish-run records."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from civiccast.publish.models import PublishRunRecord, PublishSurfaceStatus

SessionFactory = Callable[[], AbstractContextManager[Session]]


# Bind-parameter ceiling for one batched IN(...) fetch. Postgres allows 65535
# and modern SQLite 32766, but older SQLite builds cap at 999 -- and the
# installer-managed local store IS SQLite. 500 stays comfortably under every
# supported backend while keeping the query count bounded by chunk size rather
# than by the station's whole recording history (GauntletGate PE-1).
PUBLISH_RUN_FETCH_CHUNK = 500


class PublishStore(Protocol):
    def get_run(self, asset_id: str) -> PublishRunRecord | None: ...
    def get_runs(self, asset_ids: Sequence[str]) -> dict[str, PublishRunRecord]: ...
    def upsert_run(self, record: PublishRunRecord) -> PublishRunRecord: ...
    def replace_surfaces(
        self,
        asset_id: str,
        expected: list[PublishSurfaceStatus],
        updated: list[PublishSurfaceStatus],
    ) -> bool: ...


class InMemoryPublishStore:
    """In-memory publish store for tests and no-DB local runs."""

    def __init__(self) -> None:
        self._runs: dict[str, PublishRunRecord] = {}

    def get_run(self, asset_id: str) -> PublishRunRecord | None:
        return self._runs.get(asset_id)

    def get_runs(self, asset_ids: Sequence[str]) -> dict[str, PublishRunRecord]:
        return {asset_id: self._runs[asset_id] for asset_id in asset_ids if asset_id in self._runs}

    def upsert_run(self, record: PublishRunRecord) -> PublishRunRecord:
        self._runs[record.asset_id] = record
        return record

    def replace_surfaces(
        self,
        asset_id: str,
        expected: list[PublishSurfaceStatus],
        updated: list[PublishSurfaceStatus],
    ) -> bool:
        run = self._runs.get(asset_id)
        if run is None or run.surfaces != expected:
            return False
        self._runs[asset_id] = run.model_copy(update={"surfaces": updated})
        return True


class PostgresPublishStore:
    """SQLAlchemy-backed publish-run store for durable v0.7 approvals."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def get_run(self, asset_id: str) -> PublishRunRecord | None:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            row = session.execute(
                text(
                    f"SELECT asset_id, operator_id, operator_display_name, approved_at, "  # noqa: S608 - table prefix is fixed by SQLAlchemy dialect, not user input.  # nosec B608
                    f"surfaces_json, audit_events_json FROM {table}publish_runs "
                    "WHERE asset_id = :asset_id"
                ),
                {"asset_id": asset_id},
            ).first()
            if row is None:
                return None
            return PublishRunRecord.model_validate(
                {
                    "asset_id": row.asset_id,
                    "operator_id": row.operator_id,
                    "operator_display_name": row.operator_display_name,
                    "approved_at": row.approved_at,
                    "surfaces": json.loads(row.surfaces_json),
                    "audit_events": json.loads(row.audit_events_json),
                }
            )

    def get_runs(self, asset_ids: Sequence[str]) -> dict[str, PublishRunRecord]:
        """Fetch many publish runs in a bounded number of round trips.

        The publish dashboard used to call :meth:`get_run` once per asset, each
        opening its own session and issuing its own SELECT -- a query count that
        grew with every meeting the station had ever recorded (GauntletGate
        PE-1). Assets with no publish run are simply absent from the result,
        matching :meth:`get_run` returning ``None``.
        """

        unique_ids = list(dict.fromkeys(asset_ids))
        if not unique_ids:
            return {}
        found: dict[str, PublishRunRecord] = {}
        with self._session_factory() as session:
            table = self._table_prefix(session)
            for chunk in _chunked(unique_ids, PUBLISH_RUN_FETCH_CHUNK):
                # Bind every id as its own named parameter: the ids reach SQL
                # only through binds, never through the f-string.
                placeholders = ", ".join(f":id_{index}" for index in range(len(chunk)))
                rows = session.execute(
                    text(
                        f"SELECT asset_id, operator_id, operator_display_name, approved_at, "  # noqa: S608 - table prefix is fixed by SQLAlchemy dialect and placeholders are generated names, not user input.  # nosec B608
                        f"surfaces_json, audit_events_json FROM {table}publish_runs "
                        f"WHERE asset_id IN ({placeholders})"
                    ),
                    {f"id_{index}": asset_id for index, asset_id in enumerate(chunk)},
                ).all()
                for row in rows:
                    found[row.asset_id] = PublishRunRecord.model_validate(
                        {
                            "asset_id": row.asset_id,
                            "operator_id": row.operator_id,
                            "operator_display_name": row.operator_display_name,
                            "approved_at": row.approved_at,
                            "surfaces": json.loads(row.surfaces_json),
                            "audit_events": json.loads(row.audit_events_json),
                        }
                    )
        return found

    def upsert_run(self, record: PublishRunRecord) -> PublishRunRecord:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            existing = session.execute(
                text(
                    f"SELECT asset_id FROM {table}publish_runs WHERE asset_id = :asset_id"  # noqa: S608 - table prefix is fixed by SQLAlchemy dialect, not user input.  # nosec B608
                ),
                {"asset_id": record.asset_id},
            ).first()
            params = {
                "asset_id": record.asset_id,
                "operator_id": record.operator_id,
                "operator_display_name": record.operator_display_name,
                "approved_at": self._datetime_param(session, record.approved_at),
                "surfaces_json": json.dumps(
                    [surface.model_dump(mode="json") for surface in record.surfaces],
                    sort_keys=True,
                ),
                "audit_events_json": json.dumps(
                    [event.model_dump(mode="json") for event in record.audit_events],
                    sort_keys=True,
                ),
                "updated_at": self._datetime_param(session, datetime.now(UTC)),
            }
            if existing is None:
                session.execute(
                    text(
                        f"INSERT INTO {table}publish_runs "  # noqa: S608 - table prefix is fixed by SQLAlchemy dialect, not user input.  # nosec B608
                        "(asset_id, operator_id, operator_display_name, approved_at, "
                        "surfaces_json, audit_events_json, updated_at) "
                        "VALUES (:asset_id, :operator_id, :operator_display_name, "
                        ":approved_at, :surfaces_json, :audit_events_json, :updated_at)"
                    ),
                    params,
                )
            else:
                session.execute(
                    text(
                        f"UPDATE {table}publish_runs SET "  # noqa: S608 - table prefix is fixed by SQLAlchemy dialect, not user input.  # nosec B608
                        "operator_id = :operator_id, "
                        "operator_display_name = :operator_display_name, "
                        "approved_at = :approved_at, "
                        "surfaces_json = :surfaces_json, "
                        "audit_events_json = :audit_events_json, "
                        "updated_at = :updated_at "
                        "WHERE asset_id = :asset_id"
                    ),
                    params,
                )
            session.commit()
            return record

    def replace_surfaces(
        self,
        asset_id: str,
        expected: list[PublishSurfaceStatus],
        updated: list[PublishSurfaceStatus],
    ) -> bool:
        """Keep asynchronous output polling from overwriting another surface's result."""
        with self._session_factory() as session:
            table = self._table_prefix(session)
            result = session.execute(
                text(
                    f"UPDATE {table}publish_runs SET surfaces_json = :updated, "  # noqa: S608 - fixed dialect prefix.  # nosec B608
                    "updated_at = :at WHERE asset_id = :asset_id AND surfaces_json = :expected "
                    "RETURNING asset_id"
                ),
                {
                    "asset_id": asset_id,
                    "expected": json.dumps(
                        [s.model_dump(mode="json") for s in expected], sort_keys=True
                    ),
                    "updated": json.dumps(
                        [s.model_dump(mode="json") for s in updated], sort_keys=True
                    ),
                    "at": self._datetime_param(session, datetime.now(UTC)),
                },
            )
            changed = result.first() is not None
            session.commit()
            return changed

    @staticmethod
    def _table_prefix(session: Session) -> str:
        bind = session.get_bind()
        return "" if bind.dialect.name == "sqlite" else "civiccast."

    @staticmethod
    def _datetime_param(session: Session, value: datetime) -> datetime | str:
        bind = session.get_bind()
        if bind.dialect.name == "sqlite":
            return value.isoformat()
        return value


def _chunked(values: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
