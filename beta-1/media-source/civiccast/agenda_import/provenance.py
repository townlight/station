# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Import provenance: which agenda came from which vendor event (plan §5 Open
Question 5, resolved here as "yes, side table").

A new, additive, agenda_import-owned table (migration ``0067_agenda_import_
provenance``) -- NOT columns bolted onto ``meeting_agendas`` -- so this
package never has to modify :mod:`civiccast.agenda.models` /
``civiccast.agenda.store`` to add its own bookkeeping. One row per agenda_id
(a re-import overwrites the previous provenance row with the latest source/
client/external_id), enabling a future "refresh from source" button. This
row is bookkeeping only -- idempotency itself is enforced by
:mod:`civiccast.agenda_import.mapper` via the existing
``(agenda_id, order)`` skip rule, not by anything in this table.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from civiccast.agenda_import.models import AgendaSourceName
from civiccast.db import Base

SessionFactory = Callable[[], AbstractContextManager[Session]]

_TABLE = "agenda_import_provenance"


class AgendaImportProvenance(BaseModel):
    """Read-model for one provenance row (session-detached, safe to return)."""

    model_config = ConfigDict(extra="forbid")

    agenda_id: str
    source: str
    client_code: str
    external_id: str
    imported_at: datetime


class AgendaImportProvenanceDb(Base):
    """Durable "this agenda was last imported from X" row."""

    __tablename__ = _TABLE

    agenda_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    client_code: Mapped[str] = mapped_column(String(120), nullable=False)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgendaImportProvenanceStore:
    """CRUD over the one provenance table."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def record_import(
        self, *, agenda_id: str, source: AgendaSourceName, client_code: str, external_id: str
    ) -> AgendaImportProvenance:
        """Upsert the provenance row for ``agenda_id``. Latest import wins."""
        with self._session_factory() as session:
            row = session.get(AgendaImportProvenanceDb, agenda_id)
            if row is None:
                row = AgendaImportProvenanceDb(agenda_id=agenda_id)
                session.add(row)
            row.source = source
            row.client_code = client_code
            row.external_id = external_id
            row.imported_at = datetime.now(UTC)
            session.commit()
            return _to_model(row)

    def get(self, agenda_id: str) -> AgendaImportProvenance | None:
        with self._session_factory() as session:
            row = session.get(AgendaImportProvenanceDb, agenda_id)
            return _to_model(row) if row is not None else None


def _to_model(row: AgendaImportProvenanceDb) -> AgendaImportProvenance:
    return AgendaImportProvenance(
        agenda_id=row.agenda_id,
        source=row.source,
        client_code=row.client_code,
        external_id=row.external_id,
        imported_at=row.imported_at
        if row.imported_at.tzinfo
        else row.imported_at.replace(tzinfo=UTC),
    )


__all__ = [
    "AgendaImportProvenance",
    "AgendaImportProvenanceDb",
    "AgendaImportProvenanceStore",
    "SessionFactory",
]
