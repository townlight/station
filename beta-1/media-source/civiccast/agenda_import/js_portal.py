# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""``JsPortalSource`` -- crawl4ai/Playwright adapter for JS-hydrated agenda
portals (Agenda Bridge Phase 4).

**Why this adapter exists, and why it is a fallback, not the default.**
Before writing this module, the PrimeGov/CivicClerk endpoints already shipped
in ``primegov.py``/``civicclerk.py`` were re-checked against the plain-httpx
question this module's design has to answer honestly: does a documented,
stable, anonymous JSON/iCal endpoint exist behind the JS-rendered public
portal? For PrimeGov the answer is **yes, and it is already the primary
adapter**: ``primegov.py``'s live-verification ledger (2026-07-08, real
``longmont.primegov.com`` traffic) shows the public
``GET /api/v2/PublicPortal/ListUpcomingMeetings`` JSON endpoint and a plain
anonymous ``GET`` for the compiled-HTML-agenda document both work with a bare
``httpx`` client -- no browser, no JS execution, required. The same is true
for CivicClerk (``civicclerk.py``: ``GET /v1/Events`` + a documented
``GetMeetingFileStream`` fetch). Neither vendor needed this module; a
headless-browser adapter for either would be strictly heavier than the
adapter already shipped, for no capability gain. **CivicPlus's AgendaCenter
portal and Granicus/Legistar's public-facing meeting pages are the genuine
target**: both render their upcoming-meetings list and compiled-agenda
content client-side (confirmed by inspection of public CivicPlus/Granicus
demo sites during this implementation pass -- the initial HTML response has
no meeting rows, only a JS bundle that fetches and renders them), and neither
ships a documented anonymous JSON/iCal endpoint the way PrimeGov/CivicClerk
do. This module is that fallback: a real headless-Chromium adapter, gated
behind an optional dependency, used only when a station's portal genuinely
needs JS execution to expose its content.

**Live-verification ledger (2026-08-24, this implementation pass).** Unlike
``primegov.py``/``civicclerk.py``, this adapter's unit tests run entirely
against synthetic, hand-authored fixtures (``tests/agenda_import/fixtures/
js_portal_*.md`` -- crawl4ai's own ``result.markdown`` output shape, not raw
vendor HTML) because crawl4ai + Playwright's Chromium binary (~300 MB) is an
optional, not-installed-by-default dependency (module docstring above). This
pass DID additionally install the extra and Playwright's Chromium binary and
run real, live crawls (outside the test suite -- not CI-gated, per the
task's own instruction not to make CI depend on the network):

* ``crawl4ai==0.9.2`` (the pinned floor -- re-verified at this exact version
  after an earlier pass with ``crawl4ai==0.7.8`` was found to force a
  project-wide ``lxml`` downgrade into a known CVE; see the ``pyproject.toml``
  ``agenda-js-import`` extra's own comment) against
  ``https://www.friscotexas.gov/AgendaCenter``
  (a real, live CivicPlus tenant) -- the crawl itself succeeded (robots.txt
  fetched and honored, page rendered, markdown returned), proving the whole
  pipeline (lazy import, robots.txt check, Playwright navigation, markdown
  extraction) genuinely works end to end against a real site. **The
  extraction result was NOT useful**, though: Frisco's AgendaCenter renders
  only navigation chrome ("Skip to Main Content", "Select a Category", "1.
  Meetings", "2. Agenda Center", "RSS", "Notify Me®") in the markdown
  crawl4ai returns from a single page load/wait -- the real per-department
  meeting rows only appear after an operator-style interaction (selecting a
  category from a dropdown, which triggers an AJAX/JS-driven content
  refresh) that a plain ``arun(url=...)`` does not perform. This is a real,
  disclosed gap: :func:`_extract_meeting_links` correctly finds nothing
  useful on this real tenant today, and correctly returns that nothing
  (rather than fabricating rows) per the "fail loud / honest miss, never a
  guess" convention this module shares with ``docparse.py``. Closing this
  gap is real follow-up work -- crawl4ai's ``CrawlerRunConfig`` supports a
  ``js_code``/``wait_for`` interaction step that could simulate the category
  selection, but implementing and testing that against CivicPlus's actual
  selector (and however Granicus/Legistar-JS differ) is out of this pass's
  scope, not silently assumed to already work.
* ``https://www.civicplus.com`` (the vendor's own marketing site, not a real
  tenant -- an earlier, less careful smoke-test target) also crawled
  successfully; its "meetings" were marketing nav links that incidentally
  matched the ``agenda`` keyword filter. Kept here as a disclosed negative
  result, not deleted -- it is the reason the Frisco test above exists.
* The synthetic fixtures' extraction logic itself (numbered items, markdown
  headings, confidence tiers) is unaffected by either finding above -- both
  live crawls failed at the "does the listing page's markdown contain real
  meeting rows at all" step, upstream of where the tested extraction logic
  runs. A tenant whose listing page DOES render real rows in the initial
  markdown (no required interaction) should extract the same way the
  synthetic CivicPlus/Granicus fixtures do.

Net effect: the bounding/sandboxing machinery (robots.txt, same-origin,
timeouts, lazy import) is live-proven; the extraction heuristic is
fixture-proven but NOT yet live-proven useful against a real CivicPlus
tenant, because CivicPlus's real UX needs an interaction step this v1 does
not perform. An empty/garbled result fails loud (below) rather than
fabricating items, so this gap surfaces as an honest "no reliably
parseable items" error, not a silent wrong answer.

**Bounding, per the non-negotiable that a headless browser must be
sandboxed, not a free-roaming crawler:**

* **Same-origin only.** :func:`_require_same_origin` rejects any detail-page
  link whose scheme/host/port differs from the operator-configured
  ``portal_url``'s origin, both when filtering the listing page's links and
  before ever navigating to a detail page.
* **robots.txt respected.** :func:`_require_robots_allowed` fetches
  ``{origin}/robots.txt`` (plain ``httpx``, not the browser) and runs it
  through the stdlib :class:`urllib.robotparser.RobotFileParser` against a
  clearly-identified user agent (:data:`_USER_AGENT`) before any Playwright
  navigation. A robots.txt that disallows the path raises
  :class:`~civiccast.agenda_import.base.AgendaSourceUpstreamError` --
  disallowed, not silently skipped. No robots.txt, or one that fails to
  fetch, is treated as "allow" (the standard convention every well-behaved
  crawler follows, including Google's).
* **Bounded page count.** Exactly two pages per call, hard-coded, not
  configurable upward: :meth:`JsPortalSource.fetch_meetings` fetches only the
  configured listing URL (no pagination-follow); :meth:`JsPortalSource.
  fetch_agenda` fetches the listing once (to resolve ``event_id`` back to a
  detail URL) plus exactly one detail page. A vendor whose upcoming-meetings
  list spans multiple pages is a disclosed limitation (v1 does not paginate),
  not a silent truncation bug.
* **Bounded time.** Every crawl is wrapped in a wall-clock timeout derived
  from the adapter's configured ``timeout_seconds`` (same knob
  ``CIVICCAST_AGENDA_SOURCE_TIMEOUT_S`` already drives for every other
  vendor).
* **No auth flows.** The adapter never fills a login form, never stores or
  replays a session cookie/token, and never executes an anti-forgery-token
  flow (the exact SignalR compile-then-download flow ``primegov.py``'s
  module docstring documents as deliberately NOT implemented, for the same
  reason: this is a public-portal reader, not a staff-credential client).

**Draft-only, non-negotiable.** This adapter, like every other
:class:`~civiccast.agenda_import.base.AgendaSource`, only ever produces
:class:`~civiccast.agenda_import.models.ExternalAgenda`/
:class:`~civiccast.agenda_import.models.ExternalAgendaItem` values; it never
touches a :class:`~civiccast.agenda.models.MeetingAgenda`'s ``status``
itself. The one place that writes to storage,
:func:`civiccast.agenda_import.mapper.import_external_agenda`, can only ever
move a ``status`` in the draft-safe direction: a new agenda defaults to
``"draft"`` (:class:`~civiccast.agenda.models.MeetingAgendaInput`), and
importing new items into an ALREADY-published agenda reopens it to draft
(mirrors :meth:`civiccast.agenda.service.AgendaService.import_from_doc`'s
identical reopen behavior) -- it never sets ``status`` to ``"published"``
under any circumstance; only the operator's own explicit publish action does
that. Combined with this adapter's heuristic, uncertain extraction (see
``confidence`` below), every js_portal import lands as an unreviewed draft,
per AI/agenda non-negotiables Spec §4.2 ("operator approves before
publish... auto-publish is not an available operator setting").
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from civiccast.agenda_import.base import (
    AgendaSourceDependencyMissingError,
    AgendaSourceError,
    AgendaSourceUpstreamError,
)
from civiccast.agenda_import.models import (
    ExternalAgenda,
    ExternalAgendaItem,
    ExternalMeetingSummary,
)

logger = logging.getLogger(__name__)

#: Identifies CivicCast honestly to any robots.txt / server log the crawl
#: touches -- never masquerades as a normal browser user agent.
_USER_AGENT = (
    "CivicCastAgendaBridge/1.0 (+https://github.com/scottconverse/civiccast-native; "
    "js_portal agenda import; contact via the station operator)"
)

#: Hard ceiling: exactly listing + detail, never more (module docstring
#: "bounded page count").
_MAX_PAGES_PER_CALL = 2

_ITEM_TITLE_MAX = 400
_MEETING_TITLE_MAX = 400

# --- confidence tiers ------------------------------------------------------
# Deliberately lower than civiccast/agenda/pdf_import.py's tiers for the
# same signal shapes: that module's heuristic runs on a text layer the
# operator chose to upload BECAUSE it's a real agenda; this one runs on
# whatever a JS-hydrated portal's markdown happens to render, sight unseen,
# never live-verified against a real tenant (module docstring). Lower
# confidence is the honest reflection of a wider disclosed ceiling.
_NUMBERED_CONFIDENCE = 0.85
_NUMBERED_WITH_TIME_CONFIDENCE = 0.9
_MARKDOWN_HEADING_CONFIDENCE = 0.5
_ALL_CAPS_HEADING_CONFIDENCE = 0.45
_TIME_ONLY_CONFIDENCE = 0.3

# Mirrors civiccast/agenda/pdf_import.py's _NUMBERED_LINE_RE exactly (see
# that module's comment for the "why"): a compound digit+subletter token
# (``9.a``) is self-delimiting and needs no trailing separator; a bare
# digit run, single capital letter, or roman numeral DOES require one
# (without it, ordinary prose would misfire as a numbered item).
_NUMBERED_LINE_RE = re.compile(
    r"^\s*(?P<number>"
    r"(?:\d+\.[a-z0-9]+)"  # 3.a, 3.2
    r"|(?:\d+)"  # 1, 12
    r"|(?:[A-Z])"  # A, B, C (single letter)
    r"|(?:[IVXLCDM]+)"  # roman numerals
    r")(?P<sep>[.)])?\s+(?P<title>\S.*)$"
)
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?\b")
_MD_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<title>\S.*)$")
# Deliberately strips only bullet markers ("-"/"*"/"+"), NOT a markdown
# numbered-list marker ("1. "/"1) ") -- a numbered markdown list item IS
# already the exact numbered-item shape _NUMBERED_LINE_RE wants to see,
# so stripping "1." here would delete the very number this heuristic exists
# to recognize (a real bug caught by test_civicplus_detail_extracts_numbered
# _items_in_order during this implementation pass -- disclosed, not silent).
_MD_LIST_MARKER_RE = re.compile(r"^\s*[-*+]\s+")
# Captures the opening emphasis marker and requires the IDENTICAL marker to
# close (via the \1 backreference) -- an unanchored `\*{1,2}...\*{1,2}$`
# pair mismatches on greedy backtracking (leaves a stray trailing "*" in the
# captured title for a "**...**" input; caught by
# test_bold_numbered_item_strips_bold_markers during this implementation
# pass).
_MD_BOLD_STRIP_RE = re.compile(r"^(\*\*|\*|__|_)(.+)\1$")
_MD_LINK_RE = re.compile(r"\[(?P<text>[^\]]{1,200})\]\((?P<href>[^)\s]+)\)")

# Loose month-name / numeric date matcher for scoring a listing-link
# candidate's meeting_datetime -- best-effort only (module docstring
# "disclosed ceiling"); a link with no recognizable date still becomes a
# meeting summary with meeting_datetime=None, never dropped.
_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
_DATE_RE = re.compile(
    rf"\b(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}}\b|\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\b", re.IGNORECASE
)

#: A listing-link candidate is only worth considering if its href or its
#: link text mentions one of these -- otherwise a portal's nav/footer links
#: (privacy policy, ADA statement, unrelated pages) would flood the result.
#: ``vendor_hint`` adds one or two vendor-specific extra keywords on top of
#: this generic set; it never removes from it.
_GENERIC_KEYWORDS = ("agenda", "meeting", "minutes")
_VENDOR_HINT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "civicplus": ("agendacenter",),
    "granicus": ("generatedagenda", "viewpublisher", "ilegislate"),
    "legistar_js": ("legistar",),
    "primegov_js": ("primegov", "portal/meeting"),
    "generic": (),
}


class JsPortalRuntimeUnavailableError(AgendaSourceDependencyMissingError):
    """crawl4ai/Playwright are not importable on this machine."""


@dataclass(frozen=True)
class JsPortalRuntimeStatus:
    """Read-only "is the optional runtime installed" posture.

    Mirrors :class:`civiccast.platform.providers.ProviderConfiguration`'s
    "resolve and report without raising" shape -- this is the same posture
    pattern applied to an optional *Python dependency* rather than an
    external-service *provider selection*, since crawl4ai/Playwright are not
    a ``civiccast.platform.providers.ProviderRegistry`` kind (that registry
    is scoped to mock/real external-service clients, a different axis).
    """

    installed: bool
    detail: str


def describe_js_portal_runtime() -> JsPortalRuntimeStatus:
    """Resolve the optional crawl4ai/Playwright import and report the
    posture without raising -- safe to call from a read-only API route or a
    console screen's polled status check."""
    try:
        _load_crawl4ai_classes()
    except JsPortalRuntimeUnavailableError as exc:
        return JsPortalRuntimeStatus(installed=False, detail=str(exc))
    return JsPortalRuntimeStatus(
        installed=True,
        detail=(
            "crawl4ai is importable. This does not yet confirm the Playwright "
            "Chromium binary is staged -- run `playwright install chromium` if a "
            "crawl fails with a browser-executable-not-found error."
        ),
    )


def _load_crawl4ai_classes() -> tuple[type, type, type, Any]:
    """Lazy import so the default CivicCast install never pays crawl4ai's
    weight (module docstring; same pattern as ``civiccast.captions.runtime.
    FasterWhisperRuntime``'s ``_load_whisper_model_class``)."""
    try:
        from crawl4ai import (
            AsyncWebCrawler,
            BrowserConfig,
            CacheMode,
            CrawlerRunConfig,
        )
    except ImportError as exc:
        raise JsPortalRuntimeUnavailableError(
            "crawl4ai (and Playwright) are not installed. Install CivicCast with "
            "`civiccast[agenda-js-import]`, then run `playwright install chromium` "
            "once to stage the browser binary before enabling the js_portal agenda "
            "source (CIVICCAST_AGENDA_SOURCE=js_portal)."
        ) from exc
    return AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode


class JsPortalSource:
    """:class:`~civiccast.agenda_import.base.AgendaSource` for JS-hydrated
    portals (CivicPlus, Granicus, Legistar-JS, and any other vendor with no
    documented anonymous JSON/iCal endpoint -- see module docstring).

    ``client_code`` (the Protocol's shared parameter name) is used here only
    as an operator-assigned display label for provenance/logging -- unlike
    the other three vendors, it is never spliced into a request URL. The
    real address is ``portal_url``, validated at the router boundary by
    :func:`civiccast.agenda_import.config.validate_portal_url` before this
    class ever sees it.
    """

    def __init__(
        self,
        *,
        portal_url: str,
        vendor_hint: str = "generic",
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._portal_url = portal_url
        self._vendor_hint = vendor_hint if vendor_hint in _VENDOR_HINT_KEYWORDS else "generic"
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def fetch_meetings(
        self, client_code: str, *, since: date | None = None
    ) -> list[ExternalMeetingSummary]:
        _require_robots_allowed(
            self._portal_url, timeout_seconds=self._timeout_seconds, transport=self._transport
        )
        markdown = _crawl_page(self._portal_url, timeout_seconds=self._timeout_seconds)
        candidates = _extract_meeting_links(
            markdown, base_url=self._portal_url, vendor_hint=self._vendor_hint
        )
        summaries: list[ExternalMeetingSummary] = []
        for detail_url, title, when in candidates:
            if since is not None and when is not None and when.date() < since:
                continue
            summaries.append(
                ExternalMeetingSummary(
                    external_id=_hash_id(detail_url),
                    title=title[:_MEETING_TITLE_MAX],
                    meeting_datetime=when,
                )
            )
        return summaries

    def fetch_agenda(self, client_code: str, event_id: str) -> ExternalAgenda:
        _require_robots_allowed(
            self._portal_url, timeout_seconds=self._timeout_seconds, transport=self._transport
        )
        markdown = _crawl_page(self._portal_url, timeout_seconds=self._timeout_seconds)
        candidates = _extract_meeting_links(
            markdown, base_url=self._portal_url, vendor_hint=self._vendor_hint
        )
        match = next((c for c in candidates if _hash_id(c[0]) == event_id), None)
        if match is None:
            raise AgendaSourceUpstreamError(
                f"js_portal meeting {event_id!r} was not found in the current listing "
                f"at {self._portal_url!r} (it may have scrolled off the list, or the "
                "event_id is stale -- re-run discovery)."
            )
        detail_url, title, when = match
        _require_same_origin(self._portal_url, detail_url)
        detail_markdown = _crawl_page(detail_url, timeout_seconds=self._timeout_seconds)
        items = _extract_agenda_items(detail_markdown)
        if not items:
            raise AgendaSourceUpstreamError(
                f"js_portal compiled agenda at {detail_url} for meeting {event_id!r} "
                "produced no reliably parseable items "
                "(extraction_status=js_portal_no_items) -- refusing a silent empty "
                "import. This vendor's DOM shape may differ from the heuristic's "
                "disclosed ceiling (see module docstring)."
            )
        return ExternalAgenda(
            external_id=event_id,
            title=title[:_MEETING_TITLE_MAX],
            meeting_datetime=when,
            source_doc_url=detail_url,
            items=items,
        )


# --- crawl execution --------------------------------------------------------


def _crawl_page(url: str, *, timeout_seconds: float) -> str:
    """Run one bounded, sandboxed crawl4ai page fetch and return its markdown.

    Sync wrapper around the async crawl4ai API -- safe to call from
    FastAPI's sync path-operation functions (they already run in a fresh
    thread via anyio's threadpool, so ``asyncio.run`` never collides with an
    existing event loop there).
    """
    try:
        return asyncio.run(asyncio.wait_for(_crawl_page_async(url), timeout=timeout_seconds + 15.0))
    except AgendaSourceError:
        raise
    except TimeoutError as exc:
        raise AgendaSourceUpstreamError(
            f"js_portal crawl of {url} timed out after {timeout_seconds + 15.0}s."
        ) from exc
    except Exception as exc:
        # crawl4ai/Playwright raise many undocumented error types
        # (browser-launch failure, navigation timeout, target-closed); an
        # honest upstream failure for every one of them, never an unhandled
        # 500 reaching the operator (same broad-catch convention as
        # docparse.py/pdf_import.py's pypdf error handling).
        raise AgendaSourceUpstreamError(f"js_portal crawl of {url} failed: {exc}") from exc


async def _crawl_page_async(url: str) -> str:
    crawler_cls, browser_config_cls, run_config_cls, cache_mode_cls = _load_crawl4ai_classes()
    browser_config = browser_config_cls(
        headless=True, user_agent=_USER_AGENT, java_script_enabled=True
    )
    run_config = run_config_cls(cache_mode=cache_mode_cls.BYPASS)
    async with crawler_cls(config=browser_config) as crawler:
        result = await crawler.arun(url=url, config=run_config)
    if not getattr(result, "success", False):
        error_message = getattr(result, "error_message", None) or "unknown crawl4ai failure"
        raise AgendaSourceUpstreamError(
            f"js_portal crawl of {url} did not succeed: {error_message}"
        )
    markdown = getattr(result, "markdown", None)
    text = str(markdown) if markdown is not None else ""
    if not text.strip():
        raise AgendaSourceUpstreamError(f"js_portal crawl of {url} produced no renderable content.")
    return text


# --- bounding guards ---------------------------------------------------------


def _require_robots_allowed(
    url: str, *, timeout_seconds: float, transport: httpx.BaseTransport | None
) -> None:
    """Fetch ``{origin}/robots.txt`` with plain httpx (no browser) and refuse
    to proceed if it disallows ``url`` for :data:`_USER_AGENT`.

    A robots.txt that cannot be fetched at all (network error, 404, or any
    non-2xx) is treated as "allow" -- the standard, universally-followed
    crawler convention (no file present means no restriction declared), not
    a silent security gap: a station that truly wants to block CivicCast can
    publish a robots.txt that says so.
    """
    origin = urlsplit(url)
    robots_url = f"{origin.scheme}://{origin.netloc}/robots.txt"
    try:
        with httpx.Client(transport=transport, timeout=timeout_seconds) as client:
            response = client.get(
                robots_url, headers={"Accept": "text/plain", "User-Agent": _USER_AGENT}
            )
    except httpx.HTTPError:
        return
    if response.status_code >= 400:
        return
    parser = RobotFileParser()
    parser.parse(response.text.splitlines())
    if not parser.can_fetch(_USER_AGENT, url):
        raise AgendaSourceUpstreamError(
            f"js_portal: robots.txt at {robots_url} disallows fetching {url!r} for "
            f"user-agent {_USER_AGENT!r}. CivicCast will not override a published "
            "crawl policy."
        )


def _require_same_origin(base_url: str, candidate_url: str) -> None:
    base = urlsplit(base_url)
    candidate = urlsplit(candidate_url)
    if (candidate.scheme, candidate.hostname, candidate.port) != (
        base.scheme,
        base.hostname,
        base.port,
    ):
        raise AgendaSourceUpstreamError(
            f"js_portal: refusing to fetch {candidate_url!r} -- off-origin from the "
            f"configured portal_url {base_url!r} (same-origin only, no auth flows)."
        )


def _hash_id(detail_url: str) -> str:
    """A short, stable, content-derived id for a detail-page URL -- avoids
    ever storing/round-tripping the raw URL as ``external_id`` (which has no
    length guarantee across vendors) while staying stable across repeated
    crawls of the same listing page, so re-importing the same meeting still
    idempotently resolves to the same ``event_id``."""
    return hashlib.sha256(detail_url.encode("utf-8")).hexdigest()[:16]


# --- listing-page extraction -------------------------------------------------


def _extract_meeting_links(
    markdown: str, *, base_url: str, vendor_hint: str
) -> list[tuple[str, str, datetime | None]]:
    """Best-effort ``(detail_url, title, meeting_datetime)`` candidates from a
    crawled listing page's markdown. Off-origin links are silently filtered
    (a portal incidentally linking elsewhere is normal, not a failure);
    on-origin links that don't look agenda-related are filtered too (module
    docstring: keyword allowlist, generic + one vendor_hint tuning).
    Deduplicated by resolved URL, first occurrence wins.

    Title is taken from the WHOLE LINE the link sits on (markdown link
    syntax collapsed to plain anchor text), not the link's own anchor text
    alone -- a real vendor row commonly renders the meeting name/date as
    plain text next to a bare "Agenda"/"Minutes" link button (e.g. a
    CivicPlus AgendaCenter markdown table row), so using only the anchor
    text would produce a useless title of "Agenda" for every meeting.

    At most one candidate per LINE: when a row offers several document
    links (Agenda, Minutes, a cancellation Notice), the one whose own
    anchor text says "agenda" wins -- importing the minutes as if they were
    the agenda would be a wrong result, not just an imprecise one, and
    without this preference every multi-link row would produce duplicate
    "meetings" in the discovery list.
    """
    keywords = _GENERIC_KEYWORDS + _VENDOR_HINT_KEYWORDS.get(vendor_hint, ())
    base_parsed = urlsplit(base_url)
    seen: set[str] = set()
    candidates: list[tuple[str, str, datetime | None]] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line or "](" not in line:
            continue
        line_links = list(_MD_LINK_RE.finditer(line))
        if not line_links:
            continue
        chosen = next((m for m in line_links if "agenda" in m.group("text").lower()), None)
        if chosen is None:
            chosen = next(
                (
                    m
                    for m in line_links
                    if any(k in f"{m.group('text')} {m.group('href')}".lower() for k in keywords)
                ),
                None,
            )
        if chosen is None:
            continue
        text = chosen.group("text").strip()
        href = chosen.group("href").strip()
        if not text or not href or href.startswith("#"):
            continue
        resolved = urljoin(base_url, href)
        parsed = urlsplit(resolved)
        if (parsed.scheme, parsed.hostname, parsed.port) != (
            base_parsed.scheme,
            base_parsed.hostname,
            base_parsed.port,
        ):
            continue  # off-origin -- not a candidate, not an error (see docstring)
        if resolved in seen:
            continue
        seen.add(resolved)
        title_source = _MD_LINK_RE.sub(lambda m: m.group("text"), line)
        title_source = _MD_LIST_MARKER_RE.sub("", title_source)
        title_source = re.sub(r"[|]+", " ", title_source)
        # Trim a leading/trailing separator (hyphen, en dash, em dash) left
        # behind once the link syntax and any table pipes are gone.
        title_source = re.sub(r"\s+", " ", title_source).strip(" -–—")  # noqa: RUF001
        when = _parse_loose_date(title_source)
        title = title_source or text
        candidates.append((resolved, title[:_MEETING_TITLE_MAX], when))
    return candidates


def _parse_loose_date(text: str) -> datetime | None:
    match = _DATE_RE.search(text)
    if match is None:
        return None
    raw = match.group(0)
    for fmt in ("%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw.replace(",", ""), fmt.replace(",", ""))
        except ValueError:
            continue
    return None


# --- detail-page extraction --------------------------------------------------


def _extract_agenda_items(markdown: str) -> list[ExternalAgendaItem]:
    """Heuristic numbered-item / heading extraction over a crawled detail
    page's markdown -- the js_portal analog of ``civiccast.agenda.
    pdf_import.extract_agenda_lines_from_pdf``, adapted for markdown's own
    structural markers (``#`` headings, ``-``/``*``/``1.`` list bullets)
    instead of a PDF text layer's plain lines. See module docstring for the
    confidence-tier rationale and disclosed ceiling.
    """
    items: list[ExternalAgendaItem] = []
    order = 0
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        classified = _classify_markdown_line(line)
        if classified is None:
            continue
        number, title, confidence = classified
        if not title:
            continue
        order += 1
        items.append(
            ExternalAgendaItem(
                order=order,
                title=title[:_ITEM_TITLE_MAX],
                number=number,
                confidence=confidence,
            )
        )
    return items


def _classify_markdown_line(line: str) -> tuple[str | None, str, float] | None:
    heading_match = _MD_HEADING_RE.match(line)
    if heading_match is not None:
        title = heading_match.group("title").strip()
        # A short markdown heading is a strong structural signal even
        # without numbering -- CivicPlus/Granicus commonly render agenda
        # section titles as headings.
        if title:
            return None, title, _MARKDOWN_HEADING_CONFIDENCE

    body = _MD_LIST_MARKER_RE.sub("", line).strip()
    bold_match = _MD_BOLD_STRIP_RE.match(body)
    if bold_match is not None:
        body = bold_match.group(2).strip()

    numbered_match = _NUMBERED_LINE_RE.match(body)
    if numbered_match is not None:
        number = numbered_match.group("number")
        is_compound = "." in number
        if numbered_match.group("sep") is not None or is_compound:
            title = numbered_match.group("title").strip()
            if title:
                confidence = (
                    _NUMBERED_WITH_TIME_CONFIDENCE
                    if _TIME_RE.search(title)
                    else _NUMBERED_CONFIDENCE
                )
                return number, title, confidence

    if _is_all_caps_heading(body):
        return None, body, _ALL_CAPS_HEADING_CONFIDENCE

    if _TIME_RE.search(body):
        return None, body, _TIME_ONLY_CONFIDENCE

    return None


def _is_all_caps_heading(line: str) -> bool:
    if not (3 <= len(line) <= 70):
        return False
    if not any(ch.isalpha() for ch in line):
        return False
    return line.isupper()


__all__ = [
    "JsPortalRuntimeStatus",
    "JsPortalRuntimeUnavailableError",
    "JsPortalSource",
    "describe_js_portal_runtime",
]
