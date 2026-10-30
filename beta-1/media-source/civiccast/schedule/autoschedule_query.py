# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""AssetQuery -> SQL executor + pick strategies (CivicCast 3.0 — S18 slice 2).

Turns a stored :class:`civiccast.schedule.autoschedule_models.AssetQuery` (the
declarative filter a SavedSearch holds, S18 slice 1) into a **parameterized**
SQLAlchemy query over the ``assets`` table, and resolves a single asset under a
rule's pick strategy. The materializer (a later slice) calls :func:`pick_asset`
once per slot it fills; :func:`resolve_assets` backs the operator "simulate /
preview this search" surface and tests.

Security: every filter value is a bound parameter — no part of an
``AssetQuery`` is string-interpolated into SQL. ``title_contains`` is matched
with ``ILIKE`` and its LIKE metacharacters (``%`` ``_`` ``\\``) are escaped so an
operator's literal ``%`` cannot act as a wildcard (audit watch-item #1 from the
slice-1 review).

Faithfulness: the executor honours the query exactly — an empty ``AssetQuery``
(no filters) matches every asset. It deliberately does **not** hardcode
"airable" constraints (e.g. has a file / is validated). Restricting to airable
states is the saved search's job (``AssetQuery.states``) and the S4 commit gate
remains the final playability check; the auto-scheduler is a planning layer.
"""

from __future__ import annotations

import random
from collections.abc import Iterable

from sqlalchemy import Select, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from civiccast.metadata.models import CustomFieldDefDb, CustomFieldValueDb
from civiccast.schedule.autoschedule_models import (
    PICK_NEWEST,
    PICK_RANDOM_RESULT,
    PICK_TOP_RESULT,
    AssetQuery,
    CustomFieldPredicate,
    PickStrategyValue,
)
from civiccast.schedule.models import Asset, StaffAssetRow

# Result-set bounds so a resolve / random pick can never scan unbounded.
_DEFAULT_RESULT_LIMIT = 200
_MAX_RESULT_LIMIT = 1000

# AssetQuery.order_by -> the Asset column it sorts on. (The assets table has no
# created_at; published_at is its time field — slice-1 contract corrected here.)
_ORDER_COLUMNS = {
    "published_at": Asset.published_at,
    "title": Asset.title,
    "duration_seconds": Asset.duration_seconds,
}


def _bounded(limit: int) -> int:
    return max(1, min(limit, _MAX_RESULT_LIMIT))


def _like_escape(term: str) -> str:
    """Escape LIKE/ILIKE metacharacters so a literal ``%`` / ``_`` in an
    operator's search term is matched literally, not as a wildcard. Backslash
    is the escape char (passed to ``.ilike(..., escape="\\\\")``)."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _custom_field_exists(pred: CustomFieldPredicate) -> ColumnElement[bool]:
    """A correlated EXISTS clause for one custom-field predicate (S22 / S19, DC-3).

    The subquery selects from ``custom_field_values`` joined to ``custom_field_defs`` on
    ``field_id``, correlated to the outer ``Asset.asset_id``, and resolves the predicate's
    immutable ``key`` to its def in SQL. Two gates make this faithful to the spec:

    * ``custom_field_defs.key == pred.key`` — predicates store the stable machine key
      (spec §6) so a label rename never breaks a saved search; the join does the lookup.
    * ``custom_field_defs.searchable IS TRUE`` — a non-searchable field can never be
      filtered on (spec §3), so an EXISTS over a non-searchable def yields no rows.

    A correlated EXISTS (not a JOIN) keeps the asset result set one-row-per-asset, so
    LIMIT / ordering / random-pick candidate counting are unaffected and multiple
    predicates compose as AND. Every value is a bound parameter — nothing is interpolated.
    The operator dispatch hits the intended index: ``eq`` -> ``(field_id, value)``,
    ``num_range`` -> ``value_num``, ``date_range`` -> ``value_date``.
    """
    sub = (
        select(CustomFieldValueDb.value_id)
        .join(
            CustomFieldDefDb,
            CustomFieldDefDb.field_id == CustomFieldValueDb.field_id,
        )
        .where(
            CustomFieldDefDb.key == pred.key,
            CustomFieldDefDb.searchable.is_(True),
            CustomFieldValueDb.asset_id == Asset.asset_id,
        )
    )
    if pred.op == "eq":
        sub = sub.where(CustomFieldValueDb.value == pred.value)
    elif pred.op == "num_range":
        if pred.num_min is not None:
            sub = sub.where(CustomFieldValueDb.value_num >= pred.num_min)
        if pred.num_max is not None:
            sub = sub.where(CustomFieldValueDb.value_num <= pred.num_max)
    elif pred.op == "date_range":
        if pred.date_min is not None:
            sub = sub.where(CustomFieldValueDb.value_date >= pred.date_min)
        if pred.date_max is not None:
            sub = sub.where(CustomFieldValueDb.value_date <= pred.date_max)
    return sub.exists()


def _apply_filters(stmt: Select[tuple[Asset]], query: AssetQuery) -> Select[tuple[Asset]]:
    """Add every set AssetQuery filter to ``stmt`` as a bound-parameter WHERE.

    Note on NULLs: a duration / published-at bound silently excludes assets
    whose column is NULL (``NULL >= x`` is unknown, not true) — an asset with
    unknown duration or no publish date should not satisfy a bounded slot.

    Custom-field predicates (S22 / S19) each add a correlated EXISTS over
    ``custom_field_values``; an empty ``custom_fields`` list adds zero clauses, so a query
    with no custom-field predicate is byte-for-byte the old behaviour.
    """
    if query.meeting_body is not None:
        stmt = stmt.where(Asset.meeting_body == query.meeting_body)
    if query.title_contains is not None:
        pattern = f"%{_like_escape(query.title_contains)}%"
        stmt = stmt.where(Asset.title.ilike(pattern, escape="\\"))
    if query.states:
        stmt = stmt.where(Asset.state.in_(query.states))
    if query.min_duration_seconds is not None:
        stmt = stmt.where(Asset.duration_seconds >= query.min_duration_seconds)
    if query.max_duration_seconds is not None:
        stmt = stmt.where(Asset.duration_seconds <= query.max_duration_seconds)
    if query.published_after is not None:
        stmt = stmt.where(Asset.published_at >= query.published_after)
    if query.published_before is not None:
        stmt = stmt.where(Asset.published_at < query.published_before)
    for pred in query.custom_fields:
        stmt = stmt.where(_custom_field_exists(pred))
    return stmt


def _apply_query_order(stmt: Select[tuple[Asset]], query: AssetQuery) -> Select[tuple[Asset]]:
    """Order by the query's chosen column/direction, NULLs last, with a stable
    ``asset_id`` tie-break so paging and ``top_result`` are deterministic."""
    column = _ORDER_COLUMNS[query.order_by]
    direction = column.desc() if query.order_desc else column.asc()
    return stmt.order_by(direction.nulls_last(), Asset.asset_id.asc())


def _exclude(stmt: Select[tuple[Asset]], exclude_asset_ids: Iterable[str]) -> Select[tuple[Asset]]:
    ids = tuple(exclude_asset_ids)
    if ids:
        stmt = stmt.where(Asset.asset_id.not_in(ids))
    return stmt


def build_asset_query(query: AssetQuery) -> Select[tuple[Asset]]:
    """Return the parameterized ``Select`` for an AssetQuery, in the query's own
    order. Pure (no DB) — unit-testable and inspectable on its own."""
    return _apply_query_order(_apply_filters(select(Asset), query), query)


def resolve_assets(
    session: Session,
    query: AssetQuery,
    *,
    exclude_asset_ids: Iterable[str] = (),
    limit: int = _DEFAULT_RESULT_LIMIT,
) -> list[StaffAssetRow]:
    """Resolve all assets matching ``query``, in the query's order, bounded.

    ``exclude_asset_ids`` drops specific assets (the repeat-prevention window
    in a later slice passes recently-aired ids here). Returns the operator
    projection (:class:`StaffAssetRow`) the materializer / preview UI consume.
    """
    stmt = _apply_filters(select(Asset), query)
    stmt = _exclude(stmt, exclude_asset_ids)
    stmt = _apply_query_order(stmt, query).limit(_bounded(limit))
    return [row.to_staff_row() for row in session.execute(stmt).scalars()]


def pick_asset(
    session: Session,
    query: AssetQuery,
    strategy: PickStrategyValue,
    *,
    rng: random.Random | None = None,
    exclude_asset_ids: Iterable[str] = (),
) -> StaffAssetRow | None:
    """Resolve ``query`` and pick ONE asset under ``strategy``; None if none match.

    * ``top_result``    — the first row in the query's own ordering.
    * ``newest``        — the most recently published row (max ``published_at``),
                          independent of the query's order.
    * ``random_result`` — a uniformly-random eligible row. ``rng`` (a
                          ``random.Random``) is injectable so the choice is
                          deterministic in tests; production uses a fresh,
                          OS-seeded generator.
    """
    base = _exclude(_apply_filters(select(Asset), query), exclude_asset_ids)

    if strategy == PICK_TOP_RESULT:
        stmt = _apply_query_order(base, query).limit(1)
        row = session.execute(stmt).scalars().first()
    elif strategy == PICK_NEWEST:
        stmt = base.order_by(Asset.published_at.desc().nulls_last(), Asset.asset_id.asc()).limit(1)
        row = session.execute(stmt).scalars().first()
    elif strategy == PICK_RANDOM_RESULT:
        stmt = _apply_query_order(base, query).limit(_MAX_RESULT_LIMIT)
        candidates = session.execute(stmt).scalars().all()
        if not candidates:
            return None
        # Non-crypto by design: this picks a video for scheduling variety, not
        # anything security-sensitive. Injectable rng keeps tests deterministic.
        chooser = rng if rng is not None else random.Random()  # noqa: S311
        row = chooser.choice(candidates)
    else:  # pragma: no cover - guarded by the PickStrategyValue Literal + CHECK
        raise ValueError(f"unknown pick strategy: {strategy!r}")

    return row.to_staff_row() if row is not None else None
