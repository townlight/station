# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Producer, volunteer, and equipment operations (item 23).

Six durable tables (migration ``0063_producer_ops``), completing the four
pieces already shipped in :mod:`civiccast.contribute` (producer accounts,
show submission, rights/release metadata, approval queue):

* ``series_applications`` — a producer's request for a recurring series
  slot, distinct from the one-off ``ContributorSubmission`` show intake
  already covered by :mod:`civiccast.contribute`. Reviewed by staff with
  the same accept/decline shape.
* ``volunteer_roles`` — the volunteer roster: one row per person with a
  named role (camera, audio, floor director, ...) and an active flag.
* ``call_sheets`` + ``call_sheet_assignments`` — a call sheet is a shoot's
  crew/schedule plan; assignments link a call sheet to a volunteer and a
  role for that shoot.
* ``equipment_items`` + ``equipment_checkouts`` — the equipment roster and
  its checkout/return ledger (one open checkout per item at a time).
* ``training_badges`` — a badge earned by a volunteer (e.g. "camera-1",
  "live-switcher"), with an optional expiry.
* ``equipment_access_rules`` — the rule that says "checking out this
  equipment item requires this training badge." Enforced at checkout
  time; an item with no rule is uncontrolled (any active volunteer may
  check it out).

``station_id`` / ``series_id`` are loose string columns (no SQLAlchemy
``relationship``), matching the eas / ai_models / metadata / reporting /
underwriting / agenda / paywall convention.
"""
