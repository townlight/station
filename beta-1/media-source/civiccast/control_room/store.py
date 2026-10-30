# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable persistence for the S16 production control room.

Per-request store over the single global session factory (same lazy posture as
the cg / schedule stores). Soft string references, no hard FKs: audit rows
(cue events, device commands) outlive the entities they describe, and deleting
a device does not cascade into the surfaces/cues that named it.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.control_room.models import (
    ControlRoomSession,
    ControlRoomSessionDb,
    ControlSurface,
    ControlSurfaceDb,
    CueFiredEvent,
    CueFiredEventDb,
    DeviceCommand,
    DeviceCommandDb,
    DeviceProfile,
    DeviceProfileDb,
    ProductionDevice,
    ProductionDeviceDb,
    TimelineCue,
    TimelineCueDb,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


class ControlRoomStoreError(RuntimeError):
    """Base error for control-room persistence failures."""


class DeviceNotFoundError(ControlRoomStoreError):
    """Raised when a production device id does not resolve."""


class SurfaceNotFoundError(ControlRoomStoreError):
    """Raised when a control-surface id does not resolve."""


class CueNotFoundError(ControlRoomStoreError):
    """Raised when a timeline-cue id does not resolve."""


class CueImmutableError(ControlRoomStoreError):
    """Raised when a cue that has at least one ``fired`` audit event is edited
    or deleted. Once fired, a cue's definition is part of the audit trail."""


class SessionNotFoundError(ControlRoomStoreError):
    """Raised when a control-room-session id does not resolve."""


class SessionSurfaceConflictError(ControlRoomStoreError):
    """Raised when a session open loses the DB-level "one open session per
    surface" race (the unique constraint on ``control_room_sessions.surface_id``
    filtered to ``state = 'open'``). This is the clean, catchable error for the
    concurrent-open-session TOCTOU race the app-level existing-session check
    cannot fully close on its own."""

    def __init__(self, surface_id: str) -> None:
        super().__init__(f"surface {surface_id} already has an open session")
        self.surface_id = surface_id


def _now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _device_to_model(row: ProductionDeviceDb) -> ProductionDevice:
    return ProductionDevice(
        device_id=row.device_id,
        label=row.label,
        kind=row.kind,  # type: ignore[arg-type]
        transport=row.transport,  # type: ignore[arg-type]
        host=row.host,
        port=row.port,
        enabled=row.enabled,
        notes=row.notes,
        secret_ref=row.secret_ref,
        last_probed_at=_aware_utc(row.last_probed_at),
        last_reachable=row.last_reachable,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _profile_to_model(row: DeviceProfileDb) -> DeviceProfile:
    return DeviceProfile(
        profile_id=row.profile_id,
        device_id=row.device_id,
        tsr_device_type=row.tsr_device_type,
        options=dict(row.options or {}),
        capability_map=dict(row.capability_map or {}),
        take_delay_ms=row.take_delay_ms,
        post_roll_ms=row.post_roll_ms,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _surface_to_model(row: ControlSurfaceDb) -> ControlSurface:
    return ControlSurface(
        surface_id=row.surface_id,
        label=row.label,
        assigned_role=row.assigned_role,  # type: ignore[arg-type]
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _cue_to_model(row: TimelineCueDb) -> TimelineCue:
    return TimelineCue(
        cue_id=row.cue_id,
        surface_id=row.surface_id,
        label=row.label,
        device_id=row.device_id,
        action=row.action,  # type: ignore[arg-type]
        payload=dict(row.payload or {}),
        confirm_required=row.confirm_required,
        bank=row.bank,
        position=row.position,
        version=row.version,
        proof_boundary=row.proof_boundary,
        created_at=row.created_at,
    )


def _session_to_model(row: ControlRoomSessionDb) -> ControlRoomSession:
    return ControlRoomSession(
        session_id=row.session_id,
        surface_id=row.surface_id,
        operator_id=row.operator_id,
        operator_name=row.operator_name,
        program_feed_source_ref=row.program_feed_source_ref,
        mode=row.mode,  # type: ignore[arg-type]
        safe_state_cue_id=row.safe_state_cue_id,
        state=row.state,  # type: ignore[arg-type]
        started_at=_aware_utc(row.started_at) or row.started_at,
        on_air_expires_at=_aware_utc(row.on_air_expires_at),
        ended_at=_aware_utc(row.ended_at),
    )


def _cue_event_to_model(row: CueFiredEventDb) -> CueFiredEvent:
    return CueFiredEvent(
        event_id=row.event_id,
        session_id=row.session_id,
        cue_id=row.cue_id,
        operator_id=row.operator_id,
        device_id=row.device_id,
        action=row.action,  # type: ignore[arg-type]
        result=row.result,  # type: ignore[arg-type]
        fired_at=row.fired_at,
        detail=dict(row.detail or {}),
    )


def _device_command_to_model(row: DeviceCommandDb) -> DeviceCommand:
    return DeviceCommand(
        command_id=row.command_id,
        device_id=row.device_id,
        session_id=row.session_id,
        command_kind=row.command_kind,
        command_preview=row.command_preview,
        take_delay_ms=row.take_delay_ms,
        post_roll_ms=row.post_roll_ms,
        issued_by=row.issued_by,
        issued_at=row.issued_at,
        result=row.result,  # type: ignore[arg-type]
    )


class ControlRoomStore:
    """CRUD + audit appends for the production control room."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def _session(self) -> AbstractContextManager[Session]:
        return self._session_factory()

    # --- devices ---------------------------------------------------------

    def upsert_device(self, device: ProductionDevice) -> ProductionDevice:
        # Health/freshness (last_probed_at/last_reachable) is intentionally NOT
        # taken from `device` here: this method is the config write path
        # (register/edit), and a config edit must not silently reset or fake a
        # health reading. Only record_device_probe() writes those two columns.
        with self._session() as session:
            row = session.get(ProductionDeviceDb, device.device_id)
            if row is None:
                row = ProductionDeviceDb(device_id=device.device_id, created_at=device.created_at)
                session.add(row)
            row.label = device.label
            row.kind = device.kind
            row.transport = device.transport
            row.host = device.host
            row.port = device.port
            row.enabled = device.enabled
            row.notes = device.notes
            row.secret_ref = device.secret_ref
            row.updated_at = device.updated_at
            session.commit()
            return _device_to_model(row)

    def record_device_probe(
        self, device_id: str, *, reachable: bool, probed_at: datetime
    ) -> ProductionDevice:
        """Record the outcome of a probe/fire attempt as the device's current
        health + state-freshness reading (S16 item 7)."""
        with self._session() as session:
            row = session.get(ProductionDeviceDb, device_id)
            if row is None:
                raise DeviceNotFoundError(device_id)
            row.last_probed_at = probed_at
            row.last_reachable = reachable
            session.commit()
            return _device_to_model(row)

    def get_device(self, device_id: str) -> ProductionDevice | None:
        with self._session() as session:
            row = session.get(ProductionDeviceDb, device_id)
            return _device_to_model(row) if row is not None else None

    def list_devices(self) -> list[ProductionDevice]:
        with self._session() as session:
            rows = (
                session.execute(select(ProductionDeviceDb).order_by(ProductionDeviceDb.label))
                .scalars()
                .all()
            )
            return [_device_to_model(r) for r in rows]

    def delete_device(self, device_id: str) -> None:
        with self._session() as session:
            row = session.get(ProductionDeviceDb, device_id)
            if row is None:
                raise DeviceNotFoundError(device_id)
            session.delete(row)
            session.commit()

    # --- device profiles -------------------------------------------------

    def upsert_profile(self, profile: DeviceProfile) -> DeviceProfile:
        with self._session() as session:
            row = session.get(DeviceProfileDb, profile.profile_id)
            if row is None:
                row = DeviceProfileDb(profile_id=profile.profile_id, created_at=profile.created_at)
                session.add(row)
            row.device_id = profile.device_id
            row.tsr_device_type = profile.tsr_device_type
            row.options = dict(profile.options)
            row.capability_map = dict(profile.capability_map)
            row.take_delay_ms = profile.take_delay_ms
            row.post_roll_ms = profile.post_roll_ms
            row.version = profile.version
            row.updated_at = profile.updated_at
            session.commit()
            return _profile_to_model(row)

    def get_profile_for_device(self, device_id: str) -> DeviceProfile | None:
        with self._session() as session:
            row = (
                session.execute(
                    select(DeviceProfileDb)
                    .where(DeviceProfileDb.device_id == device_id)
                    .order_by(DeviceProfileDb.version.desc())
                )
                .scalars()
                .first()
            )
            return _profile_to_model(row) if row is not None else None

    # --- surfaces --------------------------------------------------------

    def upsert_surface(self, surface: ControlSurface) -> ControlSurface:
        with self._session() as session:
            row = session.get(ControlSurfaceDb, surface.surface_id)
            if row is None:
                row = ControlSurfaceDb(surface_id=surface.surface_id, created_at=surface.created_at)
                session.add(row)
            row.label = surface.label
            row.assigned_role = surface.assigned_role
            row.created_by = surface.created_by
            row.updated_at = surface.updated_at
            session.commit()
            return _surface_to_model(row)

    def get_surface(self, surface_id: str) -> ControlSurface | None:
        with self._session() as session:
            row = session.get(ControlSurfaceDb, surface_id)
            return _surface_to_model(row) if row is not None else None

    def list_surfaces(self) -> list[ControlSurface]:
        with self._session() as session:
            rows = (
                session.execute(select(ControlSurfaceDb).order_by(ControlSurfaceDb.label))
                .scalars()
                .all()
            )
            return [_surface_to_model(r) for r in rows]

    def delete_surface(self, surface_id: str) -> None:
        with self._session() as session:
            row = session.get(ControlSurfaceDb, surface_id)
            if row is None:
                raise SurfaceNotFoundError(surface_id)
            session.delete(row)
            session.commit()

    # --- cues ------------------------------------------------------------

    def upsert_cue(self, cue: TimelineCue) -> TimelineCue:
        with self._session() as session:
            row = session.get(TimelineCueDb, cue.cue_id)
            if row is None:
                row = TimelineCueDb(
                    cue_id=cue.cue_id, created_at=cue.created_at, version=cue.version
                )
                session.add(row)
            elif self._cue_has_fired(session, cue.cue_id):
                raise CueImmutableError(
                    f"cue {cue.cue_id} has already fired at least once and cannot be edited"
                )
            else:
                row.version = row.version + 1
            row.surface_id = cue.surface_id
            row.label = cue.label
            row.device_id = cue.device_id
            row.action = cue.action
            row.payload = dict(cue.payload)
            row.confirm_required = cue.confirm_required
            row.bank = cue.bank
            row.position = cue.position
            row.proof_boundary = cue.proof_boundary
            session.commit()
            return _cue_to_model(row)

    def get_cue(self, cue_id: str) -> TimelineCue | None:
        with self._session() as session:
            row = session.get(TimelineCueDb, cue_id)
            return _cue_to_model(row) if row is not None else None

    def list_cues_for_surface(self, surface_id: str) -> list[TimelineCue]:
        with self._session() as session:
            rows = (
                session.execute(
                    select(TimelineCueDb)
                    .where(TimelineCueDb.surface_id == surface_id)
                    .order_by(TimelineCueDb.bank, TimelineCueDb.position)
                )
                .scalars()
                .all()
            )
            return [_cue_to_model(r) for r in rows]

    @staticmethod
    def _cue_has_fired(session: Session, cue_id: str) -> bool:
        return (
            session.execute(
                select(CueFiredEventDb.event_id)
                .where(CueFiredEventDb.cue_id == cue_id, CueFiredEventDb.result == "fired")
                .limit(1)
            ).first()
            is not None
        )

    def has_fired_cue_event(self, cue_id: str) -> bool:
        """True once at least one ``fired`` audit event exists for this cue."""
        with self._session() as session:
            return self._cue_has_fired(session, cue_id)

    def delete_cue(self, cue_id: str) -> None:
        with self._session() as session:
            row = session.get(TimelineCueDb, cue_id)
            if row is None:
                raise CueNotFoundError(cue_id)
            if self._cue_has_fired(session, cue_id):
                raise CueImmutableError(
                    f"cue {cue_id} has already fired at least once and cannot be deleted"
                )
            session.delete(row)
            session.commit()

    # --- sessions --------------------------------------------------------

    def open_session(self, session_model: ControlRoomSession) -> ControlRoomSession:
        with self._session() as session:
            row = ControlRoomSessionDb(
                session_id=session_model.session_id,
                surface_id=session_model.surface_id,
                operator_id=session_model.operator_id,
                operator_name=session_model.operator_name,
                program_feed_source_ref=session_model.program_feed_source_ref,
                mode=session_model.mode,
                safe_state_cue_id=session_model.safe_state_cue_id,
                state=session_model.state,
                started_at=session_model.started_at,
                on_air_expires_at=session_model.on_air_expires_at,
                ended_at=session_model.ended_at,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                # The DB-level one-open-session-per-surface unique index lost
                # the race against a concurrent open (see
                # ix_control_room_sessions_one_open_per_surface in models.py):
                # roll back this half-committed insert and surface a clean,
                # catchable conflict instead of a raw IntegrityError.
                session.rollback()
                raise SessionSurfaceConflictError(session_model.surface_id) from exc
            return _session_to_model(row)

    def get_session(self, session_id: str) -> ControlRoomSession | None:
        with self._session() as session:
            row = session.get(ControlRoomSessionDb, session_id)
            return _session_to_model(row) if row is not None else None

    def get_open_session_for_surface(self, surface_id: str) -> ControlRoomSession | None:
        with self._session() as session:
            row = (
                session.execute(
                    select(ControlRoomSessionDb)
                    .where(
                        ControlRoomSessionDb.surface_id == surface_id,
                        ControlRoomSessionDb.state == "open",
                    )
                    .order_by(ControlRoomSessionDb.started_at.desc())
                )
                .scalars()
                .first()
            )
            return _session_to_model(row) if row is not None else None

    def close_session(
        self, session_id: str, *, ended_at: datetime | None = None
    ) -> ControlRoomSession:
        with self._session() as session:
            row = session.get(ControlRoomSessionDb, session_id)
            if row is None:
                raise SessionNotFoundError(session_id)
            row.state = "closed"
            row.ended_at = ended_at or _now()
            session.commit()
            return _session_to_model(row)

    # --- audit -----------------------------------------------------------

    def append_cue_event(self, event: CueFiredEvent) -> CueFiredEvent:
        with self._session() as session:
            row = CueFiredEventDb(
                event_id=event.event_id,
                session_id=event.session_id,
                cue_id=event.cue_id,
                operator_id=event.operator_id,
                device_id=event.device_id,
                action=event.action,
                result=event.result,
                fired_at=event.fired_at,
                detail=dict(event.detail),
            )
            session.add(row)
            session.commit()
            return _cue_event_to_model(row)

    def list_cue_events(self, session_id: str, *, limit: int = 200) -> list[CueFiredEvent]:
        with self._session() as session:
            rows = (
                session.execute(
                    select(CueFiredEventDb)
                    .where(CueFiredEventDb.session_id == session_id)
                    .order_by(CueFiredEventDb.fired_at.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [_cue_event_to_model(r) for r in rows]

    def append_device_command(self, command: DeviceCommand) -> DeviceCommand:
        with self._session() as session:
            row = DeviceCommandDb(
                command_id=command.command_id,
                device_id=command.device_id,
                session_id=command.session_id,
                command_kind=command.command_kind,
                command_preview=command.command_preview,
                take_delay_ms=command.take_delay_ms,
                post_roll_ms=command.post_roll_ms,
                issued_by=command.issued_by,
                issued_at=command.issued_at,
                result=command.result,
            )
            session.add(row)
            session.commit()
            return _device_command_to_model(row)

    def list_device_commands(self, device_id: str, *, limit: int = 200) -> list[DeviceCommand]:
        with self._session() as session:
            rows = (
                session.execute(
                    select(DeviceCommandDb)
                    .where(DeviceCommandDb.device_id == device_id)
                    .order_by(DeviceCommandDb.issued_at.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [_device_command_to_model(r) for r in rows]


__all__ = [
    "ControlRoomStore",
    "ControlRoomStoreError",
    "CueImmutableError",
    "CueNotFoundError",
    "DeviceNotFoundError",
    "SessionFactory",
    "SessionNotFoundError",
    "SessionSurfaceConflictError",
    "SurfaceNotFoundError",
]
