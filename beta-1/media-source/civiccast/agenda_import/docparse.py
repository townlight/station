# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Shared agenda-document item extraction (plan §5 ``docparse.py``).

Built in Phase 2 for PrimeGov, reused unchanged by Phase 3 (CivicClerk) --
neither adapter is the ONE place that decides what to do with an empty
result; both extractors here just return ``[]`` when nothing reliable is
found, and it's the calling :class:`~civiccast.agenda_import.base.AgendaSource`
adapter's job to treat that as "fail loud" per plan §9/§10.

Two extractors:

* :func:`extract_items_from_html` -- PrimeGov's real, **live-verified**
  compiled-agenda HTML shape (``longmont.primegov.com/Portal/Meeting?
  meetingTemplateId=...``, captured 2026-07-08): a flat sequence of
  ``<tr class='section-row'>`` blocks, each with a ``.number-cell-section``
  order label and a ``.section-heading .item-cell`` title (itself a run of
  ``<b>`` fragments). Regex-based, not a DOM parser -- the repo has no
  HTML-parsing dependency (`bs4`/`lxml` are not declared; stdlib ``re`` +
  ``html.unescape`` is the ladder-rung-3 answer) and this shape is regular
  enough that a non-greedy scan over real captured fixtures round-trips
  exactly (golden test: ``tests/agenda_import/test_docparse.py``).

* :func:`extract_items_from_pdf` -- a bounded, honest PDF-text extractor
  (``pypdf``) for the vendor-family case where a compiled agenda ships only
  as a PDF. **Ceiling, stated up front:** it recognizes flat, top-level
  numbered lines only (``"1. Call to order"``) -- the same numbering
  convention :func:`civiccast.agenda.service._split_number_and_title` already
  accepts for operator-pasted text. Lettered/roman-numeral sub-items,
  multi-line wrapped titles, and scanned-image PDFs (no extractable text
  layer) are NOT recognized; such a PDF yields ``[]``, and the caller must
  treat that as "no reliable items" (an honest miss), never fabricate a
  best-guess item list. Upgrade path if a PDF-heavy tenant needs more:
  layout-aware extraction (column/font-size heuristics) is real, scoped
  future work, not silently bolted on here.

  This extractor was proven against a **synthetically generated** PDF (via
  ``reportlab``, an existing repo dependency) in
  ``tests/agenda_import/test_docparse.py::TestExtractItemsFromPdf`` --
  **no live PrimeGov tenant's PDF-only compiled agenda was fetchable
  anonymously during this implementation pass** (every guessed download URL
  for a ``compileOutputType: 1`` document returned PrimeGov's own
  "Document Not Found" page; the real download path is a client-side
  SignalR compile-then-download flow gated behind an anti-forgery token, not
  a plain GET). See ``primegov.py`` module docstring for the live-verification
  ledger. This is a known, disclosed gap, not a silent one.
"""

from __future__ import annotations

import html
import re
from io import BytesIO

from pypdf import PdfReader

from civiccast.agenda_import.models import ExternalAgendaItem

_ITEM_TITLE_MAX = 400

# One ``<tr class='section-row'>`` block: an order label cell followed by a
# title cell. Non-greedy across the whole block so nested markup (an
# attachment-icon <span> in the number cell; multiple <b> runs in the title)
# is captured as raw HTML and cleaned by ``_clean_text`` below, rather than
# needing a full DOM walk.
_SECTION_ROW_RE = re.compile(
    r"class=['\"]section-row['\"]>.*?"
    r"class=['\"]number-cell-section['\"]>(?P<number>.*?)</td>.*?"
    r"class=['\"]section-heading['\"]>.*?"
    r"class=['\"]item-cell[^'\"]*['\"][^>]*>(?P<title>.*?)</div>",
    re.DOTALL,
)

# Bounded PDF heuristic (module docstring "ceiling"): a line that begins with
# a plain integer + separator, mirroring the existing operator-paste
# convention in civiccast/agenda/service.py's _NUMBER_PREFIX -- restricted to
# the digit-only form here (no roman numerals / letter-suffixed sub-items)
# because that's the form actually seen in PrimeGov's HTML output (see
# module docstring); widening it is future work, not assumed here.
_PDF_NUMBERED_LINE_RE = re.compile(r"^\s*(?P<number>\d+)[.)]\s+(?P<title>\S.*)$")


def _clean_text(fragment: str) -> str:
    """Strip tags, unescape entities, collapse whitespace."""
    no_tags = re.sub(r"<[^>]+>", " ", fragment)
    unescaped = html.unescape(no_tags)
    return re.sub(r"\s+", " ", unescaped).strip()


def extract_items_from_html(compiled_html: str) -> list[ExternalAgendaItem]:
    """Extract ordered items from a PrimeGov compiled-agenda HTML page.

    Order is the item's position in the document (1-based) -- the vendor
    doesn't ship a separate machine-readable sequence field the way Legistar
    does, but the HTML is itself already in display order, so position IS
    the order. The ``number`` label ("1.", "2.") is kept verbatim as
    display metadata, same convention as Legistar's ``EventItemAgendaNumber``.

    Returns ``[]`` if no ``section-row`` blocks are found (corrupted/
    shape-drifted HTML) -- the caller (``PrimeGovSource.fetch_agenda``)
    decides whether that's fatal, per the shared "fail loud, never a silent
    empty import" rule (plan §9/§10).
    """
    items: list[ExternalAgendaItem] = []
    for idx, match in enumerate(_SECTION_ROW_RE.finditer(compiled_html), start=1):
        number = _clean_text(match.group("number")) or None
        title = _clean_text(match.group("title"))
        if not title:
            title = f"Agenda item {idx}"
        items.append(ExternalAgendaItem(order=idx, title=title[:_ITEM_TITLE_MAX], number=number))
    return items


def extract_items_from_pdf(pdf_bytes: bytes) -> list[ExternalAgendaItem]:
    """Extract ordered items from a compiled-agenda PDF's text layer.

    Bounded on purpose -- see the module docstring's "ceiling" note. Returns
    ``[]`` (never raises) on a malformed/unreadable PDF or a PDF with no text
    lines matching the recognized numbering convention; the caller decides
    whether an empty result is fatal.
    """
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        pages_text = [page.extract_text() or "" for page in reader.pages]
    except Exception:
        # pypdf raises many undocumented error types beyond PdfReadError/
        # ValueError (NotImplementedError, KeyError, AttributeError, ...) for
        # real-world malformed/unusual vendor PDFs; extraction failure is
        # already an "honest miss" -> [] for every caller, so widening the
        # catch doesn't change behavior, it just makes that contract
        # actually hold instead of leaking a raw crash.
        return []

    items: list[ExternalAgendaItem] = []
    order = 0
    for page_text in pages_text:
        for raw_line in page_text.splitlines():
            match = _PDF_NUMBERED_LINE_RE.match(raw_line)
            if match is None:
                continue
            order += 1
            title = match.group("title").strip()
            if not title:
                continue
            items.append(
                ExternalAgendaItem(
                    order=order, title=title[:_ITEM_TITLE_MAX], number=match.group("number")
                )
            )
    return items


__all__ = ["extract_items_from_html", "extract_items_from_pdf"]
