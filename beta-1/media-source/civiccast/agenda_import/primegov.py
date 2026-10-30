# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""``PrimeGovSource`` -- anonymous PrimeGov Public Portal adapter (plan §3, §8
Phase 2). LPM's own dogfood tenant (``longmont``).

Live-verification ledger (2026-07-08, this implementation pass -- re-verified
independently of the plan's 2026-07-05 planning-pass findings, against the
real ``longmont.primegov.com`` tenant):

* ``GET https://{tenant}.primegov.com/api/v2/PublicPortal/ListUpcomingMeetings``
  -- **public, anonymous, real data.** Returned 7 real upcoming Longmont
  meetings with a ``documentList[]`` per meeting (fields used here: ``id``,
  ``title``, ``dateTime``, and each document's ``id``, ``templateId``,
  ``templateName``, ``compileOutputType``, ``link``).
* ``GET https://{tenant}.primegov.com/Portal/Meeting?meetingTemplateId={id}``
  -- **public, anonymous, real data**, for a document whose
  ``compileOutputType == 3`` ("HTML Agenda"/"HTML Packet"). Confirmed against
  two real Longmont meetings (3726 single-item orientation meeting; 3727,
  7-item Housing and Human Service Advisory Board agenda) -- server-rendered
  ``<tr class='section-row'>`` blocks, parsed by
  :func:`civiccast.agenda_import.docparse.extract_items_from_html`.
* ``GET https://{tenant}.primegov.com/Portal/MeetingPreview?
  compiledMeetingDocumentFileId={id}`` -- **requires a staff login**
  (redirects to ``/Login``). NOT used.
* Every guessed anonymous download URL for a ``compileOutputType == 1``
  (PDF) document (``/Public/CompiledDocument/Download?documentId=``, several
  other patterns) returned PrimeGov's own "Document Not Found" page. The
  real per-document PDF download is a client-side SignalR
  ``/api/MeetingItem/CompileAsync`` compile-then-download flow gated behind
  an anti-forgery token pulled from an authenticated session -- not a plain
  anonymous GET. **No verified anonymous PDF-fetch URL exists for Longmont**
  in this pass. Of Longmont's 7 real upcoming meetings, 4 have an HTML
  agenda (``compileOutputType == 3``); 3 do not (one is a real, non-cancelled
  meeting with only a ``compileOutputType == 2`` document of unknown
  meaning; the other two are "CANCELLED"/"Notice of Cancellation" meetings
  with only a PDF cancellation notice).
* Every document's ``link`` field was ``null`` on every meeting/document
  observed for Longmont. :func:`_select_fallback_document` below only fires
  for a hypothetical tenant that DOES populate ``link`` with a genuinely
  fetchable URL for a non-HTML document -- untested against any real tenant,
  disclosed honestly rather than silently assumed to work.

Net effect: this adapter's fully-proven, live-verified path is
HTML-compiled-agenda extraction. A meeting with no HTML document surfaces a
distinct, actionable :class:`~civiccast.agenda_import.base.
AgendaSourceUpstreamError` naming which extraction path was unavailable
(plan §9 "fail loud, never silently import nothing") rather than fabricating
items or guessing at an unverified PDF URL.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import httpx

from civiccast.agenda_import.base import (
    AgendaSourceAuthRequiredError,
    AgendaSourceUpstreamError,
)
from civiccast.agenda_import.docparse import extract_items_from_html, extract_items_from_pdf
from civiccast.agenda_import.models import (
    ExternalAgenda,
    ExternalAgendaItem,
    ExternalMeetingSummary,
)

_LIST_UPCOMING_PATH = "/api/v2/PublicPortal/ListUpcomingMeetings"


class PrimeGovSource:
    """:class:`~civiccast.agenda_import.base.AgendaSource` for PrimeGov.

    ``client_code`` is the tenant subdomain (e.g. ``"longmont"`` for
    ``longmont.primegov.com``) -- unlike Legistar, where it's a path segment
    on one shared host.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def fetch_meetings(
        self, client_code: str, *, since: date | None = None
    ) -> list[ExternalMeetingSummary]:
        meetings = self._list_meetings(client_code)
        summaries: list[ExternalMeetingSummary] = []
        for meeting in meetings:
            meeting_datetime = _parse_meeting_datetime(meeting.get("dateTime"))
            if (
                since is not None
                and meeting_datetime is not None
                and meeting_datetime.date() < since
            ):
                continue
            summaries.append(
                ExternalMeetingSummary(
                    external_id=str(meeting["id"]),
                    title=_meeting_title(meeting),
                    meeting_datetime=meeting_datetime,
                )
            )
        return summaries

    def fetch_agenda(self, client_code: str, event_id: str) -> ExternalAgenda:
        meetings = self._list_meetings(client_code)
        meeting = next((m for m in meetings if str(m.get("id")) == event_id), None)
        if meeting is None:
            raise AgendaSourceUpstreamError(
                f"PrimeGov meeting {event_id!r} was not found among tenant "
                f"{client_code!r}'s upcoming meetings (it may be archived, or "
                "the event_id is wrong)."
            )
        documents = meeting.get("documentList")
        if not isinstance(documents, list):
            raise AgendaSourceUpstreamError(
                f"PrimeGov meeting {event_id!r} (tenant {client_code!r}) has a "
                f"malformed documentList (got {type(documents).__name__})."
            )

        html_doc = _select_html_document(documents)
        if html_doc is not None:
            return self._fetch_html_agenda(client_code, meeting, html_doc)

        fallback_doc = _select_fallback_document(documents)
        if fallback_doc is not None:
            return self._fetch_pdf_agenda(client_code, meeting, fallback_doc)

        raise AgendaSourceUpstreamError(
            f"PrimeGov meeting {event_id!r} (tenant {client_code!r}) has no HTML "
            "compiled agenda and no fetchable fallback document "
            "(extraction_status=no_supported_document). This is a real PrimeGov "
            "limitation, not a bug -- ask LPM whether to build a fuller "
            "PDF-download flow for this tenant (plan §13 Open Question 4)."
        )

    # --- document fetch + extraction -------------------------------------

    def _fetch_html_agenda(
        self, client_code: str, meeting: dict[str, Any], document: dict[str, Any]
    ) -> ExternalAgenda:
        template_id = document.get("templateId")
        if template_id is None:
            raise AgendaSourceUpstreamError(
                f"PrimeGov HTML agenda document for meeting {meeting.get('id')!r} "
                f"(tenant {client_code!r}) is missing templateId."
            )
        doc_url = f"{_base_url(client_code)}/Portal/Meeting?meetingTemplateId={template_id}"
        page_html = self._get_text(client_code, doc_url)
        items = extract_items_from_html(page_html)
        if not items:
            raise AgendaSourceUpstreamError(
                f"PrimeGov compiled HTML agenda at {doc_url} for meeting "
                f"{meeting.get('id')!r} produced no reliably parseable items "
                "(extraction_status=html_no_items) -- refusing a silent empty "
                "import."
            )
        return _build_agenda(meeting, source_doc_url=doc_url, items=items)

    def _fetch_pdf_agenda(
        self, client_code: str, meeting: dict[str, Any], document: dict[str, Any]
    ) -> ExternalAgenda:
        link = document["link"]  # presence guaranteed by _select_fallback_document
        if not (link.startswith("http://") or link.startswith("https://")):
            # A hostile/unsupported scheme here would otherwise reach
            # httpx.Client.get() and raise a raw ValueError ("unknown url
            # type") instead of a clean adapter error -- caught here before
            # any network attempt. The mapper's validate_source_doc_url is
            # still the authoritative allowlist for what gets STORED (plan
            # §10); this is a pre-fetch guard so a hostile vendor value can't
            # crash the adapter itself.
            raise AgendaSourceUpstreamError(
                f"PrimeGov fallback document link for meeting {meeting.get('id')!r} "
                f"(tenant {client_code!r}) uses an unsupported scheme -- refusing to "
                f"fetch {link!r}."
            )
        pdf_bytes = self._get_bytes(client_code, link)
        items = extract_items_from_pdf(pdf_bytes)
        if not items:
            raise AgendaSourceUpstreamError(
                f"PrimeGov PDF compiled agenda at {link} for meeting "
                f"{meeting.get('id')!r} produced no reliably parseable items "
                "(extraction_status=pdf_no_items) -- refusing a silent empty "
                "import. This adapter's PDF extraction only recognizes flat, "
                "top-level numbered lines (see docparse.py); a differently "
                "formatted PDF is an honest miss, not a bug to silently patch "
                "around."
            )
        return _build_agenda(meeting, source_doc_url=link, items=items)

    # --- meeting list + transport -----------------------------------------

    def _list_meetings(self, client_code: str) -> list[dict[str, Any]]:
        data = self._get_json(client_code, f"{_base_url(client_code)}{_LIST_UPCOMING_PATH}")
        if not isinstance(data, list):
            raise AgendaSourceUpstreamError(
                f"PrimeGov ListUpcomingMeetings response for tenant "
                f"{client_code!r} was not a list (got {type(data).__name__})."
            )
        meetings: list[dict[str, Any]] = []
        for row in data:
            if isinstance(row, dict) and row.get("id") is not None:
                meetings.append(row)
        return meetings

    def _request(self, client_code: str, url: str) -> httpx.Response:
        try:
            with httpx.Client(transport=self._transport, timeout=self._timeout_seconds) as client:
                response = client.get(url, headers={"Accept": "application/json, text/html"})
        except httpx.TimeoutException as exc:
            raise AgendaSourceUpstreamError(
                f"PrimeGov request to {url} timed out after {self._timeout_seconds}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise AgendaSourceUpstreamError(f"PrimeGov request to {url} failed: {exc}") from exc

        if response.status_code in (401, 403):
            # Defensive, not live-proven: no PrimeGov tenant encountered during
            # this implementation pass was token-gated (unlike Legistar's NYC).
            # Kept for symmetry with LegistarSource / plan §10's error taxonomy
            # in case a real gated tenant surfaces later.
            raise AgendaSourceAuthRequiredError(
                f"PrimeGov tenant {client_code!r} returned HTTP "
                f"{response.status_code} for {url} -- this tenant may require "
                "authentication that this adapter does not support."
            )
        if response.status_code >= 400:
            raise AgendaSourceUpstreamError(
                f"PrimeGov request to {url} failed with HTTP {response.status_code}."
            )
        return response

    def _get_json(self, client_code: str, url: str) -> object:
        response = self._request(client_code, url)
        try:
            return response.json()
        except ValueError as exc:
            raise AgendaSourceUpstreamError(
                f"PrimeGov returned malformed JSON from {url}."
            ) from exc

    def _get_text(self, client_code: str, url: str) -> str:
        return self._request(client_code, url).text

    def _get_bytes(self, client_code: str, url: str) -> bytes:
        return self._request(client_code, url).content


def _base_url(client_code: str) -> str:
    return f"https://{client_code}.primegov.com"


def _meeting_title(meeting: dict[str, Any]) -> str:
    title = meeting.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return "Untitled PrimeGov meeting"


def _parse_meeting_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _select_html_document(documents: list[Any]) -> dict[str, Any] | None:
    """Prefer a ``compileOutputType == 3`` (HTML) document. When more than one
    exists (e.g. "HTML Agenda" + "HTML Packet", confirmed live), prefer the
    one whose template name doesn't say "packet" -- the plain agenda, not
    the fuller attachments-included packet."""
    html_docs = [
        d
        for d in documents
        if isinstance(d, dict)
        and d.get("compileOutputType") == 3
        and d.get("templateId") is not None
    ]
    non_packet = [d for d in html_docs if "packet" not in str(d.get("templateName", "")).lower()]
    if non_packet:
        return non_packet[0]
    return html_docs[0] if html_docs else None


def _select_fallback_document(documents: list[Any]) -> dict[str, Any] | None:
    """A non-HTML document with a genuinely vendor-provided fetchable URL.

    Untested against any real PrimeGov tenant (Longmont's ``link`` is always
    ``null`` -- see module docstring); this only fires for a tenant that
    populates it."""
    candidates = [
        d for d in documents if isinstance(d, dict) and isinstance(d.get("link"), str) and d["link"]
    ]
    return candidates[0] if candidates else None


def _build_agenda(
    meeting: dict[str, Any], *, source_doc_url: str, items: list[ExternalAgendaItem]
) -> ExternalAgenda:
    return ExternalAgenda(
        external_id=str(meeting["id"]),
        title=_meeting_title(meeting),
        meeting_datetime=_parse_meeting_datetime(meeting.get("dateTime")),
        source_doc_url=source_doc_url,
        items=items,
    )


__all__ = ["PrimeGovSource"]
