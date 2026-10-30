# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S25 meeting-agenda integration.

Net-new module that turns the meeting-asset surface into a chaptered, agenda-
synced viewing experience. A government-access viewer watching a 4-hour council
meeting can jump to the agenda item they care about by clicking it in the
agenda list beside the player; the player seeks to the item's
``video_timecode_s``. Optional source agenda PDF renders beside the player.

Two durable tables (migration ``0058_meeting_agenda``):

* ``meeting_agendas`` — one row per (station, meeting asset) agenda. Carries
  the optional ``source_doc_url`` (uploaded agenda PDF) and the
  ``draft|published`` status. Draft agendas never surface on the public
  endpoint (DC-6).
* ``agenda_items`` — ordered items under an agenda. Each carries an optional
  ``video_timecode_s`` (the seek point), an optional ``number`` (operator-
  facing label like ``3.a`` / ``VII``), an optional ``doc_anchor`` (a page or
  anchor in the source PDF), and ``notes``.

Agenda items DOUBLE as the meeting's **chapters** at read time: when a
published agenda exists for an asset, the player's chapter list IS the agenda
item list (a single source of truth — no divergent chapter pile, per S25 §6).
Operator-supplied chapters on the asset (``Asset.chapters_json``) remain the
seed source for ``sync_from_chapters`` (slice 2 service); they are not the
runtime authority once an agenda is published.

The package follows the eas / metadata / reporting / underwriting layout —
append-only-where it matters, fail-closed on missing storage (503 over silent
200), bound-param SQL, role-gated at the API surface (``records_clerk`` /
``meeting_operator`` author; the public endpoint reads only published agendas).
"""
