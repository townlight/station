# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Heuristic PDF agenda-item extraction for the operator-upload import path.

:func:`extract_agenda_lines_from_pdf` is the PDF half of
:meth:`civiccast.agenda.service.AgendaService.import_from_doc` (the slice-2
plain-text import already handles ``text/plain``; this module fills the
``NotImplementedError`` gap that used to exist for PDF).

This is a **separate, more permissive** extractor than
:func:`civiccast.agenda_import.docparse.extract_items_from_pdf`, which is
the bounded, ``[]``-on-miss extractor used by the vendor agenda bridge
(PrimeGov / CivicClerk auto-import). That extractor only recognizes flat
top-level ``"1. Title"`` numbering because it is calibrated against a real
captured vendor shape. This module instead targets the case the operator
hits when they upload an arbitrary municipal agenda PDF by hand: no known
vendor shape, inconsistent numbering conventions, section headings, and
call-time markers. Because the input is unconstrained, every extracted
line carries a **confidence** score (0.0-1.0) instead of an all-or-nothing
accept/reject, and the caller (the service layer) treats a PDF import as
AI/heuristic output that requires operator review before it can reach the
public portal (AI/agenda non-negotiables Spec §4.2) -- see
``AgendaService.import_from_doc``'s draft-reopen behavior.

Three line classes are recognized, in this priority order:

1. **Numbered / lettered items** -- ``"1. Call to order"``,
   ``"3.a) Public hearing"``, ``"A. Consent agenda"``, ``"VII. Adjourn"``.
   High confidence (0.95); boosted to 0.98 when the line also carries a
   clock-time marker (a strong "this is a real agenda line, not prose"
   signal).
2. **Section headings** -- a short, ALL-CAPS line with no numbering
   (``"CONSENT AGENDA"``, ``"PUBLIC HEARING"``). Municipal agendas commonly
   use these as unnumbered structural items. Medium confidence (0.55).
3. **Standalone time markers** -- a line that is not numbered or a heading
   but does carry a clock time (``"Public comment - 7:15 PM"``). Lower
   confidence (0.4) since a time marker alone is a weaker signal.

Anything else (prose, page furniture, blank lines) is dropped -- an honest
miss, not a fabricated item, matching the disclosed-ceiling convention
``agenda_import/docparse.py`` already established for the vendor path.

Bounded on purpose: this is a text-layer heuristic over ``pypdf``'s
extracted text, not a layout-aware / OCR parser. A scanned-image PDF with no
text layer yields ``[]``. Multi-column layouts may interleave; wrapped
(multi-line) titles are captured on a single line only. These are known,
disclosed limits -- widening them is real follow-up work, not silently
assumed here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader

#: Mirrors AgendaItem.title's max length (civiccast.agenda.models) so a
#: PDF-imported item never trips the model's length validator.
_TITLE_MAX = 400

# Leading numbering/lettering token: "1.", "1)", "3.a", "3.a)", "A.", "IV."
# A compound digit+subletter token (``3.a``) is self-delimiting -- the
# embedded literal '.' before a lowercase sub-letter is specific enough on
# its own -- so it does NOT require a trailing '.'/')' separator. A bare
# digit run, single capital letter, or roman numeral DOES require the
# trailing separator: without it, ordinary prose ("A large city...", "I
# think...") would misfire as a numbered item.
_NUMBERED_LINE_RE = re.compile(
    r"^\s*(?P<number>"
    r"(?:\d+\.[a-z0-9]+)"  # 3.a, 3.2
    r"|(?:\d+)"  # 1, 12
    r"|(?:[A-Z])"  # A, B, C (single letter)
    r"|(?:[IVXLCDM]+)"  # roman numerals
    r")(?P<sep>[.)])?\s+(?P<title>\S.*)$"
)

# A clock time anywhere in the line: "7:00 PM", "10:30 a.m.", "7:00pm".
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?\b")

# Pure page furniture -- page numbers, "Page 3 of 12" folios.
_PAGE_NOISE_RE = re.compile(r"^(page\s+)?\d+(\s+of\s+\d+)?$", re.IGNORECASE)

_HEADING_MIN_LEN = 3
_HEADING_MAX_LEN = 70

_NUMBERED_CONFIDENCE = 0.95
_NUMBERED_WITH_TIME_CONFIDENCE = 0.98
_HEADING_CONFIDENCE = 0.55
_TIME_ONLY_CONFIDENCE = 0.4


@dataclass(frozen=True)
class ParsedAgendaLine:
    """One heuristically-recognized line from an uploaded agenda PDF."""

    order: int
    number: str | None
    title: str
    confidence: float


def _is_noise_line(line: str) -> bool:
    if len(line) < 1:
        return True
    return bool(_PAGE_NOISE_RE.match(line))


def _is_heading_line(line: str) -> bool:
    if not (_HEADING_MIN_LEN <= len(line) <= _HEADING_MAX_LEN):
        return False
    if not any(ch.isalpha() for ch in line):
        return False
    # str.isupper(): True only when every cased character is uppercase and
    # at least one cased character is present -- exactly the "ALL CAPS
    # heading" signal we want, and it tolerates digits/punctuation/spaces
    # (so "CONSENT AGENDA - ITEMS 1-4" still counts).
    return line.isupper()


def _classify_line(line: str) -> tuple[str | None, str, float] | None:
    """Return ``(number, title, confidence)`` or ``None`` if the line isn't
    a recognizable agenda line."""
    match = _NUMBERED_LINE_RE.match(line)
    if match is not None:
        number = match.group("number")
        # Bare digit/letter/roman forms need the explicit separator (see the
        # regex comment above); the compound "3.a" form is self-delimiting.
        is_compound = "." in number
        if match.group("sep") is not None or is_compound:
            title = match.group("title").strip()
            if title:
                confidence = (
                    _NUMBERED_WITH_TIME_CONFIDENCE
                    if _TIME_RE.search(title)
                    else _NUMBERED_CONFIDENCE
                )
                return number, title, confidence

    if _is_heading_line(line):
        return None, line.strip(), _HEADING_CONFIDENCE

    if _TIME_RE.search(line):
        return None, line.strip(), _TIME_ONLY_CONFIDENCE

    return None


def extract_agenda_lines_from_pdf(pdf_bytes: bytes) -> list[ParsedAgendaLine]:
    """Heuristically extract candidate agenda items from a PDF's text layer.

    Never raises -- a corrupt/unreadable PDF (pypdf raises many undocumented
    error types for real-world malformed files) or a PDF with no
    recognizable lines both yield ``[]``; the caller (the service layer)
    decides whether an empty result is an operator-facing error.
    """
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        pages_text = [page.extract_text() or "" for page in reader.pages]
    except Exception:
        return []

    items: list[ParsedAgendaLine] = []
    order = 0
    for page_text in pages_text:
        for raw_line in page_text.splitlines():
            line = raw_line.strip()
            if not line or _is_noise_line(line):
                continue
            classified = _classify_line(line)
            if classified is None:
                continue
            number, title, confidence = classified
            if not title:
                continue
            order += 1
            items.append(
                ParsedAgendaLine(
                    order=order,
                    number=number,
                    title=title[:_TITLE_MAX],
                    confidence=confidence,
                )
            )
    return items


__all__ = ["ParsedAgendaLine", "extract_agenda_lines_from_pdf"]
