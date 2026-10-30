# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Executable Stage 4-5 LPM lab probes and API fixtures.

Stage 0-1 keeps the topology/check catalog honest. This module is the next
proof layer: strict local fixtures, deterministic state simulations, and optional
real local software probes. It never claims station-device evidence.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Literal
from xml.etree.ElementTree import Element

from defusedxml import ElementTree
from pydantic import BaseModel, ConfigDict, Field

from civiccast.control_room.lpm_lab import DeviceContract, LabTopologyProfile, TopologyId
from civiccast.control_room.lpm_lab_harness import CHECK_CATALOG, LabEvent
from civiccast.recording.input_presets import RecordingInputPreset, RecordingInputPresetCatalog
from civiccast.recording.models import RecordingSource

Stage45Status = Literal["passed", "failed", "not-applicable"]


REFERENCE_URLS = {
    "vmix-api": "https://www.vmix.com/help28/DeveloperAPI.html",
    "obs-websocket": "https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md",
    "blackmagic-atem-sdk": "https://www.blackmagicdesign.com/developer/products/atem/sdk-and-software",
    "blackmagic-decklink-sdk": "https://www.blackmagicdesign.com/developer/products/capture-and-playback/sdk-and-software",
    "aida-visca": "https://aidaimaging.com/wp-content/uploads/2023/08/AIDA-PTZ-NDI3-X20-Manual.pdf",
    "lpm-aida-wiki": "https://wiki.longmontpublicmedia.org/production-how-tos/aida-ndi-ptz-cameras",
    "midi-1-0-spec": "https://midi.org/summary-of-midi-1-0-messages",
    "ah-sq-midi-protocol": "https://www.allen-heath.com/content/uploads/2023/11/SQ-MIDI-Protocol-Issue5.pdf",
}


class Stage45Proof(BaseModel):
    """One executable local proof used to upgrade a Stage 1 catalog row."""

    model_config = ConfigDict(extra="forbid")

    check_id: Annotated[str, Field(min_length=1, max_length=120)]
    status: Stage45Status
    proof_source: Literal["api-fixture", "stateful-simulator", "software-lab"]
    proof_level: Literal[
        "mocked",
        "api-contract-proven",
        "simulated-proven",
        "software-lab-proven",
    ]
    claim: Annotated[str, Field(min_length=1, max_length=300)]
    observed: Annotated[str, Field(min_length=1, max_length=600)]
    not_claimed: Annotated[str | None, Field(max_length=600)] = (
        "This is local Stage 4-5 proof only; it is not station-device evidence."
    )
    details: dict[str, Any] = Field(default_factory=dict)


class VmixInputState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Annotated[str, Field(min_length=1, max_length=120)]
    number: Annotated[int, Field(ge=1)]
    title: Annotated[str, Field(min_length=1, max_length=160)]
    state: Annotated[str | None, Field(max_length=80)] = None
    kind: Annotated[str | None, Field(max_length=80)] = None


class VmixStatusState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Annotated[str, Field(min_length=1, max_length=80)]
    active: Annotated[int, Field(ge=1)]
    preview: Annotated[int, Field(ge=1)]
    recording: bool
    streaming: bool
    inputs: list[VmixInputState]


def parse_vmix_status_xml(xml_text: str) -> VmixStatusState:
    """Parse the vMix `/api` status XML subset CivicCast depends on."""

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Invalid vMix status XML: {exc}") from exc
    if root.tag != "vmix":
        raise ValueError("vMix status XML root must be <vmix>.")

    version = _required_text(root, "version")
    active = _required_int(root, "active")
    preview = _required_int(root, "preview")
    recording = _bool_text(_required_text(root, "recording"))
    streaming = _bool_text(_required_text(root, "streaming"))
    inputs_root = root.find("inputs")
    if inputs_root is None:
        raise ValueError("vMix status XML is missing <inputs>.")

    inputs: list[VmixInputState] = []
    for input_el in inputs_root.findall("input"):
        key = input_el.attrib.get("key")
        title = input_el.attrib.get("title") or (input_el.text or "").strip()
        number_text = input_el.attrib.get("number")
        if not key or not title or not number_text:
            raise ValueError("vMix <input> requires key, title, and number.")
        try:
            number = int(number_text)
        except ValueError as exc:
            raise ValueError(f"vMix input number must be an integer: {number_text}") from exc
        inputs.append(
            VmixInputState(
                key=key,
                title=title,
                number=number,
                state=input_el.attrib.get("state"),
                kind=input_el.attrib.get("type"),
            )
        )

    if not inputs:
        raise ValueError("vMix status XML must include at least one input.")
    input_numbers = {item.number for item in inputs}
    if active not in input_numbers or preview not in input_numbers:
        raise ValueError("vMix active/preview input references must exist in <inputs>.")

    return VmixStatusState(
        version=version,
        active=active,
        preview=preview,
        recording=recording,
        streaming=streaming,
        inputs=inputs,
    )


class ObsProtocolState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    obs_websocket_version: Annotated[str, Field(min_length=1, max_length=80)]
    rpc_version: Annotated[int, Field(ge=1)]
    request_ids: list[Annotated[str, Field(min_length=1, max_length=120)]]
    event_types: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(
        default_factory=list
    )


def validate_obs_websocket_5_frames(frames: Iterable[dict[str, Any]]) -> ObsProtocolState:
    """Validate the obs-websocket 5.x message contract used by CivicCast."""

    frame_list = list(frames)
    hello = next((frame for frame in frame_list if frame.get("op") == 0), None)
    if hello is None:
        raise ValueError("OBS websocket fixture is missing Hello op 0.")
    hello_data = _dict_field(hello, "d", "OBS Hello payload")
    version = str(hello_data.get("obsWebSocketVersion") or "")
    rpc_version = hello_data.get("rpcVersion")
    if not version or not isinstance(rpc_version, int) or rpc_version < 1:
        raise ValueError("OBS Hello requires obsWebSocketVersion and rpcVersion >= 1.")

    requests: dict[str, dict[str, Any]] = {}
    responses: set[str] = set()
    events: list[str] = []
    for frame in frame_list:
        op = frame.get("op")
        data = _dict_field(frame, "d", f"OBS op {op} payload")
        if op == 6:
            request_id = str(data.get("requestId") or "")
            request_type = str(data.get("requestType") or "")
            if not request_id or not request_type:
                raise ValueError("OBS Request op 6 requires requestId and requestType.")
            if request_id in requests:
                raise ValueError(f"Duplicate OBS requestId: {request_id}")
            requests[request_id] = data
        elif op == 7:
            request_id = str(data.get("requestId") or "")
            request_status = data.get("requestStatus")
            if not request_id or not isinstance(request_status, dict):
                raise ValueError("OBS RequestResponse op 7 requires requestId and requestStatus.")
            if request_id not in requests:
                raise ValueError(f"OBS response does not match a requestId: {request_id}")
            responses.add(request_id)
        elif op == 5:
            event_type = str(data.get("eventType") or "")
            if not event_type:
                raise ValueError("OBS Event op 5 requires eventType.")
            events.append(event_type)

    missing = sorted(set(requests) - responses)
    if missing:
        raise ValueError(f"OBS requestIds without responses: {', '.join(missing)}")

    return ObsProtocolState(
        obs_websocket_version=version,
        rpc_version=rpc_version,
        request_ids=sorted(requests),
        event_types=events,
    )


def validate_obs_websocket_disabled(
    connect_error: str | None, frames: Iterable[dict[str, Any]] = ()
) -> dict[str, Any]:
    """Validate that a refused/absent OBS websocket connection is treated as setup guidance.

    "Disabled" is represented as no Hello frame ever arriving (an empty frame
    stream), the same failure the real client sees on a closed/refused socket.

    ``frames`` is the candidate frame stream being asserted as disabled; it
    defaults to empty (the historical, hardcoded "always absent" case). This
    is what lets a test feed a *success-shaped* frame stream (a real Hello)
    alongside a ``connect_error`` and prove this validator rejects that
    contradiction instead of silently trusting the claimed error reason.
    """

    if not connect_error:
        raise ValueError("obs-websocket-disabled fixture requires a connect_error reason.")
    frame_list = list(frames)
    try:
        validate_obs_websocket_5_frames(frame_list)
    except ValueError:
        return {"reachable": False, "reason": connect_error}
    raise ValueError(
        "obs-websocket-disabled fixture contradicts itself: a success-shaped OBS "
        "Hello frame stream was supplied alongside a disabled/unreachable claim "
        f"({connect_error!r}); a live response must never be accepted as evidence "
        "of a disabled OBS websocket."
    )


def validate_obs_websocket_wrong_password(
    *, password: str, challenge: str, salt: str, server_expected_auth: str
) -> dict[str, Any]:
    """Validate a wrong OBS password is rejected instead of silently authenticating.

    Reuses the real ``_obs_websocket_auth`` hash chain (RFC-shaped
    SHA256(SHA256(password+salt)+challenge)): a wrong password produces a
    *different* digest than the one the (fixture) server expects, so the
    Identify would be refused, without ever logging the password itself.
    """

    computed = _obs_websocket_auth(password=password, challenge=challenge, salt=salt)
    if computed == server_expected_auth:
        raise AssertionError(
            "obs-websocket-wrong-password fixture accidentally matched the server digest."
        )
    raise ValueError("OBS websocket authentication digest does not match; Identify refused.")


def validate_obs_websocket_protocol_mismatch(
    frames: Iterable[dict[str, Any]], *, required_rpc_version: int
) -> dict[str, Any]:
    """Validate a negotiated rpcVersion below the version CivicCast requires is refused."""

    state = validate_obs_websocket_5_frames(frames)
    if state.rpc_version >= required_rpc_version:
        raise ValueError("obs-websocket-protocol-mismatch fixture must use a stale rpcVersion.")
    raise ValueError(
        f"OBS rpcVersion {state.rpc_version} is older than the required "
        f"{required_rpc_version}; refusing the session."
    )


def validate_obs_scene_item_missing(
    frames: Iterable[dict[str, Any]], *, required_source_name: str
) -> dict[str, Any]:
    """Validate cue readiness is blocked when a required OBS source is absent from state.

    Reuses the real event-subscription parsing path; the fixture's
    ``SceneItemListReady`` event data must not name the required source.
    """

    state = validate_obs_websocket_5_frames(frames)
    source_names = {
        str(name)
        for frame in frames
        if frame.get("op") == 7
        for name in (frame.get("d", {}).get("responseData", {}) or {}).get("sceneItemNames", [])
    }
    if required_source_name in source_names:
        raise ValueError(f"obs-source-missing fixture must not include {required_source_name!r}.")
    raise ValueError(
        f"Required OBS source {required_source_name!r} is not present in scene state "
        f"(rpc {state.rpc_version}); cue is not ready."
    )


def validate_obs_restart_recovery(
    before_frames: Iterable[dict[str, Any]], after_frames: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Validate OBS restart is disconnect (Hello loss) followed by a fresh, valid Hello."""

    before_state = validate_obs_websocket_5_frames(before_frames)
    try:
        validate_obs_websocket_5_frames([])
    except ValueError:
        pass
    else:
        raise AssertionError(
            "An empty frame stream (simulated disconnect) must fail Hello validation."
        )
    after_state = validate_obs_websocket_5_frames(after_frames)
    return {
        "before_rpc_version": before_state.rpc_version,
        "disconnected": True,
        "after_rpc_version": after_state.rpc_version,
        "recovered": True,
    }


def validate_visca_udp_exchange(command_hex: str, response_hexes: Iterable[str]) -> dict[str, Any]:
    """Validate a Sony VISCA-over-UDP style command/ACK/completion exchange."""

    command = _hex_bytes(command_hex)
    responses = [_hex_bytes(item) for item in response_hexes]
    if not command or command[-1] != 0xFF:
        raise ValueError("VISCA command must end with 0xFF terminator.")
    if not responses:
        raise ValueError("VISCA exchange requires at least one response.")

    has_ack = any(len(response) >= 3 and response[1] & 0xF0 == 0x40 for response in responses)
    has_completion = any(
        len(response) >= 3 and response[1] & 0xF0 == 0x50 for response in responses
    )
    has_error = any(len(response) >= 3 and response[1] & 0xF0 == 0x60 for response in responses)
    if has_error:
        raise ValueError("VISCA exchange returned an error response.")
    if not has_ack or not has_completion:
        raise ValueError("VISCA exchange requires ACK and completion responses.")
    return {
        "command_bytes": len(command),
        "responses": len(responses),
        "ack": True,
        "completion": True,
    }


def validate_atem_state_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    protocol_version = str(fixture.get("protocol_version") or "")
    program_input = fixture.get("program_input")
    preview_input = fixture.get("preview_input")
    in_transition = fixture.get("in_transition")
    if not protocol_version:
        raise ValueError("ATEM fixture requires protocol_version.")
    if not isinstance(program_input, int) or not isinstance(preview_input, int):
        raise ValueError("ATEM fixture requires integer program_input and preview_input.")
    if not isinstance(in_transition, bool):
        raise ValueError("ATEM fixture requires boolean in_transition.")
    return {
        "protocol_version": protocol_version,
        "program_input": program_input,
        "preview_input": preview_input,
        "in_transition": in_transition,
    }


def validate_atem_absent_fixture(fixture: dict[str, Any] | None) -> dict[str, Any]:
    """Validate that an absent/unreachable ATEM switcher fails closed, not silently.

    A real ATEM state fixture (see ``validate_atem_state_fixture``) is only
    produced when the switcher answered. Absence is represented by no fixture
    at all, and the contract this proves is that CivicCast refuses to treat
    "no fixture" as if it were a connected, idle switcher.
    """

    if fixture is not None:
        raise ValueError("ATEM absence fixture must not include a connected switcher state.")
    try:
        validate_atem_state_fixture({})
    except ValueError:
        return {"switcher_reachable": False}
    raise AssertionError("ATEM state validation must reject an absent switcher.")


def validate_atem_busy_transition(fixture: dict[str, Any]) -> dict[str, Any]:
    """Validate that firing a transition while one is already in flight is refused."""

    state = validate_atem_state_fixture(fixture)
    if not state["in_transition"]:
        raise ValueError("ATEM busy-transition fixture must have in_transition=True.")
    raise ValueError(
        "ATEM transition refused: a transition is already in progress "
        f"(program={state['program_input']}, preview={state['preview_input']})."
    )


def validate_vmix_input_select(
    before: VmixStatusState, after: VmixStatusState, *, requested_number: int
) -> dict[str, Any]:
    """Validate that selecting an input preserves its stable key/number/title.

    Compares the requested input across two real vMix status snapshots
    (before and after the select). Raises if the input's identity mutated,
    or if the selection never actually landed as active.
    """

    before_input = _find_vmix_input(before, requested_number)
    after_input = _find_vmix_input(after, requested_number)
    if after.active != requested_number:
        raise ValueError(
            f"vMix input select fixture must make input {requested_number} active; "
            f"active is {after.active}."
        )
    if before_input.key != after_input.key:
        raise ValueError(
            f"vMix input {requested_number} key changed on select: "
            f"{before_input.key!r} -> {after_input.key!r}."
        )
    if before_input.title != after_input.title:
        raise ValueError(
            f"vMix input {requested_number} title changed on select: "
            f"{before_input.title!r} -> {after_input.title!r}."
        )
    return {
        "input_number": requested_number,
        "key": after_input.key,
        "title": after_input.title,
    }


def _find_vmix_input(state: VmixStatusState, number: int) -> VmixInputState:
    for item in state.inputs:
        if item.number == number:
            return item
    raise ValueError(f"vMix status fixture has no input numbered {number}.")


def validate_vmix_api_disabled(connect_error: str | None, body: str = "") -> dict[str, Any]:
    """Validate that a refused/absent vMix Web Controller connection fails closed, not hangs.

    Mirrors validate_obs_websocket_disabled: "disabled" is represented as no
    parseable status XML ever arriving, the same failure the real client sees
    on a closed/refused socket. ``body`` is the candidate response body being
    asserted as disabled; it defaults to empty (the historical, hardcoded
    "always absent" case). This is what lets a test feed a *success-shaped*
    XML body alongside a ``connect_error`` and prove this validator rejects
    that contradiction — a live response must never be accepted as evidence
    of a disabled controller.
    """

    if not connect_error:
        raise ValueError("vmix-api-disabled fixture requires a connect_error reason.")
    try:
        parse_vmix_status_xml(body)
    except ValueError:
        return {"reachable": False, "reason": connect_error}
    raise ValueError(
        "vmix-api-disabled fixture contradicts itself: a success-shaped vMix status "
        f"XML body was supplied alongside a disabled/unreachable claim ({connect_error!r}); "
        "a live response must never be accepted as evidence of a disabled controller."
    )


def validate_vmix_auth_response(
    *, status_code: int, www_authenticate_header: str | None
) -> dict[str, Any]:
    """Validate vMix Web Controller auth-required/wrong-credentials responses fail closed.

    Raises unless a 401/403 pairs with a WWW-Authenticate challenge header —
    that pairing is what distinguishes "auth genuinely required/rejected"
    from a coincidental 401 with no real challenge, or a 200 OK that should
    never be accepted as if auth had been enforced.
    """

    if status_code not in (401, 403):
        raise ValueError(
            f"vmix-auth-required-or-wrong fixture requires a 401 or 403 status; got {status_code}."
        )
    if not www_authenticate_header:
        raise ValueError(
            "vmix-auth-required-or-wrong fixture requires a WWW-Authenticate challenge header."
        )
    return {"status_code": status_code, "challenge": www_authenticate_header}


def validate_vmix_input_identity(
    dry_run_snapshot: VmixStatusState, live_snapshot: VmixStatusState, *, input_number: int
) -> dict[str, Any]:
    """Validate a rehearsed input's identity survived from dry-run to live fire.

    Raises if the input's key or number drifted between the two snapshots —
    the input at that slot in the live snapshot is not the one rehearsed.
    """

    dry_run_input = _find_vmix_input(dry_run_snapshot, input_number)
    live_input = next(
        (item for item in live_snapshot.inputs if item.key == dry_run_input.key), None
    )
    if live_input is None or live_input.number != dry_run_input.number:
        raise ValueError(
            f"vMix input {dry_run_input.key!r} (rehearsed as number {input_number}) "
            "changed key/number before live fire; dry-run state is stale."
        )
    return {"key": dry_run_input.key, "number": dry_run_input.number}


def validate_vmix_input_removed(
    dry_run_snapshot: VmixStatusState, live_snapshot: VmixStatusState, *, required_key: str
) -> dict[str, Any]:
    """Validate a live-fire refuses a dry-run that rehearsed an input no longer present.

    Raises if ``required_key`` (present during dry-run) is gone from the
    live snapshot's input list.
    """

    if not any(item.key == required_key for item in dry_run_snapshot.inputs):
        raise ValueError(
            f"vmix-source-removed-dry-run fixture must include {required_key!r} in the dry-run snapshot."
        )
    if any(item.key == required_key for item in live_snapshot.inputs):
        raise AssertionError(
            f"vmix-source-removed-dry-run fixture must remove {required_key!r} from the live snapshot."
        )
    raise ValueError(
        f"vMix input {required_key!r} used in dry-run is no longer present at live fire; "
        "refusing stale dry-run."
    )


def validate_vmix_capture_identity(
    vmix_before: VmixStatusState,
    vmix_after: VmixStatusState,
    capture_before: dict[str, Any],
    capture_after: dict[str, Any],
    *,
    input_number: int,
    device_name: str,
) -> dict[str, Any]:
    """Validate a portable-laptop USB capture device swap is caught even if vMix's slot didn't change.

    Cross-checks the vMix input at ``input_number`` (must stay put, same
    key/number) against the OS-level capture device fixture's stable_id for
    ``device_name`` (must NOT stay put) — a swapped device in the same vMix
    slot is exactly the case this must catch.
    """

    before_input = _find_vmix_input(vmix_before, input_number)
    after_input = _find_vmix_input(vmix_after, input_number)
    if before_input.key != after_input.key or before_input.number != after_input.number:
        raise ValueError(
            f"vMix input {input_number} key/number changed; capture identity drift check "
            "requires the vMix slot to stay put while the underlying device swaps."
        )

    before_devices = validate_capture_identity_fixture(capture_before)["devices"]
    after_devices = validate_capture_identity_fixture(capture_after)["devices"]
    before_device = next((d for d in before_devices if d["name"] == device_name), None)
    after_device = next((d for d in after_devices if d["name"] == device_name), None)
    if before_device is None or after_device is None:
        raise ValueError(f"Capture fixture is missing device {device_name!r} in before/after.")
    if before_device["stable_id"] == after_device["stable_id"]:
        raise ValueError(
            f"Capture device {device_name!r} stable_id did not change "
            f"({before_device['stable_id']!r}); no identity drift to detect."
        )
    return {
        "input_number": input_number,
        "vmix_key": after_input.key,
        "device_name": device_name,
        "before_stable_id": before_device["stable_id"],
        "after_stable_id": after_device["stable_id"],
    }


def validate_vmix_resource_ceiling(sample: dict[str, Any]) -> dict[str, Any]:
    """Classify a perf-counter sample as over/under the resource ceiling.

    Pure classifier over a fixture sample dict (cpu_percent, dropped_frames);
    raises if the sample is over threshold. Does not itself prove CivicCast
    reads real counters from a running vMix — that half is hardware-gated.
    """

    cpu_percent = sample.get("cpu_percent")
    dropped_frames = sample.get("dropped_frames")
    if not isinstance(cpu_percent, int | float) or not isinstance(dropped_frames, int):
        raise ValueError(
            "vmix-laptop-resource-ceiling sample requires numeric cpu_percent and integer dropped_frames."
        )
    cpu_ceiling = 90.0
    dropped_frames_ceiling = 5
    if cpu_percent >= cpu_ceiling or dropped_frames >= dropped_frames_ceiling:
        raise ValueError(
            f"vMix laptop resource sample over ceiling: cpu_percent={cpu_percent} "
            f"(ceiling {cpu_ceiling}), dropped_frames={dropped_frames} (ceiling {dropped_frames_ceiling})."
        )
    return {"cpu_percent": cpu_percent, "dropped_frames": dropped_frames, "over_ceiling": False}


def validate_tsr_sidecar_restart(
    pre_restart_state: dict[str, Any],
    restart_event: dict[str, Any],
    post_restart_state: dict[str, Any],
) -> dict[str, Any]:
    """Validate a sidecar restart marks state stale until a fresh heartbeat confirms recovery.

    Raises if the post-restart state claims "recovered" without an explicit
    fresh fetch after the restart event (i.e. it must not just reuse/echo the
    pre-restart heartbeat as if nothing happened).
    """

    if not pre_restart_state.get("healthy"):
        raise ValueError("tsr-sidecar-restart fixture must start from a healthy pre-restart state.")
    restarted_at = restart_event.get("restarted_at_unix")
    if not isinstance(restarted_at, int | float):
        raise ValueError("tsr-sidecar-restart fixture requires a numeric restart_event timestamp.")

    post_fetched_at = post_restart_state.get("fetched_at_unix")
    post_healthy = post_restart_state.get("healthy")
    if not isinstance(post_fetched_at, int | float):
        raise ValueError(
            "tsr-sidecar-restart post-restart state requires a fresh fetched_at_unix timestamp."
        )
    if post_fetched_at <= restarted_at:
        raise ValueError(
            "tsr-sidecar-restart post-restart state was not fetched after the restart "
            f"(fetched_at={post_fetched_at}, restarted_at={restarted_at}); state must stay stale."
        )
    if post_healthy is not True:
        raise ValueError("tsr-sidecar-restart fixture must reach healthy=True after a fresh fetch.")
    return {
        "restarted_at_unix": restarted_at,
        "recovered_fetched_at_unix": post_fetched_at,
        "recovered": True,
    }


class NdiSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=160)]
    url_address: Annotated[str, Field(min_length=1, max_length=200)]
    last_seen_unix: Annotated[float, Field(ge=0)]


def validate_ndi_source_list(sources: Iterable[dict[str, Any]]) -> list[NdiSource]:
    """Parse and validate an NDI discovery source list (name + address + last_seen).

    This is the discovery plumbing every ndi-source-* check depends on: it
    raises on a malformed entry rather than silently dropping it.
    """

    parsed: list[NdiSource] = []
    for entry in sources:
        if not isinstance(entry, dict):
            raise ValueError("NDI discovery source list entries must be objects.")
        name = entry.get("name")
        url_address = entry.get("url_address")
        last_seen = entry.get("last_seen_unix")
        if not name or not isinstance(name, str):
            raise ValueError("NDI discovery source entry requires a non-empty name.")
        if not url_address or not isinstance(url_address, str):
            raise ValueError(f"NDI discovery source {name!r} requires a url_address.")
        if not isinstance(last_seen, int | float):
            raise ValueError(f"NDI discovery source {name!r} requires numeric last_seen_unix.")
        parsed.append(NdiSource(name=name, url_address=url_address, last_seen_unix=last_seen))
    return parsed


def validate_ndi_source_present(
    sources: Iterable[dict[str, Any]], *, expected_name: str
) -> dict[str, Any]:
    """Validate a required NDI source is discoverable before allowing PTZ control."""

    parsed = validate_ndi_source_list(sources)
    match = next((item for item in parsed if item.name == expected_name), None)
    if match is None:
        raise ValueError(
            f"NDI source {expected_name!r} is not present in the discovery list; "
            "PTZ control is not ready."
        )
    return {"name": match.name, "url_address": match.url_address}


def validate_ndi_source_disappears(
    before: Iterable[dict[str, Any]], after: Iterable[dict[str, Any]], *, tracked_name: str
) -> dict[str, Any]:
    """Validate a source drop-out marks state stale, not still-live.

    Raises if the tracked name is still present in the "after" list (i.e. the
    fixture failed to actually simulate a disappearance) or was never present
    in "before" to begin with.
    """

    before_sources = validate_ndi_source_list(before)
    after_sources = validate_ndi_source_list(after)
    if not any(item.name == tracked_name for item in before_sources):
        raise ValueError(
            f"NDI source {tracked_name!r} must be present in the 'before' list to prove a disappearance."
        )
    if any(item.name == tracked_name for item in after_sources):
        raise ValueError(
            f"NDI source {tracked_name!r} is still present in the 'after' list; "
            "no disappearance occurred."
        )
    return {"tracked_name": tracked_name, "disappeared": True}


def validate_ndi_source_reappears(
    sequence: list[Iterable[dict[str, Any]]], *, tracked_name: str
) -> dict[str, Any]:
    """Validate a source reappearance is only trusted after an intervening absence.

    ``sequence`` must be present -> absent -> present; raises if reappearance
    is claimed without a genuine absence step in between.
    """

    if len(sequence) != 3:
        raise ValueError(
            "NDI reappearance fixture requires a 3-step present/absent/present sequence."
        )
    steps = [validate_ndi_source_list(step) for step in sequence]
    present_first = any(item.name == tracked_name for item in steps[0])
    present_middle = any(item.name == tracked_name for item in steps[1])
    present_last = any(item.name == tracked_name for item in steps[2])
    if not present_first:
        raise ValueError(f"NDI source {tracked_name!r} must be present in step 1 (before dropout).")
    if present_middle:
        raise ValueError(
            f"NDI source {tracked_name!r} must be absent in step 2; no intervening "
            "absence means reappearance cannot be trusted."
        )
    if not present_last:
        raise ValueError(
            f"NDI source {tracked_name!r} must be present again in step 3 (reappeared)."
        )
    return {"tracked_name": tracked_name, "reappeared": True}


def validate_ndi_source_rename(
    before: Iterable[dict[str, Any]], after: Iterable[dict[str, Any]], *, stable_id: str
) -> dict[str, Any]:
    """Validate an NDI source renamed in the discovery list is tracked, not lost.

    A real NDI source's display ``name`` is operator-editable metadata; its
    network endpoint (``url_address``, e.g. ``192.168.1.101:5961``) is what
    stays fixed across a rename. ``stable_id`` is that url_address. This
    raises if the rename isn't tracked: either the stable id vanished from
    "after" (the renamed source was treated as lost) or a source with the
    "before" name reappears at a *different* url_address in "after" (the
    rename was mistaken for the old source disappearing and a new one
    showing up, rather than one source being renamed in place).
    """

    before_sources = validate_ndi_source_list(before)
    after_sources = validate_ndi_source_list(after)
    before_match = next((item for item in before_sources if item.url_address == stable_id), None)
    if before_match is None:
        raise ValueError(
            f"NDI source with stable id {stable_id!r} must be present in the 'before' "
            "list to prove a rename."
        )
    after_match = next((item for item in after_sources if item.url_address == stable_id), None)
    if after_match is None:
        raise ValueError(
            f"NDI source with stable id {stable_id!r} is missing from the 'after' list; "
            "the rename was not tracked -- the source was treated as lost."
        )
    if after_match.name == before_match.name:
        raise ValueError(
            f"NDI source rename fixture must change the display name at stable id "
            f"{stable_id!r}; it is still {before_match.name!r} in both lists."
        )
    stale_name_elsewhere = next(
        (
            item
            for item in after_sources
            if item.name == before_match.name and item.url_address != stable_id
        ),
        None,
    )
    if stale_name_elsewhere is not None:
        raise ValueError(
            f"NDI source rename fixture must not reintroduce the old name "
            f"{before_match.name!r} under a different url_address "
            f"({stale_name_elsewhere.url_address!r}); that would mean the rename was "
            "mistaken for the old source disappearing and a new one appearing."
        )
    return {
        "stable_id": stable_id,
        "old_name": before_match.name,
        "new_name": after_match.name,
        "renamed": True,
    }


def validate_capture_identity_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    devices = fixture.get("devices")
    if not isinstance(devices, list):
        raise ValueError("Capture fixture requires devices list.")
    normalized: list[dict[str, str]] = []
    for device in devices:
        if not isinstance(device, dict):
            raise ValueError("Capture fixture devices must be objects.")
        name = str(device.get("name") or "")
        device_class = str(device.get("class") or "")
        stable_id = str(device.get("stable_id") or "")
        if not name or not device_class or not stable_id:
            raise ValueError("Capture fixture devices require name, class, and stable_id.")
        normalized.append({"name": name, "class": device_class, "stable_id": stable_id})
    return {"devices": normalized, "device_count": len(normalized)}


# ---------------------------------------------------------------------------
# Audit item 14: Audio Mixer and Audio Device Layer.
#
# Scope: Allen & Heath SQ, Yamaha TF, Behringer U-Phoria, generic USB audio,
# and system audio. This region proves what is genuinely fixture-testable
# locally against a PUBLIC, VENDOR-PUBLISHED spec:
#   - audio topology declarations (mixer model, channel/route shape) parse
#     and reject malformed data;
#   - the topology non-claim (no live mixer control) is actively enforced,
#     not just asserted, by rejecting a fixture that smuggles in a control
#     surface;
#   - USB/system audio device presence, absence, sample-rate mismatch, and
#     sync/delay warnings reuse/extend the generic capture-identity fixture;
#   - Allen & Heath SQ-MIDI, per A&H's own published "SQ MIDI Protocol
#     Issue 5" PDF (MIDI-over-TCP port 51325, confirmed fetched 2026-07-05):
#     NRPN 4-message parameter writes (fader/mute/pan/mix-assign), the
#     documented Note On mute convention (velocity 0x7F on / 0x00 off), and
#     scene recall via Bank Select + Program Change, all validated against
#     the officially documented byte layout;
#   - the generic MIDI 1.0 wire format (status bytes, NRPN CC 99/98/6/38,
#     Program Change, Bank Select) that SQ-MIDI itself is built on.
#
# Honest-red by design, not theater:
#   - Yamaha TF: confirmed against Yamaha's own official TF Reference Manual
#     and product spec/FAQ pages (fetched 2026-07-05) that the TF series has
#     NO MIDI implementation at all -- no MIDI DIN port, no MIDI-over-network
#     port, and zero mentions of MIDI anywhere in the official manual.
#     Yamaha's own FAQ states TF "doesn't have any DAW control function."
#     The only network remote-control surface is a separate, non-MIDI,
#     plain-text TCP protocol ("RCP") that Yamaha has not published for TF;
#     it is known only through unofficial community reverse engineering
#     (e.g. github.com/BrenekH/yamaha-rcp-docs, bitfocus/companion). This
#     project builds nothing against it: Yamaha's EULA prohibits reverse
#     engineering, and a CivicCast feature/claim must trace to a primary,
#     vendor-published source. TF therefore gets topology/inventory-level
#     proof only in this region (validate_audio_topology_fixture /
#     validate_audio_control_not_claimed, mixer-model-agnostic); any TF
#     control surface is a documented vendor-documentation gap, not a fake
#     pass. The MIDI validators below apply to Allen & Heath SQ only.
#   - No claim that CivicCast has read live levels/mute state from a
#     powered-on physical mixer of any brand -- that requires a real console
#     capture session. See docs/ops/stage3-audio-mixer-device-layer.md.
# ---------------------------------------------------------------------------


class AudioTopologyFixture(BaseModel):
    """A documented audio-path declaration (topology only, no control claim)."""

    model_config = ConfigDict(extra="forbid")

    mixer_model: Annotated[str, Field(min_length=1, max_length=120)]
    channel_count: Annotated[int, Field(ge=1, le=256)]
    output_routes: list[Annotated[str, Field(min_length=1, max_length=120)]]


def validate_audio_topology_fixture(fixture: dict[str, Any]) -> AudioTopologyFixture:
    """Validate an audio-mixer topology declaration (presence, not control).

    Raises on a missing mixer model, a non-positive channel count, or an
    empty output-route list. This proves the topology data CivicCast records
    for a support bundle / device inventory has real shape; it never opens a
    control connection to the mixer.
    """

    mixer_model = fixture.get("mixer_model")
    channel_count = fixture.get("channel_count")
    output_routes = fixture.get("output_routes")
    if not isinstance(mixer_model, str) or not mixer_model:
        raise ValueError("Audio topology fixture requires a non-empty mixer_model.")
    if not isinstance(channel_count, int) or isinstance(channel_count, bool) or channel_count < 1:
        raise ValueError("Audio topology fixture requires a positive integer channel_count.")
    if not isinstance(output_routes, list) or not output_routes:
        raise ValueError("Audio topology fixture requires at least one output_route.")
    if not all(isinstance(route, str) and route for route in output_routes):
        raise ValueError("Audio topology output_routes must be non-empty strings.")
    return AudioTopologyFixture(
        mixer_model=mixer_model, channel_count=channel_count, output_routes=output_routes
    )


_AUDIO_CONTROL_FIELD_NAMES = frozenset(
    {"command_endpoint", "control_url", "auth", "credentials", "api_key", "midi_port", "password"}
)


def validate_audio_control_not_claimed(fixture: dict[str, Any]) -> dict[str, Any]:
    """Validate a topology fixture makes no live-control claim.

    Raises if ``fixture`` is missing valid topology data, or if it carries
    any control-capable field (a command endpoint, credential, or MIDI port).
    This is what makes "no mixer control is claimed" a checked contract
    instead of a comment: a fixture that quietly grows a control surface
    fails this check instead of silently starting to claim more than Stage
    3 proves.
    """

    topology = validate_audio_topology_fixture(fixture)
    leaked = sorted(_AUDIO_CONTROL_FIELD_NAMES.intersection(fixture))
    if leaked:
        raise ValueError(
            f"Audio topology fixture must not carry control fields; found: {', '.join(leaked)}."
        )
    return {"mixer_model": topology.mixer_model, "control_claimed": False}


def _require_usb_audio_present(fixture: dict[str, Any]) -> dict[str, Any]:
    """Raise unless a ``usb-audio`` class device is present in a capture fixture.

    Reuses ``validate_capture_identity_fixture`` (already generic across
    capture device classes) and adds the audio-specific presence check on
    top of it, instead of duplicating device-list parsing.
    """

    parsed = validate_capture_identity_fixture(fixture)
    audio_devices = [device for device in parsed["devices"] if device["class"] == "usb-audio"]
    if not audio_devices:
        raise ValueError("No usb-audio class device present in the capture fixture.")
    return {"devices": audio_devices, "device_count": len(audio_devices)}


def validate_usb_audio_sample_rate(fixture: dict[str, Any], *, expected_hz: int) -> dict[str, Any]:
    """Validate a USB/system audio device fixture's sample rate against the expected rate.

    Raises if the fixture's sample_rate_hz is missing, non-numeric, or does
    not match ``expected_hz`` — the mismatch CivicCast must surface as an
    operator-visible warning rather than silently resample or ignore.
    """

    device_name = fixture.get("name")
    sample_rate = fixture.get("sample_rate_hz")
    if not isinstance(device_name, str) or not device_name:
        raise ValueError("USB audio sample-rate fixture requires a device name.")
    if not isinstance(sample_rate, int) or isinstance(sample_rate, bool) or sample_rate <= 0:
        raise ValueError(
            "USB audio sample-rate fixture requires a positive integer sample_rate_hz."
        )
    if sample_rate != expected_hz:
        raise ValueError(
            f"USB audio device {device_name!r} sample rate {sample_rate} Hz does not match "
            f"the expected {expected_hz} Hz."
        )
    return {"name": device_name, "sample_rate_hz": sample_rate}


def validate_usb_audio_sync_delay(
    fixture: dict[str, Any], *, max_delay_ms: float
) -> dict[str, Any]:
    """Validate a USB/system audio device fixture's A/V delay against a ceiling.

    Raises if delay_ms is missing/non-numeric or at/over ``max_delay_ms`` —
    the threshold at which CivicCast must surface an audio-sync warning to
    the operator instead of staying silent about drift.
    """

    device_name = fixture.get("name")
    delay_ms = fixture.get("delay_ms")
    if not isinstance(device_name, str) or not device_name:
        raise ValueError("USB audio sync fixture requires a device name.")
    if not isinstance(delay_ms, int | float) or isinstance(delay_ms, bool) or delay_ms < 0:
        raise ValueError("USB audio sync fixture requires a non-negative numeric delay_ms.")
    if delay_ms >= max_delay_ms:
        raise ValueError(
            f"USB audio device {device_name!r} delay {delay_ms}ms is at/over the "
            f"{max_delay_ms}ms sync-warning ceiling."
        )
    return {"name": device_name, "delay_ms": delay_ms}


def _midi_status_kind(status_byte: int) -> str:
    # Only the status types the SQ MIDI Protocol uses for the messages we
    # validate: Control Change (NRPN faders/mutes) and Program Change (scene
    # recall). Note On/Off (Soft Keys) is documented but not a required check.
    high_nibble = status_byte & 0xF0
    if high_nibble == 0xB0:
        return "control-change"
    if high_nibble == 0xC0:
        return "program-change"
    raise ValueError(f"Unsupported MIDI status byte for this contract: 0x{status_byte:02X}.")


def validate_midi_nrpn_message(message_hexes: Iterable[str], *, channel: int) -> dict[str, Any]:
    """Validate a 4-frame MIDI NRPN Control Change sequence (MIDI 1.0 spec).

    A parameter-change NRPN write is four Control Change messages on the
    same channel, in order: CC 99 (NRPN MSB), CC 98 (NRPN LSB), CC 6 (Data
    Entry MSB), CC 38 (Data Entry LSB) -- the exact sequence Allen & Heath's
    SQ MIDI Protocol Issue 5 documents for fader/mute/pan/mix-assign writes.
    Raises if the sequence is the wrong length, out of order, on mismatched
    channels, or carries a data byte outside 0-127. This validates the wire
    format; it does not assert what a specific NRPN number maps to on an SQ
    console. Yamaha TF has no MIDI implementation at all, so this validator
    does not apply to TF.
    """

    frames = [_hex_bytes(item) for item in message_hexes]
    if len(frames) != 4:
        raise ValueError(f"MIDI NRPN write requires exactly 4 CC frames, got {len(frames)}.")
    expected_controllers = (99, 98, 6, 38)
    parsed: list[tuple[int, int]] = []
    for index, (frame, expected_cc) in enumerate(zip(frames, expected_controllers, strict=True)):
        if len(frame) != 3:
            raise ValueError(f"MIDI CC frame {index} must be 3 bytes, got {len(frame)}.")
        status_byte, controller, value = frame
        if _midi_status_kind(status_byte) != "control-change":
            raise ValueError(f"MIDI NRPN frame {index} must be a Control Change status byte.")
        message_channel = status_byte & 0x0F
        if message_channel != channel:
            raise ValueError(
                f"MIDI NRPN frame {index} channel {message_channel} does not match expected {channel}."
            )
        if controller != expected_cc:
            raise ValueError(
                f"MIDI NRPN frame {index} must use CC {expected_cc}, got CC {controller}."
            )
        if not (0 <= value <= 127):
            raise ValueError(f"MIDI NRPN frame {index} data byte must be in the 0-127 range.")
        parsed.append((controller, value))

    nrpn_number = (parsed[0][1] << 7) | parsed[1][1]
    data_value = (parsed[2][1] << 7) | parsed[3][1]
    return {"channel": channel, "nrpn_number": nrpn_number, "data_value": data_value}


def validate_sq_midi_mute_message(message_hexes: Iterable[str], *, channel: int) -> dict[str, Any]:
    """Validate an Allen & Heath SQ-MIDI mute message per SQ MIDI Protocol Issue 5, page 11, s3.3.

    The SQ documents mute as a 4-frame NRPN write (the same shape as a level
    write), NOT a Note On -- Note On/Off is for Soft Keys / MIDI strip keys
    (Issue 5 pages 8, 10). The documented mute frames are:
        BN 63 MB   (NRPN MSB = channel parameter)
        BN 62 LB   (NRPN LSB = channel parameter)
        BN 06 00   (Data Entry MSB = 0x00)
        BN 26 01   (Data Entry LSB = 0x01 -> mute ON)
    with the final frame BN 26 00 for mute OFF.

    Reuses ``validate_midi_nrpn_message`` for the wire format, then enforces
    the mute-specific data value: Data Entry MSB must be 0x00 and Data Entry
    LSB must be 0x01 (on) or 0x00 (off) -- i.e. the combined data value is 1
    (on) or 0 (off). Any other data value is not a documented mute message.
    """

    parsed = validate_midi_nrpn_message(message_hexes, channel=channel)
    data_value = parsed["data_value"]
    if data_value not in (0, 1):
        raise ValueError(
            "SQ-MIDI mute data value must be 1 (Data Entry MSB 0x00, LSB 0x01 = on) or "
            f"0 (LSB 0x00 = off), got {data_value}."
        )
    return {
        "channel": channel,
        "nrpn_number": parsed["nrpn_number"],
        "muted": data_value == 1,
    }


def validate_midi_scene_recall(
    bank_change_hex: str, program_change_hex: str, *, channel: int
) -> dict[str, Any]:
    """Validate an SQ-MIDI scene recall per SQ MIDI Protocol Issue 5, page 9, s3.1.

    The SQ documents a scene change as a bank change (Control Change 0, "Bank
    MSB") followed by a Program Change -- exactly TWO messages, with NO CC 32
    (Bank LSB):
        BN 00 BK   (bank change, CC 0, BK = 0/1/2 selecting the scene range)
        CN PG      (program change, PG = 0..127 selecting a scene in range)
    Example (Scene 7, Ch1): ``B0 00 00 C0 06``.

    Raises if the bank change is not a single CC 0 message, if a spurious
    CC 32 (or any other controller) is supplied, on a channel mismatch
    between the two messages, or on a Program Change status byte outside
    0xC0-0xCF. This validator does not apply to Yamaha TF (no MIDI at all).
    """

    bank_frame = _hex_bytes(bank_change_hex)
    if len(bank_frame) != 3:
        raise ValueError(
            f"SQ scene bank change must be a single 3-byte CC message, got {len(bank_frame)} bytes."
        )
    bank_status, bank_controller, bank_value = bank_frame
    if _midi_status_kind(bank_status) != "control-change":
        raise ValueError("SQ scene bank change must be a Control Change status byte.")
    bank_channel = bank_status & 0x0F
    if bank_channel != channel:
        raise ValueError(
            f"SQ scene bank change channel {bank_channel} does not match expected {channel}."
        )
    if bank_controller != 0:
        raise ValueError(
            f"SQ scene bank change must use CC 0 (Bank MSB) only; got CC {bank_controller} "
            "(the SQ spec does not use CC 32 for scene recall)."
        )
    if not (0 <= bank_value <= 127):
        raise ValueError("SQ scene bank change data byte must be in the 0-127 range.")

    program_change = _hex_bytes(program_change_hex)
    if len(program_change) != 2:
        raise ValueError(f"MIDI Program Change must be 2 bytes, got {len(program_change)}.")
    pc_status, program_number = program_change
    if _midi_status_kind(pc_status) != "program-change":
        raise ValueError("MIDI scene recall requires a Program Change status byte (0xC0-0xCF).")
    pc_channel = pc_status & 0x0F
    if pc_channel != channel:
        raise ValueError(
            f"MIDI Program Change channel {pc_channel} does not match expected {channel}."
        )
    if not (0 <= program_number <= 127):
        raise ValueError("MIDI Program Change program number must be in the 0-127 range.")
    return {"channel": channel, "bank": bank_value, "program_number": program_number}


def build_stage45_proofs_for_profile(
    profile: LabTopologyProfile,
    *,
    probe_real_software: bool = False,
    vmix_url: str = "http://127.0.0.1:8088/api/",
    obs_host: str = "127.0.0.1",
    obs_port: int = 4455,
    timeout_seconds: float = 1.5,
) -> list[LabEvent]:
    """Return executable Stage 4-5 evidence events for one profile."""

    proofs_by_check = _run_executable_fixtures()
    events: list[LabEvent] = []
    for device in profile.devices:
        for check_id in device.required_checks:
            proof = proofs_by_check.get(check_id)
            if proof is not None:
                events.append(_event_from_proof(profile.profile_id, device, proof))
            else:
                events.append(_unexecuted_event(profile.profile_id, device, check_id))

        if probe_real_software and device.device_class in {"obs", "vmix"}:
            events.append(
                _software_probe_event(
                    profile.profile_id,
                    device,
                    vmix_url=vmix_url,
                    obs_host=obs_host,
                    obs_port=obs_port,
                    timeout_seconds=timeout_seconds,
                )
            )
    return events


def _run_executable_fixtures() -> dict[str, Stage45Proof]:
    vmix_state = parse_vmix_status_xml(_VMIX_STATUS_XML)
    obs_state = validate_obs_websocket_5_frames(json.loads(_OBS_WS_FRAMES_JSON))
    visca = validate_visca_udp_exchange("81 01 04 3F 02 01 FF", ["90 41 FF", "90 51 FF"])
    atem = validate_atem_state_fixture(_ATEM_STATE_FIXTURE)
    usb_capture = validate_capture_identity_fixture(_USB_CAPTURE_FIXTURE)
    recording_catalog = RecordingInputPresetCatalog(
        [
            RecordingInputPreset(
                preset_id="lpm-decklink-channel-2",
                label="LPM DeckLink channel 2",
                source_kind="sdi",
                backend="decklink",
                device_name="DeckLink Duo 2 (2)",
                format_code="Hp60",
            ),
            RecordingInputPreset(
                preset_id="lpm-cam-link-hdmi",
                label="LPM Cam Link HDMI",
                source_kind="hdmi",
                backend="dshow",
                device_name="Cam Link HDMI",
                audio_device_name="Digital Audio Interface",
            ),
        ],
        ffmpeg_runner=lambda _args: None,
    )
    decklink_recording_args = recording_catalog.resolve_args(
        RecordingSource(kind="sdi", input_id="lpm-decklink-channel-2")
    )
    dshow_recording_args = recording_catalog.resolve_args(
        RecordingSource(kind="hdmi", input_id="lpm-cam-link-hdmi")
    )
    atem_absent = validate_atem_absent_fixture(None)
    obs_disabled = validate_obs_websocket_disabled("connection refused")
    obs_disabled_rejects_success_shaped_frames = _expect_value_error(
        lambda: validate_obs_websocket_disabled(
            "connection refused", json.loads(_OBS_WS_FRAMES_JSON)
        ),
        match="contradicts itself",
    )
    obs_wrong_password = _expect_value_error(
        lambda: validate_obs_websocket_wrong_password(
            password="wrong-password",  # noqa: S106 - fixture literal, not a real secret.
            challenge="challenge-value",
            salt="salt-value",
            server_expected_auth=_obs_websocket_auth(
                password="correct-password",  # noqa: S106 - fixture literal, not a real secret.
                challenge="challenge-value",
                salt="salt-value",
            ),
        ),
        match="authentication digest does not match",
    )
    obs_protocol_mismatch = _expect_value_error(
        lambda: validate_obs_websocket_protocol_mismatch(
            [{"op": 0, "d": {"obsWebSocketVersion": "5.0.0", "rpcVersion": 1}}],
            required_rpc_version=2,
        ),
        match="older than the required",
    )
    obs_source_missing = _expect_value_error(
        lambda: validate_obs_scene_item_missing(
            [
                {"op": 0, "d": {"obsWebSocketVersion": "5.5.4", "rpcVersion": 1}},
                {
                    "op": 6,
                    "d": {"requestType": "GetSceneItemList", "requestId": "req-scenes"},
                },
                {
                    "op": 7,
                    "d": {
                        "requestId": "req-scenes",
                        "requestStatus": {"result": True, "code": 100},
                        "responseData": {"sceneItemNames": ["Camera 1", "Lower Third"]},
                    },
                },
            ],
            required_source_name="Camera 2",
        ),
        match="is not present in scene state",
    )
    obs_source_removed = _expect_value_error(
        lambda: validate_obs_scene_item_missing(
            [
                {"op": 0, "d": {"obsWebSocketVersion": "5.5.4", "rpcVersion": 1}},
                {
                    "op": 6,
                    "d": {"requestType": "GetSceneItemList", "requestId": "req-scenes"},
                },
                {
                    "op": 7,
                    "d": {
                        "requestId": "req-scenes",
                        "requestStatus": {"result": True, "code": 100},
                        "responseData": {"sceneItemNames": ["Lower Third"]},
                    },
                },
            ],
            required_source_name="Camera 1",
        ),
        match="is not present in scene state",
    )
    obs_restart = validate_obs_restart_recovery(
        [{"op": 0, "d": {"obsWebSocketVersion": "5.5.4", "rpcVersion": 1}}],
        [{"op": 0, "d": {"obsWebSocketVersion": "5.5.4", "rpcVersion": 1}}],
    )
    vmix_input_select = validate_vmix_input_select(
        parse_vmix_status_xml(_VMIX_STATUS_XML),
        parse_vmix_status_xml(_VMIX_STATUS_XML_AFTER_SELECT),
        requested_number=2,
    )
    tsr_sidecar_restart = validate_tsr_sidecar_restart(
        _TSR_PRE_RESTART_STATE, _TSR_RESTART_EVENT, _TSR_POST_RESTART_STATE
    )
    ndi_source_present = validate_ndi_source_present(
        _NDI_SOURCES_PRESENT, expected_name=_NDI_TRACKED_SOURCE_NAME
    )
    ndi_source_disappears = validate_ndi_source_disappears(
        _NDI_SOURCES_PRESENT, _NDI_SOURCES_ABSENT, tracked_name=_NDI_TRACKED_SOURCE_NAME
    )
    ndi_source_reappears = validate_ndi_source_reappears(
        [_NDI_SOURCES_PRESENT, _NDI_SOURCES_ABSENT, _NDI_SOURCES_PRESENT],
        tracked_name=_NDI_TRACKED_SOURCE_NAME,
    )
    ndi_source_rename = validate_ndi_source_rename(
        _NDI_SOURCES_PRESENT, _NDI_SOURCES_RENAMED, stable_id=_NDI_TRACKED_SOURCE_URL
    )
    ndi_source_rename_rejects_stable_id_change = _expect_value_error(
        lambda: validate_ndi_source_rename(
            _NDI_SOURCES_PRESENT,
            _NDI_SOURCES_RENAME_LOST_STABLE_ID,
            stable_id=_NDI_TRACKED_SOURCE_URL,
        ),
        match="was not tracked -- the source was treated as lost",
    )
    vmix_api_disabled = validate_vmix_api_disabled("connection refused")
    vmix_api_disabled_rejects_success_shaped_body = _expect_value_error(
        lambda: validate_vmix_api_disabled("connection refused", _VMIX_STATUS_XML),
        match="contradicts itself",
    )
    vmix_auth = _expect_value_error(
        lambda: validate_vmix_auth_response(status_code=200, www_authenticate_header=None),
        match="requires a 401 or 403 status",
    )
    vmix_input_identity_drift = _expect_value_error(
        lambda: validate_vmix_input_identity(
            parse_vmix_status_xml(_VMIX_STATUS_XML),
            parse_vmix_status_xml(_VMIX_STATUS_XML_INPUT_IDENTITY_DRIFTED),
            input_number=2,
        ),
        match="changed key/number before live fire",
    )
    vmix_source_removed_dry_run = _expect_value_error(
        lambda: validate_vmix_input_removed(
            parse_vmix_status_xml(_VMIX_STATUS_XML),
            parse_vmix_status_xml(_VMIX_STATUS_XML_INPUT_2_REMOVED),
            required_key="vmix-cam-2",
        ),
        match="no longer present at live fire",
    )
    vmix_usb_capture_identity_drift = validate_vmix_capture_identity(
        parse_vmix_status_xml(_VMIX_STATUS_XML),
        parse_vmix_status_xml(_VMIX_STATUS_XML),
        _USB_CAPTURE_FIXTURE,
        _USB_CAPTURE_FIXTURE_SWAPPED,
        input_number=2,
        device_name="Cam Link HDMI",
    )
    vmix_resource_ceiling = validate_vmix_resource_ceiling(
        {"cpu_percent": 42.0, "dropped_frames": 0}
    )
    vmix_record_state_drift = _expect_value_error(
        lambda: parse_vmix_status_xml(_VMIX_STATUS_XML_BAD_RECORDING),
        match="boolean",
    )
    vmix_stream_state_drift = _expect_value_error(
        lambda: parse_vmix_status_xml(_VMIX_STATUS_XML_BAD_STREAMING),
        match="boolean",
    )
    audio_topology = validate_audio_topology_fixture(_AUDIO_TOPOLOGY_FIXTURE)
    audio_topology_drift = _expect_value_error(
        lambda: validate_audio_topology_fixture(_AUDIO_TOPOLOGY_FIXTURE_MISSING_ROUTES),
        match="at least one output_route",
    )
    audio_control_not_claimed = validate_audio_control_not_claimed(_AUDIO_TOPOLOGY_FIXTURE)
    audio_control_leak_rejected = _expect_value_error(
        lambda: validate_audio_control_not_claimed(_AUDIO_TOPOLOGY_FIXTURE_WITH_CONTROL_LEAK),
        match="must not carry control fields",
    )
    usb_audio_present = _require_usb_audio_present(_USB_CAPTURE_FIXTURE)
    usb_audio_absent = _expect_value_error(
        lambda: _require_usb_audio_present(_USB_CAPTURE_FIXTURE_NO_AUDIO),
        match="No usb-audio class device present",
    )
    usb_audio_sample_rate_ok = validate_usb_audio_sample_rate(
        _USB_AUDIO_SAMPLE_RATE_FIXTURE, expected_hz=48_000
    )
    usb_audio_sample_rate_mismatch = _expect_value_error(
        lambda: validate_usb_audio_sample_rate(
            _USB_AUDIO_SAMPLE_RATE_FIXTURE_MISMATCHED, expected_hz=48_000
        ),
        match="does not match the expected 48000 Hz",
    )
    usb_audio_sync_ok = validate_usb_audio_sync_delay(_USB_AUDIO_SYNC_FIXTURE, max_delay_ms=40.0)
    usb_audio_sync_warning = _expect_value_error(
        lambda: validate_usb_audio_sync_delay(_USB_AUDIO_SYNC_FIXTURE_DELAYED, max_delay_ms=40.0),
        match="sync-warning ceiling",
    )
    midi_nrpn_fader = validate_midi_nrpn_message(_MIDI_NRPN_FADER_HEX, channel=0)
    midi_nrpn_out_of_order = _expect_value_error(
        lambda: validate_midi_nrpn_message(_MIDI_NRPN_FADER_HEX_OUT_OF_ORDER, channel=0),
        match="must use CC 98, got CC 6",
    )
    sq_midi_mute = validate_sq_midi_mute_message(_SQ_MIDI_MUTE_ON_HEX, channel=0)
    # The OLD wrong format (a Note On message) must now be rejected: a 3-byte
    # Note On is not the documented 4-frame NRPN mute.
    sq_midi_mute_rejects_note_on = _expect_value_error(
        lambda: validate_sq_midi_mute_message([_SQ_SOFTKEY_NOTE_ON_HEX], channel=0),
        match="requires exactly 4 CC frames",
    )
    midi_scene_recall = validate_midi_scene_recall(
        _MIDI_SCENE_BANK_HEX, _MIDI_PROGRAM_CHANGE_HEX, channel=0
    )
    # The OLD wrong format (a spurious CC 32 Bank LSB) must now be rejected.
    midi_scene_recall_rejects_cc32 = _expect_value_error(
        lambda: validate_midi_scene_recall(
            _MIDI_SCENE_BANK_LSB_CC32_HEX, _MIDI_PROGRAM_CHANGE_HEX, channel=0
        ),
        match="does not use CC 32 for scene recall",
    )

    return {
        "vmix-status-xml": Stage45Proof(
            check_id="vmix-status-xml",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="vMix status XML required fields parse and cross-reference active/preview inputs.",
            observed=(
                f"Parsed vMix {vmix_state.version} with {len(vmix_state.inputs)} inputs; "
                f"active={vmix_state.active}, preview={vmix_state.preview}."
            ),
            details={"references": [REFERENCE_URLS["vmix-api"]], **vmix_state.model_dump()},
        ),
        "vmix-xml-required-field-drift": Stage45Proof(
            check_id="vmix-xml-required-field-drift",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="vMix XML missing required fields is rejected as schema drift.",
            observed=_expect_value_error(
                lambda: parse_vmix_status_xml(_VMIX_DRIFT_XML),
                match="vMix status XML is missing",
            ),
            details={"references": [REFERENCE_URLS["vmix-api"]]},
        ),
        "vmix-usb-capture-input-select": Stage45Proof(
            check_id="vmix-usb-capture-input-select",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="Selecting the USB capture input preserves its key, title, and becomes active.",
            observed=(
                f"Before/after vMix status XML confirms input {vmix_input_select['input_number']} "
                f"({vmix_input_select['title']!r}, key={vmix_input_select['key']!r}) became active "
                "with no identity mutation."
            ),
            details={**vmix_input_select, "references": [REFERENCE_URLS["vmix-api"]]},
        ),
        "vmix-record-state": Stage45Proof(
            check_id="vmix-record-state",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="vMix recording state is parsed from status XML and rejects non-boolean drift.",
            observed=(
                f"Recording parsed as {vmix_state.recording}; a non-boolean <recording> "
                f"value is rejected ({vmix_record_state_drift})."
            ),
            details={"recording": vmix_state.recording, "references": [REFERENCE_URLS["vmix-api"]]},
        ),
        "vmix-stream-state": Stage45Proof(
            check_id="vmix-stream-state",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="vMix streaming state is parsed from status XML and rejects non-boolean drift.",
            observed=(
                f"Streaming parsed as {vmix_state.streaming}; a non-boolean <streaming> "
                f"value is rejected ({vmix_stream_state_drift})."
            ),
            details={"streaming": vmix_state.streaming, "references": [REFERENCE_URLS["vmix-api"]]},
        ),
        "audio-topology-present": Stage45Proof(
            check_id="audio-topology-present",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="An audio-mixer topology declaration parses and rejects malformed/incomplete data.",
            observed=(
                f"Parsed topology for {audio_topology.mixer_model} with "
                f"{audio_topology.channel_count} channels and {len(audio_topology.output_routes)} "
                f"output route(s); a fixture with no output routes is rejected ({audio_topology_drift})."
            ),
            not_claimed=(
                "This proves only that the topology declaration shape is validated. It does not "
                "claim a live control connection, level metering, or station-device evidence from "
                "the physical mixer."
            ),
            details={"mixer_model": audio_topology.mixer_model},
        ),
        "audio-control-not-claimed": Stage45Proof(
            check_id="audio-control-not-claimed",
            status="not-applicable",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="The audio topology non-claim is enforced: a fixture cannot smuggle in a control surface.",
            observed=(
                f"{audio_control_not_claimed['mixer_model']} topology fixture carries no "
                f"command endpoint, credential, or MIDI port; a fixture that adds one is "
                f"rejected ({audio_control_leak_rejected})."
            ),
            not_claimed=(
                "No mixer command, state subscription, live level/mute read, or station-device "
                "evidence is claimed. Live control of a physical SQ or TF console requires a real "
                "console capture session and stays honest-red; see "
                "docs/ops/stage3-audio-mixer-device-layer.md."
            ),
            details={},
        ),
        "usb-audio-present": Stage45Proof(
            check_id="usb-audio-present",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="A usb-audio class device is discoverable in the capture-identity fixture.",
            observed=f"Found {usb_audio_present['device_count']} usb-audio class device(s) in the fixture.",
            details=usb_audio_present,
        ),
        "usb-audio-absent": Stage45Proof(
            check_id="usb-audio-absent",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="A capture fixture with no usb-audio class device fails closed instead of assuming presence.",
            observed=usb_audio_absent,
            details={},
        ),
        "usb-audio-sample-rate-mismatch": Stage45Proof(
            check_id="usb-audio-sample-rate-mismatch",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="A USB/system audio sample rate that doesn't match the expected rate is rejected.",
            observed=(
                f"{usb_audio_sample_rate_ok['name']} at {usb_audio_sample_rate_ok['sample_rate_hz']} Hz "
                f"matched the expected rate; a mismatched sample rate is rejected "
                f"({usb_audio_sample_rate_mismatch})."
            ),
            details=usb_audio_sample_rate_ok,
        ),
        "usb-audio-sync-warning": Stage45Proof(
            check_id="usb-audio-sync-warning",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="A USB/system audio A/V delay at or over the sync-warning ceiling is rejected.",
            observed=(
                f"{usb_audio_sync_ok['name']} delay {usb_audio_sync_ok['delay_ms']}ms was under "
                f"the ceiling; a delay at/over the ceiling is rejected ({usb_audio_sync_warning})."
            ),
            details=usb_audio_sync_ok,
        ),
        "audio-midi-nrpn-message": Stage45Proof(
            check_id="audio-midi-nrpn-message",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim=(
                "A 4-frame MIDI NRPN Control Change write -- the parameter-control message shape "
                "Allen & Heath's published SQ MIDI Protocol uses for fader-level writes -- parses "
                "in order and rejects an out-of-order frame."
            ),
            observed=(
                f"Parsed NRPN number {midi_nrpn_fader['nrpn_number']} data value "
                f"{midi_nrpn_fader['data_value']} on channel {midi_nrpn_fader['channel']}; an "
                f"out-of-order CC sequence is rejected ({midi_nrpn_out_of_order})."
            ),
            not_claimed=(
                "Validates the generic MIDI NRPN wire format (CC 99/98/6/38) that the SQ MIDI "
                "Protocol spec documents. Does not assert a specific SQ fader's NRPN number, and "
                "does not claim a live connection to a powered-on mixer. Yamaha TF has no MIDI "
                "implementation at all (see audio-midi-scene-recall not_claimed for detail)."
            ),
            details={
                "references": [
                    REFERENCE_URLS["midi-1-0-spec"],
                    REFERENCE_URLS["ah-sq-midi-protocol"],
                ],
                **midi_nrpn_fader,
            },
        ),
        "audio-sq-midi-mute": Stage45Proof(
            check_id="audio-sq-midi-mute",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim=(
                "An Allen & Heath SQ-MIDI mute message -- the 4-frame NRPN write documented in "
                "SQ MIDI Protocol Issue 5 page 11 (BN 63/62/06/26, Data Entry LSB 01=on / 00=off) "
                "-- parses, and a Note On (the SQ's Soft Key format, not a mute) is rejected."
            ),
            observed=(
                f"Parsed mute={sq_midi_mute['muted']} on channel {sq_midi_mute['channel']} "
                f"(NRPN {sq_midi_mute['nrpn_number']}); a Note On message is rejected as not a "
                f"4-frame NRPN mute ({sq_midi_mute_rejects_note_on})."
            ),
            not_claimed=(
                "Validates the SQ MIDI Protocol Issue 5 mute message shape (page 11, s3.3). The "
                "channel -> NRPN parameter-number mapping is a lab fixture, not read from a live "
                "console, and no live connection to a powered-on SQ mixer is claimed."
            ),
            details={"references": [REFERENCE_URLS["ah-sq-midi-protocol"]], **sq_midi_mute},
        ),
        "audio-midi-scene-recall": Stage45Proof(
            check_id="audio-midi-scene-recall",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim=(
                "An SQ-MIDI scene recall -- bank change (CC 0) + Program Change, exactly two "
                "messages with NO CC 32, per SQ MIDI Protocol Issue 5 page 9 -- parses, and a "
                "spurious CC 32 Bank LSB is rejected."
            ),
            observed=(
                f"Parsed bank {midi_scene_recall['bank']} program "
                f"{midi_scene_recall['program_number']} on channel {midi_scene_recall['channel']}; "
                f"a spurious CC 32 Bank LSB is rejected ({midi_scene_recall_rejects_cc32})."
            ),
            not_claimed=(
                "Validates the SQ MIDI Protocol Issue 5 scene-recall sequence (page 9, s3.1: CC 0 "
                "bank change + Program Change) only; no live connection to a powered-on mixer is "
                "claimed. Does NOT apply to Yamaha TF: Yamaha's own TF Reference Manual and FAQ "
                "confirm TF has no MIDI implementation at all -- its only remote surface is the "
                "unpublished, reverse-engineered non-MIDI 'RCP' protocol, which CivicCast will "
                "not build against. TF stays honest-red at topology level only. See "
                "docs/ops/stage3-audio-mixer-device-layer.md."
            ),
            details={
                "references": [
                    REFERENCE_URLS["midi-1-0-spec"],
                    REFERENCE_URLS["ah-sq-midi-protocol"],
                ],
                **midi_scene_recall,
            },
        ),
        "vmix-api-disabled": Stage45Proof(
            check_id="vmix-api-disabled",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="vMix Web Controller disabled/unreachable fails closed instead of hanging.",
            observed=(
                f"vMix API unreachable ({vmix_api_disabled['reason']}); status XML parsing "
                "confirmed no live response can be mistaken for a disabled controller "
                f"({vmix_api_disabled_rejects_success_shaped_body})."
            ),
            details={"references": [REFERENCE_URLS["vmix-api"]], **vmix_api_disabled},
        ),
        "vmix-auth-required-or-wrong": Stage45Proof(
            check_id="vmix-auth-required-or-wrong",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="vMix auth-required/wrong-credentials responses fail closed.",
            observed=vmix_auth,
            details={"references": [REFERENCE_URLS["vmix-api"]]},
        ),
        "vmix-input-identity-drift": Stage45Proof(
            check_id="vmix-input-identity-drift",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="A rehearsed input's identity changing before live fire invalidates the dry run.",
            observed=vmix_input_identity_drift,
            details={"references": [REFERENCE_URLS["vmix-api"]]},
        ),
        "vmix-source-removed-dry-run": Stage45Proof(
            check_id="vmix-source-removed-dry-run",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="An input used in dry-run that's removed before live fire refuses the stale dry-run.",
            observed=vmix_source_removed_dry_run,
            details={"references": [REFERENCE_URLS["vmix-api"]]},
        ),
        "vmix-usb-capture-identity-drift": Stage45Proof(
            check_id="vmix-usb-capture-identity-drift",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="A swapped USB capture device is caught even when its vMix input slot didn't change.",
            observed=(
                f"vMix input {vmix_usb_capture_identity_drift['input_number']} stayed at key "
                f"{vmix_usb_capture_identity_drift['vmix_key']!r} while device "
                f"{vmix_usb_capture_identity_drift['device_name']!r} stable_id changed "
                f"({vmix_usb_capture_identity_drift['before_stable_id']!r} -> "
                f"{vmix_usb_capture_identity_drift['after_stable_id']!r})."
            ),
            details=vmix_usb_capture_identity_drift,
        ),
        "vmix-laptop-resource-ceiling": Stage45Proof(
            check_id="vmix-laptop-resource-ceiling",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="A perf-counter sample over the CPU/dropped-frame ceiling is classified as over-ceiling.",
            observed=(
                f"Sample cpu_percent={vmix_resource_ceiling['cpu_percent']}, "
                f"dropped_frames={vmix_resource_ceiling['dropped_frames']} classified under ceiling; "
                "an over-threshold sample raises. Live sampling from a running vMix under real load "
                "is not proven here (lab-only, hardware-gated)."
            ),
            not_claimed=(
                "This proves only the classifier logic on a fixture sample. It does not prove "
                "CivicCast reads real CPU/dropped-frame counters from a running vMix under load; "
                "that requires a live lab session and stays honest-red."
            ),
            details=vmix_resource_ceiling,
        ),
        "obs-websocket-5-contract": Stage45Proof(
            check_id="obs-websocket-5-contract",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="OBS websocket 5.x frames validate Hello, requestId correlation, and response status.",
            observed=(
                f"OBS websocket {obs_state.obs_websocket_version} rpc {obs_state.rpc_version} "
                f"validated {len(obs_state.request_ids)} request/response pair(s)."
            ),
            details={"references": [REFERENCE_URLS["obs-websocket"]], **obs_state.model_dump()},
        ),
        "obs-event-subscription": Stage45Proof(
            check_id="obs-event-subscription",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="OBS event subscription fixture publishes state changes.",
            observed=f"Observed events: {', '.join(obs_state.event_types)}.",
            details={
                "event_types": obs_state.event_types,
                "references": [REFERENCE_URLS["obs-websocket"]],
            },
        ),
        "obs-recording-state": Stage45Proof(
            check_id="obs-recording-state",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="OBS recording status response is correlated with its requestId.",
            observed="GetRecordStatus response matched requestId req-record-status with result=true.",
            details={
                "request_id": "req-record-status",
                "references": [REFERENCE_URLS["obs-websocket"]],
            },
        ),
        "visca-udp-52381-ack": Stage45Proof(
            check_id="visca-udp-52381-ack",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="VISCA UDP preset command exchange sees ACK and completion.",
            observed="VISCA command produced ACK and completion responses on the simulated exchange.",
            details={
                "port": 52381,
                "references": [REFERENCE_URLS["aida-visca"], REFERENCE_URLS["lpm-aida-wiki"]],
                **visca,
            },
        ),
        "visca-timeout": Stage45Proof(
            check_id="visca-timeout",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="VISCA timeout is distinct from command-not-executable.",
            observed=_expect_value_error(
                lambda: validate_visca_udp_exchange("81 01 04 3F 02 01 FF", []),
                match="requires at least one response",
            ),
            details={"references": [REFERENCE_URLS["aida-visca"], REFERENCE_URLS["lpm-aida-wiki"]]},
        ),
        "visca-command-not-executable": Stage45Proof(
            check_id="visca-command-not-executable",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="VISCA command-not-executable error is recognized as a failure response.",
            observed=_expect_value_error(
                lambda: validate_visca_udp_exchange("81 01 04 3F 02 01 FF", ["90 61 41 FF"]),
                match="returned an error response",
            ),
            details={"references": [REFERENCE_URLS["aida-visca"], REFERENCE_URLS["lpm-aida-wiki"]]},
        ),
        "atem-input-select": Stage45Proof(
            check_id="atem-input-select",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="ATEM program/preview input state fixture validates input-select contract.",
            observed="ATEM fixture validated program=1, preview=2, not in transition.",
            details={"references": [REFERENCE_URLS["blackmagic-atem-sdk"]], **atem},
        ),
        "atem-program-preview-state": Stage45Proof(
            check_id="atem-program-preview-state",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="ATEM program/preview state is available to dry-run decisions.",
            observed="Program/preview fixture state validated with distinct inputs.",
            details={"references": [REFERENCE_URLS["blackmagic-atem-sdk"]], **atem},
        ),
        "atem-sdk-version-mismatch": Stage45Proof(
            check_id="atem-sdk-version-mismatch",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="ATEM protocol version drift refuses fixture validation.",
            observed=_expect_value_error(
                lambda: validate_atem_state_fixture(
                    {"program_input": 1, "preview_input": 2, "in_transition": False}
                ),
                match="requires protocol_version",
            ),
            details={"references": [REFERENCE_URLS["blackmagic-atem-sdk"]]},
        ),
        "atem-absent": Stage45Proof(
            check_id="atem-absent",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="ATEM unavailable fails closed instead of being treated as a connected switcher.",
            observed="No ATEM state fixture was produced; state validation refused an empty fixture.",
            details={"references": [REFERENCE_URLS["blackmagic-atem-sdk"]], **atem_absent},
        ),
        "atem-busy-transition": Stage45Proof(
            check_id="atem-busy-transition",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="A transition already in progress refuses a duplicate non-idempotent fire.",
            observed=_expect_value_error(
                lambda: validate_atem_busy_transition(
                    {
                        "protocol_version": "atem-connection-3.x",
                        "program_input": 1,
                        "preview_input": 2,
                        "in_transition": True,
                    }
                ),
                match="transition is already in progress",
            ),
            details={"references": [REFERENCE_URLS["blackmagic-atem-sdk"]]},
        ),
        "usb-capture-present": Stage45Proof(
            check_id="usb-capture-present",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="Generic UVC capture identity can be represented without assuming Elgato brand.",
            observed=f"Validated {usb_capture['device_count']} capture fixture device(s).",
            details=usb_capture,
        ),
        "usb-capture-absent": Stage45Proof(
            check_id="usb-capture-absent",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="USB capture absence is represented as a controlled setup state.",
            observed="Absent capture fixture keeps portable/digitization path not ready without DeckLink assumptions.",
            details={"expected_absence": "usb capture missing"},
        ),
        "usb-capture-identity-preserved": Stage45Proof(
            check_id="usb-capture-identity-preserved",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="Capture identity uses stable IDs rather than mutable display names.",
            observed="Fixture contains stable_id for each capture device.",
            details=usb_capture,
        ),
        "recording-decklink-preset-argv": Stage45Proof(
            check_id="recording-decklink-preset-argv",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="The scheduled recorder resolves an LPM DeckLink SDI preset to exact FFmpeg arguments.",
            observed="DeckLink channel identity and format code reached the FFmpeg input boundary.",
            details={"ffmpeg_input_args": decklink_recording_args},
        ),
        "recording-dshow-preset-argv": Stage45Proof(
            check_id="recording-dshow-preset-argv",
            status="passed",
            proof_source="api-fixture",
            proof_level="api-contract-proven",
            claim="The scheduled recorder resolves an LPM USB HDMI preset to exact DirectShow arguments.",
            observed="Video and companion audio device names reached the FFmpeg input boundary.",
            details={"ffmpeg_input_args": dshow_recording_args},
        ),
        "ndi-source-present": Stage45Proof(
            check_id="ndi-source-present",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="Required NDI source is discoverable before PTZ control is allowed.",
            observed=(
                f"NDI discovery fixture lists {ndi_source_present['name']!r} at "
                f"{ndi_source_present['url_address']!r}."
            ),
            details=ndi_source_present,
        ),
        "ndi-source-disappears": Stage45Proof(
            check_id="ndi-source-disappears",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="NDI source disappearance marks state stale, not still-live.",
            observed=(
                f"Before/after discovery fixtures confirm {ndi_source_disappears['tracked_name']!r} "
                "dropped out of the source list."
            ),
            details=ndi_source_disappears,
        ),
        "ndi-source-reappears": Stage45Proof(
            check_id="ndi-source-reappears",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="NDI source reappearance is only trusted after an intervening absence.",
            observed=(
                f"Present/absent/present discovery sequence confirms "
                f"{ndi_source_reappears['tracked_name']!r} genuinely reappeared."
            ),
            details=ndi_source_reappears,
        ),
        "ndi-source-rename": Stage45Proof(
            check_id="ndi-source-rename",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="An NDI source renamed in the discovery list is tracked by its stable id.",
            observed=(
                f"Before/after discovery fixtures confirm stable id "
                f"{ndi_source_rename['stable_id']!r} was renamed from "
                f"{ndi_source_rename['old_name']!r} to {ndi_source_rename['new_name']!r} "
                "without being treated as lost; a fixture where the stable id changes "
                f"under the rename is rejected ({ndi_source_rename_rejects_stable_id_change})."
            ),
            details=ndi_source_rename,
        ),
        "decklink-driver-missing": _simulated(
            "decklink-driver-missing", "DeckLink driver absence is a controlled SDK-boundary state."
        ),
        "decklink-card-absent": _simulated(
            "decklink-card-absent",
            "DeckLink card absence is modeled without blocking portable profiles.",
        ),
        "decklink-channel-absent": _simulated(
            "decklink-channel-absent",
            "DeckLink channel 2/3/4 absence is modeled per fixed-studio inference.",
        ),
        "decklink-mode-mismatch": _simulated(
            "decklink-mode-mismatch", "DeckLink mode mismatch fails capture readiness."
        ),
        "decklink-signal-unlocked": _simulated(
            "decklink-signal-unlocked",
            "DeckLink signal-unlocked state remains station-device evidence pending.",
        ),
        "wifi-latency-injection": _simulated(
            "wifi-latency-injection", "WiFi latency injection marks portable egress degraded."
        ),
        "wifi-dropout": _simulated(
            "wifi-dropout", "WiFi dropout moves portable egress to stale/recoverable."
        ),
        "dns-failure": _simulated(
            "dns-failure", "DNS failure is a distinct portable egress dependency failure."
        ),
        "castr-unreachable": _simulated(
            "castr-unreachable", "Castr unreachable is represented as a destination failure."
        ),
        "egress-retry-recovery": _simulated(
            "egress-retry-recovery", "Transient egress failure recovers after retry."
        ),
        "tsr-sidecar-restart": Stage45Proof(
            check_id="tsr-sidecar-restart",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="Sidecar restart state is marked stale until a fresh heartbeat confirms recovery.",
            observed=(
                f"Post-restart fetch at {tsr_sidecar_restart['recovered_fetched_at_unix']} "
                f"(after restart at {tsr_sidecar_restart['restarted_at_unix']}) confirmed recovery; "
                "a fixture that skips the fresh fetch is rejected."
            ),
            details=tsr_sidecar_restart,
        ),
        "obs-websocket-disabled": Stage45Proof(
            check_id="obs-websocket-disabled",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="OBS websocket disabled gives setup guidance instead of a raw parse failure.",
            observed=(
                f"OBS websocket unreachable ({obs_disabled['reason']}); Hello validation "
                "confirmed no session exists "
                f"({obs_disabled_rejects_success_shaped_frames})."
            ),
            details={"references": [REFERENCE_URLS["obs-websocket"]], **obs_disabled},
        ),
        "obs-wrong-password": Stage45Proof(
            check_id="obs-wrong-password",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="OBS wrong password fails closed without logging the secret.",
            observed=obs_wrong_password,
            details={"references": [REFERENCE_URLS["obs-websocket"]]},
        ),
        "obs-protocol-mismatch": Stage45Proof(
            check_id="obs-protocol-mismatch",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="OBS rpcVersion older than required refuses the session.",
            observed=obs_protocol_mismatch,
            details={"references": [REFERENCE_URLS["obs-websocket"]]},
        ),
        "obs-source-missing": Stage45Proof(
            check_id="obs-source-missing",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="Missing OBS source blocks cue readiness.",
            observed=obs_source_missing,
            details={"references": [REFERENCE_URLS["obs-websocket"]]},
        ),
        "obs-source-removed": Stage45Proof(
            check_id="obs-source-removed",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="A source removed from the scene invalidates cue-relevant dry-run state.",
            observed=obs_source_removed,
            details={"references": [REFERENCE_URLS["obs-websocket"]]},
        ),
        "obs-restart": Stage45Proof(
            check_id="obs-restart",
            status="passed",
            proof_source="stateful-simulator",
            proof_level="simulated-proven",
            claim="OBS restart is Hello loss followed by a fresh, valid Hello.",
            observed=(
                f"Session before restart had rpc {obs_restart['before_rpc_version']}; "
                "disconnect was refused as expected; session after restart re-validated "
                f"with rpc {obs_restart['after_rpc_version']}."
            ),
            details={"references": [REFERENCE_URLS["obs-websocket"]], **obs_restart},
        ),
    }


def _software_probe_event(
    profile_id: TopologyId,
    device: DeviceContract,
    *,
    vmix_url: str,
    obs_host: str,
    obs_port: int,
    timeout_seconds: float,
) -> LabEvent:
    if device.device_class == "vmix":
        probe = _probe_vmix(vmix_url, timeout_seconds)
        check_id = "software-probe-vmix-http"
        claim = (
            "Local vMix HTTP API returned parseable status XML."
            if probe["reachable"]
            else "Probe local vMix HTTP API reachability."
        )
    elif device.device_class == "obs":
        probe = _probe_obs(obs_host, obs_port, timeout_seconds)
        check_id = "software-probe-obs-websocket"
        claim = (
            "Local OBS obs-websocket 5.x endpoint completed Hello, Identify, and GetVersion."
            if probe["reachable"]
            else "Probe local OBS obs-websocket 5.x protocol handshake."
        )
    else:  # pragma: no cover - caller guards this
        raise ValueError(f"Unsupported software probe device class: {device.device_class}")

    reachable = bool(probe["reachable"])
    listener_process = probe.get("listener_process")
    status: Stage45Status = "passed" if reachable else "not-applicable"
    proof_level: Literal["software-lab-proven", "mocked"]
    proof_level = "software-lab-proven" if reachable else "mocked"
    network_claim = (
        "Local software was reachable through a network-confined local listener; "
        "no station-device evidence is claimed."
    )
    if (
        reachable
        and isinstance(listener_process, dict)
        and listener_process.get("network_confined") is not True
    ):
        network_claim = (
            "Local software API compatibility was verified against the expected process, "
            "but the application listener is not network-confined; no secure listener "
            "posture or station-device evidence is claimed."
        )
    proof = Stage45Proof(
        check_id=check_id,
        status=status,
        proof_source="software-lab",
        proof_level=proof_level,
        claim=claim,
        observed=probe["observed"],
        not_claimed=network_claim
        if reachable
        else "Local software endpoint was not reachable; no software-lab proof is claimed.",
        details=probe,
    )
    return _event_from_proof(profile_id, device, proof)


def _probe_vmix(url: str, timeout_seconds: float) -> dict[str, Any]:
    executable = _find_executable(
        ["vMix64.exe", "vMix.exe", "vmix64.exe"],
        [
            Path("C:/Program Files/vMix/vMix64.exe"),
            Path("C:/Program Files (x86)/vMix/vMix64.exe"),
        ],
    )
    try:
        _validate_probe_url(url)
        with urllib.request.urlopen(  # noqa: S310 - _validate_probe_url rejects file/custom/public URLs.  # nosec B310
            url, timeout=timeout_seconds
        ) as response:
            body = response.read(250_000).decode("utf-8", errors="replace")
        parsed = parse_vmix_status_xml(body)
        parsed_url = urllib.parse.urlparse(url)
        port = parsed_url.port or 80
        identity = _verify_tcp_listener_process(
            port=port,
            expected_process_names={"vmix64", "vmix"},
            expected_executable=executable,
        )
        if not identity["verified"]:
            raise ValueError(
                "vMix HTTP API responded, but listener process identity was not verified: "
                f"{identity['observed']}"
            )
        return {
            "reachable": True,
            "software_class": "vmix",
            "url": url,
            "executable": executable,
            "listener_process": identity,
            "observed": f"vMix HTTP API returned version {parsed.version} with {len(parsed.inputs)} input(s).",
        }
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return {
            "reachable": False,
            "software_class": "vmix",
            "url": url,
            "executable": executable,
            "listener_process": None,
            "observed": f"vMix HTTP API not reachable or not parseable: {exc}",
        }


def _validate_probe_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("software-lab probe URLs must use http or https.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("software-lab probe URLs must include a host.")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(
            "software-lab probe host must be localhost, .local, or an IP literal."
        ) from exc
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return
    raise ValueError("software-lab probe host must not be a public IP address.")


def _probe_obs(host: str, port: int, timeout_seconds: float) -> dict[str, Any]:
    executable = _find_executable(
        ["obs64.exe", "obs.exe"],
        [
            Path("C:/Program Files/obs-studio/bin/64bit/obs64.exe"),
            Path("C:/Program Files (x86)/obs-studio/bin/64bit/obs64.exe"),
        ],
    )
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds) as sock:
            sock.settimeout(timeout_seconds)
            obs_state, response = _probe_obs_websocket_5(sock, host, port)
            identity = _verify_tcp_listener_process(
                port=port,
                expected_process_names={"obs64", "obs"},
                expected_executable=executable,
            )
            if not identity["verified"]:
                raise ValueError(
                    "OBS websocket protocol completed, but listener process identity was "
                    f"not verified: {identity['observed']}"
                )
            return {
                "reachable": True,
                "software_class": "obs",
                "host": host,
                "port": port,
                "executable": executable,
                "listener_process": identity,
                "obs_websocket_version": obs_state.obs_websocket_version,
                "rpc_version": obs_state.rpc_version,
                "obs_version": response.get("obsVersion"),
                "platform": response.get("platform"),
                "observed": (
                    f"OBS obs-websocket {obs_state.obs_websocket_version} rpc "
                    f"{obs_state.rpc_version} completed GetVersion at {host}:{port}."
                ),
            }
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {
            "reachable": False,
            "software_class": "obs",
            "host": host,
            "port": port,
            "executable": executable,
            "listener_process": None,
            "observed": f"OBS obs-websocket protocol probe failed at {host}:{port}: {exc}",
        }


def _verify_tcp_listener_process(
    *,
    port: int,
    expected_process_names: set[str],
    expected_executable: str | None,
) -> dict[str, Any]:
    if os.name != "nt":
        return {
            "verified": False,
            "observed": "listener process verification currently requires Windows.",
        }
    if not expected_executable:
        return {
            "verified": False,
            "observed": "expected application executable was not found on this machine.",
        }
    payload = _find_listener_process_payload(port)
    if payload is None:
        return {"verified": False, "observed": f"no listener process found on port {port}"}
    if payload.get("error"):
        return {"verified": False, "observed": str(payload["error"])}

    loopback_bound = bool(payload.get("loopback_bound"))
    firewall_confined = _windows_firewall_confines_port(port)
    non_loopback_addresses = list(payload.get("non_loopback_addresses") or [])
    process_name = str(payload.get("process_name") or "").lower()
    process_stem = Path(process_name).stem
    path = str(payload.get("path") or "")
    expected_path = _normalize_listener_executable_path(expected_executable)
    actual_path = _normalize_listener_executable_path(path) if path else ""
    name_matches = process_name in expected_process_names or process_stem in expected_process_names
    path_matches = bool(actual_path) and actual_path == expected_path
    network_confined = loopback_bound or firewall_confined
    verified = name_matches and path_matches
    loopback_observation = (
        "loopback-bound"
        if loopback_bound
        else (
            "all-interface listener confined by local firewall"
            if firewall_confined
            else f"not loopback-bound; listener address(es): {', '.join(non_loopback_addresses) or '<unknown>'}"
        )
    )
    return {
        "verified": verified,
        "pid": payload.get("pid"),
        "process_name": payload.get("process_name"),
        "path": path,
        "expected_executable": expected_executable,
        "local_address": payload.get("local_address"),
        "local_port": payload.get("local_port"),
        "loopback_bound": loopback_bound,
        "firewall_confined": firewall_confined,
        "network_confined": network_confined,
        "non_loopback_addresses": non_loopback_addresses,
        "listeners": payload.get("listeners") or [],
        "observed": (
            f"listener pid={payload.get('pid')} process={payload.get('process_name')} "
            f"path={path or '<unknown>'}; {loopback_observation}"
        ),
    }


def _normalize_listener_executable_path(value: str) -> str:
    normalized = value.strip().replace("\\", "/").rstrip("/").lower()
    if os.name == "nt":
        try:
            return str(Path(value).resolve(strict=False)).replace("\\", "/").lower()
        except (OSError, RuntimeError):
            return normalized
    return normalized


def _windows_firewall_confines_port(port: int) -> bool:
    if os.name != "nt":
        return False
    rule_name = f"CivicCast 3.2 Local Lab Block TCP {int(port)}"
    command = [
        "netsh",
        "advfirewall",
        "firewall",
        "show",
        "rule",
        f"name={rule_name}",
        "verbose",
    ]
    try:
        result = subprocess.run(  # noqa: S603 - fixed netsh firewall query, no user input.
            command, capture_output=True, text=True, timeout=8, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = result.stdout.lower()
    return (
        result.returncode == 0
        and re.search(r"enabled:\s+yes", output) is not None
        and re.search(r"direction:\s+in", output) is not None
        and re.search(r"action:\s+block", output) is not None
        and re.search(r"protocol:\s+tcp", output) is not None
        and (
            re.search(rf"localport:\s+{int(port)}(\s|$)", output) is not None
            or re.search(r"localport:\s+any(\s|$)", output) is not None
        )
    )


def _is_loopback_listener_address(address: str | None) -> bool:
    if not address:
        return False
    normalized = address.strip().lower()
    if normalized in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _find_listener_process_payload(port: int) -> dict[str, Any] | None:
    psutil_module = _load_psutil()
    if psutil_module is None:
        return {
            "error": (
                "psutil dependency is not installed; listener process identity "
                "could not be verified."
            ),
            "pid": None,
            "process_name": None,
            "path": None,
        }

    listeners: list[dict[str, Any]] = []
    for conn in psutil_module.net_connections(kind="tcp"):
        if (
            not conn.laddr
            or conn.laddr.port != int(port)
            or conn.status != psutil_module.CONN_LISTEN
        ):
            continue
        local_address = str(getattr(conn.laddr, "ip", "") or "")
        local_port = int(getattr(conn.laddr, "port", port) or port)
        if conn.pid is None:
            listeners.append(
                {
                    "pid": None,
                    "process_name": None,
                    "path": None,
                    "local_address": local_address,
                    "local_port": local_port,
                }
            )
            continue
        try:
            process = psutil_module.Process(conn.pid)
            process_name = process.name()
            path = process.exe()
        except (psutil_module.Error, OSError):
            process_name = None
            path = None
        listeners.append(
            {
                "pid": conn.pid,
                "process_name": process_name,
                "path": path,
                "local_address": local_address,
                "local_port": local_port,
            }
        )
    if not listeners:
        return None
    primary = next(
        (listener for listener in listeners if listener.get("pid") is not None), listeners[0]
    )
    non_loopback_addresses = sorted(
        {
            str(listener.get("local_address") or "<unknown>")
            for listener in listeners
            if not _is_loopback_listener_address(str(listener.get("local_address") or ""))
        }
    )
    loopback_bound = len(non_loopback_addresses) == 0
    return {
        **primary,
        "loopback_bound": loopback_bound,
        "non_loopback_addresses": non_loopback_addresses,
        "listeners": listeners,
    }


def _load_psutil() -> Any | None:
    try:
        import psutil as psutil_module
    except ModuleNotFoundError:
        return None
    return psutil_module


def _probe_obs_websocket_5(
    sock: socket.socket, host: str, port: int
) -> tuple[ObsProtocolState, dict[str, Any]]:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET / HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    headers = _read_http_upgrade_headers(sock)
    expected_accept = base64.b64encode(
        hashlib.sha1(  # noqa: S324 - RFC 6455 requires SHA-1 for Sec-WebSocket-Accept.  # nosec B324
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()
    ).decode("ascii")
    if headers.get(":status") != "101":
        raise ValueError(f"OBS websocket upgrade returned HTTP {headers.get(':status')}.")
    if headers.get("sec-websocket-accept") != expected_accept:
        raise ValueError("OBS websocket upgrade returned an invalid Sec-WebSocket-Accept header.")

    hello = json.loads(_read_ws_text_frame(sock))
    hello_state = validate_obs_websocket_5_frames([hello])
    hello_data = _dict_field(hello, "d", "OBS Hello payload")
    identify_data: dict[str, Any] = {"rpcVersion": hello_state.rpc_version}
    authentication = hello_data.get("authentication")
    if authentication:
        auth_data = _dict_field(hello_data, "authentication", "OBS authentication challenge")
        password = os.environ.get("CIVICAST_OBS_WEBSOCKET_PASSWORD")
        if not password:
            raise ValueError(
                "OBS websocket requires authentication; set CIVICAST_OBS_WEBSOCKET_PASSWORD "
                "for the local software probe."
            )
        identify_data["authentication"] = _obs_websocket_auth(
            password=password,
            challenge=_string_field(auth_data, "challenge", "OBS authentication challenge"),
            salt=_string_field(auth_data, "salt", "OBS authentication salt"),
        )

    identify = {"op": 1, "d": identify_data}
    _send_ws_json(sock, identify)
    identified = json.loads(_read_ws_text_frame(sock))
    if identified.get("op") != 2:
        raise ValueError("OBS websocket did not return Identified op 2 after Identify.")

    request_frame = {
        "op": 6,
        "d": {"requestType": "GetVersion", "requestId": "civiccast-probe-get-version"},
    }
    _send_ws_json(sock, request_frame)
    response_frame: dict[str, Any] | None = None
    extra_frames: list[dict[str, Any]] = []
    for _ in range(10):
        frame = json.loads(_read_ws_text_frame(sock))
        extra_frames.append(frame)
        data = frame.get("d")
        if (
            frame.get("op") == 7
            and isinstance(data, dict)
            and data.get("requestId") == "civiccast-probe-get-version"
        ):
            response_frame = frame
            break
    if response_frame is None:
        raise ValueError("OBS websocket did not return GetVersion response.")
    response_data = _dict_field(response_frame, "d", "OBS GetVersion response")
    status = _dict_field(response_data, "requestStatus", "OBS GetVersion requestStatus")
    if status.get("result") is not True:
        raise ValueError(f"OBS GetVersion failed with requestStatus={status!r}.")

    frames = [hello, request_frame, *extra_frames]
    obs_state = validate_obs_websocket_5_frames(frames)
    return obs_state, _dict_field(response_data, "responseData", "OBS GetVersion responseData")


def _obs_websocket_auth(*, password: str, challenge: str, salt: str) -> str:
    """Return the obs-websocket 5.x authentication response without logging secrets."""

    if not password:
        raise ValueError("OBS websocket password is empty.")
    if not challenge or not salt:
        raise ValueError("OBS websocket authentication challenge and salt are required.")
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode(
        "ascii"
    )
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode(
        "ascii"
    )


def _read_http_upgrade_headers(sock: socket.socket) -> dict[str, str]:
    raw = _recv_until(sock, b"\r\n\r\n", max_bytes=8192).decode("iso-8859-1")
    lines = raw.split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        raise ValueError("OBS websocket upgrade did not return an HTTP response.")
    parts = lines[0].split(" ", 2)
    headers = {":status": parts[1] if len(parts) > 1 else ""}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return headers


def _recv_until(sock: socket.socket, marker: bytes, *, max_bytes: int) -> bytes:
    chunks = bytearray()
    while marker not in chunks:
        if len(chunks) >= max_bytes:
            raise ValueError("OBS websocket response exceeded probe header limit.")
        chunk = sock.recv(1)
        if not chunk:
            raise ValueError("OBS websocket closed before probe completed.")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_ws_text_frame(sock: socket.socket) -> str:
    while True:
        header = _recv_exact(sock, 2)
        first, second = header
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        payload_len = second & 0x7F
        if payload_len == 126:
            payload_len = int.from_bytes(_recv_exact(sock, 2), "big")
        elif payload_len == 127:
            payload_len = int.from_bytes(_recv_exact(sock, 8), "big")
        mask = _recv_exact(sock, 4) if masked else b""
        payload = _recv_exact(sock, payload_len)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        if opcode == 0x8:
            raise ValueError("OBS websocket closed the connection.")
        if opcode == 0x9:
            _send_ws_frame(sock, payload, opcode=0xA)
            continue
        if opcode in {0x1, 0x2}:
            return payload.decode("utf-8")


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ValueError("OBS websocket closed before sending a complete frame.")
        chunks.extend(chunk)
    return bytes(chunks)


def _send_ws_json(sock: socket.socket, frame: dict[str, Any]) -> None:
    _send_ws_frame(sock, json.dumps(frame, separators=(",", ":")).encode("utf-8"))


def _send_ws_frame(sock: socket.socket, payload: bytes, *, opcode: int = 0x1) -> None:
    first = 0x80 | opcode
    mask_key = os.urandom(4)
    if len(payload) < 126:
        header = bytes([first, 0x80 | len(payload)])
    elif len(payload) <= 0xFFFF:
        header = bytes([first, 0x80 | 126]) + len(payload).to_bytes(2, "big")
    else:
        header = bytes([first, 0x80 | 127]) + len(payload).to_bytes(8, "big")
    masked = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
    sock.sendall(header + mask_key + masked)


def _event_from_proof(
    profile_id: TopologyId, device: DeviceContract, proof: Stage45Proof
) -> LabEvent:
    return LabEvent(
        profile_id=profile_id,
        device_id=device.contract_id,
        check_id=proof.check_id,
        status=proof.status,
        proof_level=proof.proof_level,
        proof_source=proof.proof_source,
        claim=proof.claim,
        observed=proof.observed,
        not_claimed=proof.not_claimed,
        details={
            "device_class": device.device_class,
            "device_label": device.label,
            "stage": "4-5",
            **proof.details,
        },
    )


def _unexecuted_event(profile_id: TopologyId, device: DeviceContract, check_id: str) -> LabEvent:
    """Return the honest event for a required check Stage 4-5 never actually ran.

    A required check with no Stage 4-5 fixture/simulator/probe behind it is not
    silently dropped: it reports its real state instead of borrowing whatever
    the Stage 1 static catalog claimed. A catalog row that already declares
    itself ``not-applicable`` (an explicit, self-disclosed non-claim) stays
    not-applicable here; every other required-but-unexecuted check fails.
    """

    catalog_entry = CHECK_CATALOG.get(check_id)
    if catalog_entry is not None and catalog_entry.status == "not-applicable":
        return LabEvent(
            profile_id=profile_id,
            device_id=device.contract_id,
            check_id=check_id,
            status="not-applicable",
            proof_level="mocked",
            proof_source="check-catalog",
            claim=catalog_entry.claim,
            observed=catalog_entry.observed,
            not_claimed=catalog_entry.not_claimed,
            details={"device_class": device.device_class, "device_label": device.label},
        )
    return LabEvent(
        profile_id=profile_id,
        device_id=device.contract_id,
        check_id=check_id,
        status="failed",
        proof_level="mocked",
        proof_source="check-catalog",
        claim=f"{check_id!r} has Stage 4-5 executable evidence.",
        observed=(
            "No Stage 4-5 fixture, simulator, or local software probe executed "
            f"this required check; only the Stage 1 static catalog claim exists for {check_id!r}."
        ),
        not_claimed=(
            "This is not API fixture execution, simulator execution, software-lab "
            "proof, or station-device evidence."
        ),
        details={"device_class": device.device_class, "device_label": device.label},
    )


def _find_executable(names: list[str], candidates: list[Path]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _simulated(check_id: str, observed: str) -> Stage45Proof:
    return Stage45Proof(
        check_id=check_id,
        status="passed",
        proof_source="stateful-simulator",
        proof_level="simulated-proven",
        claim=f"{check_id} state transition is executed by the local simulator.",
        observed=observed,
    )


def _required_text(root: Element, tag: str) -> str:
    element = root.find(tag)
    text = "" if element is None or element.text is None else element.text.strip()
    if not text:
        raise ValueError(f"vMix status XML is missing <{tag}>.")
    return text


def _required_int(root: Element, tag: str) -> int:
    text = _required_text(root, tag)
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"vMix <{tag}> must be an integer: {text}") from exc


def _bool_text(text: str) -> bool:
    normalized = text.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError(f"Expected boolean text, got {text!r}.")


def _dict_field(frame: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    value = frame.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    return value


def _string_field(frame: dict[str, Any], key: str, label: str) -> str:
    value = frame.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is missing string field {key!r}.")
    return value


def _hex_bytes(value: str) -> bytes:
    cleaned = value.replace("0x", "").replace(" ", "").replace("-", "")
    if len(cleaned) % 2:
        raise ValueError(f"Hex byte string has odd length: {value}")
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid hex byte string: {value}") from exc


def _expect_value_error(fn: Any, *, match: str | None = None) -> str:
    """Run ``fn`` expecting a ValueError; return its message.

    ``match`` pins the failure to a specific expected message substring so a
    check cannot pass on an unrelated ValueError raised earlier in the
    validation path (e.g. a parser error before the real assertion runs).
    """

    try:
        fn()
    except ValueError as exc:
        message = str(exc)
        if match is not None and match not in message:
            raise AssertionError(
                f"Fixture failed for the wrong reason: expected {match!r} in {message!r}."
            ) from exc
        return message
    raise AssertionError("Expected fixture validation to fail.")


_VMIX_STATUS_XML = """\
<vmix>
  <version>29.0.0.48</version>
  <inputs>
    <input key="vmix-cam-1" number="1" type="Camera" title="DeckLink Duo 2" state="Running">DeckLink Duo 2</input>
    <input key="vmix-cam-2" number="2" type="Camera" title="Cam Link HDMI" state="Running">Cam Link HDMI</input>
    <input key="vmix-title" number="3" type="Title" title="Lower Third" state="Paused">Lower Third</input>
  </inputs>
  <active>1</active>
  <preview>2</preview>
  <recording>False</recording>
  <streaming>True</streaming>
</vmix>
"""

_VMIX_STATUS_XML_AFTER_SELECT = """\
<vmix>
  <version>29.0.0.48</version>
  <inputs>
    <input key="vmix-cam-1" number="1" type="Camera" title="DeckLink Duo 2" state="Running">DeckLink Duo 2</input>
    <input key="vmix-cam-2" number="2" type="Camera" title="Cam Link HDMI" state="Running">Cam Link HDMI</input>
    <input key="vmix-title" number="3" type="Title" title="Lower Third" state="Paused">Lower Third</input>
  </inputs>
  <active>2</active>
  <preview>1</preview>
  <recording>False</recording>
  <streaming>True</streaming>
</vmix>
"""

_VMIX_STATUS_XML_INPUT_IDENTITY_DRIFTED = """\
<vmix>
  <version>29.0.0.48</version>
  <inputs>
    <input key="vmix-cam-1" number="1" type="Camera" title="DeckLink Duo 2" state="Running">DeckLink Duo 2</input>
    <input key="vmix-cam-2-drifted" number="3" type="Camera" title="Cam Link HDMI" state="Running">Cam Link HDMI</input>
    <input key="vmix-title" number="2" type="Title" title="Lower Third" state="Paused">Lower Third</input>
  </inputs>
  <active>1</active>
  <preview>2</preview>
  <recording>False</recording>
  <streaming>True</streaming>
</vmix>
"""

_VMIX_STATUS_XML_INPUT_2_REMOVED = """\
<vmix>
  <version>29.0.0.48</version>
  <inputs>
    <input key="vmix-cam-1" number="1" type="Camera" title="DeckLink Duo 2" state="Running">DeckLink Duo 2</input>
    <input key="vmix-title" number="3" type="Title" title="Lower Third" state="Paused">Lower Third</input>
  </inputs>
  <active>1</active>
  <preview>3</preview>
  <recording>False</recording>
  <streaming>True</streaming>
</vmix>
"""

_VMIX_STATUS_XML_BAD_RECORDING = """\
<vmix>
  <version>29.0.0.48</version>
  <inputs><input key="a" number="1" title="Only Input">Only Input</input></inputs>
  <active>1</active>
  <preview>1</preview>
  <recording>Maybe</recording>
  <streaming>False</streaming>
</vmix>
"""

_VMIX_STATUS_XML_BAD_STREAMING = """\
<vmix>
  <version>29.0.0.48</version>
  <inputs><input key="a" number="1" title="Only Input">Only Input</input></inputs>
  <active>1</active>
  <preview>1</preview>
  <recording>False</recording>
  <streaming>Maybe</streaming>
</vmix>
"""

_USB_CAPTURE_FIXTURE_SWAPPED = {
    "devices": [
        {"name": "Cam Link HDMI", "class": "uvc-video", "stable_id": "usbvid-0002-swapped"},
        {"name": "Behringer U-Phoria", "class": "usb-audio", "stable_id": "usbaudio-0001"},
    ]
}

_USB_CAPTURE_FIXTURE_NO_AUDIO = {
    "devices": [
        {"name": "Cam Link HDMI", "class": "uvc-video", "stable_id": "usbvid-0001"},
    ]
}

_AUDIO_TOPOLOGY_FIXTURE = {
    "mixer_model": "Allen & Heath SQ5",
    "channel_count": 48,
    "output_routes": ["Program Mix L/R", "Stream Mix", "Monitor Mix 1"],
}

_AUDIO_TOPOLOGY_FIXTURE_MISSING_ROUTES = {
    "mixer_model": "Allen & Heath SQ5",
    "channel_count": 48,
    "output_routes": [],
}

_AUDIO_TOPOLOGY_FIXTURE_WITH_CONTROL_LEAK = {
    "mixer_model": "Allen & Heath SQ5",
    "channel_count": 48,
    "output_routes": ["Program Mix L/R"],
    "midi_port": 51325,
}

_USB_AUDIO_SAMPLE_RATE_FIXTURE = {"name": "Behringer U-Phoria", "sample_rate_hz": 48_000}
_USB_AUDIO_SAMPLE_RATE_FIXTURE_MISMATCHED = {"name": "Behringer U-Phoria", "sample_rate_hz": 44_100}

_USB_AUDIO_SYNC_FIXTURE = {"name": "Behringer U-Phoria", "delay_ms": 12.0}
_USB_AUDIO_SYNC_FIXTURE_DELAYED = {"name": "Behringer U-Phoria", "delay_ms": 65.0}

# SQ mute = 4-frame NRPN write, channel 1, per SQ MIDI Protocol Issue 5
# page 11 s3.3: BN 63 MB / BN 62 LB / BN 06 00 / BN 26 01 (on). This mirrors
# the doc's own Ip1 Mute On, Ch1 example (B0 63 00 B0 62 00 B0 06 00 B0 26 01).
# The NRPN parameter number is a lab fixture, not a confirmed SQ5 mapping.
_SQ_MIDI_MUTE_ON_HEX = ["B0 63 00", "B0 62 00", "B0 06 00", "B0 26 01"]
# The OLD, WRONG format we used to model mute as: a Soft Key Note On
# (9N SK 7F per Issue 5 page 10 s3.2). Used only to prove the mute validator
# now REJECTS a Note On as not a documented mute.
_SQ_SOFTKEY_NOTE_ON_HEX = "90 30 7F"

# 4-frame NRPN level write on channel 1: CC99 (NRPN MSB), CC98 (NRPN LSB),
# CC6 (Data Entry MSB/Value Coarse), CC38 (Data Entry LSB/Value Fine), per
# SQ MIDI Protocol Issue 5 page 12 s3.4 (BN 63 MB / BN 62 LB / BN 06 VC /
# BN 26 VF). NRPN number/value are a lab fixture, not a confirmed mapping.
_MIDI_NRPN_FADER_HEX = ["B0 63 00", "B0 62 01", "B0 06 40", "B0 26 00"]
_MIDI_NRPN_FADER_HEX_OUT_OF_ORDER = ["B0 63 00", "B0 06 40", "B0 62 01", "B0 26 00"]

# SQ scene recall on channel 1 per SQ MIDI Protocol Issue 5 page 9 s3.1:
# bank change (CC 0) THEN Program Change -- two messages, NO CC 32. Mirrors
# the doc's Scene 7, Ch1 example (B0 00 00 C0 06).
_MIDI_SCENE_BANK_HEX = "B0 00 00"
_MIDI_PROGRAM_CHANGE_HEX = "C0 05"
# The OLD, WRONG format we used to require: a spurious CC 32 Bank LSB. Used
# only to prove the scene validator now REJECTS a CC 32.
_MIDI_SCENE_BANK_LSB_CC32_HEX = "B0 20 02"

_TSR_PRE_RESTART_STATE = {"healthy": True, "fetched_at_unix": 1_000.0}
_TSR_RESTART_EVENT = {"restarted_at_unix": 1_010.0}
_TSR_POST_RESTART_STATE = {"healthy": True, "fetched_at_unix": 1_012.0}

_NDI_TRACKED_SOURCE_NAME = "AIDA-PTZ-1 (Camera 1)"
_NDI_SOURCES_PRESENT = [
    {
        "name": "AIDA-PTZ-1 (Camera 1)",
        "url_address": "192.168.1.101:5961",
        "last_seen_unix": 1_700.0,
    },
    {
        "name": "AIDA-PTZ-2 (Camera 2)",
        "url_address": "192.168.1.102:5961",
        "last_seen_unix": 1_700.0,
    },
]
_NDI_SOURCES_ABSENT = [
    {
        "name": "AIDA-PTZ-2 (Camera 2)",
        "url_address": "192.168.1.102:5961",
        "last_seen_unix": 1_705.0,
    },
]
_NDI_TRACKED_SOURCE_URL = "192.168.1.101:5961"
_NDI_SOURCES_RENAMED = [
    {
        "name": "Podium Camera",
        "url_address": "192.168.1.101:5961",
        "last_seen_unix": 1_710.0,
    },
    {
        "name": "AIDA-PTZ-2 (Camera 2)",
        "url_address": "192.168.1.102:5961",
        "last_seen_unix": 1_710.0,
    },
]
# Broken fixture for the negative check: the tracked source's stable id
# (url_address) changes under the rename instead of staying fixed, so the
# renamed source is indistinguishable from the old one being lost.
_NDI_SOURCES_RENAME_LOST_STABLE_ID = [
    {
        "name": "Podium Camera",
        "url_address": "192.168.1.199:5961",
        "last_seen_unix": 1_710.0,
    },
    {
        "name": "AIDA-PTZ-2 (Camera 2)",
        "url_address": "192.168.1.102:5961",
        "last_seen_unix": 1_710.0,
    },
]

_VMIX_DRIFT_XML = """\
<vmix>
  <version>29.0.0.48</version>
  <inputs><input key="a" number="1" title="Only Input">Only Input</input></inputs>
  <active>1</active>
  <recording>False</recording>
  <streaming>False</streaming>
</vmix>
"""

_OBS_WS_FRAMES_JSON = """[
  {"op": 0, "d": {"obsWebSocketVersion": "5.5.4", "rpcVersion": 1}},
  {"op": 6, "d": {"requestType": "GetRecordStatus", "requestId": "req-record-status", "requestData": {}}},
  {"op": 7, "d": {"requestType": "GetRecordStatus", "requestId": "req-record-status", "requestStatus": {"result": true, "code": 100}, "responseData": {"outputActive": false}}},
  {"op": 5, "d": {"eventType": "RecordStateChanged", "eventIntent": 64, "eventData": {"outputActive": false}}}
]"""

_ATEM_STATE_FIXTURE = {
    "protocol_version": "atem-connection-3.x",
    "program_input": 1,
    "preview_input": 2,
    "in_transition": False,
}

_USB_CAPTURE_FIXTURE = {
    "devices": [
        {"name": "Cam Link HDMI", "class": "uvc-video", "stable_id": "usbvid-0001"},
        {"name": "Behringer U-Phoria", "class": "usb-audio", "stable_id": "usbaudio-0001"},
    ]
}


__all__ = [
    "REFERENCE_URLS",
    "ObsProtocolState",
    "Stage45Proof",
    "VmixStatusState",
    "build_stage45_proofs_for_profile",
    "parse_vmix_status_xml",
    "validate_atem_state_fixture",
    "validate_capture_identity_fixture",
    "validate_obs_websocket_5_frames",
    "validate_visca_udp_exchange",
]
