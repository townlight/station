# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable store for custom metadata field definitions + values (S22).

Per-request store over the single global session factory (same lazy posture as the
eas / ai_models stores). Enforces the S22 write-time contracts at the store boundary:

* **Key immutability** — once a def exists, its ``key`` cannot change (label is editable);
  saved-searches/reports key off ``key`` so it must be stable (spec §6).
* **Typed validation** — ``set_values`` validates each value against its def: ``list`` must
  be one of ``options``; ``number`` parses into ``value_num``; ``date`` parses into
  ``value_date``; ``boolean`` must be true/false; ``required`` fields must be present and
  non-blank. ``asset_ref``/``producer_ref`` resolution is layered on in the service (it
  needs the asset/producer stores); the store validates the type-shape it can see.
* **Delete safety** — deleting a def with existing values is blocked unless ``confirm`` is
  passed, in which case the value rows cascade (never a silent loss).

Value writes are full-replace per asset (like the chapters branch of the asset store): the
caller supplies the complete desired value set for an asset and the store reconciles it.
A missing value row is the valid zero-state — absence of any custom field is always valid.

``asset_id``/``field_id`` are loose string columns (no DB FK); referential integrity for
``asset_ref``/``producer_ref`` lives in the service layer, matching the eas/ai_models
convention.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.metadata.models import (
    CUSTOM_FIELD_TYPES,
    CustomFieldDef,
    CustomFieldDefDb,
    CustomFieldValue,
    CustomFieldValueDb,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]

# A resolved search predicate (the field_id is already looked up from a key, and exposure
# gating is already applied by the caller). ``eq`` matches the canonical string; ``num_range``
# / ``date_range`` match the denormalized value_num / value_date columns.
PredicateOp = Literal["eq", "num_range", "date_range"]


@dataclass(frozen=True)
class FieldPredicate:
    """One resolved custom-field search clause (field_id + operator + bound values)."""

    field_id: str
    op: PredicateOp = "eq"
    value: str | None = None
    num_min: float | None = None
    num_max: float | None = None
    date_min: date | None = None
    date_max: date | None = None


class CustomFieldStoreError(RuntimeError):
    """Base error for custom-field persistence failures."""


class FieldNotFoundError(CustomFieldStoreError):
    """Raised when a field_id does not resolve."""


class FieldImmutableKeyError(CustomFieldStoreError):
    """Raised when an upsert attempts to change an existing def's immutable ``key``."""


class FieldAlreadyExistsError(CustomFieldStoreError):
    """Raised when creation collides with an existing field ID or station key."""


class FieldValuesExistError(CustomFieldStoreError):
    """Raised when deleting a def that still has values without ``confirm=True``."""


class FieldValidationError(CustomFieldStoreError):
    """Raised when a value fails typed validation against its def (spec §6)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Normalize a possibly-naive datetime (SQLite drops tz) to UTC-aware."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def mint_value_id(asset_id: str, field_id: str) -> str:
    """Deterministic value PK from ``(asset_id, field_id)`` (one value row per pair)."""
    digest = hashlib.sha1(f"{asset_id}|{field_id}".encode()).hexdigest()  # noqa: S324  # nosec B324
    return f"cfv_{digest}"


# Booleans accepted as canonical strings for a type=boolean field.
_TRUE_TOKENS = frozenset({"true", "1", "yes", "on"})
_FALSE_TOKENS = frozenset({"false", "0", "no", "off"})


def _parse_num(raw: str) -> float:
    try:
        parsed = float(raw)
    except (TypeError, ValueError) as exc:
        raise FieldValidationError(f"{raw!r} is not a valid number.") from exc
    # Reject non-finite values (NaN / inf / -inf): they break range scans (NaN is invisible
    # to every comparison) and the value<->value_num invariant (NaN denormalizes to NULL), so
    # they are not a valid number for a custom-field value (spec §6 typed validation).
    if not math.isfinite(parsed):
        raise FieldValidationError(f"{raw!r} is not a valid number.")
    return parsed


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise FieldValidationError(f"{raw!r} is not a valid ISO-8601 date (YYYY-MM-DD).") from exc


def _canonical_bool(raw: str) -> str:
    token = raw.strip().lower()
    if token in _TRUE_TOKENS:
        return "true"
    if token in _FALSE_TOKENS:
        return "false"
    raise FieldValidationError(f"{raw!r} is not a valid boolean.")


class CustomFieldStore:
    """CRUD for custom-field defs + per-asset values, with S22 write-time validation."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def _session(self) -> AbstractContextManager[Session]:
        return self._session_factory()

    # --- field definitions ----------------------------------------------

    def upsert_def(
        self, definition: CustomFieldDef, *, create_only: bool = False
    ) -> CustomFieldDef:
        """Write a field def; create-only writes never replace existing definitions."""
        conflict_message = "A field with this key or ID already exists. Use Edit to change it."
        with self._session() as session:
            row = session.get(CustomFieldDefDb, definition.field_id)
            if create_only and row is not None:
                raise FieldAlreadyExistsError(conflict_message)
            if row is None:
                row = CustomFieldDefDb(
                    field_id=definition.field_id, created_at=definition.created_at
                )
                row.key = definition.key
                session.add(row)
            elif row.key != definition.key:
                raise FieldImmutableKeyError(
                    f"Field {definition.field_id!r} key is immutable: "
                    f"cannot change {row.key!r} -> {definition.key!r}."
                )
            row.station_id = definition.station_id
            row.label = definition.label
            row.type = definition.type
            row.options = list(definition.options)
            row.required = definition.required
            row.searchable = definition.searchable
            row.api_exposed = definition.api_exposed
            row.order = definition.order
            row.updated_at = _now()
            try:
                session.commit()
            except IntegrityError as exc:
                if not create_only:
                    raise
                # The PK and (station_id, key) unique constraint also protect
                # simultaneous creates. Roll back before reading the winner;
                # unrelated integrity errors must retain their original cause.
                session.rollback()
                conflict = session.execute(
                    select(CustomFieldDefDb.field_id).where(
                        or_(
                            CustomFieldDefDb.field_id == definition.field_id,
                            and_(
                                CustomFieldDefDb.station_id == definition.station_id,
                                CustomFieldDefDb.key == definition.key,
                            ),
                        )
                    )
                ).first()
                if conflict is not None:
                    raise FieldAlreadyExistsError(conflict_message) from exc
                raise
            return _def_to_model(row)

    def get_def(self, field_id: str) -> CustomFieldDef | None:
        with self._session() as session:
            row = session.get(CustomFieldDefDb, field_id)
            return _def_to_model(row) if row is not None else None

    def list_defs(self, station_id: str, *, exposed_only: bool = False) -> list[CustomFieldDef]:
        """Defs for a station, ordered by ``order`` then ``label``.

        ``exposed_only`` filters to public-facing fields (``searchable AND api_exposed``)
        for the public search projection (the exposure boundary).
        """
        with self._session() as session:
            stmt = (
                select(CustomFieldDefDb)
                .where(CustomFieldDefDb.station_id == station_id)
                .order_by(CustomFieldDefDb.order, CustomFieldDefDb.label)
            )
            if exposed_only:
                stmt = stmt.where(
                    CustomFieldDefDb.searchable.is_(True),
                    CustomFieldDefDb.api_exposed.is_(True),
                )
            return [_def_to_model(r) for r in session.execute(stmt).scalars().all()]

    def delete_def(self, field_id: str, *, confirm: bool = False) -> None:
        """Delete a field def. Blocked if values exist unless ``confirm`` cascades them."""
        with self._session() as session:
            row = session.get(CustomFieldDefDb, field_id)
            if row is None:
                raise FieldNotFoundError(f"Custom field {field_id!r} not found.")
            value_count = session.execute(
                select(CustomFieldValueDb.value_id).where(CustomFieldValueDb.field_id == field_id)
            ).first()
            if value_count is not None and not confirm:
                raise FieldValuesExistError(
                    f"Custom field {field_id!r} has existing values; pass confirm=True to "
                    "cascade-delete them."
                )
            if value_count is not None:
                session.execute(
                    delete(CustomFieldValueDb).where(CustomFieldValueDb.field_id == field_id)
                )
            session.delete(row)
            session.commit()

    # --- search ----------------------------------------------------------

    def matching_asset_ids(self, predicates: list[FieldPredicate]) -> set[str] | None:
        """Asset ids matching ALL predicates (intersection / AND semantics).

        Each predicate is an already-resolved ``field_id`` plus an operator (``eq`` /
        ``num_range`` / ``date_range``). The caller (the service) is responsible for gating
        which ``field_id``s are eligible (public search → searchable+api_exposed only) so the
        store never has to know the exposure rules. Returns ``None`` when there are no
        predicates (the "match every asset" signal, so an empty filter never collapses the
        result set); otherwise the set of asset_ids present in EVERY predicate's match set.

        All comparison values are bound parameters (no string interpolation): ``eq`` hits the
        ``(field_id, value)`` index, ``num_range`` the ``value_num`` index, ``date_range`` the
        ``value_date`` index — the three S22 index paths.
        """
        if not predicates:
            return None
        with self._session() as session:
            result: set[str] | None = None
            for pred in predicates:
                stmt = select(CustomFieldValueDb.asset_id).where(
                    CustomFieldValueDb.field_id == pred.field_id
                )
                if pred.op == "eq":
                    stmt = stmt.where(CustomFieldValueDb.value == pred.value)
                elif pred.op == "num_range":
                    if pred.num_min is not None:
                        stmt = stmt.where(CustomFieldValueDb.value_num >= pred.num_min)
                    if pred.num_max is not None:
                        stmt = stmt.where(CustomFieldValueDb.value_num <= pred.num_max)
                elif pred.op == "date_range":
                    if pred.date_min is not None:
                        stmt = stmt.where(CustomFieldValueDb.value_date >= pred.date_min)
                    if pred.date_max is not None:
                        stmt = stmt.where(CustomFieldValueDb.value_date <= pred.date_max)
                matched = {aid for (aid,) in session.execute(stmt).all()}
                result = matched if result is None else (result & matched)
                if not result:
                    return set()
            return result

    # --- asset values ----------------------------------------------------

    def get_values(self, asset_id: str) -> list[CustomFieldValue]:
        """This asset's custom-field values (empty list = the valid zero-state)."""
        with self._session() as session:
            stmt = (
                select(CustomFieldValueDb)
                .where(CustomFieldValueDb.asset_id == asset_id)
                .order_by(CustomFieldValueDb.field_id)
            )
            return [_value_to_model(r) for r in session.execute(stmt).scalars().all()]

    def set_values(
        self,
        asset_id: str,
        values: list[CustomFieldValue],
        *,
        definitions: list[CustomFieldDef],
    ) -> list[CustomFieldValue]:
        """Full-replace this asset's values, typed-validated against ``definitions``.

        ``definitions`` is the station's field set (the caller resolves it). Validation:
        every supplied value's field must exist; ``list`` must be an option; ``number``
        denormalizes into ``value_num``; ``date`` into ``value_date``; ``boolean`` is
        canonicalized; ``required`` fields must be present and non-blank. Returns the
        persisted, normalized values.
        """
        defs_by_id = {d.field_id: d for d in definitions}
        prepared = self._validate(asset_id, values, defs_by_id)
        with self._session() as session:
            session.execute(
                delete(CustomFieldValueDb).where(CustomFieldValueDb.asset_id == asset_id)
            )
            now = _now()
            for v in prepared:
                session.add(
                    CustomFieldValueDb(
                        value_id=mint_value_id(asset_id, v.field_id),
                        asset_id=asset_id,
                        field_id=v.field_id,
                        value=v.value,
                        value_num=v.value_num,
                        value_date=v.value_date,
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.commit()
        return self.get_values(asset_id)

    @staticmethod
    def _validate(
        asset_id: str,
        values: list[CustomFieldValue],
        defs_by_id: dict[str, CustomFieldDef],
    ) -> list[CustomFieldValue]:
        """Typed validation (spec §6). Returns normalized values with denormalized copies."""
        supplied = {v.field_id for v in values}
        if len(supplied) != len(values):
            raise FieldValidationError("Duplicate field_id in the value set.")

        # required-present: every required def must have a non-blank supplied value.
        provided_nonblank = {v.field_id for v in values if v.value.strip()}
        for d in defs_by_id.values():
            if d.required and d.field_id not in provided_nonblank:
                raise FieldValidationError(
                    f"Required field {d.field_id!r} ({d.label!r}) must be present."
                )

        prepared: list[CustomFieldValue] = []
        for v in values:
            definition = defs_by_id.get(v.field_id)
            if definition is None:
                raise FieldValidationError(f"Unknown field {v.field_id!r}.")
            prepared.append(_normalize_value(asset_id, v, definition))
        return prepared


def _normalize_value(
    asset_id: str, value: CustomFieldValue, definition: CustomFieldDef
) -> CustomFieldValue:
    """Type-check + denormalize one value against its def. Raises FieldValidationError."""
    if definition.type not in CUSTOM_FIELD_TYPES:  # defensive; DB-check parity
        raise FieldValidationError(f"Unknown field type {definition.type!r}.")

    canonical = value.value
    value_num: float | None = None
    value_date: date | None = None

    if definition.type == "list":
        if canonical not in definition.options:
            raise FieldValidationError(
                f"{canonical!r} is not an option for {definition.field_id!r} "
                f"(allowed: {definition.options})."
            )
    elif definition.type == "number":
        value_num = _parse_num(canonical)
    elif definition.type == "date":
        value_date = _parse_date(canonical)
    elif definition.type == "boolean":
        canonical = _canonical_bool(canonical)
    # text / longtext / asset_ref / producer_ref: the store keeps the canonical string
    # as-is. Reference resolution (asset_ref/producer_ref must resolve) is enforced in the
    # service, which has the asset/producer stores.

    return CustomFieldValue(
        asset_id=asset_id,
        field_id=value.field_id,
        value=canonical,
        value_num=value_num,
        value_date=value_date,
    )


# --- row → model converters ----------------------------------------------------


def _def_to_model(row: CustomFieldDefDb) -> CustomFieldDef:
    return CustomFieldDef(
        field_id=row.field_id,
        station_id=row.station_id,
        key=row.key,
        label=row.label,
        type=row.type,  # type: ignore[arg-type]
        options=list(row.options or []),
        required=row.required,
        searchable=row.searchable,
        api_exposed=row.api_exposed,
        order=row.order,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _value_to_model(row: CustomFieldValueDb) -> CustomFieldValue:
    return CustomFieldValue(
        asset_id=row.asset_id,
        field_id=row.field_id,
        value=row.value,
        value_num=row.value_num,
        value_date=row.value_date,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


__all__ = [
    "CustomFieldStore",
    "CustomFieldStoreError",
    "FieldImmutableKeyError",
    "FieldNotFoundError",
    "FieldPredicate",
    "FieldValidationError",
    "FieldValuesExistError",
    "PredicateOp",
    "SessionFactory",
    "mint_value_id",
]
