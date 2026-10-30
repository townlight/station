# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S16 cue plan/fire service (build step 9 slice 2b).

The plan-then-fire discipline borrowed from facility/router_control.py: every
cue is *resolved and validated server-side* (:meth:`plan_cue`) before any device
socket carries a live command (:meth:`fire_cue`). Planning opens NO connection;
firing goes through the injected ``TsrClient`` (the Node TSR sidecar contract).
Every fire appends to the session's append-only audit; GPI / serial / router-take
actions (S18 gap-8) also append a timed DeviceCommand record.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from civiccast.control_room.lpm_lab import LabTopologyProfile, build_lpm_lab_profiles
from civiccast.control_room.models import (
    DEVICE_HEALTH_STALE_AFTER_SECONDS,
    ControlRoomLpmDeviceCoverage,
    ControlRoomLpmProfileCoverage,
    ControlRoomReadinessCheck,
    ControlRoomReadinessReport,
    ControlRoomSession,
    CueFiredEvent,
    CuePlan,
    DeviceCommand,
    DeviceProfile,
    ProductionDevice,
    SessionMode,
    TimelineCue,
)
from civiccast.control_room.policy import (
    ControlRoomPolicyError,
    MaterialStateChangedError,
    UnsafeDeviceTargetError,
    assert_device_target_allowed,
    assert_material_state_matches,
    material_state_fingerprint,
    validate_cue_for_device,
)
from civiccast.control_room.store import (
    ControlRoomStore,
    ControlRoomStoreError,
    CueNotFoundError,
    DeviceNotFoundError,
    SessionNotFoundError,
    SessionSurfaceConflictError,
    SurfaceNotFoundError,
)
from civiccast.control_room.tsr_client import (
    NullTsrClient,
    TsrClient,
    TsrClientError,
    TsrProbeResult,
)

_PLAN_BOUNDARY = "Cue plan preview only; no device socket is opened by this API."
_READINESS_BOUNDARY = (
    "Readiness is computed from CivicCast configuration, policy checks, and Stage 0-1 "
    "LPM contract-lab profiles. It is not clean Windows install evidence, simulator evidence, "
    "real OBS/vMix/ATEM/NDI evidence, or station-device evidence."
)
# Actions that drive facility hardware (S18 gap-8) and get a timed DeviceCommand
# audit row in addition to the cue-fired event. OSC and HTTP cue types are
# intentionally excluded — they are facility-network-only sidecar paths that do
# not produce DeviceCommand rows (no gap-8 timing contract for those paths).
# Must match the CueAction values that produce DeviceCommand rows (see models.py DeviceCommand).
_DEVICE_COMMAND_ACTIONS = frozenset({"gpi_pulse", "serial_send", "router_take"})


class ControlRoomServiceError(ControlRoomStoreError):
    """Base error for control-room service operations."""


class SessionClosedError(ControlRoomServiceError):
    """Raised when an operation targets a session that is not open."""


class CueSurfaceMismatchError(ControlRoomServiceError):
    """Raised when a cue does not belong to the session's surface."""


class CueNotReadyError(ControlRoomServiceError):
    """Raised when firing a cue whose device is disabled / not ready."""


class CuePolicyError(ControlRoomServiceError):
    """Raised when a cue/device violates the control-room safety policy."""


class CueMaterialStateChangedError(CuePolicyError):
    """Raised when Live Fire receives a stale Dry Run material-state fingerprint."""


class SessionAlreadyOpenError(ControlRoomServiceError):
    """Raised when opening a session on a surface that already has one open.

    Carries the existing lock holder (operator id/name + when it was opened)
    so the API/UI can tell a second operator WHO holds the surface lock,
    instead of only that a session already exists."""

    def __init__(self, surface_id: str, existing: ControlRoomSession) -> None:
        super().__init__(surface_id)
        self.surface_id = surface_id
        self.existing_session = existing


class SessionLockOverrideForbiddenError(ControlRoomServiceError):
    """Raised when a non-setup/support-admin tries to force-close another
    operator's open session (breaking their surface lock)."""


class SessionOperatorMismatchError(ControlRoomServiceError):
    """Raised when a non-owner tries to fire a cue from another operator's session."""


class OnAirConfirmationRequiredError(ControlRoomServiceError):
    """Raised when On-Air Mode is requested without the explicit operator confirm."""


class OnAirReadinessBlockedError(ControlRoomServiceError):
    """Raised when On-Air Mode is requested while readiness checks still block it."""


class SafeStateCueRequiredError(ControlRoomServiceError):
    """Raised when On-Air Mode is requested without a valid safe-state cue."""


class OnAirSessionExpiredError(SessionClosedError):
    """Raised when an On-Air session has exceeded its idle/expiry window."""


class RollbackNotAvailableError(ControlRoomServiceError):
    """Raised when rollback is requested on a session with no safe-state cue."""


def _compact(payload: dict[str, Any]) -> str:
    if not payload:
        return "(no payload)"
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))[:200]


def _preview_cue_action(action: str, payload: dict[str, Any], device_label: str) -> str:
    """A human-readable, secret-free rendering of what the cue will send.

    Cue payloads never carry credentials (those live in the device keyring via
    secret_ref), so the payload is safe to echo in the preview."""
    p = payload
    if action == "scene":
        return f"{device_label}: set scene -> {p.get('scene', '?')}"
    if action == "input":
        return f"{device_label}: take input -> {p.get('input', '?')}"
    if action == "transition":
        return f"{device_label}: transition ({p.get('transition', 'cut')})"
    if action == "macro":
        return f"{device_label}: run macro -> {p.get('macro', '?')}"
    if action in {"deck_play", "deck_cue"}:
        return f"{device_label}: {action.replace('_', ' ')} {p.get('clip', '')}".strip()
    if action == "ptz_preset":
        return f"{device_label}: recall PTZ preset {p.get('preset', '?')}"
    if action in {"overlay_push", "overlay_clear"}:
        return f"{device_label}: {action.replace('_', ' ')}"
    if action == "gpi_pulse":
        return f"{device_label}: GPI pulse pin {p.get('pin', '?')}"
    if action == "serial_send":
        return f"{device_label}: serial send {_compact(p)}"
    if action == "router_take":
        return f"{device_label}: router take {p.get('source', '?')}->{p.get('destination', '?')}"
    return f"{device_label}: {action} {_compact(p)}"


class ControlRoomService:
    """Plan/fire cues against the production control room."""

    def __init__(
        self,
        store: ControlRoomStore,
        tsr_client: TsrClient | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._tsr = tsr_client or NullTsrClient()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id = id_factory or (lambda: uuid.uuid4().hex)

    # --- sessions --------------------------------------------------------

    def open_session(
        self,
        *,
        surface_id: str,
        operator_id: str,
        operator_name: str | None = None,
        program_feed_source_ref: str | None = None,
        mode: SessionMode = "test",
        safe_state_cue_id: str | None = None,
        confirm_on_air: bool = False,
    ) -> ControlRoomSession:
        if self._store.get_surface(surface_id) is None:
            raise SurfaceNotFoundError(surface_id)
        existing = self._store.get_open_session_for_surface(surface_id)
        if existing is not None:
            raise SessionAlreadyOpenError(surface_id, existing)
        expires_at: datetime | None = None
        if mode == "on_air":
            if not confirm_on_air:
                raise OnAirConfirmationRequiredError(
                    "Opening On-Air Mode requires an explicit operator confirmation."
                )
            if not safe_state_cue_id:
                raise SafeStateCueRequiredError(
                    "Opening On-Air Mode requires a configured safe-state cue."
                )
            safe_cue = self._store.get_cue(safe_state_cue_id)
            if safe_cue is None or safe_cue.surface_id != surface_id:
                raise SafeStateCueRequiredError(
                    "The safe-state cue must exist on the selected control surface."
                )
            if not safe_cue.confirm_required:
                raise SafeStateCueRequiredError(
                    "The safe-state cue must be marked confirm-required before On-Air Mode."
                )
            readiness = self.readiness_report()
            if not readiness.ready_for_on_air:
                blockers = [check for check in readiness.checks if check.status == "blocked"]
                actions = "; ".join(
                    f"{check.label}: {check.operator_action}" for check in blockers[:3]
                )
                if len(blockers) > 3:
                    actions += f"; plus {len(blockers) - 3} more blocker(s)."
                raise OnAirReadinessBlockedError(
                    f"Opening On-Air Mode is blocked until control-room readiness passes. {actions}"
                )
            expires_at = self._clock() + timedelta(minutes=30)
        try:
            return self._store.open_session(
                ControlRoomSession(
                    session_id=f"crs_{self._id()}",
                    surface_id=surface_id,
                    operator_id=operator_id,
                    operator_name=operator_name,
                    program_feed_source_ref=program_feed_source_ref,
                    mode=mode,
                    safe_state_cue_id=safe_state_cue_id,
                    state="open",
                    started_at=self._clock(),
                    on_air_expires_at=expires_at,
                )
            )
        except SessionSurfaceConflictError:
            # Lost the DB-level race against a concurrent open_session for
            # this surface (the check above raced past it). Re-fetch the
            # winner and report the same clean, lock-holder-carrying error
            # as the non-racy duplicate-open path.
            winner = self._store.get_open_session_for_surface(surface_id)
            if winner is None:
                raise
            raise SessionAlreadyOpenError(surface_id, winner) from None

    def close_session(
        self, *, session_id: str, requested_by: str | None = None, is_lock_override: bool = False
    ) -> ControlRoomSession:
        """Close a session, ending its operator lock on the surface.

        ``requested_by``/``is_lock_override`` implement the operator-lock
        override: the owning operator can always close their own session;
        anyone else needs ``is_lock_override=True`` (the router only sets this
        for setup_admin/support_admin), so a stuck/abandoned lock can be
        cleared without every operator being able to kick another off air."""
        if requested_by is not None and not is_lock_override:
            session = self._store.get_session(session_id)
            if session is not None and session.operator_id != requested_by:
                raise SessionLockOverrideForbiddenError(
                    f"session {session_id} is locked by another operator"
                )
        return self._store.close_session(session_id, ended_at=self._clock())

    # --- plan / fire -----------------------------------------------------

    def _resolve_cue(
        self, session_id: str, cue_id: str
    ) -> tuple[ControlRoomSession, TimelineCue, ProductionDevice]:
        session = self._store.get_session(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        if session.state != "open":
            raise SessionClosedError(session_id)
        cue = self._store.get_cue(cue_id)
        if cue is None:
            raise CueNotFoundError(cue_id)
        if cue.surface_id != session.surface_id:
            raise CueSurfaceMismatchError(
                f"cue {cue_id} is not on the session's surface {session.surface_id}"
            )
        device = self._store.get_device(cue.device_id)
        if device is None:
            raise DeviceNotFoundError(cue.device_id)
        return session, cue, device

    def _assert_on_air_session_active(self, session: ControlRoomSession) -> None:
        if session.mode != "on_air":
            return
        if session.on_air_expires_at is None:
            return
        if self._clock() <= session.on_air_expires_at:
            return
        raise OnAirSessionExpiredError(
            "On-Air Mode expired before this cue could fire. Open a new On-Air session to continue."
        )

    def _profile_for(self, device_id: str) -> DeviceProfile | None:
        return self._store.get_profile_for_device(device_id)

    def _validate_static_policy(self, *, cue: TimelineCue, device: ProductionDevice) -> None:
        try:
            validate_cue_for_device(device, cue)
        except ControlRoomPolicyError as exc:
            raise CuePolicyError(str(exc)) from exc

    def _validate_live_policy(self, *, device: ProductionDevice) -> None:
        profile = self._profile_for(device.device_id)
        try:
            assert_device_target_allowed(device, profile)
        except UnsafeDeviceTargetError as exc:
            raise CuePolicyError(str(exc)) from exc

    def plan_cue(self, *, session_id: str, cue_id: str) -> CuePlan:
        """Resolve + validate a cue into an inspectable plan. Opens no socket."""
        _session, cue, device = self._resolve_cue(session_id, cue_id)
        profile = self._profile_for(device.device_id)
        self._validate_static_policy(cue=cue, device=device)
        self._validate_live_policy(device=device)
        take_delay = profile.take_delay_ms if profile is not None else 0
        post_roll = profile.post_roll_ms if profile is not None else 0
        fingerprint = material_state_fingerprint(cue=cue, device=device, profile=profile)
        return CuePlan(
            cue_id=cue.cue_id,
            surface_id=cue.surface_id,
            device_id=device.device_id,
            label=cue.label,
            action=cue.action,
            resolved_payload=dict(cue.payload),
            command_preview=_preview_cue_action(cue.action, cue.payload, device.label),
            ready_to_send=device.enabled,
            confirm_required=cue.confirm_required,
            material_state_fingerprint=fingerprint,
            take_delay_ms=take_delay,
            post_roll_ms=post_roll,
            operator_action=(
                f"Preview shows the resolved action; fire to send it to {device.label}."
                if device.enabled
                else f"Enable {device.label} before firing this cue."
            ),
            proof_boundary=_PLAN_BOUNDARY,
        )

    def fire_cue(
        self,
        *,
        session_id: str,
        cue_id: str,
        operator_id: str,
        expected_material_state_fingerprint: str | None = None,
        _bypass_on_air_expiry: bool = False,
    ) -> CueFiredEvent:
        """Fire a planned cue through the TSR sidecar and append the audit.

        Re-resolves + re-validates (the plan is stateless), refuses a cue whose
        device is not ready, then calls the injected TsrClient. On a transport
        failure the failed attempt is still audited (and the gap-8 device-command
        row recorded as failed) before the error propagates — no silent drop.

        ``_bypass_on_air_expiry`` is private: only :meth:`rollback_session` sets
        it. A panic rollback to the safe-state cue must still reach the device
        on an expired on-air session -- that is precisely when it is needed
        most -- while a normal cue fire must keep respecting expiry."""
        session, cue, device = self._resolve_cue(session_id, cue_id)
        if session.operator_id != operator_id:
            raise SessionOperatorMismatchError(
                "This control-room session belongs to another operator."
            )
        if not device.enabled:
            raise CueNotReadyError(f"device {device.device_id} is disabled")
        profile = self._profile_for(device.device_id)
        self._validate_static_policy(cue=cue, device=device)
        self._validate_live_policy(device=device)
        fingerprint = material_state_fingerprint(cue=cue, device=device, profile=profile)
        try:
            assert_material_state_matches(expected_material_state_fingerprint, fingerprint)
        except MaterialStateChangedError as exc:
            raise CueMaterialStateChangedError(str(exc)) from exc
        now = self._clock()
        if session.mode == "test":
            return self._store.append_cue_event(
                CueFiredEvent(
                    event_id=f"cre_{self._id()}",
                    session_id=session_id,
                    cue_id=cue.cue_id,
                    operator_id=operator_id,
                    device_id=device.device_id,
                    action=cue.action,
                    result="planned",
                    fired_at=now,
                    detail={
                        "mode": "test",
                        "test_mode": True,
                        "device_command_blocked": True,
                        "material_state_fingerprint": fingerprint,
                    },
                )
            )
        if not _bypass_on_air_expiry:
            self._assert_on_air_session_active(session)

        result_state: dict[str, Any] = {}
        outcome = "fired"
        error: TsrClientError | None = None
        try:
            applied = self._tsr.apply_cue(
                device=device, profile=profile, action=cue.action, payload=dict(cue.payload)
            )
            outcome = "fired"
            result_state = dict(applied.device_state)
        except TsrClientError as exc:
            outcome = "failed"
            error = exc
            result_state = {"detail": str(exc)}

        event = self._store.append_cue_event(
            CueFiredEvent(
                event_id=f"cre_{self._id()}",
                session_id=session_id,
                cue_id=cue.cue_id,
                operator_id=operator_id,
                device_id=device.device_id,
                action=cue.action,
                result=outcome,  # type: ignore[arg-type]
                fired_at=now,
                detail={**result_state, "material_state_fingerprint": fingerprint},
            )
        )
        if cue.action in _DEVICE_COMMAND_ACTIONS:
            self._store.append_device_command(
                DeviceCommand(
                    command_id=f"crc_{self._id()}",
                    device_id=device.device_id,
                    session_id=session_id,
                    command_kind=cue.action,
                    command_preview=_preview_cue_action(cue.action, cue.payload, device.label),
                    take_delay_ms=profile.take_delay_ms if profile is not None else 0,
                    post_roll_ms=profile.post_roll_ms if profile is not None else 0,
                    issued_by=operator_id,
                    issued_at=now,
                    result=outcome,  # type: ignore[arg-type]
                )
            )
        if error is not None:
            raise error
        return event

    def rollback_session(self, *, session_id: str, operator_id: str) -> CueFiredEvent:
        """Partial-failure recovery policy (S16 item 7): fire the session's
        configured safe-state/panic cue on demand.

        This is deliberately NOT automatic undo of a specific failed cue —
        production-control actions (a scene take, a router take) have no
        generic inverse. The recovery policy CivicCast can actually keep is
        "return the surface to its known-safe cue," the same cue Safe State /
        On-Air setup already requires be configured and confirm-required
        before On-Air Mode can open."""
        session = self._store.get_session(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        if session.state != "open":
            raise SessionClosedError(session_id)
        if not session.safe_state_cue_id:
            raise RollbackNotAvailableError(
                "This session has no configured safe-state cue to roll back to."
            )
        return self.fire_cue(
            session_id=session_id,
            cue_id=session.safe_state_cue_id,
            operator_id=operator_id,
            _bypass_on_air_expiry=True,
        )

    def probe_device(self, *, device_id: str) -> TsrProbeResult:
        """Open a control connection to verify reachability + capabilities.

        Records the outcome as the device's health/state-freshness reading
        (S16 item 7) regardless of success or failure, so a device that goes
        unreachable shows up as such rather than keeping a stale "reachable"
        status forever."""
        device = self._store.get_device(device_id)
        if device is None:
            raise DeviceNotFoundError(device_id)
        profile = self._profile_for(device_id)
        try:
            assert_device_target_allowed(device, profile)
        except UnsafeDeviceTargetError as exc:
            raise CuePolicyError(str(exc)) from exc
        try:
            result = self._tsr.probe_device(device=device, profile=profile)
        except TsrClientError:
            self._store.record_device_probe(device_id, reachable=False, probed_at=self._clock())
            raise
        self._store.record_device_probe(
            device_id, reachable=result.reachable, probed_at=self._clock()
        )
        return result

    def readiness_report(self) -> ControlRoomReadinessReport:
        """Summarize control-room setup readiness without touching devices."""

        devices = self._store.list_devices()
        device_by_id = {device.device_id: device for device in devices}
        surfaces = self._store.list_surfaces()
        cues_by_surface = {
            surface.surface_id: self._store.list_cues_for_surface(surface.surface_id)
            for surface in surfaces
        }
        cues = [cue for surface_cues in cues_by_surface.values() for cue in surface_cues]
        profiles_by_device = {
            device.device_id: self._store.get_profile_for_device(device.device_id)
            for device in devices
        }
        open_sessions = [
            session
            for surface in surfaces
            if (session := self._store.get_open_session_for_surface(surface.surface_id)) is not None
        ]
        missing_profile = [
            device.device_id for device in devices if profiles_by_device[device.device_id] is None
        ]
        disabled_devices = [device.device_id for device in devices if not device.enabled]
        now = self._clock()
        stale_or_unhealthy = [
            device.device_id
            for device in devices
            if device.enabled and _device_is_stale_or_unhealthy(device, now)
        ]
        unsafe_targets = _device_policy_failures(devices, profiles_by_device)
        cue_policy_failures = _cue_policy_failures(cues, device_by_id)
        surfaces_missing_safe_state = [
            surface.label
            for surface in surfaces
            if cues_by_surface[surface.surface_id]
            and not any(cue.confirm_required for cue in cues_by_surface[surface.surface_id])
        ]
        tsr_configured = not isinstance(self._tsr, NullTsrClient)
        tsr_unavailable_detail = getattr(
            self._tsr, "detail", "The Node TSR control service is not configured"
        )
        try:
            tsr_health = self._tsr.health()
        except TsrClientError as exc:
            tsr_health = TsrProbeResult(reachable=False, detail=str(exc))
        tsr_ready = tsr_configured and tsr_health.reachable
        tsr_detail = (
            "A TSR control client is configured and the sidecar health check passed."
            if tsr_ready
            else (
                f"The TSR control service is configured but not reachable: {tsr_health.detail}."
                if tsr_configured
                else f"{tsr_unavailable_detail}; live cue fire/probe paths fail closed."
            )
        )
        tsr_action = (
            "Keep the sidecar supervised and visible in station health before On-Air use."
            if tsr_ready
            else (
                "Start or restart the loopback TSR sidecar before opening On-Air Mode."
                if tsr_configured
                else "Configure and supervise CIVICCAST_CONTROL_ROOM_TSR_URL before opening On-Air Mode."
            )
        )
        checks = [
            _readiness_check(
                "tsr-control-service",
                "TSR control service",
                "passed" if tsr_ready else "blocked",
                "info" if tsr_ready else "blocker",
                tsr_detail,
                tsr_action,
                "control_room.tsr_client",
            ),
            _readiness_check(
                "device-inventory",
                "Device inventory",
                "passed" if devices else "blocked",
                "info" if devices else "blocker",
                (
                    f"{len(devices)} production device(s) are configured."
                    if devices
                    else "No production devices are configured."
                ),
                (
                    "Probe each production device before On-Air use."
                    if devices
                    else "Register OBS, vMix, ATEM, PTZ, or capture-path devices in Control Room setup."
                ),
                "production_devices",
            ),
            _readiness_check(
                "device-profiles",
                "Device profiles",
                "passed" if devices and not missing_profile else "blocked",
                "info" if devices and not missing_profile else "blocker",
                (
                    "Every configured production device has a TSR/profile row."
                    if devices and not missing_profile
                    else (
                        f"Device(s) missing profiles: {', '.join(missing_profile)}."
                        if devices
                        else "No device profiles can exist until production devices are registered."
                    )
                ),
                (
                    "Review transition timing and capability maps before On-Air use."
                    if devices and not missing_profile
                    else (
                        "Save a device profile for every production device."
                        if devices
                        else "Register devices, then save their profiles."
                    )
                ),
                "device_profiles",
            ),
            _readiness_check(
                "enabled-devices",
                "Enabled devices",
                "passed"
                if devices and len(disabled_devices) == 0
                else ("blocked" if devices else "not_applicable"),
                "info"
                if devices and len(disabled_devices) == 0
                else ("blocker" if devices else "info"),
                (
                    "All configured devices are enabled."
                    if devices and len(disabled_devices) == 0
                    else (
                        f"Disabled device(s): {', '.join(disabled_devices)}."
                        if devices
                        else "No devices exist yet, so enabled-device status is not applicable."
                    )
                ),
                (
                    "No action needed."
                    if devices and len(disabled_devices) == 0
                    else (
                        "Enable required devices or remove unused disabled devices before On-Air Mode."
                        if devices
                        else "Register devices first."
                    )
                ),
                "production_devices.enabled",
            ),
            _readiness_check(
                "device-health",
                "Device health",
                "passed"
                if devices and not stale_or_unhealthy
                else ("warning" if devices else "not_applicable"),
                "info" if devices and not stale_or_unhealthy else "warning",
                (
                    "Every enabled device's last probe was reachable and recent."
                    if devices and not stale_or_unhealthy
                    else (
                        f"Stale or unhealthy device(s): {', '.join(stale_or_unhealthy)}."
                        if devices
                        else "No devices exist yet, so device health is not applicable."
                    )
                ),
                (
                    "No action needed."
                    if devices and not stale_or_unhealthy
                    else (
                        "Probe these devices again before On-Air use; a probe result older than "
                        f"{DEVICE_HEALTH_STALE_AFTER_SECONDS // 60} minutes is treated as stale."
                        if devices
                        else "Register devices first."
                    )
                ),
                "production_devices.last_probed_at",
            ),
            _readiness_check(
                "device-target-policy",
                "Device target safety",
                "passed"
                if devices and not unsafe_targets
                else ("blocked" if devices else "not_applicable"),
                "info" if devices and not unsafe_targets else ("blocker" if devices else "info"),
                (
                    "All enabled device targets satisfy the local/private network safety policy."
                    if devices and not unsafe_targets
                    else (
                        f"Unsafe device target(s): {'; '.join(unsafe_targets)}."
                        if devices
                        else "No devices exist yet, so target safety is not applicable."
                    )
                ),
                (
                    "No action needed."
                    if devices and not unsafe_targets
                    else (
                        "Move devices to localhost/private station networks or record an explicit setup-admin public-host override reason."
                        if devices
                        else "Register devices first."
                    )
                ),
                "production_devices.host",
            ),
            _readiness_check(
                "surface-inventory",
                "Control surfaces",
                "passed" if surfaces else "blocked",
                "info" if surfaces else "blocker",
                (
                    f"{len(surfaces)} operator surface(s) are configured."
                    if surfaces
                    else "No operator control surfaces are configured."
                ),
                (
                    "Review role assignments before On-Air use."
                    if surfaces
                    else "Create at least one meeting-operator control surface."
                ),
                "control_surfaces",
            ),
            _readiness_check(
                "cue-inventory",
                "Timeline cues",
                "passed" if cues else "blocked",
                "info" if cues else "blocker",
                f"{len(cues)} timeline cue(s) are configured."
                if cues
                else "No timeline cues are configured.",
                "Author dry-run cues for the selected LPM profile."
                if not cues
                else "Dry-run cue banks before On-Air Mode.",
                "timeline_cues",
            ),
            _readiness_check(
                "cue-policy",
                "Cue action safety",
                "passed"
                if cues and not cue_policy_failures
                else ("blocked" if cues else "not_applicable"),
                "info" if cues and not cue_policy_failures else ("blocker" if cues else "info"),
                (
                    "All configured cues satisfy the device action allowlist and payload limits."
                    if cues and not cue_policy_failures
                    else (
                        f"Unsafe cue(s): {'; '.join(cue_policy_failures)}."
                        if cues
                        else "No cues exist yet, so cue action safety is not applicable."
                    )
                ),
                (
                    "No action needed."
                    if cues and not cue_policy_failures
                    else (
                        "Edit or remove unsupported cues before On-Air Mode."
                        if cues
                        else "Create cues first."
                    )
                ),
                "timeline_cues.action",
            ),
            _readiness_check(
                "safe-state-candidate",
                "Safe-state cue candidate",
                "passed"
                if cues and not surfaces_missing_safe_state
                else ("blocked" if cues else "not_applicable"),
                "info"
                if cues and not surfaces_missing_safe_state
                else ("blocker" if cues else "info"),
                (
                    "Every cue-bearing surface has at least one confirm-required safe-state candidate."
                    if cues and not surfaces_missing_safe_state
                    else (
                        f"Surface(s) missing a confirm-required safe-state candidate: {', '.join(surfaces_missing_safe_state)}."
                        if cues
                        else "No cues exist yet."
                    )
                ),
                (
                    "Select and test the safe-state cue when opening On-Air Mode."
                    if cues
                    else "Create cues before assigning safe-state behavior."
                ),
                "timeline_cues.confirm_required",
            ),
            _readiness_check(
                "station-device-evidence",
                "Station-device evidence",
                "warning",
                "warning",
                "This control room has not been verified against your station's real equipment yet.",
                "Run a check against the room's actual devices before relying on it for a live broadcast.",
                "control_room.lpm_lab",
            ),
        ]
        blockers = [check for check in checks if check.status == "blocked"]
        return ControlRoomReadinessReport(
            generated_at=self._clock(),
            ready_for_on_air=not blockers,
            station_device_ready=False,
            summary=(
                "Ready for local dry runs. On-air readiness is confirmed once a check against this room's actual devices passes."
                if not blockers
                else f"Control-room configuration has {len(blockers)} blocker(s) before On-Air use."
            ),
            devices_configured=len(devices),
            devices_enabled=len(devices) - len(disabled_devices),
            devices_missing_profile=missing_profile,
            surfaces_configured=len(surfaces),
            cues_configured=len(cues),
            open_sessions=len(open_sessions),
            open_on_air_sessions=sum(1 for session in open_sessions if session.mode == "on_air"),
            checks=checks,
            lpm_profiles=[
                _profile_coverage(profile)
                for profile in sorted(build_lpm_lab_profiles().values(), key=lambda p: p.priority)
            ],
            proof_boundary=_READINESS_BOUNDARY,
        )


def _device_is_stale_or_unhealthy(device: ProductionDevice, now: datetime) -> bool:
    """True when a device has never been probed, was last unreachable, or its
    probe result is older than DEVICE_HEALTH_STALE_AFTER_SECONDS (state
    freshness — S16 item 7). CivicCast only knows what the last probe/fire
    told it, so an old reading is treated as unknown, not still-good."""
    if device.last_probed_at is None or device.last_reachable is not True:
        return True
    age = (now - device.last_probed_at).total_seconds()
    return age > DEVICE_HEALTH_STALE_AFTER_SECONDS


def _device_policy_failures(
    devices: list[ProductionDevice], profiles_by_device: dict[str, DeviceProfile | None]
) -> list[str]:
    failures: list[str] = []
    for device in devices:
        if not device.enabled:
            continue
        profile = profiles_by_device.get(device.device_id)
        try:
            assert_device_target_allowed(device, profile)
        except UnsafeDeviceTargetError as exc:
            failures.append(f"{device.label} ({device.device_id}): {exc}")
    return failures


def _cue_policy_failures(
    cues: list[TimelineCue], device_by_id: dict[str, ProductionDevice]
) -> list[str]:
    failures: list[str] = []
    for cue in cues:
        device = device_by_id.get(cue.device_id)
        if device is None:
            failures.append(f"{cue.label} ({cue.cue_id}): device {cue.device_id!r} is missing")
            continue
        if not device.enabled:
            continue
        try:
            validate_cue_for_device(device, cue)
        except ControlRoomPolicyError as exc:
            failures.append(f"{cue.label} ({cue.cue_id}): {exc}")
    return failures


def _readiness_check(
    check_id: str,
    label: str,
    status: str,
    severity: str,
    detail: str,
    operator_action: str,
    evidence_ref: str,
) -> ControlRoomReadinessCheck:
    return ControlRoomReadinessCheck(
        check_id=check_id,
        label=label,
        status=status,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        detail=detail,
        operator_action=operator_action,
        evidence_ref=evidence_ref,
    )


def _profile_coverage(profile: LabTopologyProfile) -> ControlRoomLpmProfileCoverage:
    return ControlRoomLpmProfileCoverage(
        profile_id=profile.profile_id,
        label=profile.label,
        priority=profile.priority,
        proof_status="contract_only_not_station_device_evidence",
        devices=[
            ControlRoomLpmDeviceCoverage(
                profile_id=profile.profile_id,
                device_contract_id=device.contract_id,
                label=device.label,
                device_class=device.device_class,
                integration_surface=device.integration_surface,
                proof_level=device.proof_level,
                station_device_evidence_required=device.station_device_evidence_required,
                required_checks_count=len(device.required_checks),
            )
            for device in profile.devices
        ],
        required_absences=list(profile.required_absences),
        egress_destinations=list(profile.egress_destinations),
        not_claimed=list(profile.not_claimed),
    )


__all__ = [
    "ControlRoomService",
    "ControlRoomServiceError",
    "CueMaterialStateChangedError",
    "CueNotReadyError",
    "CuePolicyError",
    "CueSurfaceMismatchError",
    "OnAirConfirmationRequiredError",
    "OnAirReadinessBlockedError",
    "OnAirSessionExpiredError",
    "RollbackNotAvailableError",
    "SafeStateCueRequiredError",
    "SessionAlreadyOpenError",
    "SessionClosedError",
    "SessionLockOverrideForbiddenError",
]
