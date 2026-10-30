# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable persistence for S13 operator AI-model selection.

Per-request store over the single global session factory (same lazy posture as the
eas / control_room stores). Two durable entities:

* ``ai_model_configuration`` — the station-wide config singleton (created/updated stamps).
* ``feature_model_registry`` — the per-feature operator selection, soft-delete aware so a
  selection can be cleared and re-created. A partial-unique index keeps at most one LIVE
  row per feature; clearing soft-deletes the live row (history is preserved).

Catalog data (the available tiers, costs, the local default) is NOT stored here — it is
hard-coded in :mod:`civiccast.ai_models.catalog` (decision A). This store only persists
the operator's *choice*.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.ai_models.models import (
    AiModelConfiguration,
    AiModelConfigurationDb,
    FeatureModelRegistryDb,
    ModelTierBand,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]

# The singleton row id for the global config table.
_CONFIG_ID = "default"


class AiModelStoreError(RuntimeError):
    """Base error for AI-model persistence failures."""


class FeatureNotFoundError(AiModelStoreError):
    """Raised when a feature has no live selection row to act on."""


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Normalize a possibly-naive datetime (SQLite drops tz) to UTC-aware."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class AiModelStore:
    """CRUD for the per-feature operator selection + the global config singleton."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def _session(self) -> AbstractContextManager[Session]:
        return self._session_factory()

    # --- selections ------------------------------------------------------

    def _live_row(self, session: Session, feature: str) -> FeatureModelRegistryDb | None:
        return session.execute(
            select(FeatureModelRegistryDb).where(
                FeatureModelRegistryDb.feature == feature,
                FeatureModelRegistryDb.deleted_at.is_(None),
            )
        ).scalar_one_or_none()

    def get_selection(self, feature: str) -> str | None:
        """The operator's currently-selected model_key for ``feature`` (or None)."""
        with self._session() as session:
            row = self._live_row(session, feature)
            return row.model_key if row is not None else None

    def get_selection_row(self, feature: str) -> FeatureModelRegistryDb | None:
        """The full live selection row for ``feature`` (incl. consent audit) or None.

        The dispatch seam needs the persisted consent flag — not just the model_key —
        to construct a cloud adapter from durable state.
        """
        with self._session() as session:
            row = self._live_row(session, feature)
            return _detach(row) if row is not None else None

    def set_selection(
        self,
        feature: str,
        *,
        model_key: str,
        tier: ModelTierBand,
        consent_accepted: bool = False,
        consent_actor: str | None = None,
    ) -> FeatureModelRegistryDb:
        """Upsert the operator's selection for ``feature`` (one live row per feature).

        For a cloud/frontier band, ``consent_accepted`` records the operator's TOS
        acceptance and stamps ``consent_at`` (who/when), so the billable, content-
        egressing choice is auditable from durable state. Local selections clear any
        prior consent stamp (the new selection does not egress).
        """
        with self._session() as session:
            row = self._live_row(session, feature)
            if row is None:
                row = FeatureModelRegistryDb(
                    registry_id=uuid.uuid4().hex,
                    feature=feature,
                    created_at=_now(),
                )
                session.add(row)
            row.model_key = model_key
            row.tier = tier
            row.consent_accepted = consent_accepted
            row.consent_actor = consent_actor if consent_accepted else None
            row.consent_at = _now() if consent_accepted else None
            row.updated_at = _now()
            session.commit()
            session.refresh(row)
            return _detach(row)

    def clear_selection(self, feature: str) -> None:
        """Soft-delete the live selection for ``feature`` (reverts to the default)."""
        with self._session() as session:
            row = self._live_row(session, feature)
            if row is None:
                raise FeatureNotFoundError(f"No live model selection for feature {feature!r}.")
            row.deleted_at = _now()
            row.updated_at = _now()
            session.commit()

    def list_selections(self) -> list[FeatureModelRegistryDb]:
        """Every LIVE per-feature selection (soft-deleted rows excluded)."""
        with self._session() as session:
            rows = (
                session.execute(
                    select(FeatureModelRegistryDb)
                    .where(FeatureModelRegistryDb.deleted_at.is_(None))
                    .order_by(FeatureModelRegistryDb.feature)
                )
                .scalars()
                .all()
            )
            return [_detach(r) for r in rows]

    # --- global config singleton -----------------------------------------

    def get_or_create_configuration(self) -> AiModelConfiguration:
        """Return the station-wide config row, creating the singleton on first use."""
        with self._session() as session:
            row = session.get(AiModelConfigurationDb, _CONFIG_ID)
            if row is None:
                row = AiModelConfigurationDb(
                    config_id=_CONFIG_ID, created_at=_now(), updated_at=_now()
                )
                session.add(row)
                session.commit()
                session.refresh(row)
            return AiModelConfiguration(
                created_at=_as_utc(row.created_at),
                updated_at=_as_utc(row.updated_at),
                features={},
            )


def _detach(row: FeatureModelRegistryDb) -> FeatureModelRegistryDb:
    """A tz-normalized, session-independent copy of a selection row."""
    return FeatureModelRegistryDb(
        registry_id=row.registry_id,
        feature=row.feature,
        model_key=row.model_key,
        tier=row.tier,
        consent_accepted=bool(row.consent_accepted),
        consent_actor=row.consent_actor,
        consent_at=_as_utc(row.consent_at) if row.consent_at else None,
        deleted_at=_as_utc(row.deleted_at) if row.deleted_at else None,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


__all__ = [
    "AiModelStore",
    "AiModelStoreError",
    "FeatureNotFoundError",
    "SessionFactory",
]
