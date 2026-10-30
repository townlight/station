# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CAP 1.2 + NWS GeoJSON parsing → normalized EasCapAlert, plus the source filter.

Pure functions (no I/O) so they unit-test on any platform. IPAWS-OPEN and most state
AMBER feeds emit CAP 1.2 XML; NWS api.weather.gov emits GeoJSON by default. Both
normalize to the same EasCapAlert. Parsing is defensive: a malformed feed yields the
alerts it CAN parse (or none) and never raises out — the poll worker treats a parse
failure as source-health-down (fail-closed), it never fabricates an alert.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from xml.etree import ElementTree as ET  # parsing is hardened below (defensive try/except)

from civiccast.eas.models import (
    EasCapAlert,
    EasCapSource,
    EasMsgType,
    EasSeverity,
    severity_at_or_above,
)

_CAP12_NS = "urn:oasis:names:tc:emergency:cap:1.2"
_CAP11_NS = "urn:oasis:names:tc:emergency:cap:1.1"

_SEVERITIES: set[str] = {"unknown", "minor", "moderate", "severe", "extreme"}
_MSG_TYPES: set[str] = {"alert", "update", "cancel", "ack", "error"}


def stable_alert_id(sender: str, identifier: str) -> str:
    """A deterministic PK for an alert, stable across re-polls (dedup is (sender,id))."""
    digest = hashlib.sha1(f"{sender}|{identifier}".encode()).hexdigest()  # noqa: S324 — id only, not security  # nosec B324
    return f"eas-{digest[:40]}"


def _norm_severity(value: str | None) -> EasSeverity:
    low = (value or "").strip().lower()
    return low if low in _SEVERITIES else "unknown"  # type: ignore[return-value]


def _norm_msg_type(value: str | None) -> EasMsgType:
    low = (value or "alert").strip().lower()
    return low if low in _MSG_TYPES else "alert"  # type: ignore[return-value]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        # CAP uses ISO-8601 with a numeric offset; Python 3.11+ handles 'Z' too.
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _require_dt(value: str | None) -> datetime:
    return _parse_dt(value) or datetime.now(UTC)


# --- CAP 1.2 XML ---------------------------------------------------------------


def parse_cap_xml(xml_text: str, *, source_id: str) -> list[EasCapAlert]:
    """Parse a CAP 1.1/1.2 ``<alert>`` document into normalized alerts.

    A CAP document is a single alert with one or more ``<info>`` blocks; we take the
    first info for the display fields and union all ``<area>`` geocodes. Returns ``[]``
    for an empty/garbage document (the worker maps that to source-health-down)."""
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 — feeds are operator-configured endpoints  # nosec B314
    except ET.ParseError:
        return []
    ns = _CAP12_NS if _CAP12_NS in (root.tag or "") else _CAP11_NS
    if "alert" not in (root.tag or ""):
        return []

    def _t(parent: ET.Element, tag: str) -> str | None:
        el = parent.find(f"{{{ns}}}{tag}")
        return el.text if el is not None else None

    sender = (_t(root, "sender") or "").strip()
    identifier = (_t(root, "identifier") or "").strip()
    if not sender or not identifier:
        return []
    info = root.find(f"{{{ns}}}info")
    if info is None:
        return []
    areas: list[str] = []
    for area in info.findall(f"{{{ns}}}area"):
        for geocode in area.findall(f"{{{ns}}}geocode"):
            value = geocode.find(f"{{{ns}}}value")
            if value is not None and value.text:
                areas.append(value.text.strip())
    alert = EasCapAlert(
        alert_id=stable_alert_id(sender, identifier),
        source_id=source_id,
        sender=sender,
        identifier=identifier,
        sent=_require_dt(_t(root, "sent")),
        msg_type=_norm_msg_type(_t(root, "msgType")),
        event=(_t(info, "event") or "Public safety alert").strip()[:255],
        severity=_norm_severity(_t(info, "severity")),
        urgency=(_t(info, "urgency") or "unknown")[:40],
        certainty=(_t(info, "certainty") or "unknown")[:40],
        headline=_clip(_t(info, "headline"), 500),
        description=_t(info, "description"),
        instruction=_t(info, "instruction"),
        areas=areas,
        references=_clip(_t(root, "references"), 4000),
        effective=_parse_dt(_t(info, "effective")),
        onset=_parse_dt(_t(info, "onset")),
        expires=_parse_dt(_t(info, "expires")),
    )
    return [alert]


# --- NWS api.weather.gov GeoJSON ----------------------------------------------


def parse_nws_geojson(json_text: str, *, source_id: str) -> list[EasCapAlert]:
    """Parse an api.weather.gov ``/alerts`` GeoJSON FeatureCollection."""
    try:
        data = json.loads(json_text)
    except (ValueError, TypeError):
        return []
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        return []
    alerts: list[EasCapAlert] = []
    for feature in features:
        props = feature.get("properties") if isinstance(feature, dict) else None
        if not isinstance(props, dict):
            continue
        sender = (props.get("sender") or props.get("senderName") or "").strip()
        identifier = (props.get("id") or "").strip()
        if not sender or not identifier:
            continue
        raw_geocode = props.get("geocode")
        geocode = raw_geocode if isinstance(raw_geocode, dict) else {}
        areas = [*(geocode.get("SAME") or []), *(geocode.get("UGC") or [])]
        references = props.get("references")
        ref_str = _nws_references(references)
        alerts.append(
            EasCapAlert(
                alert_id=stable_alert_id(sender, identifier),
                source_id=source_id,
                sender=sender,
                identifier=identifier,
                sent=_require_dt(props.get("sent")),
                msg_type=_norm_msg_type(props.get("messageType")),
                event=(props.get("event") or "Public safety alert")[:255],
                severity=_norm_severity(props.get("severity")),
                urgency=(props.get("urgency") or "unknown")[:40],
                certainty=(props.get("certainty") or "unknown")[:40],
                headline=_clip(props.get("headline"), 500),
                description=props.get("description"),
                instruction=props.get("instruction"),
                areas=[str(a) for a in areas],
                references=_clip(ref_str, 4000),
                effective=_parse_dt(props.get("effective")),
                onset=_parse_dt(props.get("onset")),
                expires=_parse_dt(props.get("expires")),
            )
        )
    return alerts


def _nws_references(references: object) -> str | None:
    """NWS references is a list of @id URLs (or absent). Join to a single field."""
    if isinstance(references, list):
        parts = [
            str(r.get("identifier") or r.get("@id") or "")
            for r in references
            if isinstance(r, dict)
        ]
        return " ".join(p for p in parts if p) or None
    if isinstance(references, str):
        return references
    return None


def _clip(value: str | None, length: int) -> str | None:
    return value[:length] if value else None


# --- source filter -------------------------------------------------------------


def alert_passes_source_filter(alert: EasCapAlert, source: EasCapSource) -> bool:
    """True if the alert meets the source's severity floor AND geocode filter.

    An empty geocode filter means no geo restriction (accept all areas). Ack/Error
    messages are operational, never displayable, so they're dropped here."""
    if alert.msg_type in ("ack", "error"):
        return False
    if not severity_at_or_above(alert.severity, source.severity_floor):
        return False
    if source.geocode_filter:
        wanted = {code.strip() for code in source.geocode_filter}
        if not wanted.intersection({a.strip() for a in alert.areas}):
            return False
    return True
