# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Asset store Protocol and in-memory implementation.

The Protocol exists so the database-backed store landing at rung 0.3 can
replace ``InMemoryAssetStore`` without touching any caller. The router,
the embed builder, and any future syndication code only depend on the
``AssetStore`` shape.

Sprint 0.3 task 2 (this file) extends the Protocol with ``list()`` and
``create()`` per Director Decision 1 — the v0.2 docstring already
anticipated the extension; no third-party adapters exist yet, so the
extension-in-place is the cleanest path. ``AssetAlreadyExistsError`` is
defined alongside the Protocol per Decisions 1 + 3 — store implementers
raise this domain exception on duplicate ``asset_id``; routers translate
to ``HTTPException(409, ...)``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from civiccast.vod.models import AssetMetadata


class AssetAlreadyExistsError(Exception):
    """Raised by :meth:`AssetStore.create` when the asset_id already exists.

    Carries the offending ``asset_id`` as an attribute so the router (or
    any other caller) can surface a clear operator-readable message
    without re-parsing the exception text. Per Decision 3 — domain
    exception in the store, HTTP translation in the router.
    """

    def __init__(self, *, asset_id: str) -> None:
        super().__init__(f"Asset already exists: {asset_id}")
        self.asset_id = asset_id


@runtime_checkable
class AssetStore(Protocol):
    """Asset store Protocol.

    Sprint 0.3 added ``list()`` and ``create()`` to the v0.2 ``get()``
    surface. Mutating callers (the staff POST endpoint) use ``create``;
    public read callers (list + get endpoints) use ``list``/``get``.
    """

    def get(self, asset_id: str) -> AssetMetadata | None:
        """Return the asset metadata, or None if not found."""
        ...

    def list(self) -> list[AssetMetadata]:
        """Return every asset in the store as a list.

        Ordering is implementation-defined; conformance tests assert
        presence (set comparison), not order, so the natural ordering of
        each backend stays pluggable.
        """
        ...

    def create(self, asset: AssetMetadata) -> AssetMetadata:
        """Persist ``asset`` and return the canonical persisted form.

        Raises :class:`AssetAlreadyExistsError` when ``asset.asset_id``
        already exists in the store. The return value is the canonical
        persisted asset (re-fetched after write) so callers see any
        server-side normalization (e.g., timestamp tzinfo coercion).
        """
        ...


def _list_sort_key(asset: AssetMetadata) -> tuple[bool, float, str]:
    """Sort key for ``list()``: published_at DESC NULLS LAST, asset_id ASC.

    Tuple-key handles ``published_at is None`` by sorting True (None)
    after False (not-None); the negated timestamp gives DESC for
    populated values; ``asset_id`` is the deterministic tiebreaker.
    """
    if asset.published_at is None:
        return (True, 0.0, asset.asset_id)
    return (False, -asset.published_at.timestamp(), asset.asset_id)


class InMemoryAssetStore:
    """Sprint 0.2 reference store: a dict.

    Useful for tests, for demos, and for stations that want to manually
    seed a handful of assets via a config file before the schedule
    module exists. Replaced by a Postgres-backed store at 0.3.
    """

    def __init__(self, assets: dict[str, AssetMetadata] | None = None) -> None:
        self._assets: dict[str, AssetMetadata] = dict(assets or {})

    def get(self, asset_id: str) -> AssetMetadata | None:
        return self._assets.get(asset_id)

    def put(self, asset: AssetMetadata) -> None:
        """Insert or replace. Not part of the AssetStore Protocol — only
        the in-memory store exposes mutation via this idempotent path."""
        self._assets[asset.asset_id] = asset

    def list(self) -> list[AssetMetadata]:
        """Return every stored asset, sorted by (published_at DESC NULLS LAST,
        asset_id ASC). Conformance tests rely on set-comparison so the order
        is ergonomic, not contractual."""
        return sorted(self._assets.values(), key=_list_sort_key)

    def create(self, asset: AssetMetadata) -> AssetMetadata:
        """Persist ``asset``; raise :class:`AssetAlreadyExistsError` on
        duplicate ``asset_id``. Returns the persisted asset."""
        if asset.asset_id in self._assets:
            raise AssetAlreadyExistsError(asset_id=asset.asset_id)
        self._assets[asset.asset_id] = asset
        return self._assets[asset.asset_id]
