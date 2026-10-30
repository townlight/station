# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Merge revision — unifies the S21 + S26 chain heads.

This is a data-free Alembic merge revision. After S21 landed as a
sibling off ``0055_asrun_and_epg`` (declaring ``down_revision =
"0055_asrun_and_epg"``), the global alembic chain temporarily had TWO
heads:

* ``0056_scheduled_recording`` (S21, the sibling branch)
* ``0059_paywall_access`` (S26, the linear-chain head before the merge)

A two-headed chain breaks ``alembic upgrade head`` (it raises
``MultipleHeads``) AND ``tests/live/test_real_postgres.py`` which asserts
a single head. This revision is the merge that restores the
single-head invariant — its ``down_revision`` is the TUPLE of both
heads, so applying it stamps both as ancestors and leaves
``0060_recording_paywall_merge`` as the new sole head.

The merge ships ``upgrade()`` + ``downgrade()`` as no-ops because no
schema change accompanies the merge. The chain shape after this
revision applies is:

    0054 → 0055 ─┬→ 0056 ─────┐
                 │             ↓
                 └→ 0057 → 0058 → 0059 → 0060_recording_paywall_merge (HEAD)

This is the FINAL chain-shape for V1 S18 parity work. All S18 gaps
(scheduling, recording, custom-metadata, as-run/EPG, underwriting,
agenda, paywall) are now on disk.
"""

from __future__ import annotations

# Note the TUPLE in ``down_revision`` — that's what makes Alembic
# treat this as a merge rather than a regular linear revision.
revision = "0060_recording_paywall_merge"
down_revision = ("0056_scheduled_recording", "0059_paywall_access")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Data-free: the only purpose of this revision is to unify the heads.
    # No schema change accompanies the merge.
    pass


def downgrade() -> None:
    # Inverse of upgrade: also a no-op. Downgrading from the merge
    # restores the two-headed state, which is then continued by
    # downgrading either branch independently.
    pass
