# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S23 ReportingService — report-aggregation layer over the as-run ledger.

The :class:`civiccast.reporting.store.ReportingStore` owns persistence (append +
row-by-row read). This service is the **aggregation** layer: it runs SQL
``SUM`` / ``GROUP BY`` queries directly against the bound session so the
franchise-compliance numbers (DC-3 hours-by-category, DC-2 shows report) are
computed in Postgres, not in Python loops, and a 100k-row window still returns
in milliseconds.

Three reports:

* :meth:`ReportingService.shows_report` (DC-2) — group ``as_run_log`` by
  ``asset_id`` over a half-open window; sum airtime; count plays; emit
  first/last aired. Rows with ``asset_id=None`` (filler/slate/live with no
  library asset) are excluded — those are not "shows". ``spot`` rows with an
  ``asset_id`` ARE included (S24 underwriting).

* :meth:`ReportingService.hours_by_category` (DC-3) — resolve a S22 custom
  field by ``(station_id, key)``; LEFT JOIN ``as_run_log`` →
  ``custom_field_values`` on ``(asset_id, field_id)``; group by
  ``custom_field_values.value`` and sum ``duration_s``; rows where the join is
  NULL (no value for that field on that asset, or no asset at all) land in a
  single ``(uncategorized)`` bucket emitted as the last row regardless of size.
  When the field key does not resolve to a def, returns ``field_not_found=True``
  with no rows (a missing field is not an error — the report just shows
  nothing). All comparisons are bound parameters; ``DROP TABLE`` payloads
  passed as field values are stored and compared as strings, never SQL.

* :meth:`ReportingService.as_run_report` — a structured projection over
  ``ReportingStore.list_as_run`` plus an optional derived ``category`` resolved
  the same way the hours-by-category report does. The field is resolved once,
  then values for every matched asset are fetched in a single bound ``IN``
  query — never per-row lookups.

Plus four exporters: :func:`export_as_run_csv`, :func:`export_as_run_xml`,
:func:`export_shows_csv`, :func:`export_shows_xml`. CSV writes through
:mod:`csv` (RFC-4180-ish, header row); XML serializes through
:mod:`xml.etree.ElementTree` (every value passes through ``.text`` /
attribute setters, never f-string concatenation — no XML-injection seam).
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Annotated, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from civiccast.metadata.models import CustomFieldDefDb, CustomFieldValueDb
from civiccast.reporting.models import AsRunLogEntry, AsRunLogEntryDb, Slug
from civiccast.reporting.store import ReportingStore

SessionFactory = Callable[[], AbstractContextManager[Session]]

# The category label used when a row has no value for the chosen field — emitted
# as the LAST row regardless of size so the franchise total still adds up but
# the bucket is visually distinct from a real category.
UNCATEGORIZED_LABEL = "(uncategorized)"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ShowsReportRow(BaseModel):
    """One row of the Shows report: one asset, aggregated across its plays."""

    model_config = ConfigDict(extra="forbid")

    asset_id: Slug
    play_count: int
    total_airtime_s: int
    first_aired: datetime
    last_aired: datetime


class ShowsReport(BaseModel):
    """Shows report for one station over [from_ts, to_ts), optionally channel-scoped."""

    model_config = ConfigDict(extra="forbid")

    station_id: Slug
    from_ts: datetime
    to_ts: datetime
    channel_id: Slug | None = None
    rows: list[ShowsReportRow] = Field(default_factory=list)


class HoursByCategoryRow(BaseModel):
    """One row of the Hours-by-Category report (the franchise number)."""

    model_config = ConfigDict(extra="forbid")

    category: Annotated[str, Field(min_length=1, max_length=1000)]
    total_hours: float
    total_seconds: int
    entry_count: int


class HoursByCategoryReport(BaseModel):
    """Hours-by-Category report — sum of as-run duration grouped by a custom field.

    ``field_not_found=True`` when the ``(station_id, field_key)`` does not
    resolve to a custom-field def: the report is empty by construction (no
    field → no categories), but the request is not an error.
    """

    model_config = ConfigDict(extra="forbid")

    station_id: Slug
    from_ts: datetime
    to_ts: datetime
    channel_id: Slug | None = None
    field_key: str
    field_not_found: bool = False
    rows: list[HoursByCategoryRow] = Field(default_factory=list)


class AsRunReportRow(BaseModel):
    """One row of the As-Run report: a ledger entry plus a derived category.

    Composition (not inheritance) over :class:`AsRunLogEntry` so the entry's
    pydantic contract — every required field, the ``extra=forbid`` lock, the
    ``Slug`` constraints — is the single source of truth and nothing drifts.
    ``category`` is ``None`` when no ``field_key`` was requested, when the
    field has no value on this asset, or when the entry has no ``asset_id``.
    """

    model_config = ConfigDict(extra="forbid")

    entry: AsRunLogEntry
    category: str | None = None


class AsRunReport(BaseModel):
    """As-Run report for one station over [from_ts, to_ts), optionally scoped."""

    model_config = ConfigDict(extra="forbid")

    station_id: Slug
    from_ts: datetime
    to_ts: datetime
    channel_id: Slug | None = None
    field_key: str | None = None
    rows: list[AsRunReportRow] = Field(default_factory=list)


# --- Public-projection shapes (Q-1) ----------------------------------------
#
# The public ``/api/public/reports/as-run`` route is unauthenticated; the
# public as-aired report must NOT leak engine-internal
# metadata. ``verified`` exposes whether each entry was proof-event-backed
# (an internal engine state); ``created_at`` / ``updated_at`` expose
# ledger-write timestamps that hint at engine-write latency and (with
# enough samples) internal scheduling characteristics. The dedicated
# Public* shapes below drop all three, matching the published contract on
# the public route's module docstring.


class PublicAsRunLogEntry(BaseModel):
    """Public projection of :class:`AsRunLogEntry` — drops engine-internal
    metadata (``verified`` / ``created_at`` / ``updated_at``).

    Used only by the unauthenticated public route. Every other field is
    identical to the staff shape so a public client and a staff client
    decoding the same entry agree on the public fields.
    """

    model_config = ConfigDict(extra="forbid")

    entry_id: Slug
    station_id: Slug
    channel_id: Slug
    schedule_item_id: Slug | None = None
    asset_id: Slug | None = None
    scheduled_start: datetime | None = None
    actual_start: datetime
    actual_end: datetime
    duration_s: int
    source_kind: str


class PublicAsRunReportRow(BaseModel):
    """Public projection of :class:`AsRunReportRow` — wraps a
    :class:`PublicAsRunLogEntry` plus an optional ``category``."""

    model_config = ConfigDict(extra="forbid")

    entry: PublicAsRunLogEntry
    category: str | None = None


class PublicAsRunReport(BaseModel):
    """Public projection of :class:`AsRunReport`. Same window/scope shape but
    every inner ``entry`` is a :class:`PublicAsRunLogEntry`."""

    model_config = ConfigDict(extra="forbid")

    station_id: Slug
    from_ts: datetime
    to_ts: datetime
    channel_id: Slug | None = None
    rows: list[PublicAsRunReportRow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ReportingService:
    """Aggregation reports over the as-run ledger (DC-2 / DC-3 / data export).

    Takes the same session-factory shape the durable stores use (a
    contextmanager-yielding callable). The store layer is reused for the
    row-by-row reads the As-Run report needs; the aggregate reports
    (Shows, Hours-by-Category) run their own SUM/GROUP BY queries on the
    same session so the database does the math.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._store = ReportingStore(session_factory)

    def _session(self) -> AbstractContextManager[Session]:
        return self._session_factory()

    # --- Shows report (DC-2) --------------------------------------------

    def shows_report(
        self,
        *,
        station_id: str,
        from_ts: datetime,
        to_ts: datetime,
        channel_id: str | None = None,
    ) -> ShowsReport:
        """Per-asset airtime + play count over the window.

        Excludes rows with ``asset_id IS NULL`` (filler/slate/live without a
        library asset are not "shows"). Includes every other ``source_kind``
        including ``spot`` (S24 underwriting). Sum/group is done in SQL —
        index-friendly on the ``(station_id, actual_start)`` index path.
        """
        with self._session() as session:
            stmt = (
                select(
                    AsRunLogEntryDb.asset_id.label("asset_id"),
                    func.count().label("play_count"),
                    func.sum(AsRunLogEntryDb.duration_s).label("total_airtime_s"),
                    func.min(AsRunLogEntryDb.actual_start).label("first_aired"),
                    func.max(AsRunLogEntryDb.actual_start).label("last_aired"),
                )
                .where(AsRunLogEntryDb.station_id == station_id)
                .where(AsRunLogEntryDb.actual_start >= from_ts)
                .where(AsRunLogEntryDb.actual_start < to_ts)
                .where(AsRunLogEntryDb.asset_id.is_not(None))
                .group_by(AsRunLogEntryDb.asset_id)
                # Sort by total airtime desc, ties broken by asset_id asc — so
                # the row order is deterministic across runs / backends.
                .order_by(
                    func.sum(AsRunLogEntryDb.duration_s).desc(),
                    AsRunLogEntryDb.asset_id.asc(),
                )
            )
            if channel_id is not None:
                stmt = stmt.where(AsRunLogEntryDb.channel_id == channel_id)
            rows = [
                ShowsReportRow(
                    asset_id=r.asset_id,
                    play_count=int(r.play_count),
                    total_airtime_s=int(r.total_airtime_s or 0),
                    first_aired=_as_utc(r.first_aired),
                    last_aired=_as_utc(r.last_aired),
                )
                for r in session.execute(stmt).all()
            ]
        return ShowsReport(
            station_id=station_id,
            from_ts=from_ts,
            to_ts=to_ts,
            channel_id=channel_id,
            rows=rows,
        )

    # --- Hours-by-Category (DC-3) ---------------------------------------

    def hours_by_category(
        self,
        *,
        station_id: str,
        field_key: str,
        from_ts: datetime,
        to_ts: datetime,
        channel_id: str | None = None,
    ) -> HoursByCategoryReport:
        """Sum airtime per S22 custom-field value over the window.

        Resolves ``field_id`` from ``(station_id, key)`` (returns
        ``field_not_found=True`` with no rows when the key has no def — a
        missing field is not an error, the report just shows nothing). Then
        LEFT JOINs ``as_run_log`` → ``custom_field_values`` on
        ``(asset_id, field_id)``; groups by ``custom_field_values.value`` (or
        the ``(uncategorized)`` sentinel when the join is NULL); sums
        ``duration_s``.

        All comparison values are bound parameters — a value containing SQL
        fragments is stored and compared as a literal string, never
        interpolated. Ordering is descending total_seconds then category name,
        with ``(uncategorized)`` always last regardless of size.

        NOTE: GROUP BY uses the database's default collation. SQLite is
        binary/case-sensitive (``"News"`` and ``"news"`` become two buckets).
        Postgres' default ``text`` collation matches. Operators using
        ``ICU`` / ``en_US.UTF-8`` strength-1 collations on a column-level
        ``COLLATE`` will see different bucketing. If your franchise
        compliance contract requires case-insensitive bucketing, normalize
        via a generated lowercase column on ``custom_field_values.value``
        (out of scope for S23).
        """
        with self._session() as session:
            field_id = self._resolve_field_id(session, station_id, field_key)
            if field_id is None:
                return HoursByCategoryReport(
                    station_id=station_id,
                    from_ts=from_ts,
                    to_ts=to_ts,
                    channel_id=channel_id,
                    field_key=field_key,
                    field_not_found=True,
                    rows=[],
                )

            # COALESCE the joined value into the uncategorized sentinel so the
            # NULL bucket is a real group in the GROUP BY (vs. a NULL group
            # SQLite and PG would treat slightly differently). All literals
            # below are bound (sentinel + field_id + window + station/channel).
            uncategorized = UNCATEGORIZED_LABEL
            category_col = func.coalesce(CustomFieldValueDb.value, uncategorized)

            stmt = (
                select(
                    category_col.label("category"),
                    func.sum(AsRunLogEntryDb.duration_s).label("total_seconds"),
                    func.count().label("entry_count"),
                )
                .select_from(AsRunLogEntryDb)
                .outerjoin(
                    CustomFieldValueDb,
                    (CustomFieldValueDb.asset_id == AsRunLogEntryDb.asset_id)
                    & (CustomFieldValueDb.field_id == field_id),
                )
                .where(AsRunLogEntryDb.station_id == station_id)
                .where(AsRunLogEntryDb.actual_start >= from_ts)
                .where(AsRunLogEntryDb.actual_start < to_ts)
                .group_by(category_col)
                # Uncategorized last: CASE puts the sentinel into bucket 1,
                # everything else into bucket 0. Then within each bucket, order
                # by total_seconds desc, ties broken by category name asc.
                .order_by(
                    case((category_col == uncategorized, 1), else_=0).asc(),
                    func.sum(AsRunLogEntryDb.duration_s).desc(),
                    category_col.asc(),
                )
            )
            if channel_id is not None:
                stmt = stmt.where(AsRunLogEntryDb.channel_id == channel_id)

            rows: list[HoursByCategoryRow] = []
            for r in session.execute(stmt).all():
                total_seconds = int(r.total_seconds or 0)
                rows.append(
                    HoursByCategoryRow(
                        category=r.category,
                        total_hours=round(total_seconds / 3600.0, 3),
                        total_seconds=total_seconds,
                        entry_count=int(r.entry_count),
                    )
                )

        return HoursByCategoryReport(
            station_id=station_id,
            from_ts=from_ts,
            to_ts=to_ts,
            channel_id=channel_id,
            field_key=field_key,
            field_not_found=False,
            rows=rows,
        )

    # --- As-Run report --------------------------------------------------

    def as_run_report(
        self,
        *,
        station_id: str,
        from_ts: datetime,
        to_ts: datetime,
        channel_id: str | None = None,
        field_key: str | None = None,
    ) -> AsRunReport:
        """Per-entry projection over the as-run ledger plus a derived category.

        ``field_key=None`` → every ``category`` is ``None`` (no extra query).
        ``field_key`` supplied → resolve ``field_id`` once, then fetch all
        ``(asset_id → value)`` pairs for the matched assets in ONE bound
        ``IN`` query — never per-row lookups. An unknown ``field_key`` is not
        an error; every ``category`` is ``None``, matching the
        ``hours_by_category`` empty-rows policy.
        """
        entries = self._store.list_as_run(
            station_id, channel_id=channel_id, from_ts=from_ts, to_ts=to_ts
        )

        category_by_asset: dict[str, str] = {}
        if field_key is not None and entries:
            with self._session() as session:
                field_id = self._resolve_field_id(session, station_id, field_key)
                if field_id is not None:
                    asset_ids = {e.asset_id for e in entries if e.asset_id is not None}
                    if asset_ids:
                        value_stmt = (
                            select(
                                CustomFieldValueDb.asset_id,
                                CustomFieldValueDb.value,
                            )
                            .where(CustomFieldValueDb.field_id == field_id)
                            .where(CustomFieldValueDb.asset_id.in_(asset_ids))
                        )
                        category_by_asset = dict(
                            cast(
                                Iterable[tuple[str, str]],
                                session.execute(value_stmt).all(),
                            )
                        )

        rows = [
            AsRunReportRow(
                entry=entry,
                category=(
                    category_by_asset.get(entry.asset_id) if entry.asset_id is not None else None
                ),
            )
            for entry in entries
        ]
        return AsRunReport(
            station_id=station_id,
            from_ts=from_ts,
            to_ts=to_ts,
            channel_id=channel_id,
            field_key=field_key,
            rows=rows,
        )

    # --- Public As-Run projection (Q-1) ---------------------------------

    def public_as_run_report(
        self,
        *,
        station_id: str,
        from_ts: datetime,
        to_ts: datetime,
        channel_id: str | None = None,
    ) -> PublicAsRunReport:
        """Public projection of the as-run ledger — drops engine-internal
        ``verified`` / ``created_at`` / ``updated_at`` per the published
        contract on the public route. No category enrichment (categories
        are an internal S22 concern); the public route does not accept a
        ``field_key`` query parameter.
        """
        entries = self._store.list_as_run(
            station_id, channel_id=channel_id, from_ts=from_ts, to_ts=to_ts
        )
        rows = [
            PublicAsRunReportRow(
                entry=PublicAsRunLogEntry(
                    entry_id=entry.entry_id,
                    station_id=entry.station_id,
                    channel_id=entry.channel_id,
                    schedule_item_id=entry.schedule_item_id,
                    asset_id=entry.asset_id,
                    scheduled_start=entry.scheduled_start,
                    actual_start=entry.actual_start,
                    actual_end=entry.actual_end,
                    duration_s=entry.duration_s,
                    source_kind=entry.source_kind,
                ),
                category=None,
            )
            for entry in entries
        ]
        return PublicAsRunReport(
            station_id=station_id,
            from_ts=from_ts,
            to_ts=to_ts,
            channel_id=channel_id,
            rows=rows,
        )

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _resolve_field_id(session: Session, station_id: str, field_key: str) -> str | None:
        """Look up ``custom_field_defs.field_id`` by ``(station_id, key)``.

        Returns ``None`` when no def exists — the caller decides whether that
        is ``field_not_found=True`` (hours_by_category) or "all categories
        null" (as_run_report).
        """
        stmt = (
            select(CustomFieldDefDb.field_id)
            .where(CustomFieldDefDb.station_id == station_id)
            .where(CustomFieldDefDb.key == field_key)
        )
        return session.execute(stmt).scalar_one_or_none()


def _as_utc(value: datetime) -> datetime:
    """Normalize a possibly-naive datetime (SQLite drops tz) to UTC-aware."""
    from datetime import UTC

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Exporters (CSV / XML)
# ---------------------------------------------------------------------------


_AS_RUN_HEADERS = (
    "entry_id",
    "station_id",
    "channel_id",
    "asset_id",
    "schedule_item_id",
    "scheduled_start",
    "actual_start",
    "actual_end",
    "duration_s",
    "source_kind",
    "verified",
    "category",
)

_SHOWS_HEADERS = (
    "asset_id",
    "play_count",
    "total_airtime_s",
    "first_aired",
    "last_aired",
)


def _iso(value: datetime | None) -> str:
    """ISO-8601 string for a datetime, or ``""`` for ``None`` (CSV/XML safe)."""
    return value.isoformat() if value is not None else ""


def _str(value: object) -> str:
    """Stringify a CSV/XML cell value; ``None`` → empty string."""
    return "" if value is None else str(value)


def export_as_run_csv(rows: list[AsRunReportRow]) -> str:
    """Render an As-Run report as CSV (header row + one row per entry).

    Uses :mod:`csv` so embedded commas / quotes / newlines round-trip per
    RFC-4180. UTF-8 string out — no BOM, ``\\r\\n`` line endings.
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(_AS_RUN_HEADERS)
    for row in rows:
        entry = row.entry
        writer.writerow(
            (
                entry.entry_id,
                entry.station_id,
                entry.channel_id,
                _str(entry.asset_id),
                _str(entry.schedule_item_id),
                _iso(entry.scheduled_start),
                _iso(entry.actual_start),
                _iso(entry.actual_end),
                str(entry.duration_s),
                entry.source_kind,
                "true" if entry.verified else "false",
                _str(row.category),
            )
        )
    return buf.getvalue()


def export_as_run_xml(rows: list[AsRunReportRow]) -> str:
    """Render an As-Run report as XML (``<as_run><row>…</row></as_run>``).

    Every value is written through ElementTree's text setter — no f-string
    concatenation, so XML-special characters in a category value cannot break
    out of an element (no XML-injection seam).
    """
    root = ET.Element("as_run")
    for row in rows:
        entry = row.entry
        item = ET.SubElement(root, "row")
        _xml_text(item, "entry_id", entry.entry_id)
        _xml_text(item, "station_id", entry.station_id)
        _xml_text(item, "channel_id", entry.channel_id)
        _xml_text(item, "asset_id", _str(entry.asset_id))
        _xml_text(item, "schedule_item_id", _str(entry.schedule_item_id))
        _xml_text(item, "scheduled_start", _iso(entry.scheduled_start))
        _xml_text(item, "actual_start", _iso(entry.actual_start))
        _xml_text(item, "actual_end", _iso(entry.actual_end))
        _xml_text(item, "duration_s", str(entry.duration_s))
        _xml_text(item, "source_kind", entry.source_kind)
        _xml_text(item, "verified", "true" if entry.verified else "false")
        _xml_text(item, "category", _str(row.category))
    return ET.tostring(root, encoding="unicode")


def export_shows_csv(rows: list[ShowsReportRow]) -> str:
    """Render a Shows report as CSV (header row + one row per asset)."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(_SHOWS_HEADERS)
    for row in rows:
        writer.writerow(
            (
                row.asset_id,
                str(row.play_count),
                str(row.total_airtime_s),
                _iso(row.first_aired),
                _iso(row.last_aired),
            )
        )
    return buf.getvalue()


def export_shows_xml(rows: list[ShowsReportRow]) -> str:
    """Render a Shows report as XML (``<shows><row>…</row></shows>``)."""
    root = ET.Element("shows")
    for row in rows:
        item = ET.SubElement(root, "row")
        _xml_text(item, "asset_id", row.asset_id)
        _xml_text(item, "play_count", str(row.play_count))
        _xml_text(item, "total_airtime_s", str(row.total_airtime_s))
        _xml_text(item, "first_aired", _iso(row.first_aired))
        _xml_text(item, "last_aired", _iso(row.last_aired))
    return ET.tostring(root, encoding="unicode")


def _xml_text(parent: ET.Element, tag: str, value: str) -> None:
    """Add ``<tag>value</tag>`` under ``parent`` with ElementTree-safe text."""
    el = ET.SubElement(parent, tag)
    el.text = value


__all__ = [
    "UNCATEGORIZED_LABEL",
    "AsRunReport",
    "AsRunReportRow",
    "HoursByCategoryReport",
    "HoursByCategoryRow",
    "PublicAsRunLogEntry",
    "PublicAsRunReport",
    "PublicAsRunReportRow",
    "ReportingService",
    "SessionFactory",
    "ShowsReport",
    "ShowsReportRow",
    "export_as_run_csv",
    "export_as_run_xml",
    "export_shows_csv",
    "export_shows_xml",
]
