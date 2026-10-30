# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Source adapter contract + the first concrete adapter: Cablecast.

:class:`SourceAdapter` is the seam every incumbent-system importer
implements. Only :class:`CablecastAdapter` exists today (0.4.0 scope);
TelVue / Castus / Leightronix are next-lane work that implement the same
Protocol against their own export shapes — the diff planner and writer
in :mod:`civiccast.migrate.service` never see a source-specific field name.

Cablecast facts this adapter is built against (Tightrope Media Systems /
cablecast.tv): a station's server exposes a public REST API at
``<server>/cablecastapi/v1`` — ``shows``, ``scheduleitems``, ``producers``,
``categories``, ``projects`` (JSON). Public read access with no auth is how
many stations' own public sites render their schedules, so this adapter
supports anonymous GETs by default and optional HTTP Basic auth for private
servers.

Verified against a real, publicly reachable Cablecast server
(``access-sacramento.cablecast.tv``) on 2026-07-08 — see
``tests/migrate/test_adapters_cablecast.py`` for the live, read-only check
and its recorded evidence. Findings that shaped this adapter:

* The list endpoints are ``shows``, ``scheduleitems`` (one word, lowercase —
  NOT ``schedules``), ``producers``, ``categories``, ``projects``.
* Responses wrap the array in a key matching the resource name
  (``{"shows": [...]}``, but ``{"scheduleItems": [...]}`` — camelCase for
  that one endpoint only) alongside a ``meta`` object
  (``{"offset", "pageSize", "count"}``). Real servers observed here return a
  fixed ``pageSize`` (50 for ``shows``, 200 for ``scheduleitems``)
  regardless of a requested ``pageSize`` query param — pagination is driven
  by ``offset`` + the server's own page size, not a client-chosen one.
  ``?<field>=<value>`` query params filter list endpoints server-side
  (confirmed with ``scheduleitems?show=<id>``).
* A show's ``totalRunTime`` (seconds) is the authoritative duration —
  used both for the show's own ``duration_seconds`` and (via a lookup) for
  any schedule item that references it, since ``scheduleitems`` carries no
  duration field of its own.
* ``scheduleitems`` rows with ``show == -1`` are manual/filler events with
  no show reference — skipped, not an error.
* A show's ``reels`` list holds internal reel ids; ``GET reels/{id}``
  returns the reel's ``media`` id. No endpoint here serves the media bytes
  themselves — ``media_ref`` is built as a same-server API pointer
  (``{base_url}/reels/{reel_id}``), never a downloaded file (see the module
  docstring in ``civiccast/migrate/__init__.py``).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any, Protocol, runtime_checkable

import httpx

from civiccast.migrate.models import (
    ImportedPlaylist,
    ImportedScheduleItem,
    ImportedShow,
    NormalizedInventory,
)


class SourceFormatError(ValueError):
    """A file-based adapter's export could not be parsed as that vendor's
    format: a missing required column, an empty/truncated file, or a row
    whose field count does not match the header (the usual symptom of the
    wrong delimiter). Distinct from :class:`httpx.HTTPError` (which means
    "unreachable") -- this means "reachable, but not this vendor's shape."
    """


# Safety cap on how many rows a single fetch_inventory() call will pull from
# any one list endpoint. A real station's scheduleitems history can run into
# the hundreds of thousands of rows (observed: 633,861 on a real server) and
# the public API offers no date-range filter to narrow that server-side.
# ponytail: a flat page cap, not a real cursor/streaming importer — raise
# this (or add real incremental/paged import) if a station's history
# genuinely needs more than this in one dry-run; 0.4.0 scope is "don't lose
# history", not "import 600k rows in one HTTP round trip."
_DEFAULT_MAX_ROWS_PER_ENDPOINT = 2000


@runtime_checkable
class SourceAdapter(Protocol):
    """Contract every incumbent-system adapter implements.

    ``source_system`` is the stable machine token this adapter's imports are
    tagged with in the provenance ledger (:class:`civiccast.migrate.models
    .ImportBatchItemDb.entity_type` pairs with it via the batch row).
    """

    source_system: str

    def fetch_inventory(self) -> NormalizedInventory:
        """Fetch this source's full exportable inventory, normalized."""
        ...


@dataclass(frozen=True)
class CablecastConnection:
    """Connection details for one Cablecast server.

    ``base_url`` is the station's ``cablecastapi/v1`` endpoint, e.g.
    ``https://station.example.org/cablecastapi/v1`` (no trailing slash).
    ``username``/``password`` are optional — omit both for a public,
    anonymous-read server.
    """

    base_url: str
    username: str | None = None
    password: str | None = None
    timeout_seconds: float = 30.0
    max_rows_per_endpoint: int = _DEFAULT_MAX_ROWS_PER_ENDPOINT


def _to_ref(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


class CablecastAdapter:
    """:class:`SourceAdapter` for Tightrope Media Systems' Cablecast API."""

    source_system = "cablecast"

    def __init__(
        self,
        connection: CablecastConnection,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._conn = connection
        self._transport = transport

    def _client(self) -> httpx.Client:
        auth = None
        if self._conn.username is not None or self._conn.password is not None:
            auth = httpx.BasicAuth(self._conn.username or "", self._conn.password or "")
        return httpx.Client(
            base_url=self._conn.base_url.rstrip("/"),
            timeout=self._conn.timeout_seconds,
            transport=self._transport,
            auth=auth,
        )

    def _paginate(self, client: httpx.Client, path: str, list_key: str) -> list[dict[str, Any]]:
        """GET a Cablecast list endpoint, walking ``meta.offset`` if present.

        Real servers verified here only paginate ``shows`` and
        ``scheduleitems`` (both carry a ``meta`` object with
        ``offset``/``pageSize``/``count``); ``producers``, ``categories``,
        and ``projects`` return their FULL list in one response with no
        ``meta`` key at all. A response with no ``meta`` key is therefore
        treated as complete after one request — looping on ``offset`` against
        an endpoint that ignores it would otherwise re-fetch the same full
        list forever (bounded only by ``max_rows_per_endpoint``, so wrong
        but slow rather than wrong and instant).

        Otherwise stops at ``meta.count``, an empty page, or
        ``max_rows_per_endpoint`` — whichever comes first (see the
        module-level cap docstring).
        """
        rows: list[dict[str, Any]] = []
        offset = 0
        cap = self._conn.max_rows_per_endpoint
        while len(rows) < cap:
            response = client.get(path, params={"offset": offset})
            response.raise_for_status()
            body = response.json()
            page = body.get(list_key) or []
            if not page:
                break
            rows.extend(page)
            meta = body.get("meta")
            if meta is None:
                break
            total = meta.get("count")
            offset += len(page)
            if total is not None and offset >= total:
                break
        return rows[:cap]

    def fetch_inventory(self) -> NormalizedInventory:
        with self._client() as client:
            shows_raw = self._paginate(client, "shows", "shows")
            # The real server this adapter was verified against keys this
            # one endpoint's array as camelCase ("scheduleItems") unlike
            # every other list endpoint (which are lowercase, matching the
            # path). Try the observed shape first; fall back to a lowercase
            # key in case another server's Cablecast version differs.
            schedule_raw = self._paginate(client, "scheduleitems", "scheduleItems")
            if not schedule_raw:
                schedule_raw = self._paginate(client, "scheduleitems", "scheduleitems")
            producers_raw = self._paginate(client, "producers", "producers")
            categories_raw = self._paginate(client, "categories", "categories")
            projects_raw = self._paginate(client, "projects", "projects")

        producer_names = {row["id"]: row.get("name") for row in producers_raw if "id" in row}
        category_names = {row["id"]: row.get("name") for row in categories_raw if "id" in row}
        duration_by_show: dict[int, int] = {}

        shows: list[ImportedShow] = []
        for row in shows_raw:
            show_id = row.get("id")
            if show_id is None:
                continue
            duration = row.get("totalRunTime")
            if isinstance(duration, int) and duration > 0:
                duration_by_show[show_id] = duration
            reels = row.get("reels") or []
            media_ref = f"{self._conn.base_url.rstrip('/')}/reels/{reels[0]}" if reels else None
            shows.append(
                ImportedShow(
                    source_ref=str(show_id),
                    title=row.get("title") or f"Untitled show {show_id}",
                    description=row.get("comments") or None,
                    producer=producer_names.get(row.get("producer")),
                    category=category_names.get(row.get("category")),
                    duration_seconds=duration if isinstance(duration, int) else None,
                    air_date=_parse_datetime(row.get("eventDate")),
                    media_ref=media_ref,
                )
            )

        schedule_items: list[ImportedScheduleItem] = []
        for row in schedule_raw:
            item_id = row.get("id")
            show_ref = row.get("show")
            run_at = _parse_datetime(row.get("runDateTime"))
            if item_id is None or show_ref is None or show_ref == -1 or run_at is None:
                # show == -1 is Cablecast's own "manual/filler event, no
                # show" marker; no run time means nothing to schedule.
                continue
            schedule_items.append(
                ImportedScheduleItem(
                    source_ref=str(item_id),
                    show_source_ref=str(show_ref),
                    channel_ref=_to_ref(row.get("channel")),
                    scheduled_at=run_at,
                    duration_seconds=duration_by_show.get(show_ref),
                )
            )

        shows_by_project: dict[int, list[str]] = {}
        for row in shows_raw:
            project_id = row.get("project")
            show_id = row.get("id")
            if project_id is None or show_id is None:
                continue
            shows_by_project.setdefault(project_id, []).append(str(show_id))

        playlists = [
            ImportedPlaylist(
                source_ref=str(row["id"]),
                name=row.get("name") or f"Untitled project {row['id']}",
                item_source_refs=shows_by_project.get(row["id"], []),
            )
            for row in projects_raw
            if "id" in row and row.get("name")
        ]

        return NormalizedInventory(
            source_system=self.source_system,
            shows=shows,
            schedule_items=schedule_items,
            playlists=playlists,
        )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Shared parsing helpers for the FILE-based adapters (TelVue / Castus /
# Leightronix). These vendors' stations export a schedule file to a local
# operator -- there is no network round trip, so each adapter below takes
# the raw exported text directly instead of an httpx client.
#
# None of the three sourced formats carry a timezone (TelVue's Date/Time
# columns are documented as plain MM/DD/YYYY + HH:MM:SS; the other two are
# equally bare wall-clock values) -- ``ScheduleItemCreate`` requires a
# timezone-aware ``scheduled_at``, so every parsed value here is stamped
# UTC.
# ponytail: treats the export's wall-clock time as UTC outright rather than
# asking for a per-station timezone/offset input; correct only for a
# station whose local clock genuinely is UTC. Add a station-timezone field
# on ``ConnectionInfo`` if a real deployment needs a real offset here.
# ---------------------------------------------------------------------------

_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d")
_TIME_FORMATS = ("%H:%M:%S", "%I:%M:%S %p", "%H:%M", "%I:%M %p")
_DATETIME_FORMATS = (
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%Y-%m-%dT%H:%M:%S",
)


def _parse_date_only(value: str) -> date | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_time_only(value: str) -> time | None:
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    return None


def _combine_utc(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=UTC)


def _parse_datetime_combined(value: str) -> datetime | None:
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _parse_duration_seconds(value: str) -> int | None:
    """Accepts a plain integer-seconds value or an ``H:MM:SS`` / ``MM:SS``
    clock value. Returns ``None`` (never raises) for anything else -- a bad
    duration on one row is a per-row data problem the existing
    ``MigrationService.dry_run`` "no usable duration" skip already handles,
    not a structural format error."""
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    parts = value.split(":")
    if 2 <= len(parts) <= 3 and all(p.strip().isdigit() for p in parts):
        digits = [int(p) for p in parts]
        seconds = digits.pop()
        minutes = digits.pop()
        hours = digits.pop() if digits else 0
        return hours * 3600 + minutes * 60 + seconds
    return None


def _read_csv_rows(text: str, *, source_system: str) -> tuple[list[str], list[list[str]]]:
    """Shared CSV framing: split into a header row + data rows, catching the
    two failure shapes every malformed export shares (empty file, and a row
    whose field count does not match the header -- the classic symptom of
    the wrong delimiter or a file truncated mid-row)."""
    try:
        rows = [row for row in csv.reader(io.StringIO(text)) if row]
    except csv.Error as exc:
        raise SourceFormatError(f"{source_system}: could not parse as CSV: {exc}") from exc
    if not rows:
        raise SourceFormatError(f"{source_system}: schedule file is empty")
    header, data_rows = rows[0], rows[1:]
    seen_columns: set[str] = set()
    duplicate_columns: set[str] = set()
    for column in header:
        (duplicate_columns if column in seen_columns else seen_columns).add(column)
    if duplicate_columns:
        raise SourceFormatError(
            f"{source_system}: duplicate column name(s) {sorted(duplicate_columns)} in header "
            f"{header!r} (each column must be uniquely named)"
        )
    for line_no, row in enumerate(data_rows, start=2):
        if len(row) != len(header):
            raise SourceFormatError(
                f"{source_system}: row {line_no} has {len(row)} field(s), expected "
                f"{len(header)} (a wrong delimiter or a truncated file will produce this)"
            )
    return header, data_rows


# ---------------------------------------------------------------------------
# TelVue HyperCaster -- "Native CSV" schedule export
# ---------------------------------------------------------------------------

# Verbatim-sourced from TelVue's own knowledge base
# (https://telvue.com/knowledgebase/native-csv-formating/ and
# https://telvue.com/knowledgebase/programming-functions-import-and-export-events/,
# fetched 2026-07-08). TelVue describes Native CSV as "TelVue's format that
# covers all event types on single-channel and multi-channel systems,"
# usable to "import a schedule via the programming tab or by way of the
# import/native hot folder on the server," and "recommended for batch
# copying, batch imports, and backups." The documented columns:
#
# * Output   -- "the server channel on which the video will be played"
# * Date     -- "date of event in MM/DD/YYYY format"
# * Time     -- "time of scheduled event in HH:MM:SS format"
# * Type     -- one of PLAYOUT, PLAYLIST, SWITCH, STREAM, INPUT, NDI,
#               OVERLAY, SLIDE, ADTRIGGER, CAPTURE, ENCODE
# * Source ID   -- "the unique ID of the content that resides on the
#               server" -- "null this field if you wish to use the Source
#               Name instead" (blank Source ID means "match on filename")
# * Source Name -- "the name of the content item on the server"
# * Offset   -- "how far from the beginning of the file you want to start
#               playing in seconds"
# * Title    -- "used for overlays" (TelVue's own description -- this is
#               NOT documented as canonical program metadata the way
#               Cablecast's `shows.title` is; see the class docstring)
# * Duration -- "the length of time you want the playout to run" in seconds
_TELVUE_REQUIRED_COLUMNS = (
    "Output",
    "Date",
    "Time",
    "Type",
    "Source ID",
    "Source Name",
    "Offset",
    "Title",
    "Duration",
)


@dataclass(frozen=True)
class TelvueConnection:
    """Raw text content of one TelVue HyperCaster "Native CSV" schedule
    export -- staff pulls this file from HyperCaster's Programming tab (or
    the export hot folder) and pastes/uploads its content; nothing is
    fetched from a TelVue server."""

    schedule_csv: str


class TelvueAdapter:
    """:class:`SourceAdapter` for TelVue HyperCaster's Native CSV export.

    Only ``Type == PLAYOUT`` rows are imported as a show + schedule item --
    a single video file airing is the only Native CSV row shape that maps
    onto CivicCast's asset/schedule-item model. The other documented event
    types (PLAYLIST references a bundle of events, not one file; SWITCH /
    STREAM / INPUT / NDI are live sources with no importable file; OVERLAY /
    SLIDE are graphics-only; ADTRIGGER / CAPTURE / ENCODE are automation
    triggers) have no single importable asset -- they are excluded, not
    coerced into a fake show.

    Native CSV's own ``Title`` column is documented by TelVue as "used for
    overlays," not as canonical program metadata (Native CSV carries no
    separate description/producer/category the way Cablecast's ``shows``
    endpoint does) -- see ``format_grounding`` in the 0.4.0 task report for
    the exact citations. ``Title`` is used as the show title when present;
    ``Source Name`` (the on-server filename) is the fallback so every
    PLAYOUT row still gets a usable title.
    """

    source_system = "telvue"

    def __init__(self, connection: TelvueConnection) -> None:
        self._conn = connection

    def fetch_inventory(self) -> NormalizedInventory:
        header, data_rows = _read_csv_rows(self._conn.schedule_csv, source_system="telvue")
        missing = [c for c in _TELVUE_REQUIRED_COLUMNS if c not in header]
        if missing:
            raise SourceFormatError(
                f"telvue: Native CSV is missing required column(s) {missing} in header {header!r}"
            )

        shows: dict[str, ImportedShow] = {}
        schedule_items: list[ImportedScheduleItem] = []

        for row in data_rows:
            record = dict(zip(header, row, strict=True))
            if record["Type"].strip().upper() != "PLAYOUT":
                continue

            source_id = record["Source ID"].strip()
            source_name = record["Source Name"].strip()
            source_ref = (source_id or source_name)[:120]
            if not source_ref:
                continue  # no content reference at all on this row

            air_date = _parse_date_only(record["Date"])
            air_time = _parse_time_only(record["Time"])
            if air_date is None or air_time is None:
                continue  # unparseable Date/Time -- skip, don't guess

            duration_seconds = _parse_duration_seconds(record["Duration"])
            title = record["Title"].strip() or source_name or source_ref
            output = record["Output"].strip()
            raw_extra = {"Offset": record["Offset"], "Output": output}

            if source_ref not in shows:
                shows[source_ref] = ImportedShow(
                    source_ref=source_ref,
                    title=title[:200],
                    duration_seconds=duration_seconds,
                    media_ref=source_name or None,
                    raw_extra=raw_extra,
                )

            scheduled_at = _combine_utc(air_date, air_time)
            item_ref = f"{source_ref}@{scheduled_at.isoformat()}@{output or 'default'}"[:120]
            schedule_items.append(
                ImportedScheduleItem(
                    source_ref=item_ref,
                    show_source_ref=source_ref,
                    channel_ref=output or None,
                    scheduled_at=scheduled_at,
                    duration_seconds=duration_seconds,
                    raw_extra=raw_extra,
                )
            )

        return NormalizedInventory(
            source_system=self.source_system,
            shows=list(shows.values()),
            schedule_items=schedule_items,
        )


# ---------------------------------------------------------------------------
# Castus / Leightronix -- header-driven generic CSV
# ---------------------------------------------------------------------------

# Unlike TelVue's Native CSV (one fixed, vendor-documented schema), neither
# of these two vendors' real export columns could be pinned to a single
# hardcoded set, for a SOURCED reason each:
#
# * Leightronix NEXUS/UltraNEXUS exports a schedule as CSV through an
#   operator-built "Export/Print template" that lets the operator choose
#   which Days/Channels/**Columns** appear in the report -- see
#   https://support.leightronix.com/exporting-the-nexus-/-ultranexus-schedule-as-a-csv-file
#   (fetched 2026-07-08). There is no one column set to hardcode; it is
#   configured per station. (Their own NEXUS video file format doc,
#   https://support.leightronix.com/nexus-video-file-format, does confirm
#   video filenames are "up to 27 alpha-numeric characters (no spaces or
#   symbols)" with a ".mpg" extension -- used here only as a plausible
#   file-name shape, never as a required pattern.)
# * Castus (castus.tv) does not publish its schedule/playlist export schema
#   anywhere reachable without an active support contract -- their own site
#   states "the full CASTUS Manual is available through the support
#   portal" for customers with a current support plan
#   (https://castus.tv/new-knowledge-base/, fetched 2026-07-08). No column
#   name from Castus's real export format could be sourced this session --
#   see ``honest_reds``.
#
# So instead of guessing either vendor's exact header, this reader
# recognizes common case-insensitive aliases for the concepts every
# schedule row needs -- a title-or-file, a date+time (or one combined
# column), and a duration -- and carries every OTHER column through
# verbatim in ``raw_extra`` rather than inventing meaning for it. A header
# missing every alias for one of those required concepts is a typed
# :class:`SourceFormatError`, not a silently-empty import.
_TITLE_ALIASES = {"title", "program", "program title", "show", "show title", "name", "event"}
_FILE_ALIASES = {"file", "video file", "filename", "file name", "clip", "media", "media file"}
_DATE_ALIASES = {"date", "air date", "event date"}
_TIME_ALIASES = {"time", "air time", "event start", "event start time", "start time"}
_COMBINED_DATETIME_ALIASES = {"start", "air date/time", "date/time", "scheduled at", "datetime"}
_DURATION_ALIASES = {"duration", "length", "run time", "runtime", "run length"}
_CHANNEL_ALIASES = {"channel", "output", "device", "virtual channel"}


def _find_column(header: list[str], aliases: set[str]) -> str | None:
    by_normalized = {h.strip().lower(): h for h in header}
    for alias in aliases:
        if alias in by_normalized:
            return by_normalized[alias]
    return None


def _parse_generic_schedule_csv(text: str, *, source_system: str) -> NormalizedInventory:
    """Header-driven CSV reader shared by :class:`CastusAdapter` and
    :class:`LeightronixAdapter` -- see the module-level comment above this
    section for why neither vendor gets a hardcoded column schema."""
    header, data_rows = _read_csv_rows(text, source_system=source_system)

    title_col = _find_column(header, _TITLE_ALIASES)
    file_col = _find_column(header, _FILE_ALIASES)
    if title_col is None and file_col is None:
        raise SourceFormatError(
            f"{source_system}: no recognizable title/program or file column in header {header!r}"
        )
    date_col = _find_column(header, _DATE_ALIASES)
    time_col = _find_column(header, _TIME_ALIASES)
    datetime_col = _find_column(header, _COMBINED_DATETIME_ALIASES)
    if datetime_col is None and (date_col is None or time_col is None):
        raise SourceFormatError(
            f"{source_system}: no recognizable schedule date/time column in header {header!r}"
        )
    duration_col = _find_column(header, _DURATION_ALIASES)
    if duration_col is None:
        raise SourceFormatError(
            f"{source_system}: no recognizable duration/length column in header {header!r}"
        )
    channel_col = _find_column(header, _CHANNEL_ALIASES)

    known_cols = {
        c
        for c in (title_col, file_col, date_col, time_col, datetime_col, duration_col, channel_col)
        if c is not None
    }

    shows: dict[str, ImportedShow] = {}
    schedule_items: list[ImportedScheduleItem] = []

    for row in data_rows:
        record = dict(zip(header, row, strict=True))
        raw_extra = {k: v for k, v in record.items() if k not in known_cols and v}

        title = record[title_col].strip() if title_col else ""
        file_name = record[file_col].strip() if file_col else ""
        display_title = title or file_name
        source_ref = (file_name or title)[:120]
        if not display_title or not source_ref:
            continue  # no identifiable content on this row -- skip, don't fabricate

        if datetime_col is not None:
            scheduled_at = _parse_datetime_combined(record[datetime_col])
        else:
            # Enforced by the header check above: datetime_col is None here
            # only because date_col and time_col were both found instead.
            assert date_col is not None
            assert time_col is not None
            parsed_date = _parse_date_only(record[date_col])
            parsed_time = _parse_time_only(record[time_col])
            scheduled_at = (
                _combine_utc(parsed_date, parsed_time)
                if parsed_date is not None and parsed_time is not None
                else None
            )
        if scheduled_at is None:
            continue  # unparseable date/time -- skip this row, don't guess

        duration_seconds = _parse_duration_seconds(record[duration_col])
        channel_ref = record[channel_col].strip() or None if channel_col else None

        if source_ref not in shows:
            shows[source_ref] = ImportedShow(
                source_ref=source_ref,
                title=display_title[:200],
                duration_seconds=duration_seconds,
                media_ref=file_name or None,
                raw_extra=raw_extra or None,
            )

        item_ref = f"{source_ref}@{scheduled_at.isoformat()}@{channel_ref or 'default'}"[:120]
        schedule_items.append(
            ImportedScheduleItem(
                source_ref=item_ref,
                show_source_ref=source_ref,
                channel_ref=channel_ref,
                scheduled_at=scheduled_at,
                duration_seconds=duration_seconds,
                raw_extra=raw_extra or None,
            )
        )

    return NormalizedInventory(
        source_system=source_system,
        shows=list(shows.values()),
        schedule_items=schedule_items,
    )


@dataclass(frozen=True)
class CastusConnection:
    """Raw text content of one Castus (castus.tv) schedule/playlist export.

    Castus's exact export schema could not be sourced from any publicly
    reachable document this session (see the module-level comment above)
    -- validate this adapter against a real customer export before relying
    on it in production; see ``honest_reds``."""

    schedule_csv: str


class CastusAdapter:
    """:class:`SourceAdapter` for Castus (castus.tv) QuickRoll/QuickCast
    schedule exports -- see the header-driven parsing rationale above
    :func:`_parse_generic_schedule_csv`."""

    source_system = "castus"

    def __init__(self, connection: CastusConnection) -> None:
        self._conn = connection

    def fetch_inventory(self) -> NormalizedInventory:
        return _parse_generic_schedule_csv(
            self._conn.schedule_csv, source_system=self.source_system
        )


@dataclass(frozen=True)
class LeightronixConnection:
    """Raw text content of one Leightronix NEXUS/UltraNEXUS WinLGX schedule
    CSV export (an operator-templated report -- see the header-driven
    parsing rationale above :func:`_parse_generic_schedule_csv`)."""

    schedule_csv: str


class LeightronixAdapter:
    """:class:`SourceAdapter` for Leightronix NEXUS/UltraNEXUS (WinLGX /
    PEG Central / Nexus) schedule CSV exports -- see the header-driven
    parsing rationale above :func:`_parse_generic_schedule_csv`."""

    source_system = "leightronix"

    def __init__(self, connection: LeightronixConnection) -> None:
        self._conn = connection

    def fetch_inventory(self) -> NormalizedInventory:
        return _parse_generic_schedule_csv(
            self._conn.schedule_csv, source_system=self.source_system
        )


__all__ = [
    "CablecastAdapter",
    "CablecastConnection",
    "CastusAdapter",
    "CastusConnection",
    "LeightronixAdapter",
    "LeightronixConnection",
    "SourceAdapter",
    "SourceFormatError",
    "TelvueAdapter",
    "TelvueConnection",
]
