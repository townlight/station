# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""``LegistarSource`` — anonymous OData Legistar Web API adapter (plan §3, §8).

Two-call fetch per plan step 3: an ``Events`` list (filtered by date) drives
:meth:`fetch_meetings` / meeting discovery; a single ``Events/{id}`` +
``Events/{id}/EventItems?Attachments=1`` pair drives :meth:`fetch_agenda`
for one already-selected meeting. No SDK, no new dependency — ``httpx`` is
already a repo dependency (see ``civiccast/subscribe/webhook.py``).

Field mapping (plan §8 task 3, verified live against ``webapi.legistar.com/
v1/seattle`` 2026-07-05 and again during this implementation):

* ``EventId`` -> ``external_id``
* ``EventBodyName`` + ``EventDate`` -> ``title`` / ``meeting_datetime``
* ``EventAgendaFile`` (fallback ``EventInSiteURL``) -> ``source_doc_url``
* ``EventItemAgendaSequence`` -> ``order``
* ``EventItemTitle`` -> item ``title`` (fallback chain below — some Legistar
  items carry no title at all, e.g. a bare "CALL TO ORDER" separator with a
  blank ``EventItemTitle``, confirmed live against Seattle event 5705)
* ``EventItemAgendaNumber`` -> ``number``
* first ``EventItemMatterAttachments[].MatterAttachmentHyperlink`` -> ``doc_url``

Items with no ``EventItemAgendaSequence`` at all (Legistar emits these for
signature-block / closing boilerplate rows, confirmed live) are skipped —
they have nothing to order by, so they cannot become an ``AgendaItem`` (which
requires ``order``). This is a per-item skip, not a silent empty import: the
"fail loud on empty" rule (plan §9) fires in :meth:`fetch_agenda` if
skipping leaves zero items.

Tenant auth is bimodal in the wild (plan §3): most tenants (Seattle) are
fully anonymous; a minority (NYC, confirmed live) require a token. Legistar's
documented anonymous-tenant convention has no header of its own for this, so
an operator-supplied ``CIVICCAST_AGENDA_SOURCE_TOKEN`` is sent as the ``token``
query parameter on every request when configured — the closest documented
Legistar Web API convention for a per-tenant API key. This sprint has no
gated-tenant credential to prove that parameter name against live traffic
(open item — flag to LPM if a partner station's token doesn't work); what
*is* proven live is the 401/403-without-a-token path (NYC fixture).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx

from civiccast.agenda_import.base import (
    AgendaSourceAuthRequiredError,
    AgendaSourceUpstreamError,
)
from civiccast.agenda_import.models import (
    ExternalAgenda,
    ExternalAgendaItem,
    ExternalMeetingSummary,
)

_BASE_URL = "https://webapi.legistar.com/v1"


class LegistarSource:
    """:class:`~civiccast.agenda_import.base.AgendaSource` for Legistar."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._token = token
        self._transport = transport

    def fetch_meetings(
        self, client_code: str, *, since: date | None = None
    ) -> list[ExternalMeetingSummary]:
        since = since or date.today()
        odata_filter = f"EventDate ge datetime'{since.isoformat()}'"
        data = self._get(
            client_code,
            f"{_BASE_URL}/{client_code}/Events",
            params={"$filter": odata_filter, "$orderby": "EventDate"},
        )
        if not isinstance(data, list):
            raise AgendaSourceUpstreamError(
                f"Legistar Events response for client_code={client_code!r} "
                f"was not a list (got {type(data).__name__})."
            )
        summaries: list[ExternalMeetingSummary] = []
        for row in data:
            if not isinstance(row, dict) or row.get("EventId") is None:
                continue
            summaries.append(
                ExternalMeetingSummary(
                    external_id=str(row["EventId"]),
                    title=_event_title(row),
                    meeting_datetime=_parse_event_date(row.get("EventDate")),
                )
            )
        return summaries

    def fetch_agenda(self, client_code: str, event_id: str) -> ExternalAgenda:
        event = self._get(client_code, f"{_BASE_URL}/{client_code}/Events/{event_id}")
        if not isinstance(event, dict) or event.get("EventId") is None:
            raise AgendaSourceUpstreamError(
                f"Legistar Event {event_id!r} for client_code={client_code!r} "
                "returned a malformed response (missing EventId)."
            )
        items_raw = self._get(
            client_code,
            f"{_BASE_URL}/{client_code}/Events/{event_id}/EventItems",
            params={"AgendaNote": "0", "MinutesNote": "0", "Attachments": "1"},
        )
        if not isinstance(items_raw, list):
            raise AgendaSourceUpstreamError(
                f"Legistar EventItems response for event {event_id!r} "
                f"(client_code={client_code!r}) was not a list "
                f"(got {type(items_raw).__name__})."
            )
        items: list[ExternalAgendaItem] = []
        for idx, row in enumerate(items_raw):
            if not isinstance(row, dict):
                continue
            sequence = row.get("EventItemAgendaSequence")
            if sequence is None:
                # ponytail: no order to sort by (signature-block / closing
                # boilerplate rows, confirmed live) -- skip this row, not
                # the whole fetch. The empty-items guard below still fires
                # if skipping leaves nothing real behind.
                continue
            items.append(
                ExternalAgendaItem(
                    order=int(sequence),
                    title=_event_item_title(row, idx),
                    number=row.get("EventItemAgendaNumber"),
                    doc_url=_first_attachment_url(row.get("EventItemMatterAttachments")),
                )
            )
        if not items:
            raise AgendaSourceUpstreamError(
                f"Legistar Event {event_id!r} (client_code={client_code!r}) has no "
                "orderable agenda items -- refusing a silent empty import."
            )
        items.sort(key=lambda i: i.order)
        return ExternalAgenda(
            external_id=str(event["EventId"]),
            title=_event_title(event),
            meeting_datetime=_parse_event_date(event.get("EventDate")),
            source_doc_url=event.get("EventAgendaFile") or event.get("EventInSiteURL"),
            items=items,
        )

    # --- transport -------------------------------------------------------

    def _get(self, client_code: str, url: str, *, params: dict[str, str] | None = None) -> object:
        request_params = dict(params or {})
        if self._token:
            request_params["token"] = self._token
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout_seconds) as client:
                response = client.get(
                    url, params=request_params, headers={"Accept": "application/json"}
                )
        except httpx.TimeoutException as exc:
            raise AgendaSourceUpstreamError(
                f"Legistar request to {url} timed out after {self._timeout_seconds}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise AgendaSourceUpstreamError(f"Legistar request to {url} failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise AgendaSourceAuthRequiredError(
                f"Legistar tenant {client_code!r} requires an API token "
                f"(HTTP {response.status_code} from {url}). Set "
                "CIVICCAST_AGENDA_SOURCE_TOKEN to a token from the city's IT "
                "contact and retry."
            )
        if response.status_code >= 400:
            raise AgendaSourceUpstreamError(
                f"Legistar request to {url} failed with HTTP {response.status_code}."
            )
        try:
            return response.json()
        except ValueError as exc:
            raise AgendaSourceUpstreamError(
                f"Legistar returned malformed JSON from {url}."
            ) from exc


def _event_title(event: dict[str, object]) -> str:
    body_name = event.get("EventBodyName")
    date_str = _parse_event_date(event.get("EventDate"))
    if body_name and date_str is not None:
        return f"{body_name} — {date_str.date().isoformat()}"
    if body_name:
        return str(body_name)
    return "Untitled Legistar meeting"


def _event_item_title(row: dict[str, object], index: int) -> str:
    raw = row.get("EventItemTitle")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:400]
    matter_file = row.get("EventItemMatterFile")
    if isinstance(matter_file, str) and matter_file.strip():
        return matter_file.strip()
    number = row.get("EventItemAgendaNumber")
    if isinstance(number, str) and number.strip():
        return f"Agenda item {number.strip()}"
    return f"Agenda item {index + 1}"


def _first_attachment_url(attachments: object) -> str | None:
    if not isinstance(attachments, list):
        return None
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        link = attachment.get("MatterAttachmentHyperlink")
        if isinstance(link, str) and link.strip():
            return link.strip()
    return None


def _parse_event_date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = ["LegistarSource"]
