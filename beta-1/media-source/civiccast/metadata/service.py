# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S22 CustomFieldService — validation + orchestration over the durable store.

The store (:class:`civiccast.metadata.store.CustomFieldStore`) owns persistence and the
type-shape validation it can do without external lookups (list-is-an-option,
number/date denormalization, boolean canonicalization, required-present, key
immutability, delete-safety). This service is the seam that adds the two contracts the
store deliberately leaves to it (spec §6):

* **Reference resolution** — ``asset_ref`` / ``producer_ref`` values must resolve to an
  existing asset / producer. That needs the asset / producer lookups, which are injected
  as ``Callable[[str], bool]`` resolvers so the service is unit-testable offline (the
  ai_models injected-probe convention). Resolution runs BEFORE the store write so a bad
  reference is never persisted (a failed ref rolls the whole value set back — full-replace
  is atomic in the store, and the service refuses to call it on an unresolved ref).

* **Orchestration** — def CRUD (``create_field`` / ``update_field`` / ``delete_field``)
  and per-asset value read/set, where ``set_asset_values`` resolves the station's
  definitions itself (the store needs the full def set to validate) so the router does not
  have to. ``get_public_asset_values`` projects an asset's values down to the
  searchable+api_exposed fields — the DC-5 public-exposure boundary helper
  ``search_public_assets`` builds every asset's public custom-field list through, so a
  non-exposed value can never leak and there is one projection path.

The service is router-agnostic; the router (slice 3) maps its errors to HTTP. Errors:
:class:`FieldReferenceError` subclasses :class:`FieldValidationError` so a single 422 path
catches every typed-validation failure (a bad type-shape AND an unresolved reference).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from datetime import date

from sqlalchemy.orm import Session

from civiccast.metadata.models import (
    AssetMetadata,
    CustomFieldDef,
    CustomFieldType,
    CustomFieldValue,
    PublicCustomFieldValue,
    PublicSearchAsset,
    PublicTranscript,
    PublicTranscriptCue,
)
from civiccast.metadata.store import (
    CustomFieldStore,
    FieldPredicate,
    FieldValidationError,
)

# Injected existence probes. ``asset_exists(asset_id)`` / ``producer_exists(producer_id)``
# return True iff the referenced entity exists. Injected so the service is testable offline
# and never hard-couples to the asset/contributor stores at import time.
AssetResolver = Callable[[str], bool]
ProducerResolver = Callable[[str], bool]

# Injected supplier of the packaged public asset list (the projection the public-search
# endpoint filters over). Injected so the service is testable offline and never imports the
# asset store at module load; the default binds the real ``PostgresAssetStore.list()``.
PublicAssetLister = Callable[[], list[AssetMetadata]]

# The typed kinds whose ``value`` is an id that must resolve to an existing entity.
_REF_TYPES: frozenset[CustomFieldType] = frozenset({"asset_ref", "producer_ref"})


class FieldReferenceError(FieldValidationError):
    """Raised when an ``asset_ref`` / ``producer_ref`` value does not resolve.

    Subclasses :class:`FieldValidationError` so the router's single 422 path catches an
    unresolved reference alongside the store's type-shape failures (spec §6: a reference
    that does not resolve is a typed-validation failure, not a server fault).
    """


def _durable_session() -> AbstractContextManager[Session]:
    """A per-call session contextmanager over the bound (durable) engine.

    Drains the ``get_session`` generator so its ``finally:`` closes the session at exit —
    the same posture ``app._wire_durable_stores`` uses. Lazy-imported so this module never
    pulls the DB engine at import time; works against whatever engine is bound (real
    Postgres in prod, SQLite in tests).
    """
    from civiccast.db import get_session

    @contextmanager
    def _scope() -> Iterator[Session]:
        gen = get_session()
        session = next(gen)
        try:
            yield session
        finally:
            with suppress(StopIteration):
                next(gen)

    return _scope()


def _default_asset_exists(asset_id: str) -> bool:
    """Resolve an ``asset_ref`` against the real asset store (lazy import, durable DB).

    Uses the operator-side row lookup (``get_staff_row``) so an uploaded-but-not-yet-
    packaged asset still resolves — a custom-field reference to a library asset must hold
    regardless of publish state. The store is wired with the durable session factory in
    ``app._wire_durable_stores``; this default is the offline-safe fallback that binds the
    same bound engine without importing ``app`` (no circular import).
    """
    from civiccast.schedule.store import PostgresAssetStore

    return PostgresAssetStore(_durable_session).get_staff_row(asset_id) is not None


def _default_producer_exists(producer_id: str) -> bool:
    """Resolve a ``producer_ref`` against durable producer (contributor-account) identity.

    A producer is a contributor account (``ContributorAccount.account_id``). Resolution keys
    off the durable producer-identity ledger (``known_producer_ids`` — the distinct account
    ids the station has on record across ALL submissions in ANY state), NOT "has a submission
    sitting in the operator review queue": a valid producer whose submissions are all
    published/declined/purged still resolves.

    The store is constructed over the SAME durable path the contributor router uses
    (``default_contributor_store_path``), not the path-less ephemeral default — so the
    identity set actually reflects persisted producers. Imported lazily so this module never
    pulls the contribute package at import time.
    """
    from civiccast.contribute.store import (
        ContributorSubmissionStore,
        default_contributor_store_path,
    )

    store = ContributorSubmissionStore(default_contributor_store_path())
    return producer_id in store.known_producer_ids()


def _default_public_assets() -> list[AssetMetadata]:
    """The packaged public asset list (manifest_url IS NOT NULL), durable DB.

    Mirrors ``GET /api/public/assets`` exactly: ``PostgresAssetStore.list()`` returns
    packaged-only assets, so the public custom-field search filters the same corpus the
    portal already shows. Lazy-imported so this module never pulls the asset store at import
    time; binds the same bound engine as the rest of the durable wiring.
    """
    from civiccast.schedule.store import PostgresAssetStore

    return PostgresAssetStore(_durable_session).list()


class CustomFieldService:
    """Validate + orchestrate custom-field defs and per-asset values over the store."""

    def __init__(
        self,
        store: CustomFieldStore,
        *,
        asset_exists: AssetResolver = _default_asset_exists,
        producer_exists: ProducerResolver = _default_producer_exists,
        public_asset_lister: PublicAssetLister = _default_public_assets,
        transcript_provider: Callable[[str], list[PublicTranscriptCue]] = lambda _asset: [],
    ) -> None:
        self._store = store
        self._asset_exists = asset_exists
        self._producer_exists = producer_exists
        self._public_asset_lister = public_asset_lister
        self._transcript_provider = transcript_provider

    def public_transcript(
        self, asset_id: str, *, transcript_access: Callable[[str], bool]
    ) -> PublicTranscript | None:
        if not any(
            asset.asset_id == asset_id and asset.published_at is not None
            for asset in self._public_asset_lister()
        ):
            return None
        if not transcript_access(asset_id):
            raise PermissionError("Sign in with access to this recording to read its transcript.")
        cues = self._transcript_provider(asset_id)
        return PublicTranscript(asset_id=asset_id, available=bool(cues), cues=cues)

    # --- field definitions ----------------------------------------------

    def create_field(self, definition: CustomFieldDef) -> CustomFieldDef:
        """Create a new field def without replacing an existing ID or key."""
        return self._store.upsert_def(definition, create_only=True)

    def update_field(self, definition: CustomFieldDef) -> CustomFieldDef:
        """Update a field def. The store rejects a change to the immutable ``key``."""
        return self._store.upsert_def(definition)

    def get_field(self, field_id: str) -> CustomFieldDef | None:
        return self._store.get_def(field_id)

    def list_fields(self, station_id: str, *, exposed_only: bool = False) -> list[CustomFieldDef]:
        """A station's field defs, ordered. ``exposed_only`` -> searchable+api_exposed."""
        return self._store.list_defs(station_id, exposed_only=exposed_only)

    def delete_field(self, field_id: str, *, confirm: bool = False) -> None:
        """Delete a field def. Blocked if values exist unless ``confirm`` cascades them."""
        self._store.delete_def(field_id, confirm=confirm)

    # --- asset values ----------------------------------------------------

    def get_asset_values(self, asset_id: str) -> list[CustomFieldValue]:
        """This asset's custom-field values (empty list = the valid zero-state)."""
        return self._store.get_values(asset_id)

    def get_public_asset_values(self, asset_id: str, *, station_id: str) -> list[CustomFieldValue]:
        """This asset's values filtered to the public-exposure set (DC-5).

        Only values for fields that are BOTH ``searchable`` and ``api_exposed`` are
        returned; a non-exposed field's value can never reach the public projection
        through this helper. ``search_public_assets`` builds every asset's public
        custom-field list through this one server-side filter, so the exposure boundary is
        never a client-side concern and there is a single projection path.
        """
        exposed_ids = {d.field_id for d in self._store.list_defs(station_id, exposed_only=True)}
        return [v for v in self._store.get_values(asset_id) if v.field_id in exposed_ids]

    def set_asset_values(
        self,
        asset_id: str,
        values: list[CustomFieldValue],
        *,
        station_id: str,
    ) -> list[CustomFieldValue]:
        """Full-replace this asset's values, typed-validated against the station's defs.

        The service resolves the station's definition set itself (the store needs the full
        set to validate required-present and per-field types), runs reference resolution
        for ``asset_ref`` / ``producer_ref`` values BEFORE the write (a failed reference
        raises :class:`FieldReferenceError` and nothing persists), then delegates the typed
        validation + atomic full-replace to the store. Reference resolution canonicalizes
        each ref value to the stripped form it validated, so the stored ``value`` matches
        what resolved (an exact ``cf.<key>=<id>`` / S19 eq filter then agrees).
        """
        definitions = self._store.list_defs(station_id)
        defs_by_id = {d.field_id: d for d in definitions}
        resolved = self._resolve_references(values, defs_by_id)
        return self._store.set_values(asset_id, resolved, definitions=definitions)

    # --- public search ---------------------------------------------------

    def search_public_assets(
        self,
        *,
        station_id: str,
        query_params: Mapping[str, str],
        transcript_access: Callable[[str], bool] = lambda _asset: False,
    ) -> list[PublicSearchAsset]:
        """Public custom-field search over packaged assets (the DC-5 exposure boundary).

        ``query_params`` is the raw request query map; this parses the ``cf.<key>`` family:

        * ``cf.<key>=<value>``           — exact match on the canonical ``value``;
        * ``cf.<key>_gte`` / ``cf.<key>_lte`` — numeric (value_num) OR date (value_date)
          range bounds, switched on the field's type.

        Only fields that are BOTH ``searchable`` and ``api_exposed`` are eligible: a key that
        resolves to a non-exposed (or unknown) field is **silently ignored** — never a 400 —
        so a hidden field cannot be confirmed-to-exist or used to enumerate assets by value
        (no inference leak). Predicates compose as AND. With no recognized predicate the full
        packaged list is returned (every asset, each carrying ONLY its exposed values).
        """
        exposed = self._store.list_defs(station_id, exposed_only=True)
        defs_by_key = {d.key: d for d in exposed}

        predicates = self._parse_predicates(query_params, defs_by_key)
        matched_ids = self._store.matching_asset_ids(predicates)

        assets = self._public_asset_lister()
        if matched_ids is not None:
            assets = [a for a in assets if a.asset_id in matched_ids]

        # Attach each matched asset's exposed values (key/label/value) for facet display.
        # The exposed-only filter goes through ``get_public_asset_values`` (the single DC-5
        # projection path); the key/label come from the same exposed-def set.
        keys_by_field_id = {d.field_id: d.key for d in exposed}
        labels_by_field_id = {d.field_id: d.label for d in exposed}
        results: list[PublicSearchAsset] = []
        needle = query_params.get("q", "").strip().casefold()[:200]
        for asset in assets:
            if asset.published_at is None:
                continue
            matches: list[PublicTranscriptCue] = []
            cues = (
                self._transcript_provider(asset.asset_id)
                if transcript_access(asset.asset_id)
                else None
            )
            if needle:
                if cues is not None:
                    for cue in cues:
                        position = cue.text.casefold().find(needle)
                        if position >= 0:
                            # Bound excerpts without dropping the matching passage.
                            start = max(0, position - 90)
                            excerpt = cue.text[start : position + len(needle) + 150]
                            matches.append(cue.model_copy(update={"text": excerpt}))
                            if len(matches) == 3:
                                break
                if (
                    not matches
                    and needle not in f"{asset.title} {asset.description or ''}".casefold()
                ):
                    continue
            exposed_values = [
                PublicCustomFieldValue(
                    key=keys_by_field_id[v.field_id],
                    label=labels_by_field_id[v.field_id],
                    value=v.value,
                )
                for v in self.get_public_asset_values(asset.asset_id, station_id=station_id)
            ]
            results.append(
                PublicSearchAsset(
                    **asset.model_dump(),
                    custom_fields=exposed_values,
                    transcript_matches=matches,
                    transcript_available=None if cues is None else bool(cues),
                )
            )
        return results

    @staticmethod
    def _parse_predicates(
        query_params: Mapping[str, str],
        defs_by_key: dict[str, CustomFieldDef],
    ) -> list[FieldPredicate]:
        """Translate the ``cf.<key>`` query family into resolved store predicates.

        Range bounds for the same key collapse into ONE ``num_range`` / ``date_range``
        predicate; an ``eq`` is its own predicate. Unknown / non-exposed keys and malformed
        range values are dropped (the public boundary: ignore, never error).
        """
        # Bucket raw params by (key, kind): kind is "eq" | "gte" | "lte".
        eq: dict[str, str] = {}
        gte: dict[str, str] = {}
        lte: dict[str, str] = {}
        for raw_key, raw_value in query_params.items():
            if not raw_key.startswith("cf."):
                continue
            spec = raw_key[len("cf.") :]
            if spec.endswith("_gte"):
                gte[spec[: -len("_gte")]] = raw_value
            elif spec.endswith("_lte"):
                lte[spec[: -len("_lte")]] = raw_value
            else:
                eq[spec] = raw_value

        predicates: list[FieldPredicate] = []
        for key, value in eq.items():
            definition = defs_by_key.get(key)
            if definition is not None:
                predicates.append(FieldPredicate(field_id=definition.field_id, value=value))

        for key in set(gte) | set(lte):
            definition = defs_by_key.get(key)
            if definition is None:
                continue
            pred = _range_predicate(definition, gte.get(key), lte.get(key))
            if pred is not None:
                predicates.append(pred)
        return predicates

    def _resolve_references(
        self,
        values: list[CustomFieldValue],
        defs_by_id: dict[str, CustomFieldDef],
    ) -> list[CustomFieldValue]:
        """Verify every ``asset_ref`` / ``producer_ref`` value resolves (spec §6).

        Runs before the store write so an unresolved reference never persists. A value for
        an unknown field is left for the store to reject (its ``Unknown field`` error), so
        this only checks references for fields it can see in ``defs_by_id``.

        Returns the value list with each ref value rewritten to the stripped form it
        resolved against, so the canonical ``value`` the store persists is exactly what
        validated (no value/resolution drift on a ref typed with surrounding whitespace).
        Non-ref values pass through untouched.
        """
        resolved: list[CustomFieldValue] = []
        for value in values:
            definition = defs_by_id.get(value.field_id)
            if definition is None or definition.type not in _REF_TYPES:
                resolved.append(value)
                continue
            ref_id = value.value.strip()
            if definition.type == "asset_ref":
                if not self._asset_exists(ref_id):
                    raise FieldReferenceError(
                        f"asset_ref {ref_id!r} for field {definition.field_id!r} "
                        f"({definition.label!r}) does not resolve to an existing asset."
                    )
            elif not self._producer_exists(ref_id):  # producer_ref
                raise FieldReferenceError(
                    f"producer_ref {ref_id!r} for field {definition.field_id!r} "
                    f"({definition.label!r}) does not resolve to an existing producer."
                )
            resolved.append(value.model_copy(update={"value": ref_id}))
        return resolved


def _range_predicate(
    definition: CustomFieldDef,
    raw_min: str | None,
    raw_max: str | None,
) -> FieldPredicate | None:
    """Build a num_range/date_range predicate for an exposed number/date field.

    Returns ``None`` when the field is not a range type or every bound is malformed (the
    public boundary ignores bad input rather than erroring). A single valid bound is enough.
    """
    if definition.type == "number":
        num_min = _maybe_float(raw_min)
        num_max = _maybe_float(raw_max)
        if num_min is None and num_max is None:
            return None
        return FieldPredicate(
            field_id=definition.field_id, op="num_range", num_min=num_min, num_max=num_max
        )
    if definition.type == "date":
        date_min = _maybe_date(raw_min)
        date_max = _maybe_date(raw_max)
        if date_min is None and date_max is None:
            return None
        return FieldPredicate(
            field_id=definition.field_id, op="date_range", date_min=date_min, date_max=date_max
        )
    return None


def _maybe_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        parsed = float(raw)
    except ValueError:
        return None
    # A non-finite bound (NaN / inf / -inf) is dropped like any malformed bound: the public
    # boundary ignores bad input rather than binding a non-finite value into a range predicate.
    if not math.isfinite(parsed):
        return None
    return parsed


def _maybe_date(raw: str | None) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


__all__ = [
    "AssetResolver",
    "CustomFieldService",
    "FieldReferenceError",
    "ProducerResolver",
    "PublicAssetLister",
]
