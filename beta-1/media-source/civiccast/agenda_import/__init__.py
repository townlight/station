# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""4.1.0 "Agenda Bridge" — read-only agenda importers (Legistar/PrimeGov/CivicClerk).

All three vendors ship as of this release (Legistar Phase 1, PrimeGov
Phase 2, CivicClerk Phase 3 -- pure reuse of Phase 2's docparse.py). The
package is named ``agenda_import`` (not ``civicclerk_bridge`` or any
"clerk"/"bridge" synonym) deliberately: ``civiccast/civicclerk_bridge/`` is a
separate, already-speced, separate-repo CivicSuite event-bus integration and
is NOT touched, renamed, or repurposed by this package. See the "Agenda
Bridge" sprint plan (``civiccast-agenda-bridge-plan-v0.3.md``) §1 for the
full correction -- not tracked in this repo's ``docs/spec/`` tree.

Writes land through :func:`civiccast.agenda_import.mapper.import_external_agenda`
— the sole writer into the existing :mod:`civiccast.agenda` domain — via
``AgendaStore.upsert_item``'s idempotent ``(agenda_id, order)`` skip rule, the
same idempotency mechanism :meth:`civiccast.agenda.service.AgendaService.
sync_from_chapters` and ``import_from_doc`` already use.
"""

from __future__ import annotations
