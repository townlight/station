# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""``ExternalAgenda`` -> ``AgendaStore`` writes -- the ONE place that writes.

Reuses :meth:`civiccast.agenda.store.AgendaStore.upsert_item`'s existing
``(agenda_id, order)`` idempotency: :meth:`civiccast.agenda.service.
AgendaService.sync_from_chapters` / ``import_from_doc`` already skip an item
whose ``order`` is taken, so operator edits survive a re-run. This module
applies the exact same skip rule for external imports -- "re-running import
on the same EventId does not duplicate or clobber operator-edited items" is
the single most important acceptance test in the sprint (plan §8), and it
rides infrastructure that is already tested, not new logic.

Vendor data crosses the same trust boundary as an operator-typed URL, so
every ``doc_url`` (item-level and the agenda-level ``source_doc_url``) is
validated through the existing :func:`civiccast.agenda.models.
validate_source_doc_url` allowlist (plan §10) BEFORE any write happens --
one hostile link anywhere in the payload rejects the whole import rather
than leaving a partially-written agenda.

**Draft-only guarantee (AI/agenda non-negotiables Spec §4.2).** A new
agenda already starts as a draft, so the only case that matters is
importing into an agenda that is ALREADY published: written items land, and
if the agenda was published, it is reopened to draft (mirrors
:meth:`civiccast.agenda.service.AgendaService.import_from_doc`'s identical
reopen behavior, added here Agenda Bridge Phase 4 alongside the
``js_portal`` adapter, whose heuristic output is the first vendor-import
content that is genuinely uncertain -- see ``js_portal.py``). This function
never sets ``status`` to ``"published"`` under any circumstance -- the only
status transition it can ever perform is published -> draft, never the
reverse; only the operator's own explicit publish action does that.
"""

from __future__ import annotations

from civiccast.agenda.models import AgendaItem, validate_source_doc_url
from civiccast.agenda.store import AgendaNotFoundError, AgendaStore
from civiccast.agenda_import.models import ExternalAgenda, ExternalAgendaItem

_TITLE_MAX = 400
_DOC_ANCHOR_MAX = 200


def import_external_agenda(
    store: AgendaStore, agenda_id: str, external: ExternalAgenda
) -> list[AgendaItem]:
    """Write ``external`` into ``agenda_id``. Returns only the items written.

    Raises :class:`AgendaNotFoundError` if the agenda does not exist, or
    ``ValueError`` (propagated from ``validate_source_doc_url``) if any
    ``doc_url`` in the payload uses a disallowed scheme -- the caller (the
    router) maps both to an HTTP error; nothing is written in either case.
    """
    agenda = store.get_agenda(agenda_id)
    if agenda is None:
        raise AgendaNotFoundError(f"Meeting agenda {agenda_id!r} not found.")

    existing_orders = {item.order for item in store.list_items(agenda_id, order_by="order")}

    # Validate everything up front (including the agenda-level
    # source_doc_url) before writing anything, so a single hostile URL
    # anywhere in the payload rejects the whole call instead of leaving a
    # partially-imported agenda.
    prepared: list[tuple[ExternalAgendaItem, str | None]] = []
    for ext_item in external.items:
        if ext_item.order in existing_orders:
            continue  # idempotent skip -- operator edits survive a re-import
        doc_anchor = _drop_if_too_long(validate_source_doc_url(ext_item.doc_url), _DOC_ANCHOR_MAX)
        prepared.append((ext_item, doc_anchor))
    agenda_doc_url = validate_source_doc_url(external.source_doc_url)

    written: list[AgendaItem] = []
    for ext_item, doc_anchor in prepared:
        item = AgendaItem(
            item_id=f"{agenda_id}-ext-{ext_item.order}",
            agenda_id=agenda_id,
            order=ext_item.order,
            number=ext_item.number,
            title=_clip(ext_item.title, _TITLE_MAX) or ext_item.title,
            doc_anchor=doc_anchor,
            # None for Legistar/PrimeGov/CivicClerk (structural parses, never
            # uncertain -- see ExternalAgendaItem.confidence's docstring);
            # js_portal's heuristic text classification is the first source to
            # set this on the vendor-import path. Passed straight through --
            # this module does no scoring of its own.
            confidence=ext_item.confidence,
        )
        written.append(store.upsert_item(item))

    # Best-effort: only fills the agenda's source_doc_url if the operator
    # hasn't already set one -- an import must never clobber an operator edit.
    if agenda.source_doc_url is None and agenda_doc_url:
        store.upsert_agenda(agenda.model_copy(update={"source_doc_url": agenda_doc_url}))

    # AI/agenda non-negotiables Spec §4.2 -- "operator approves before
    # publish... auto-publish is not an available operator setting." A new
    # agenda already starts as a draft (MeetingAgendaInput), so this branch
    # only matters for the case §4.2 actually targets: importing external
    # content INTO an agenda that is already public. Mirrors
    # civiccast.agenda.service.AgendaService.import_from_doc's exact reopen
    # behavior for the same reason -- newly-written items from a
    # third-party vendor (or, for js_portal, a heuristic best-effort guess
    # with a per-item confidence score) have not been operator-reviewed yet,
    # so a published agenda must not silently gain unreviewed public content.
    # Scoped to "something was actually written" -- a no-op re-import (every
    # order already taken, `written == []`) must not flip a published
    # agenda back to draft for zero new content.
    if written and agenda.status == "published":
        store.set_status(agenda_id, "draft")

    return written


def _clip(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value[:limit]


def _drop_if_too_long(value: str | None, limit: int) -> str | None:
    """Unlike ``_clip``, drop the value entirely rather than truncate it --
    a truncated URL is never a valid URL, so a vendor link that exceeds the
    storage limit becomes "no link" (honest) instead of a silently dead one.
    """
    if value is None or len(value) > limit:
        return None
    return value


__all__ = ["import_external_agenda"]
