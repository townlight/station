# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""As-run capture seam for the egress engine (S23 §6.1).

The playout engine emits an ``EgressProofEvent`` at every ACTUAL source
transition (after the encoder is live — the on-air instant, not scheduled
intent). S23 proof-of-performance needs a permanent, append-only record of what
ACTUALLY aired, distinct from what was scheduled, so the engine writes an
as-run entry at each transition.

This module defines the *engine-side seam* only:

* ``AsRunRecorder`` — the protocol the daemon calls (``record_transition`` at
  each ON_AIR/FALLBACK_SLATE proof-event site; ``close_open`` at terminal
  states). The concrete, store-backed implementation lives in
  ``civiccast.reporting`` so the engine never imports the reporting package
  (keeps the hot playout path import-clean and dependency-free).
* ``map_source_kind`` — maps the engine's source taxonomy
  (``EgressSourceKind`` = ``program``/``slate``/``live``/``cg``, plus the
  ``FALLBACK_SLATE`` running state) to the as-run ``source_kind``
  (``program``/``filler``/``live``/``slate``/``spot``). ``cg`` filler bulletins
  map to ``filler``; a forced/fallback slate maps to ``slate``.
* ``asset_id_for_segment`` — the asset id to record, guarded on kind: only a
  ``program`` segment carries a real library ``asset_id`` in ``source_ref``
  (slate is ``civiccast-slate``, filler is ``bulletin-<id>`` — neither is a
  library asset).

The recorder is **append-only and fail-safe**: a capture error must never break
playout (the daemon wraps every call in a ``try/except`` that only logs, exactly
like the existing proof-event / reap auditing seams).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from civiccast.egress.models import EgressSourcePlan

# The as-run source_kind values (mirrors civiccast.reporting.models.SourceKind;
# duplicated as a plain tuple here so the engine need not import the reporting
# package). The reporting recorder enriches scheduled programs to ``spot``
# only when their exact occurrence, channel and asset match a trafficked placement.
ASRUN_SOURCE_KINDS: tuple[str, ...] = ("program", "filler", "live", "slate", "spot")


class AsRunCaptureSchemaError(RuntimeError):
    """Raised by an :class:`AsRunRecorder` when an as-run write fails pydantic
    schema validation (e.g. ``channel_id`` violates the ledger ``Slug`` pattern).

    Distinct from transport / DB failures — the daemon catches this separately
    and surfaces a loud degraded-mode marker, so silent loss of the as-aired
    ledger (the exact failure mode S23 §6.1 was written to prevent) is impossible.
    Defined here (engine-side) so the daemon can ``except`` it without importing
    the reporting package; the concrete recorder in ``civiccast.reporting``
    re-exports the same class.
    """


@runtime_checkable
class AsRunRecorder(Protocol):
    """The engine-side as-run capture seam (implemented in civiccast.reporting).

    All methods are append-only side-effects on the durable as-run ledger; they
    must never raise into the playout path (the daemon guards every call).
    """

    def record_transition(
        self,
        *,
        channel_id: str,
        source_kind: str,
        asset_id: str | None,
        source_label: str,
        actual_start: datetime,
        proof_event_id: str,
        schedule_item_id: str | None = None,
    ) -> None:
        """Record that ``channel_id`` ACTUALLY went on air with this source at
        ``actual_start`` (the proof-event ``observed_at``). Closes the channel's
        previously-open as-run row (its ``actual_end`` = this ``actual_start``)
        and opens a new one. ``verified=True`` — backed by the proof event."""
        ...

    def close_open(self, *, channel_id: str, actual_end: datetime) -> None:
        """Close the channel's currently-open as-run row at ``actual_end``
        (clean stop / error terminal / drain — the channel left air with no new
        source taking over). A no-op when no row is open."""
        ...


def map_source_kind(*, segment_kind: str, running_state: str) -> str:
    """Map the engine source taxonomy → the as-run ``source_kind``.

    * ``running_state == "FALLBACK_SLATE"`` → ``slate`` (a forced/fallback slate,
      regardless of the segment's declared kind).
    * ``cg`` (community-bulletin filler boards) → ``filler``.
    * ``program`` / ``live`` / ``slate`` pass through 1:1.

    Anything unexpected falls back to ``filler`` (a safe, non-program bucket) so
    a never-before-seen kind can never produce an invalid enum value that the DB
    CHECK constraint would reject on the playout path.
    """
    if running_state == "FALLBACK_SLATE":
        return "slate"
    if segment_kind == "cg":
        return "filler"
    if segment_kind in ("program", "live", "slate"):
        return segment_kind
    return "filler"


def asset_id_for_segment(*, source_plan: EgressSourcePlan, source_kind: str) -> str | None:
    """The library ``asset_id`` to record for this transition, or ``None``.

    Only a ``program`` segment's ``source_ref`` is a real library asset id
    (``source_plan.py`` sets ``source_ref=item.asset_id``). Slate
    (``civiccast-slate``) and filler (``bulletin-<id>``) ``source_ref`` values
    are NOT library assets, so they are recorded with ``asset_id=None`` (the
    hours-by-category join keys on library assets; filler/slate are correctly
    uncategorized).
    """
    if source_kind != "program":
        return None
    return source_plan.segments[0].source_ref


__all__ = [
    "ASRUN_SOURCE_KINDS",
    "AsRunCaptureSchemaError",
    "AsRunRecorder",
    "asset_id_for_segment",
    "map_source_kind",
]
