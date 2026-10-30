# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Safety policy for production control-room devices and cues.

This module is intentionally pure: no sockets, DNS lookups, keyring reads, or
database access. The service/router call it before probe/plan/fire so the live
path refuses unsupported cue actions and unsafe network targets before the TSR
sidecar can touch a device.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Any

from civiccast.control_room.models import DeviceProfile, ProductionDevice, TimelineCue

MAX_CUE_PAYLOAD_BYTES = 32_768

PUBLIC_HOST_OVERRIDE_FLAG = "allow_public_host_override"
PUBLIC_HOST_OVERRIDE_REASON = "public_host_override_reason"

ALLOWED_ACTIONS_BY_KIND: dict[str, frozenset[str]] = {
    "obs": frozenset({"scene", "transition", "overlay_push", "overlay_clear"}),
    # vMix input select is in scope. Input rename and other configuration
    # mutations are intentionally not represented as CivicCast cue actions.
    "vmix": frozenset({"input", "transition", "overlay_push", "overlay_clear", "macro"}),
    "atem": frozenset({"input", "transition", "macro"}),
    "hyperdeck": frozenset({"deck_play", "deck_cue"}),
    "ptz": frozenset({"ptz_preset"}),
    "osc": frozenset({"osc"}),
    "tcp": frozenset({"serial_send", "gpi_pulse", "router_take"}),
    "http": frozenset({"http"}),
    "casparcg": frozenset({"deck_play", "deck_cue", "overlay_push", "overlay_clear"}),
    # Honest label: 'gpi' and 'serial' are network-relay triggers (TCP) only —
    # there is no direct GPI contact-closure / serial hardware driver in this
    # release (tsr_service/index.mjs routes both through TSR's TCPSEND
    # adapter, same as the 'tcp' kind above). See ProductionDevice.kind's
    # Field description and CAPABILITIES.md for the full disclosure.
    "gpi": frozenset({"gpi_pulse"}),
    "serial": frozenset({"serial_send"}),
}

_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "rename",
        "rename_input",
        "new_name",
        "new_label",
        "delete",
        "remove",
        "destination_path",
        "recording_path",
    }
)


class ControlRoomPolicyError(ValueError):
    """Raised when a device/cue violates the control-room safety policy."""


class UnsafeDeviceTargetError(ControlRoomPolicyError):
    """Raised when probe/fire would target a public network host without override."""


class UnsupportedCueActionError(ControlRoomPolicyError):
    """Raised when a cue action is not allowed for the target device kind."""


class MaterialStateChangedError(ControlRoomPolicyError):
    """Raised when Live Fire receives a stale material-state fingerprint."""


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def assert_payload_within_limit(payload: dict[str, Any]) -> None:
    size = len(_json_bytes(payload))
    if size > MAX_CUE_PAYLOAD_BYTES:
        raise ControlRoomPolicyError(
            f"Cue payload is {size} bytes; the limit is {MAX_CUE_PAYLOAD_BYTES} bytes."
        )


def _flatten_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).lower())
            keys.update(_flatten_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_flatten_keys(child))
    return keys


def validate_cue_for_device(device: ProductionDevice, cue: TimelineCue) -> None:
    """Validate that ``cue`` is safe and supported for ``device``.

    This is not a claim that the device is reachable. It is only the static
    allowlist that prevents unsupported/destructive actions from being authored
    or fired.
    """

    action = str(cue.action)
    kind = str(device.kind)
    allowed = ALLOWED_ACTIONS_BY_KIND.get(kind, frozenset())
    if action not in allowed:
        raise UnsupportedCueActionError(
            f"Action {action!r} is not exposed for {kind!r} devices in CivicCast 3.1."
        )

    assert_payload_within_limit(cue.payload)
    forbidden = sorted(_flatten_keys(cue.payload) & _FORBIDDEN_PAYLOAD_KEYS)
    if forbidden:
        raise UnsupportedCueActionError(
            f"Cue payload contains unsupported configuration mutation keys: {', '.join(forbidden)}."
        )


def _has_public_host_override(profile: DeviceProfile | None) -> bool:
    if profile is None:
        return False
    options = dict(profile.options or {})
    return bool(options.get(PUBLIC_HOST_OVERRIDE_FLAG)) and bool(
        str(options.get(PUBLIC_HOST_OVERRIDE_REASON, "")).strip()
    )


def assert_device_target_allowed(
    device: ProductionDevice, profile: DeviceProfile | None = None
) -> None:
    """Refuse probe/fire to public hosts unless the profile explicitly overrides it."""

    host = (device.host or "").strip()
    if not host:
        return

    normalized = host.lower().strip("[]")
    if (
        normalized in {"localhost", "localhost.localdomain"}
        or normalized.endswith(".local")
        or "." not in normalized
    ):
        return

    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError as exc:
        if _has_public_host_override(profile):
            return
        raise UnsafeDeviceTargetError(
            "Device host must be localhost, .local, or a private/link-local IP unless a "
            "setup admin records a public-host override reason in the device profile."
        ) from exc

    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return
    if _has_public_host_override(profile):
        return
    raise UnsafeDeviceTargetError(
        "Device host resolves to a public IP; live control is blocked without an explicit "
        "public-host override reason in the device profile."
    )


def material_state_fingerprint(
    *, cue: TimelineCue, device: ProductionDevice, profile: DeviceProfile | None
) -> str:
    """Hash only the state slice material to this cue's action.

    The fingerprint deliberately excludes continuously changing telemetry such
    as audio meters, last-update timestamps, and full device state snapshots.
    """

    profile_body = (
        {
            "profile_id": profile.profile_id,
            "version": profile.version,
            "tsr_device_type": profile.tsr_device_type,
            "options": profile.options,
            "capability_map": profile.capability_map,
            "take_delay_ms": profile.take_delay_ms,
            "post_roll_ms": profile.post_roll_ms,
        }
        if profile is not None
        else None
    )
    body = {
        "cue_id": cue.cue_id,
        "surface_id": cue.surface_id,
        "device_id": device.device_id,
        "device_kind": device.kind,
        "device_host": device.host,
        "device_port": device.port,
        "device_enabled": device.enabled,
        "action": cue.action,
        "payload": cue.payload,
        "profile": profile_body,
    }
    return hashlib.sha256(_json_bytes(body)).hexdigest()


def assert_material_state_matches(expected: str | None, actual: str) -> None:
    if expected is not None and expected != actual:
        raise MaterialStateChangedError(
            "The cue preview is stale because material device/cue state changed. "
            "Dry Run the cue again before Live Fire."
        )


__all__ = [
    "ALLOWED_ACTIONS_BY_KIND",
    "ControlRoomPolicyError",
    "MaterialStateChangedError",
    "UnsafeDeviceTargetError",
    "UnsupportedCueActionError",
    "assert_device_target_allowed",
    "assert_material_state_matches",
    "assert_payload_within_limit",
    "material_state_fingerprint",
    "validate_cue_for_device",
]
