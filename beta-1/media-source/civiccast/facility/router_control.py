# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Deterministic AV router command planning."""

from __future__ import annotations

from datetime import timedelta

from civiccast.facility.models import (
    ROUTER_VENDOR_BLACKMAGIC,
    ROUTER_VENDOR_EVERTZ,
    ROUTER_VENDOR_GENERIC,
    ROUTER_VENDOR_ROSS,
    ROUTER_VENDOR_UTAH,
    RouterEndpoint,
    RouterInput,
    RouterOutput,
    RouterScheduledTakePlan,
    RouterScheduledTakeRequest,
    RouterTakePlan,
    RouterTakeRequest,
    VirtualRouterButton,
    VirtualRouterPanel,
)


def build_router_take_plan(request: RouterTakeRequest) -> RouterTakePlan:
    """Return a send-ready command plan without opening a device connection."""

    command = _command_for(request.endpoint, request.source, request.destination)
    return RouterTakePlan(
        request_id=request.request_id,
        endpoint_id=request.endpoint.endpoint_id,
        vendor=request.endpoint.vendor,
        protocol=request.endpoint.protocol,
        transport=request.endpoint.transport,
        target=_target_label(request.endpoint),
        command_preview=_preview(command),
        command_bytes_hex=command.hex().upper(),
        source_label=request.source.label,
        destination_label=request.destination.label,
        scheduled_for=request.scheduled_for,
        ready_to_send=request.endpoint.enabled,
        operator_action=(
            "Confirm the previewed route, then send the command from the router panel."
            if request.endpoint.enabled
            else "Enable the router endpoint before sending this command."
        ),
        proof_boundary="Command planning only; no hardware connection is opened by this API.",
    )


def build_router_scheduled_take_plan(
    request: RouterScheduledTakeRequest,
) -> RouterScheduledTakePlan:
    """Return an automatic take plan for a scheduled channel event."""

    scheduled_take_at = request.starts_at - timedelta(seconds=request.preroll_seconds)
    take_plan = build_router_take_plan(
        RouterTakeRequest(
            request_id=request.request_id,
            endpoint=request.endpoint,
            source=request.source,
            destination=request.destination,
            requested_by=request.requested_by,
            scheduled_for=scheduled_take_at,
            reason=request.reason or f"Scheduled router take for {request.schedule_item_id}",
        )
    )
    ready = take_plan.ready_to_send and scheduled_take_at <= request.starts_at
    return RouterScheduledTakePlan(
        request_id=request.request_id,
        schedule_item_id=request.schedule_item_id,
        channel_id=request.channel_id,
        starts_at=request.starts_at,
        scheduled_take_at=scheduled_take_at,
        preroll_seconds=request.preroll_seconds,
        take_plan=take_plan,
        automatic_take_ready=ready,
        operator_action=(
            "Arm this schedule rule to preview the source before the event starts."
            if ready
            else "Resolve the router endpoint or schedule timing before arming this automatic take."
        ),
        proof_boundary="Schedule-to-router command planning only; no hardware command is sent.",
    )


def build_virtual_router_panel(
    *,
    panel_id: str,
    label: str,
    endpoint: RouterEndpoint,
    sources: list[RouterInput],
    destinations: list[RouterOutput],
) -> VirtualRouterPanel:
    """Build a compact operator panel from enabled sources and destinations."""

    buttons: list[VirtualRouterButton] = []
    for source in sources:
        for destination in destinations:
            button_id = f"{source.input_id}-to-{destination.output_id}"
            enabled = endpoint.enabled and source.enabled and destination.enabled
            buttons.append(
                VirtualRouterButton(
                    button_id=button_id,
                    label=f"{source.label} to {destination.label}",
                    source_id=source.input_id,
                    destination_id=destination.output_id,
                    enabled=enabled,
                    operator_action=(
                        "Tap to preview this route before taking it live."
                        if enabled
                        else "This route is disabled until the endpoint, source, and destination are ready."
                    ),
                )
            )
    return VirtualRouterPanel(
        panel_id=panel_id,
        label=label,
        endpoint_id=endpoint.endpoint_id,
        buttons=buttons,
        mobile_columns=2,
    )


def _command_for(endpoint: RouterEndpoint, source: RouterInput, destination: RouterOutput) -> bytes:
    source_port = source.physical_port
    destination_port = destination.physical_port
    if endpoint.vendor == ROUTER_VENDOR_BLACKMAGIC:
        return f"VIDEO OUTPUT ROUTING:\n{destination_port} {source_port}\n\n".encode()
    if endpoint.vendor == ROUTER_VENDOR_ROSS:
        return f"XPT {source_port}:{destination_port}\r\n".encode()
    if endpoint.vendor == ROUTER_VENDOR_UTAH:
        return f"@ TAKE {destination_port} {source_port}\r".encode()
    if endpoint.vendor == ROUTER_VENDOR_EVERTZ:
        return f"X {destination_port},{source_port}\r".encode()
    if endpoint.vendor == ROUTER_VENDOR_GENERIC:
        return _generic_command(endpoint, source_port, destination_port)
    raise ValueError(f"unsupported router vendor: {endpoint.vendor}")


def _generic_command(endpoint: RouterEndpoint, source: str, destination: str) -> bytes:
    template = endpoint.command_template or ""
    rendered = template.format(source=source, destination=destination)
    if endpoint.protocol == "generic-hex":
        return bytes.fromhex(rendered.replace(" ", ""))
    return rendered.encode()


def _target_label(endpoint: RouterEndpoint) -> str:
    if endpoint.transport in {"tcp", "udp"}:
        return f"{endpoint.transport}://{endpoint.host}:{endpoint.port}"
    return f"rs232://{endpoint.serial_port}@{endpoint.baud_rate}"


def _preview(command: bytes) -> str:
    try:
        text = command.decode()
    except UnicodeDecodeError:
        return command.hex().upper()
    return text.replace("\r", "\\r").replace("\n", "\\n")
