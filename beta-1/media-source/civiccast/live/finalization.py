# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Recording-finalization handler -- idempotent asset creation at state ``recorded``.

Sprint 0.4 Slice 1 Commit 7. Implements the finalization contract the
v0.4 scope-lock (`docs/releases/v0.4-scope-lock.md` section 1 line
129 -- "Recording finalization contract: the event payload emitted
at on-air -> ending -> recorded, the asset-row insert it triggers,
the idempotency guarantee") and the design note
(`docs/research/v04-slice1-broadcast-spine-design.md` "Finalization
Event Design") call for.

The finalizer composes three writes inside a single transaction:

1. Insert a typed ``live_session_events`` row with
   ``event_type='session.finalized'`` and ``event_seq=1``. The
   composite primary key ``(live_session_id, event_type, event_seq)``
   is the idempotency gate -- a duplicate finalize collides on the PK
   and the transaction rolls back, so the caller's retry returns the
   pre-existing asset rather than creating a second one.

2. Insert an ``assets`` row at state ``recorded`` with
   ``source_live_session_id`` set to the live session's id. The
   partial unique index ``assets_source_live_session_unique`` enforces
   "at most one asset per source live session" even if an application-
   layer caller bypasses the event-row uniqueness guard. The asset's
   ``asset_id`` is set to ``live_session_id`` (Slice 1 simplification --
   operators using ``live_session_id`` slugs distinct from upload
   ``asset_id`` slugs avoid the natural-PK collision; a separate
   asset-id override parameter can land in a later commit if operator
   workflows demand it).

3. UPDATE ``live_sessions`` SET state='recorded' WHERE
   ``live_session_id=? AND state='ending'``. The conditional WHERE
   serializes concurrent finalizers and protects against state drift
   between SELECT and UPDATE; if the UPDATE matches 0 rows, the
   transaction rolls back and the finalizer raises
   :class:`civiccast.live.store.LiveSessionStateError`.

A finalize call against an ``idle`` / ``preflight`` / ``on_air``
session raises ``LiveSessionStateError`` before any write happens. A
finalize call against a missing session raises
:class:`civiccast.live.store.LiveSessionNotFoundError`. A finalize
call against an ``ending`` session that races a concurrent finalizer
returns the existing asset + event with ``idempotent=True`` (the
loser's event INSERT collides on PK; the loser's transaction rolls
back; the loser re-queries and returns the winner's rows).

Slice 1 Commit 7 does NOT include:

- The staff API endpoint that triggers finalization. Recording
  finalization is an internal handler -- operators end a broadcast
  via ``POST /api/staff/live/sessions/{id}/end-broadcast``, and a
  subsequent agent or worker invokes ``finalize_recording`` once the
  recording file has settled on disk. A direct staff-facing
  ``POST /finalize`` route is out of scope for this commit per the
  audit directive and the scope-lock's "no operator UI yet" Slice 1
  posture.
- The ``session.started`` and ``session.ended`` event rows. The
  schema (and the DB CHECK on ``event_type``) supports them; a later
  commit can emit them at the matching state transitions.

Load-bearing-contract note (audit-team v0.4 Slice 1 ENG-001
hardening pass, 2026-05-11):

The idempotency gate is the composite primary key
``(live_session_id, event_type, event_seq)`` on
``live_session_events`` plus the **explicit** ``session.flush()`` at
the body of :meth:`LiveRecordingFinalizer.finalize_recording`. The
flush forces SQLAlchemy to issue the INSERT to the database
synchronously so a duplicate-finalize PK collision surfaces as
``IntegrityError`` inside this method's try/except, where
:func:`_handle_integrity_error` distinguishes "duplicate finalize"
(return idempotent result) from "unrelated asset_id collision"
(raise :class:`LiveRecordingAssetCollisionError`).

Do not refactor this transaction to remove the explicit flush, swap
to ``Session(autoflush=False)``, switch to async session without
re-proving the IntegrityError-at-flush-time contract, or wrap the
finalizer in a retry decorator that masks ``IntegrityError`` -- any
of those changes can silently turn idempotent replay into a double
finalize that lands two ``assets`` rows for one meeting (a public-
record incident under the three-tier publish contract). See ADR
0011 ``docs/adr/0011-recording-finalization-transactional-event.md``
for the full rationale; the audit's prescribed Postgres-only
``INSERT ... ON CONFLICT DO NOTHING RETURNING`` alternative remains
a tracked improvement for a future rung but is not required today
because the explicit-flush contract already pins the gate.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.live.models import (
    LIVE_SESSION_EVENT_FINALIZED,
    LIVE_SESSION_STATE_ENDING,
    LIVE_SESSION_STATE_RECORDED,
    LiveSession,
    LiveSessionEvent,
    LiveSessionEventResponse,
)
from civiccast.live.recording_paths import local_recording_path
from civiccast.live.store import (
    LiveSessionNotFoundError,
    LiveSessionStateError,
)
from civiccast.schedule.models import (
    ASSET_STATE_RECORDED,
    Asset,
    AssetStateValue,
    RetentionPolicyValue,
    StaffAssetRow,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class LiveRecordingAssetCollisionError(Exception):
    """Raised when finalization can't create the asset because an asset
    with the same id already exists for a non-live source (e.g., an
    operator-uploaded asset with the same slug).

    Distinct from :class:`LiveSessionNotFoundError` and
    :class:`LiveSessionStateError` so the router layer (when a future
    commit exposes the finalization endpoint) can map this to a
    specific 409 detail body that names the conflicting upload path.

    Carries the conflicting ``asset_id`` so the operator surface can
    surface "asset 'X' already exists" without re-querying.
    """

    def __init__(self, live_session_id: str, asset_id: str) -> None:
        self.live_session_id = live_session_id
        self.asset_id = asset_id
        super().__init__(
            f"Cannot finalize LiveSession {live_session_id!r}: an asset "
            f"with id {asset_id!r} already exists outside the live-recording "
            f"path. Rename the upload or use a distinct live_session_id slug."
        )


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


class FinalizationResult(BaseModel):
    """Typed return shape for ``LiveRecordingFinalizer.finalize_recording``.

    Carries every artifact the caller needs to surface a successful
    finalization without a second round-trip:

    * ``asset`` -- the operator-side ``StaffAssetRow`` for the recorded
      asset. ``state == 'recorded'``; ``source_live_session_id`` is
      always set to the originating ``live_session_id``.
    * ``event`` -- the ``live_session_events`` row that locked
      idempotency.
    * ``idempotent`` -- ``True`` when this call returned a pre-existing
      finalization (duplicate finalize), ``False`` when this call did
      the write. Callers logging the finalization should branch on
      this flag to avoid double-counting completions.
    """

    model_config = ConfigDict(extra="forbid")

    asset: StaffAssetRow
    event: LiveSessionEventResponse
    idempotent: bool = Field(
        default=False,
        description=(
            "True if this call returned a pre-existing finalization "
            "(duplicate finalize); False if this call performed the write."
        ),
    )


# ---------------------------------------------------------------------------
# Finalizer
# ---------------------------------------------------------------------------


class LiveRecordingFinalizer:
    """Composes the three-write finalization transaction.

    Constructor takes the same session-factory shape as
    :class:`civiccast.live.store.LiveSessionStore` so a caller can
    share an engine binding across stores. ``finalize_recording``
    opens one session per call and commits or rolls back the entire
    transaction; the caller never sees a leaked SA session.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def finalize_recording(
        self,
        live_session_id: str,
        *,
        recording_uri: str,
        duration_seconds: int | None = None,
        trim_in_seconds: float | None = None,
        trim_out_seconds: float | None = None,
        finalized_at: datetime | None = None,
    ) -> FinalizationResult:
        """Finalize a recording for ``live_session_id``.

        Parameters:

        * ``live_session_id`` -- the session to finalize. Must exist
          and be in state ``ending`` (or ``recorded`` if a duplicate
          finalize is being retried).
        * ``recording_uri`` -- where the recorded file landed. The original
          URI is preserved in the finalization event. Local file URIs are
          normalized to filesystem paths in ``assets.file_path`` (D3) so
          media integrity, packaging, and relink workflows can open them --
          a raw ``file://`` string fails every downstream ``Path(...)
          .is_file()`` check even though the file is right there.
        * ``duration_seconds`` -- optional duration of the recording in
          whole seconds; persisted to ``assets.duration_seconds`` so
          the operator library can render the length without re-probing
          the file. Slice 1 keeps this an integer column; Slice 4 will
          widen trim precision (separate concern).
        * ``finalized_at`` -- optional caller-supplied timestamp; if
          omitted, ``datetime.now(UTC)`` is used. Recorded in the
          event payload for audit purposes.

        Returns a :class:`FinalizationResult` carrying the asset row,
        the event row, and an ``idempotent`` flag.

        Raises:

        * :class:`civiccast.live.store.LiveSessionNotFoundError` when
          ``live_session_id`` does not exist.
        * :class:`civiccast.live.store.LiveSessionStateError` when the
          session exists but is not in ``ending`` (and not already in
          ``recorded`` via a prior successful finalize).
        * :class:`LiveRecordingAssetCollisionError` when an asset row
          with ``asset_id == live_session_id`` already exists and does
          not belong to this live session (i.e., not a duplicate
          finalize but a name collision with an unrelated upload).
        """
        finalized_at_resolved = finalized_at or datetime.now(UTC)
        _validate_trim_window(
            trim_in_seconds=trim_in_seconds,
            trim_out_seconds=trim_out_seconds,
            duration_seconds=duration_seconds,
        )
        payload = json.dumps(
            {
                "recording_uri": recording_uri,
                "duration_seconds": duration_seconds,
                "trim_in_seconds": trim_in_seconds,
                "trim_out_seconds": trim_out_seconds,
                "finalized_at": finalized_at_resolved.isoformat(),
            },
            sort_keys=True,
        )

        with self._session_factory() as session:
            live_session_row = session.execute(
                select(LiveSession).where(LiveSession.live_session_id == live_session_id)
            ).scalar_one_or_none()
            if live_session_row is None:
                raise LiveSessionNotFoundError(live_session_id)

            # Duplicate-finalize fast path: if the session has already
            # advanced to ``recorded``, return the existing rows with
            # ``idempotent=True``. The event row must exist (a session
            # can only reach ``recorded`` through this handler in v0.4)
            # but we tolerate its absence by raising the same state
            # error a fresh non-ending call would get -- belt-and-
            # suspenders against future code paths that bypass the
            # event row.
            if live_session_row.state == LIVE_SESSION_STATE_RECORDED:
                return _build_idempotent_result(session, live_session_id)

            if live_session_row.state != LIVE_SESSION_STATE_ENDING:
                raise LiveSessionStateError(
                    live_session_id=live_session_id,
                    current_state=live_session_row.state,
                    attempted_transition="finalize_recording",
                )

            # State is ``ending``; attempt the three writes.
            #
            # LOAD-BEARING CONTRACT (audit-team v0.4 ENG-001): the
            # explicit ``session.flush()`` below is the idempotency
            # gate, not a performance hint. It forces SA to issue the
            # INSERT synchronously so the composite-PK collision on a
            # duplicate finalize surfaces as IntegrityError inside this
            # try/except -- where ``_handle_integrity_error`` returns
            # the idempotent result. Removing the explicit flush, or
            # switching this code to ``Session(autoflush=False)`` /
            # async session / retry-on-IntegrityError, can silently
            # turn idempotent replay into a double-finalize. See the
            # module docstring's load-bearing-contract note and ADR
            # 0011 before changing this block.
            event_row = LiveSessionEvent(
                live_session_id=live_session_id,
                event_type=LIVE_SESSION_EVENT_FINALIZED,
                event_seq=1,
                payload_json=payload,
            )
            local_path = local_recording_path(recording_uri)
            asset_row = Asset(
                asset_id=live_session_id,
                title=live_session_row.title,
                state=ASSET_STATE_RECORDED,
                source_live_session_id=live_session_id,
                file_path=str(local_path) if local_path is not None else None,
                manifest_url=None,
                duration_seconds=duration_seconds,
                trim_in_seconds=trim_in_seconds,
                trim_out_seconds=trim_out_seconds,
            )
            session.add(event_row)
            session.add(asset_row)

            try:
                session.flush()
            except IntegrityError as exc:
                session.rollback()
                return _handle_integrity_error(
                    session=session,
                    live_session_id=live_session_id,
                    exc=exc,
                )

            # Inserts succeeded. Conditional state advance protects
            # against state drift inside the transaction (race with
            # another transaction that already finalized would have
            # already surfaced via IntegrityError above; the conditional
            # UPDATE is defense in depth).
            update_result = session.execute(
                update(LiveSession)
                .where(LiveSession.live_session_id == live_session_id)
                .where(LiveSession.state == LIVE_SESSION_STATE_ENDING)
                .values(state=LIVE_SESSION_STATE_RECORDED)
            )
            matched: int = update_result.rowcount  # type: ignore[attr-defined]
            if matched != 1:
                # State drifted out from under us. Roll back the inserts
                # and re-read for a clean error.
                session.rollback()
                current_row = session.execute(
                    select(LiveSession).where(LiveSession.live_session_id == live_session_id)
                ).scalar_one_or_none()
                if current_row is None:
                    # Live session vanished mid-transaction; surface as
                    # not-found rather than state-error so the caller
                    # sees the actual condition.
                    raise LiveSessionNotFoundError(live_session_id)
                raise LiveSessionStateError(
                    live_session_id=live_session_id,
                    current_state=current_row.state,
                    attempted_transition="finalize_recording",
                )

            session.commit()
            session.refresh(event_row)
            session.refresh(asset_row)
            return FinalizationResult(
                asset=_asset_to_staff_row(asset_row),
                event=LiveSessionEventResponse.model_validate(event_row),
                idempotent=False,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_trim_window(
    *,
    trim_in_seconds: float | None,
    trim_out_seconds: float | None,
    duration_seconds: int | None,
) -> None:
    if trim_in_seconds is not None and trim_in_seconds < 0:
        raise ValueError("trim_in_seconds must be greater than or equal to 0.")
    if trim_out_seconds is not None and trim_out_seconds <= 0:
        raise ValueError("trim_out_seconds must be greater than 0.")
    if (
        trim_in_seconds is not None
        and trim_out_seconds is not None
        and trim_in_seconds >= trim_out_seconds
    ):
        raise ValueError("trim_in_seconds must be strictly less than trim_out_seconds.")
    if (
        trim_out_seconds is not None
        and duration_seconds is not None
        and trim_out_seconds > duration_seconds
    ):
        raise ValueError("trim_out_seconds cannot exceed duration_seconds.")


def _handle_integrity_error(
    *,
    session: Session,
    live_session_id: str,
    exc: IntegrityError,
) -> FinalizationResult:
    """Distinguish "duplicate finalize" from "asset_id collision" after
    the inserts collided.

    Re-queries the event table: if an ``session.finalized`` row exists
    for ``live_session_id``, the integrity error was the duplicate-
    finalize idempotency gate -- return the existing rows. If no event
    row exists, the integrity error was an unrelated asset PK collision
    (an uploaded asset with the same slug already occupies the row),
    surface as :class:`LiveRecordingAssetCollisionError`.
    """
    event_check = session.execute(
        select(LiveSessionEvent)
        .where(LiveSessionEvent.live_session_id == live_session_id)
        .where(LiveSessionEvent.event_type == LIVE_SESSION_EVENT_FINALIZED)
    ).scalar_one_or_none()

    if event_check is not None:
        return _build_idempotent_result(session, live_session_id)

    raise LiveRecordingAssetCollisionError(
        live_session_id=live_session_id,
        asset_id=live_session_id,
    ) from exc


def _build_idempotent_result(
    session: Session,
    live_session_id: str,
) -> FinalizationResult:
    """Return a ``FinalizationResult(idempotent=True)`` built from the
    pre-existing event + asset rows.

    Used by both the duplicate-finalize fast path (state is already
    ``recorded`` at SELECT time) and the IntegrityError recovery path
    (concurrent finalizer beat us; event row exists, our rollback
    leaves nothing of our own).
    """
    event_row = session.execute(
        select(LiveSessionEvent)
        .where(LiveSessionEvent.live_session_id == live_session_id)
        .where(LiveSessionEvent.event_type == LIVE_SESSION_EVENT_FINALIZED)
        .where(LiveSessionEvent.event_seq == 1)
    ).scalar_one()
    asset_row = session.execute(
        select(Asset).where(Asset.source_live_session_id == live_session_id)
    ).scalar_one()
    return FinalizationResult(
        asset=_asset_to_staff_row(asset_row),
        event=LiveSessionEventResponse.model_validate(event_row),
        idempotent=True,
    )


def _asset_to_staff_row(asset: Asset) -> StaffAssetRow:
    """Render an :class:`Asset` SA row as a :class:`StaffAssetRow`.

    Mirrors the projection the schedule store applies for the operator
    asset library so the finalization caller sees an asset shape
    identical to what ``GET /api/staff/assets/{id}`` would return.
    Chapters are intentionally always empty for a freshly finalized
    recording -- the chapter editor lands on top of the asset later.
    """
    chapters: list[Any] = []
    return StaffAssetRow(
        asset_id=asset.asset_id,
        title=asset.title,
        description=asset.description,
        state=cast(AssetStateValue, asset.state),
        manifest_url=asset.manifest_url,
        published_at=asset.published_at,
        file_path=asset.file_path,
        file_size_bytes=asset.file_size_bytes,
        duration_seconds=asset.duration_seconds,
        codec_video=asset.codec_video,
        codec_audio=asset.codec_audio,
        width_px=asset.width_px,
        height_px=asset.height_px,
        bitrate_bps=asset.bitrate_bps,
        format_name=asset.format_name,
        trim_in_seconds=asset.trim_in_seconds,
        trim_out_seconds=asset.trim_out_seconds,
        chapters=chapters,
        retention_policy=cast(RetentionPolicyValue, asset.retention_policy),
        retention_until=asset.retention_until,
        version=asset.version,
        source_live_session_id=asset.source_live_session_id,
    )
