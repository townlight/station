# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""0.4.0 migration/import core — bringing a station's history in from another system.

A station switching to CivicCast from an incumbent PEG/broadcast-automation
system (Cablecast, TelVue, Castus, Leightronix, ...) must not lose its show
library, schedule history, or start over. This module is the source-agnostic
core that makes that possible:

* :mod:`civiccast.migrate.models` — a normalized, source-agnostic import
  model (:class:`NormalizedInventory`) any incumbent adapter maps its export
  into, plus the batch/provenance ledger (``import_batches`` /
  ``import_batch_items``, migration ``0067``).
* :mod:`civiccast.migrate.adapters` — the :class:`SourceAdapter` Protocol and
  the first concrete adapter, :class:`CablecastAdapter` (Tightrope Media
  Systems' public ``cablecastapi/v1`` REST API).
* :mod:`civiccast.migrate.service` — :class:`MigrationService`: dry-run diff
  planning, apply (writes into the REAL ``civiccast.schedule`` stores —
  :class:`civiccast.schedule.models.Asset` / ``ScheduleItem`` — not a
  parallel database), and exact rollback by batch.
* :mod:`civiccast.migrate.store` — :class:`MigrationStore`: persistence for
  the batch/provenance ledger only (the show/schedule rows themselves live in
  the schedule module's own tables).
* :mod:`civiccast.migrate.router` — ``setup_admin``-gated staff API
  (``/api/staff/migrate/dry-run`` / ``/apply`` / ``/rollback`` / ``/batches``).

Honest scope (0.4.0, per ``docs/ROADMAP.md``):

* **Cablecast only.** TelVue / Castus / Leightronix are next-lane work; the
  :class:`~civiccast.migrate.adapters.SourceAdapter` Protocol is the seam a
  future adapter implements — nothing here assumes Cablecast shapes leak
  past the adapter boundary.
* **Metadata + file references, not file copying.** An imported show's
  ``media_ref`` is a pointer into the source system (a reel/media
  reference, or the exact source-served file path when the source exposes
  one) — the media bytes are NOT copied. Physically moving potentially
  terabytes of recordings across systems is an operator/beta-stage
  operation, not something 0.4.0 does silently on an operator's behalf.
* **No operator-console UI.** This module ships the API surface only; a
  migration wizard screen is a follow-up.
* Real-Cablecast-server validation in this module's tests is limited to
  public, read-only requests against a handful of real station servers
  (see ``tests/migrate/test_adapters_cablecast.py``); a private/authenticated
  server (LPM's real Vio, for instance) is beta-stage validation.
"""

from __future__ import annotations
