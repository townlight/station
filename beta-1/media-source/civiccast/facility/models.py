# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Typed facility integration models for router and hardware control."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROUTER_VENDOR_BLACKMAGIC = "blackmagic-design"
ROUTER_VENDOR_ROSS = "ross-video"
ROUTER_VENDOR_UTAH = "utah-scientific"
ROUTER_VENDOR_EVERTZ = "evertz"
ROUTER_VENDOR_GENERIC = "generic"

RouterVendor = Literal[
    "blackmagic-design",
    "ross-video",
    "utah-scientific",
    "evertz",
    "generic",
]
RouterTransport = Literal["tcp", "udp", "rs232"]
RouterProtocol = Literal[
    "blackmagic-videohub",
    "ross-rosstalk",
    "utah-scientific",
    "evertz",
    "generic-text",
    "generic-hex",
]


class RouterEndpoint(BaseModel):
    """Configured control endpoint for a facility router or command device."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    vendor: RouterVendor
    protocol: RouterProtocol
    transport: RouterTransport
    host: Annotated[str | None, Field(default=None, min_length=1, max_length=255)] = None
    port: Annotated[int | None, Field(default=None, ge=1, le=65535)] = None
    serial_port: Annotated[str | None, Field(default=None, min_length=1, max_length=255)] = None
    baud_rate: Annotated[int | None, Field(default=None, ge=1200, le=921600)] = None
    command_template: Annotated[str | None, Field(default=None, min_length=1, max_length=500)] = (
        None
    )
    enabled: bool = True
    notes: Annotated[str | None, Field(default=None, max_length=500)] = None

    @model_validator(mode="after")
    def _transport_fields_are_consistent(self) -> RouterEndpoint:
        if self.transport in {"tcp", "udp"} and (not self.host or self.port is None):
            raise ValueError("tcp and udp router endpoints require host and port")
        if self.transport == "rs232" and (not self.serial_port or self.baud_rate is None):
            raise ValueError("rs232 router endpoints require serial_port and baud_rate")
        if self.protocol in {"generic-text", "generic-hex"} and not self.command_template:
            raise ValueError("generic router protocols require command_template")
        if self.protocol == "generic-hex":
            cleaned = (self.command_template or "").replace(" ", "")
            try:
                bytes.fromhex(cleaned.format(source="01", destination="02"))
            except (KeyError, ValueError) as exc:
                raise ValueError("generic-hex command_template must render as hex bytes") from exc
        return self


class RouterInput(BaseModel):
    """One source input on a facility router."""

    model_config = ConfigDict(extra="forbid")

    input_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    physical_port: Annotated[str, Field(min_length=1, max_length=120)]
    live_source_id: Annotated[str | None, Field(default=None, min_length=1, max_length=120)] = None
    enabled: bool = True


class RouterOutput(BaseModel):
    """One destination output on a facility router."""

    model_config = ConfigDict(extra="forbid")

    output_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    physical_port: Annotated[str, Field(min_length=1, max_length=120)]
    channel_id: Annotated[str | None, Field(default=None, min_length=1, max_length=120)] = None
    enabled: bool = True


class RouterInventory(BaseModel):
    """Operator-safe facility router inventory."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    endpoints: list[RouterEndpoint]
    sources: list[RouterInput]
    destinations: list[RouterOutput]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=300)]


class RouterTakePreviewRequest(BaseModel):
    """API request for previewing one source take."""

    model_config = ConfigDict(extra="forbid")

    request_id: Annotated[str, Field(min_length=1, max_length=120)]
    endpoint_id: Annotated[str, Field(min_length=1, max_length=120)]
    source_id: Annotated[str, Field(min_length=1, max_length=120)]
    destination_id: Annotated[str, Field(min_length=1, max_length=120)]
    requested_by: Annotated[str, Field(min_length=1, max_length=120)]
    scheduled_for: datetime | None = None
    reason: Annotated[str | None, Field(default=None, max_length=300)] = None


class RouterScheduledTakePreviewRequest(BaseModel):
    """API request for previewing an automatic schedule-triggered source take."""

    model_config = ConfigDict(extra="forbid")

    request_id: Annotated[str, Field(min_length=1, max_length=120)]
    schedule_item_id: Annotated[str, Field(min_length=1, max_length=160)]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    starts_at: datetime
    endpoint_id: Annotated[str, Field(min_length=1, max_length=120)]
    source_id: Annotated[str, Field(min_length=1, max_length=120)]
    destination_id: Annotated[str, Field(min_length=1, max_length=120)]
    requested_by: Annotated[str, Field(min_length=1, max_length=120)]
    preroll_seconds: Annotated[int, Field(ge=0, le=600)] = 5
    reason: Annotated[str | None, Field(default=None, max_length=300)] = None


class RouterTakeRequest(BaseModel):
    """Operator or scheduler request to route one input to one output."""

    model_config = ConfigDict(extra="forbid")

    request_id: Annotated[str, Field(min_length=1, max_length=120)]
    endpoint: RouterEndpoint
    source: RouterInput
    destination: RouterOutput
    requested_by: Annotated[str, Field(min_length=1, max_length=120)]
    scheduled_for: datetime | None = None
    reason: Annotated[str | None, Field(default=None, max_length=300)] = None

    @model_validator(mode="after")
    def _source_and_destination_are_enabled(self) -> RouterTakeRequest:
        if not self.source.enabled:
            raise ValueError("router take source is disabled")
        if not self.destination.enabled:
            raise ValueError("router take destination is disabled")
        return self


class RouterScheduledTakeRequest(BaseModel):
    """Scheduler request to arm a router take before a scheduled channel event."""

    model_config = ConfigDict(extra="forbid")

    request_id: Annotated[str, Field(min_length=1, max_length=120)]
    schedule_item_id: Annotated[str, Field(min_length=1, max_length=160)]
    channel_id: Annotated[str, Field(min_length=1, max_length=120)]
    starts_at: datetime
    endpoint: RouterEndpoint
    source: RouterInput
    destination: RouterOutput
    requested_by: Annotated[str, Field(min_length=1, max_length=120)]
    preroll_seconds: Annotated[int, Field(ge=0, le=600)] = 5
    reason: Annotated[str | None, Field(default=None, max_length=300)] = None

    @model_validator(mode="after")
    def _source_and_destination_are_enabled(self) -> RouterScheduledTakeRequest:
        if not self.source.enabled:
            raise ValueError("router scheduled take source is disabled")
        if not self.destination.enabled:
            raise ValueError("router scheduled take destination is disabled")
        return self


class RouterTakePlan(BaseModel):
    """Safe, inspectable command plan for a router take."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    endpoint_id: str
    vendor: RouterVendor
    protocol: RouterProtocol
    transport: RouterTransport
    target: str
    command_preview: Annotated[str, Field(min_length=1, max_length=1000)]
    command_bytes_hex: Annotated[str, Field(min_length=2, max_length=2000)]
    source_label: str
    destination_label: str
    scheduled_for: datetime | None = None
    ready_to_send: bool
    operator_action: Annotated[str, Field(min_length=1, max_length=300)]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=300)]


class RouterScheduledTakePlan(BaseModel):
    """Preview of an automatic router take tied to one schedule item."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    schedule_item_id: str
    channel_id: str
    starts_at: datetime
    scheduled_take_at: datetime
    preroll_seconds: int
    take_plan: RouterTakePlan
    automatic_take_ready: bool
    operator_action: Annotated[str, Field(min_length=1, max_length=300)]
    proof_boundary: Annotated[str, Field(min_length=1, max_length=300)]


class VirtualRouterButton(BaseModel):
    """Mobile-friendly panel button for a source-to-destination route."""

    model_config = ConfigDict(extra="forbid")

    button_id: Annotated[str, Field(min_length=1, max_length=160)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    source_id: Annotated[str, Field(min_length=1, max_length=120)]
    destination_id: Annotated[str, Field(min_length=1, max_length=120)]
    enabled: bool
    requires_confirmation: bool = True
    operator_action: Annotated[str, Field(min_length=1, max_length=300)]


class VirtualRouterPanel(BaseModel):
    """Operator-facing virtual panel for common source takes."""

    model_config = ConfigDict(extra="forbid")

    panel_id: Annotated[str, Field(min_length=1, max_length=120)]
    label: Annotated[str, Field(min_length=1, max_length=160)]
    endpoint_id: Annotated[str, Field(min_length=1, max_length=120)]
    buttons: list[VirtualRouterButton]
    mobile_columns: Annotated[int, Field(ge=1, le=4)] = 2

    @model_validator(mode="after")
    def _button_ids_are_unique(self) -> VirtualRouterPanel:
        ids = [button.button_id for button in self.buttons]
        if len(ids) != len(set(ids)):
            raise ValueError("virtual router panel button IDs must be unique")
        return self
