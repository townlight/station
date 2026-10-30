# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S24 underwriting / sponsorship-spot management.

Net-new module that turns underwriting acknowledgments (47 CFR 73.503 sponsor-ID
spots — name/logo/location/value-neutral; NO calls-to-action / price /
qualitative claims) into first-class scheduled assets with trafficking,
break-insertion, and per-underwriter proof-of-airing affidavits.

Three durable tables (migration ``0057_underwriting_spots``):

* ``underwriting_spots`` — one row per spot (underwriter + the :15/:30
  acknowledgment asset + the operator's editorial FCC-compliance attestation).
* ``spot_flights`` — flight window (start/end dates) + frequency cap + optional
  daypart (an S19 ``ScheduleBlock`` id) + channel scope. A flight binds a spot
  to "when and how often" it airs.
* ``spot_placements`` — what the trafficking compiler actually placed: a
  materialized program-log break/interstitial slot (``schedule_item_id``,
  ``scheduled_at``).

Per-underwriter affidavits are NOT a separate table — they are a report
view over S23's ``as_run_log`` filtered to ``source_kind="spot"`` and joined
back through ``spot_placements → spot_flights → underwriting_spots`` to
attribute each aired second to its underwriter (see ``service.py`` slice 3 —
the ``# slice <n>:`` headers in that file delineate the trafficking compiler
(slice 2) from the affidavit report (slice 3); slice 4 wires the policy /
break-slot integration with S4).

The package follows the eas / metadata / reporting layout — append-only-where
it matters, fail-closed on missing storage (503 over silent 200), bound-param
SQL everywhere, role-gated at the API surface (``publish_operator`` /
``setup_admin`` manage; ``support_admin`` reads affidavits).
"""
