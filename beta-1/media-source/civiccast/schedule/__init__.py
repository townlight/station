# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""civiccast.schedule — VOD asset schedule + Postgres-backed AssetStore + HTTP API.

Sprint 0.3 task 1b + task 2. Public re-exports:

* :class:`Asset` — SQLAlchemy 2.0 declarative model for the
  ``civiccast.assets`` table.
* :class:`AssetMetadata` — schedule-local Pydantic peer mirroring
  :class:`civiccast.vod.models.AssetMetadata` (long-term convergence
  tracked in next-cleanup.md per Director Decision 2).
* :class:`PostgresAssetStore` — :class:`civiccast.vod.store.AssetStore`
  Protocol implementation backed by SQLAlchemy + Postgres (or any
  SA-supported dialect — ephemeral SQLite for fast tests, real Postgres
  for the ADR 0008 §Compliance test).
* :data:`public_router` / :data:`staff_router` — FastAPI APIRouter
  instances mounted under ``/api/public`` and ``/api/staff`` by
  ``civiccast.app.create_app``. The ``get_asset_store`` dependency is
  the seam the app factory overrides when ``DATABASE_URL`` is set.

Importing this package registers the ``Asset`` model with
``Base.metadata`` so Alembic autogenerate workflows discover it.
"""

from __future__ import annotations

from civiccast.schedule.models import Asset, AssetMetadata
from civiccast.schedule.router import (
    get_asset_store,
    public_router,
    staff_router,
)
from civiccast.schedule.store import PostgresAssetStore

__all__ = [
    "Asset",
    "AssetMetadata",
    "PostgresAssetStore",
    "get_asset_store",
    "public_router",
    "staff_router",
]
