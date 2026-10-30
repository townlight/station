# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S23 slice 4 — EPG (electronic program guide) exporters.

Three serialization formats (X-List / XMLTV / plain CSV) + an
:class:`EpgExporter` orchestrator that:

1. Reads committed schedule slots over ``[now, now + horizon_days)`` from an
   injected :class:`CommittedScheduleReader` (any object satisfying the
   Protocol — the API layer passes a real reader, tests pass
   :class:`InMemoryCommittedScheduleReader`).
2. Serializes with the format chosen on :class:`EpgExportConfig`, applying
   ``field_map`` to rename CSV columns. (XMLTV ignores ``field_map`` in this
   version — XMLTV is a fixed schema; renaming would break aggregator
   ingest. Documented here so callers aren't surprised.)
3. If ``config.endpoint`` is ``None``: return the document for download.
   If set: POST it via the operator-injected ``http_post`` (or the bundled
   :func:`_safe_http_post` that SSRF-guards + refuses redirects).
   A failed push is **captured** into ``EpgGenerateResult.error`` — never
   raised — because aggregator endpoints are flaky in the field and the
   spec contract is "generate the document AND attempt the push"; the API
   layer should be able to show the operator the document even when the
   push failed.

SSRF guard pattern mirrors :mod:`civiccast.ai_models.cloud.egress`'s
``_NoRedirectHandler`` (we cannot reuse :func:`require_cloud_https`
directly — that one is bound to a fixed per-provider allowlist, and EPG
aggregator URLs are operator-supplied, with no fixed set). The local
guard here enforces: https only, off-box only (no loopback / private /
link-local), 5-second timeout, no redirect following.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from civiccast.reporting.models import EpgExportConfig, EpgFormat

# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------


class EpgValidationError(ValueError):
    """Raised when a produced or supplied EPG document fails schema validation."""


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class CommittedSlot(BaseModel):
    """One committed schedule slot — the unit the EPG export consumes.

    The exporter does not care how slots are produced. ``asset_id`` is
    optional: filler / live / slate slots have no library asset, but the
    slot still carries a title + start/end + duration and is exported.
    """

    model_config = ConfigDict(extra="forbid")

    slot_id: str
    asset_id: str | None = None
    title: str
    start: datetime
    end: datetime
    duration_s: int
    description: str | None = None
    category: str | None = None
    rating: str | None = None


class EpgGenerateResult(BaseModel):
    """Outcome of one :meth:`EpgExporter.generate` call.

    Either ``document`` is set (download path, or pushed-but-we-also-kept it
    on push failure → no, actually we drop ``document`` on push success to
    avoid two divergent representations; when push fails ``document`` is
    also ``None`` so callers can detect "push attempted, didn't land" via
    ``error`` + ``pushed_to is None``).

    The four outcomes:

    * download    : ``document!=None``, ``pushed_to=None``, ``error=None``
    * push ok     : ``document=None``,  ``pushed_to=url``,  ``error=None``
    * push failed : ``document=None``,  ``pushed_to=None``, ``error=str``
    """

    model_config = ConfigDict(extra="forbid")

    format: EpgFormat
    slot_count: int
    bytes: int
    document: str | None = None
    pushed_to: str | None = None
    pushed_at: datetime | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Schedule reader Protocol + an in-memory implementation for tests
# ---------------------------------------------------------------------------


@runtime_checkable
class CommittedScheduleReader(Protocol):
    """Read committed schedule slots for one station+channel over a window.

    The window is half-open ``[from_ts, to_ts)`` — slots starting before
    ``from_ts`` or at-or-after ``to_ts`` are excluded.
    """

    def list_committed(
        self,
        *,
        station_id: str,
        channel_id: str,
        from_ts: datetime,
        to_ts: datetime,
    ) -> list[CommittedSlot]: ...


class InMemoryCommittedScheduleReader:
    """Trivial :class:`CommittedScheduleReader` for tests / seeded harnesses.

    Filters by ``station_id`` / ``channel_id`` (when supplied on each slot
    via attribute, otherwise treats slots as unfiltered) and by the half-open
    window on slot ``start``. The seeded slots themselves do not carry
    station/channel today — the per-slot filter is matched on *all* seeded
    slots so the orchestrator's per-station call still narrows by window.
    """

    def __init__(self, *, slots: list[CommittedSlot]) -> None:
        self._slots = list(slots)

    def list_committed(
        self,
        *,
        station_id: str,
        channel_id: str,
        from_ts: datetime,
        to_ts: datetime,
    ) -> list[CommittedSlot]:
        return [s for s in self._slots if from_ts <= s.start < to_ts]


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

# Canonical CivicCast-side columns for X-List + plain CSV. ``field_map`` lets
# the operator rename these to whatever the aggregator wants in its header.
_DEFAULT_COLUMNS: tuple[str, ...] = (
    "start_date",
    "start_time",
    "end_date",
    "end_time",
    "title",
    "description",
    "category",
    "rating",
)


def _rename(column: str, field_map: dict[str, str]) -> str:
    """Apply ``field_map`` (CivicCast field → aggregator column). Default passes through."""
    return field_map.get(column, column)


def _row_for(slot: CommittedSlot) -> tuple[str, ...]:
    """Render one slot into the canonical 8-column row (date/time split UTC)."""
    start = slot.start.astimezone(UTC) if slot.start.tzinfo else slot.start.replace(tzinfo=UTC)
    end = slot.end.astimezone(UTC) if slot.end.tzinfo else slot.end.replace(tzinfo=UTC)
    return (
        start.strftime("%Y-%m-%d"),
        start.strftime("%H:%M:%S"),
        end.strftime("%Y-%m-%d"),
        end.strftime("%H:%M:%S"),
        slot.title,
        slot.description or "",
        slot.category or "",
        slot.rating or "",
    )


def serialize_xlist(
    slots: list[CommittedSlot],
    *,
    field_map: dict[str, str],
    station_call_sign: str | None = None,
) -> str:
    """Render as TitanTV / TV-Guide X-List CSV.

    CRLF line terminator (Excel / TitanTV ingest convention). UTC timestamps,
    split into ``YYYY-MM-DD`` + ``HH:MM:SS`` columns. ``field_map`` renames
    column headers; rows are unchanged. When ``station_call_sign`` is
    supplied, an extra ``station_call_sign`` column is appended (also
    renameable via ``field_map``) with the call-sign value on every row.
    """
    headers = [_rename(c, field_map) for c in _DEFAULT_COLUMNS]
    if station_call_sign is not None:
        headers.append(_rename("station_call_sign", field_map))

    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(headers)
    for slot in slots:
        row = list(_row_for(slot))
        if station_call_sign is not None:
            row.append(station_call_sign)
        writer.writerow(row)
    return buf.getvalue()


def serialize_csv(slots: list[CommittedSlot], *, field_map: dict[str, str]) -> str:
    """Render plain RFC-4180 CSV (no X-List quirks). LF line terminator."""
    headers = [_rename(c, field_map) for c in _DEFAULT_COLUMNS]

    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(headers)
    for slot in slots:
        writer.writerow(_row_for(slot))
    return buf.getvalue()


def serialize_xmltv(
    slots: list[CommittedSlot],
    *,
    field_map: dict[str, str],
    channel_id: str,
    channel_display_name: str,
) -> str:
    """Render an XMLTV-compliant ``<tv>...</tv>`` document.

    Schema (subset): one ``<channel>`` with ``<display-name>``, plus one
    ``<programme>`` per slot with ``start`` + ``stop`` (``YYYYMMDDHHMMSS
    +0000``) + ``channel`` attributes and ``<title>`` / ``<desc>`` /
    ``<category>`` / ``<rating>`` children (optional ones omitted when
    ``None``).

    ``field_map`` is intentionally a **no-op** here: XMLTV is a fixed schema
    consumed by aggregators that key off canonical tag names — renaming
    ``<title>`` to ``<ProgramTitle>`` would silently break ingest. CSV /
    X-List honor ``field_map``; XMLTV does not.

    Building via :mod:`xml.etree.ElementTree` so every text value passes
    through ``.text`` / attribute setters — no f-string XML concatenation,
    no XML-injection seam.
    """
    tv = ET.Element("tv")

    channel_el = ET.SubElement(tv, "channel", attrib={"id": channel_id})
    ET.SubElement(channel_el, "display-name").text = channel_display_name

    for slot in slots:
        start = slot.start.astimezone(UTC) if slot.start.tzinfo else slot.start.replace(tzinfo=UTC)
        end = slot.end.astimezone(UTC) if slot.end.tzinfo else slot.end.replace(tzinfo=UTC)
        prog = ET.SubElement(
            tv,
            "programme",
            attrib={
                "start": start.strftime("%Y%m%d%H%M%S +0000"),
                "stop": end.strftime("%Y%m%d%H%M%S +0000"),
                "channel": channel_id,
            },
        )
        ET.SubElement(prog, "title").text = slot.title
        if slot.description is not None:
            ET.SubElement(prog, "desc").text = slot.description
        if slot.category is not None:
            ET.SubElement(prog, "category").text = slot.category
        if slot.rating is not None:
            ET.SubElement(prog, "rating").text = slot.rating

    body = ET.tostring(tv, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>' + body


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def validate_xmltv(doc: str, *, expected_programmes: int) -> None:
    """Parse + schema-check an XMLTV document.

    Asserts: root tag is ``tv``; at least one ``<channel>``; the count of
    ``<programme>`` elements equals ``expected_programmes``.
    """
    try:
        # ElementTree does no DTD / external-entity processing; the validator
        # only inspects tag names + counts (not text content), so XXE is moot
        # on this seam.
        root = ET.fromstring(doc)  # noqa: S314 — no DTD/entity processing in stdlib ET; validator only checks tag names + counts  # nosec B314
    except ET.ParseError as exc:
        raise EpgValidationError(f"XMLTV doc is not well-formed: {exc}") from exc
    if root.tag != "tv":
        raise EpgValidationError(f"XMLTV root must be <tv>, got <{root.tag}>")
    channels = root.findall("channel")
    if not channels:
        raise EpgValidationError("XMLTV doc must contain at least one <channel>")
    programmes = root.findall("programme")
    if len(programmes) != expected_programmes:
        raise EpgValidationError(
            f"XMLTV doc has {len(programmes)} <programme> elements, expected {expected_programmes}"
        )


def validate_xlist(doc: str, *, expected_columns: list[str]) -> None:
    """Parse the X-List header row + verify the columns match exactly."""
    reader = csv.reader(io.StringIO(doc))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise EpgValidationError("X-List doc is empty (no header row)") from exc
    if header != expected_columns:
        raise EpgValidationError(
            f"X-List header mismatch: got {header}, expected {expected_columns}"
        )


# ---------------------------------------------------------------------------
# SSRF-safe default POST seam
# ---------------------------------------------------------------------------


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow any 3xx — mirror of the cloud-egress handler.

    Same rationale as :mod:`civiccast.ai_models.cloud.egress`: the SSRF
    pre-flight only validates the INITIAL URL; a redirect off an
    operator-supplied URL could land on a metadata service or loopback.
    Cannot reuse :func:`require_cloud_https` because that one is bound to
    a fixed per-provider allowlist; EPG aggregator hosts are
    operator-configured and have no closed set.
    """

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


# Module-private opener — tests may monkeypatch ``_EPG_OPENER`` to inject a
# fake. Not installed process-wide (would mutate behavior for unrelated
# callers, same reason cloud egress builds its own opener).
_EPG_OPENER = urllib.request.build_opener(_NoRedirectHandler)

_HTTP_TIMEOUT_S = 5.0


def _refuse_off_box_targets(url: str) -> None:
    """Refuse a URL whose host is loopback / private / link-local.

    The 169.254.169.254 metadata service (AWS / GCP / Azure) is canonically
    SSRF-targeted; private / loopback IPs would let a misconfigured config
    push to an internal service. https-only because the aggregator push
    carries no credentials but plain http would let a network attacker
    silently swap the document mid-flight.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"EPG push requires https; refusing scheme {parsed.scheme!r}")
    host = parsed.hostname
    if host is None:
        raise ValueError(f"EPG push URL has no host: {url}")

    if host.lower() in {"localhost", "ip6-localhost", "ip6-loopback"}:
        raise ValueError(f"EPG push must be off-box; refusing loopback host {host!r}")

    # If host parses as an IP literal, reject loopback / private / link-local.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A hostname — DNS rebinding is out of scope here; deeper resolution
        # would require socket-level guards. Aggregator URLs are operator-
        # vetted at config time. We do reject literal loopback names above.
        return
    # E-2 fix: also reject multicast (224.0.0.0/4) and the unspecified
    # any-address (0.0.0.0 / ::). The guard is operator-URL-facing — no
    # allowlist sits in front of it — so these edge IPs matter even though
    # they're unusual operator typos.
    # Note (E-5): DNS rebinding remains out of scope here; a hostname that
    # resolves first to a public IP then to a metadata IP on a second
    # resolution would still defeat the guard. Aggregator URLs are
    # operator-vetted at config time; deeper socket-level guards would be
    # required for a true rebinding defense.
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValueError(
            f"EPG push must be off-box; refusing loopback/private/link-local/"
            f"multicast/unspecified IP {host!r}"
        )


def _safe_http_post(url: str, body: str, content_type: str) -> None:
    """SSRF-safe POST: https-only, off-box-only, no redirect, 5s timeout.

    Tests stub the module-level ``_EPG_OPENER`` to inject canned responses /
    redirects without real network. Operators wanting their own poster pass
    one to :class:`EpgExporter`; the SSRF guard does not trigger there.
    """
    _refuse_off_box_targets(url)
    req = urllib.request.Request(  # noqa: S310 — https + off-box enforced
        url=url,
        data=body.encode("utf-8"),
        method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        with _EPG_OPENER.open(req, timeout=_HTTP_TIMEOUT_S) as _resp:
            _resp.read()
    except urllib.error.HTTPError as exc:
        # 3xx lands here because _NoRedirectHandler returned None.
        raise RuntimeError(f"EPG push HTTP error {exc.code} (no redirect followed): {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"EPG push transport error: {exc}") from exc


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


_CONTENT_TYPES: dict[str, str] = {
    "xlist": "text/csv",
    "csv": "text/csv",
    "xmltv": "application/xml",
}


class EpgExporter:
    """Compile + serialize + (optionally) push the committed schedule.

    The orchestrator does not validate the produced document — validators
    are exposed separately so the API layer can run them after-the-fact and
    surface failures as a structured 5xx (the contract is
    "generate AND attempt push"; validation is the operator-visible proof
    of DC-4).
    """

    def __init__(
        self,
        schedule_reader: CommittedScheduleReader,
        *,
        http_post: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._reader = schedule_reader
        self._http_post = http_post if http_post is not None else _safe_http_post

    def generate(self, config: EpgExportConfig, *, now: datetime) -> EpgGenerateResult:
        """Run the §6 EPG export algorithm for one :class:`EpgExportConfig`."""
        from_ts = now
        to_ts = now + timedelta(days=config.horizon_days)
        slots = self._reader.list_committed(
            station_id=config.station_id,
            channel_id=config.channel_id,
            from_ts=from_ts,
            to_ts=to_ts,
        )

        document = self._serialize(config, slots)
        slot_count = len(slots)
        body_bytes = len(document.encode("utf-8"))

        if config.endpoint is None:
            return EpgGenerateResult(
                format=config.format,
                slot_count=slot_count,
                bytes=body_bytes,
                document=document,
                pushed_to=None,
                pushed_at=None,
                error=None,
            )

        content_type = _CONTENT_TYPES[config.format]
        try:
            self._http_post(config.endpoint, document, content_type)
        except Exception as exc:  # fail-safe by spec contract — never re-raise
            return EpgGenerateResult(
                format=config.format,
                slot_count=slot_count,
                bytes=body_bytes,
                document=None,
                pushed_to=None,
                pushed_at=None,
                error=str(exc),
            )
        return EpgGenerateResult(
            format=config.format,
            slot_count=slot_count,
            bytes=body_bytes,
            document=None,
            pushed_to=config.endpoint,
            pushed_at=now,
            error=None,
        )

    @staticmethod
    def _serialize(config: EpgExportConfig, slots: list[CommittedSlot]) -> str:
        """Dispatch to the right serializer for ``config.format``."""
        if config.format == "xlist":
            return serialize_xlist(slots, field_map=config.field_map)
        if config.format == "csv":
            return serialize_csv(slots, field_map=config.field_map)
        if config.format == "xmltv":
            # In this version the API surface picks a channel display name
            # from the channel_id (no Channel entity is wired into S23
            # slice 4); slice 5 / the API layer can resolve a richer name.
            return serialize_xmltv(
                slots,
                field_map=config.field_map,
                channel_id=config.channel_id,
                channel_display_name=config.channel_id,
            )
        raise ValueError(f"Unknown EPG format: {config.format!r}")


__all__ = [
    "CommittedScheduleReader",
    "CommittedSlot",
    "EpgExporter",
    "EpgGenerateResult",
    "EpgValidationError",
    "InMemoryCommittedScheduleReader",
    "serialize_csv",
    "serialize_xlist",
    "serialize_xmltv",
    "validate_xlist",
    "validate_xmltv",
]
