# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S25 meeting-agenda service layer (slice 2).

Sits above :mod:`civiccast.agenda.store` and provides the operations the
router (slice 3) and the player code paths consume:

* :meth:`AgendaService.publish` / :meth:`unpublish` — the publish gate.
  Publishing refuses an empty agenda — DC-1 says a published agenda surfaces
  items + timecodes to viewers, so a zero-item agenda has no public value.
* :meth:`AgendaService.sync_from_chapters` — seeds agenda items from the
  meeting asset's operator-supplied chapter markers (``Asset.chapters_json``).
  The chapter source is injected via ``asset_chapter_provider`` so the
  service stays decoupled from :mod:`civiccast.schedule.store`. Idempotent:
  re-running skips items that already exist at the same ``order`` so
  operator edits survive a re-sync.
* :meth:`AgendaService.import_from_doc` — best-effort import of an
  operator-uploaded agenda doc. ``text/plain`` bodies get a plain line-by-line
  parse: one non-blank line per item, with a leading numbering token
  (``3.a`` / ``VII`` / ``12``) split off into :attr:`AgendaItem.number`.
  ``application/pdf`` bodies go through :mod:`civiccast.agenda.pdf_import`'s
  heuristic text-layer extractor (numbered items, ALL-CAPS section headings,
  standalone time markers), and each resulting item carries a
  :attr:`AgendaItem.confidence` score. Because PDF extraction is a guess, not
  a literal transcription, importing PDF items into an ALREADY-PUBLISHED
  agenda reopens it to ``draft`` (AI/agenda non-negotiables Spec Sec4.2 --
  operator approval before publish) so the new, unreviewed items cannot reach
  the public portal until the operator re-reviews and explicitly republishes.
* :meth:`AgendaService.as_chapter_list` — the read path the player uses:
  returns published-and-timecoded items projected into
  :class:`civiccast.schedule.models.Chapter` shape. Draft agendas project to
  ``[]`` (DC-6 — drafts never reach the public surface).
* :meth:`AgendaService.public_view` — the read path the public endpoint
  uses: returns a :class:`PublicMeetingAgenda` only when the agenda exists
  AND is published; ``None`` otherwise (DC-6 cornerstone).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from civiccast.agenda.models import (
    AgendaChapterSyncResult,
    AgendaChapterSyncSkip,
    AgendaItem,
    MeetingAgenda,
    PublicAgendaItem,
    PublicMeetingAgenda,
)
from civiccast.agenda.pdf_import import extract_agenda_lines_from_pdf
from civiccast.agenda.store import (
    AgendaNotFoundError,
    AgendaPublishEmptyError,
    AgendaStore,
)
from civiccast.schedule.models import Chapter

#: Provider that returns operator-supplied chapter markers for a given
#: meeting asset id. Wired in production to
#: ``PostgresAssetStore.get_staff_row(...).chapters_json`` (decoded). Kept as
#: an injection seam so the service has zero runtime dependency on the
#: schedule store.
AssetChapterProvider = Callable[[str], list[Chapter]]

# Numbering token at the start of an imported line. Examples we accept:
#   "3.a Roll call"          -> number="3.a",  title="Roll call"
#   "3.1 Roll call"          -> number="3.1",  title="Roll call"
#   "12 New business"        -> number="12",   title="New business"
#   "VII Adjourn"            -> number="VII",  title="Adjourn"
# Anything else falls through and the whole line becomes the title.
_NUMBER_PREFIX = re.compile(r"^(?P<number>(?:\d+(?:\.[a-z0-9]+)?)|(?:[IVXLCDM]+))\s+(?P<title>.+)$")

#: Pydantic caps ``AgendaItem.title`` at 400 chars. Long doc lines (paragraph-
#: as-line typos) get clipped so the import doesn't 422 on an otherwise fine
#: paste.
_TITLE_MAX = 400


class AgendaServiceError(RuntimeError):
    """Base error raised by :class:`AgendaService` operations."""


class AgendaPublishError(AgendaServiceError):
    """Publish refused — typically because the agenda has zero items."""


class AgendaImportDecodeError(AgendaServiceError):
    """Raised when the import body cannot be decoded as UTF-8.

    The router translates this to 415 so the operator gets a clean
    diagnostic ("the bytes you uploaded aren't UTF-8"), not a raw 500
    (E-3 / Q-1).
    """


class AgendaImportNoItemsError(AgendaServiceError):
    """Raised when a PDF import found zero recognizable agenda lines.

    Distinct from :class:`AgendaImportDecodeError`: the content type IS
    supported (``application/pdf``) and the PDF WAS readable, but the
    heuristic extractor's disclosed ceiling
    (:mod:`civiccast.agenda.pdf_import`) means an unrecognized layout
    (scanned image, no text layer, non-standard numbering) yields nothing
    reliable. The router maps this to 422 so the operator sees "we couldn't
    find items in this PDF," not a silent empty import (never fabricate a
    best-guess item list) and not a generic 500.
    """


class AgendaService:
    """Publish gating + sync-from-chapters + import + chapter projection."""

    def __init__(
        self,
        store: AgendaStore,
        *,
        asset_chapter_provider: AssetChapterProvider | None = None,
        playback_window_provider: Callable[[str], tuple[float, float | None] | None] | None = None,
    ) -> None:
        self._store = store
        self._chapter_provider = asset_chapter_provider
        self._playback_window = playback_window_provider

    # --- publish gate ---------------------------------------------------

    def publish(self, agenda_id: str) -> MeetingAgenda:
        """Flip the agenda to ``published``. Refuses if it has zero items.

        DC-1 says a published agenda exposes items + timecodes to viewers,
        so a zero-item publish has no public value and is almost certainly
        an operator slip. DC-6 — the public endpoint trusts ``status`` — so
        we fail closed here rather than serve an empty list.

        Atomicity (E-5): the items-exist check + status flip ride a single
        transaction inside :meth:`AgendaStore.publish_if_nonempty`, so a
        concurrent ``delete_item`` between them cannot publish an empty
        agenda. The router never sees a non-atomic race here.
        """
        try:
            return self._store.publish_if_nonempty(agenda_id)
        except AgendaPublishEmptyError as exc:
            raise AgendaPublishError(
                f"Cannot publish agenda {agenda_id!r}: it has zero items. "
                "Add at least one item (DC-1) before publishing."
            ) from exc

    def unpublish(self, agenda_id: str) -> MeetingAgenda:
        """Flip the agenda back to ``draft``. No item-count check."""
        agenda = self._store.get_agenda(agenda_id)
        if agenda is None:
            raise AgendaNotFoundError(f"Meeting agenda {agenda_id!r} not found.")
        return self._store.set_status(agenda_id, "draft")

    # --- sync from chapters --------------------------------------------

    def sync_from_chapters(self, agenda_id: str) -> AgendaChapterSyncResult:
        """Seed items from the meeting asset's ``chapters_json``.

        Each chapter becomes a draft item:
        ``item_id = f"{agenda_id}-ch-{idx}"``, ``order=idx``,
        ``title=Chapter.name``, ``video_timecode_s=int(Chapter.t)``.

        Idempotent: an existing item at the same ``(agenda_id, order)`` is
        SKIPPED — the operator may have edited it, and we don't blast their
        work on a re-sync. Returns ONLY the items the service wrote on this
        call (skipped items are not in the return list).
        """
        if self._chapter_provider is None:
            raise AgendaServiceError(
                "sync_from_chapters requires an asset_chapter_provider; "
                "the service was constructed without one."
            )
        agenda = self._store.get_agenda(agenda_id)
        if agenda is None:
            raise AgendaNotFoundError(f"Meeting agenda {agenda_id!r} not found.")
        existing_by_order = {
            item.order: item for item in self._store.list_items(agenda_id, order_by="order")
        }
        chapters = self._chapter_provider(agenda.meeting_asset_id)
        source_offset = 0.0
        if agenda.source_recording_job_id and self._playback_window is not None:
            window = self._playback_window(agenda.meeting_asset_id)
            if window is not None:
                source_offset = window[0]
        written: list[AgendaItem] = []
        skipped: list[AgendaChapterSyncSkip] = []
        for idx, chapter in enumerate(chapters):
            existing_item = existing_by_order.get(idx)
            if existing_item is not None:
                skipped.append(
                    AgendaChapterSyncSkip(
                        order=idx,
                        chapter_title=chapter.name,
                        existing_item_title=existing_item.title,
                    )
                )
                continue
            item = AgendaItem(
                item_id=f"{agenda_id}-ch-{idx}",
                agenda_id=agenda_id,
                order=idx,
                title=chapter.name,
                video_timecode_s=int(chapter.t + source_offset),
            )
            written.append(self._store.upsert_item(item))
        return AgendaChapterSyncResult(
            chapter_count=len(chapters),
            added_items=written,
            skipped_existing=skipped,
        )

    # --- import from operator-uploaded doc ------------------------------

    def import_from_doc(
        self,
        agenda_id: str,
        *,
        doc_bytes: bytes,
        content_type: str = "text/plain",
    ) -> list[AgendaItem]:
        """Best-effort parse of an operator-uploaded agenda doc.

        ``text/plain``: each non-blank line becomes a draft item, taken
        literally. A leading numbering token (``3.a`` / ``3.1`` / ``12`` /
        Roman ``VII``) is split off into :attr:`AgendaItem.number`.
        :attr:`AgendaItem.confidence` stays ``None`` — the operator typed or
        pasted this text themselves, so there is nothing to be uncertain
        about.

        ``application/pdf``: text-layer heuristic extraction via
        :mod:`civiccast.agenda.pdf_import` (numbered items, ALL-CAPS section
        headings, standalone time markers). Each item's
        :attr:`AgendaItem.confidence` carries the heuristic's reliability
        score. A PDF that yields zero recognizable lines raises
        :class:`AgendaImportNoItemsError` rather than silently importing
        nothing. Because this is AI/heuristic-guessed content (AI/agenda
        non-negotiables Spec Sec4.2 — operator approval before publish), a
        PDF import that lands items on an agenda that is currently
        ``published`` reopens it to ``draft`` so the new items cannot reach
        the public portal until the operator reviews and republishes.

        Any other content type raises :class:`NotImplementedError`.

        ``video_timecode_s`` stays ``None`` for every import path — the
        operator scrubs each item to its in-video moment afterwards.

        Idempotent on re-run via the same ``(agenda_id, order)`` skip rule
        :meth:`sync_from_chapters` uses.
        """
        normalized = content_type.split(";", 1)[0].strip().lower()
        if normalized not in ("text/plain", "application/pdf"):
            raise NotImplementedError(
                f"import_from_doc: content_type {content_type!r} is not supported. "
                "Only 'text/plain' and 'application/pdf' are parsed; paste the doc's "
                "extracted text in the meantime for anything else (DOCX, ...)."
            )
        agenda = self._store.get_agenda(agenda_id)
        if agenda is None:
            raise AgendaNotFoundError(f"Meeting agenda {agenda_id!r} not found.")
        existing = self._store.list_items(agenda_id, order_by="order")
        taken_orders = {item.order for item in existing}

        written: list[AgendaItem] = []
        if normalized == "application/pdf":
            parsed_lines = extract_agenda_lines_from_pdf(doc_bytes)
            if not parsed_lines:
                raise AgendaImportNoItemsError(
                    f"No recognizable agenda items were found in the uploaded PDF for "
                    f"agenda {agenda_id!r}. The PDF may be a scanned image with no text "
                    "layer, or use a numbering/heading style the importer doesn't "
                    "recognize yet. Paste the agenda's text instead."
                )
            for parsed in parsed_lines:
                idx = parsed.order - 1
                if idx in taken_orders:
                    continue
                item = AgendaItem(
                    item_id=f"{agenda_id}-imp-{idx}",
                    agenda_id=agenda_id,
                    order=idx,
                    number=parsed.number,
                    title=parsed.title[:_TITLE_MAX],
                    confidence=parsed.confidence,
                )
                written.append(self._store.upsert_item(item))
            # AI-guessed content must clear operator review before it can be
            # public again (Spec Sec4.2). A draft agenda was already gated by
            # the ordinary publish flow; only a currently-published agenda
            # needs this explicit reopen.
            if written and agenda.status == "published":
                self._store.set_status(agenda_id, "draft")
            return written

        try:
            text = doc_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            # The router maps this to 415 so the operator sees "your bytes
            # weren't UTF-8" rather than a raw 500 (E-3 / Q-1). The byte
            # position lands in the message so the dev can trace it.
            raise AgendaImportDecodeError(
                f"Body could not be decoded as UTF-8 (invalid byte at position {exc.start})."
            ) from exc
        lines = [line.strip() for line in text.splitlines()]
        lines = [line for line in lines if line]

        for idx, line in enumerate(lines):
            if idx in taken_orders:
                continue
            number, title = _split_number_and_title(line)
            item = AgendaItem(
                item_id=f"{agenda_id}-imp-{idx}",
                agenda_id=agenda_id,
                order=idx,
                number=number,
                title=title[:_TITLE_MAX],
            )
            written.append(self._store.upsert_item(item))
        return written

    # --- read projections ----------------------------------------------

    def as_chapter_list(self, agenda_id: str) -> list[Chapter]:
        """Project the agenda's published-and-timecoded items as Chapters.

        Used by the player's chapter list when an agenda exists for the
        meeting asset. Returns ``[]`` for a draft agenda (DC-6 — drafts
        never surface to viewers) OR for a published agenda whose items
        all lack timecodes (nothing to seek to).
        """
        agenda = self._store.get_agenda(agenda_id)
        if agenda is None or agenda.status != "published":
            return []
        items = self._store.list_items(agenda_id, order_by="timecode")
        chapters: list[Chapter] = []
        for item in items:
            if item.video_timecode_s is None:
                continue
            chapters.append(
                Chapter(
                    t=float(item.video_timecode_s),
                    name=item.title,
                    sub=None,
                )
            )
        return chapters

    def source_playback_window(self, asset_id: str) -> tuple[float, float | None] | None:
        """Immutable package window for source-cited staff records."""
        return self._playback_window(asset_id) if self._playback_window else None

    def playback_duration_seconds(self, asset_id: str) -> float | None:
        """Published player duration, when an immutable package range is known."""
        window = self._playback_window(asset_id) if self._playback_window else None
        return window[1] - window[0] if window and window[1] is not None else None

    def public_view(
        self,
        station_id: str,
        meeting_asset_id: str,
    ) -> PublicMeetingAgenda | None:
        """Return the public projection if and only if a published agenda
        exists for ``(station_id, meeting_asset_id)``. ``None`` otherwise.

        DC-6 cornerstone: draft agendas (and missing agendas) MUST surface
        as 404 on the public endpoint — the router translates this ``None``.
        """
        agenda = self._store.get_agenda_by_asset(station_id, meeting_asset_id)
        if agenda is None or agenda.status != "published":
            return None
        items = self._store.list_items(agenda.agenda_id, order_by="order")
        start, end = 0.0, None
        if self._playback_window is not None:
            window = self._playback_window(meeting_asset_id)
            if window is None:
                return None
            if agenda.source_recording_job_id:
                start, end = window
        # A chapter spans until the next timed item. Keep the chapter active at
        # a trimmed package's beginning, rebased to zero, and omit clipped items.
        timed = sorted(
            {item.video_timecode_s for item in items if item.video_timecode_s is not None}
        )
        mapped = []
        for item in items:
            point = item.video_timecode_s
            if point is not None:
                following = next((value for value in timed if value > point), end)
                if (end is not None and point >= end) or (
                    following is not None and following <= start
                ):
                    continue
                item = item.model_copy(
                    update={"video_timecode_s": max(float(point), start) - start}
                )
            mapped.append(item)
        return PublicMeetingAgenda(
            agenda_id=agenda.agenda_id,
            meeting_asset_id=meeting_asset_id,
            source_doc_url=agenda.source_doc_url,
            items=[
                PublicAgendaItem(
                    item_id=item.item_id,
                    order=item.order,
                    number=item.number,
                    title=item.title,
                    video_timecode_s=item.video_timecode_s,
                    doc_anchor=item.doc_anchor,
                )
                for item in mapped
            ],
        )


def _split_number_and_title(line: str) -> tuple[str | None, str]:
    """Split ``"3.a Roll call"`` into ``("3.a", "Roll call")``.

    Returns ``(None, line)`` when the line has no recognizable leading
    numbering token. The regex is intentionally narrow: a stray sentence
    starting with a digit shouldn't get a phantom ``number``.
    """
    match = _NUMBER_PREFIX.match(line)
    if match is None:
        return None, line
    return match.group("number"), match.group("title").strip()


__all__ = [
    "AgendaImportDecodeError",
    "AgendaImportNoItemsError",
    "AgendaPublishError",
    "AgendaService",
    "AgendaServiceError",
    "AssetChapterProvider",
]
