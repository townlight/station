# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Service-provisioned federation configuration and safe operator projections."""

from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[
    str, Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
]


def https_origin(value: str) -> str:
    """Accept a fixed HTTPS origin only, never a URL path or credentials."""
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise ValueError("Station addresses must be HTTPS origins without whitespace.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use an HTTPS origin without credentials, path, query or fragment.")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
            raise ValueError("Station hostname is invalid.") from None
    else:
        if address.is_unspecified or address.is_multicast or address.is_link_local:
            raise ValueError("Station address must identify a trusted station host.")
        host = f"[{host}]" if address.version == 6 else host
    port = parsed.port
    return f"https://{host}" + (f":{port}" if port not in {None, 443} else "")


class StationPeer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    station_id: Identifier
    label: str = Field(min_length=1, max_length=120)
    public_origin: str = Field(max_length=500)
    staff_origin: str | None = Field(default=None, max_length=500)
    token_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,119}$")
    channel_ids: tuple[Identifier, ...] = Field(default=(), max_length=32)

    @field_validator("public_origin", "staff_origin")
    @classmethod
    def validate_origin(cls, value: str | None) -> str | None:
        return https_origin(value) if value is not None else None

    @model_validator(mode="after")
    def complete_control(self) -> StationPeer:
        if bool(self.staff_origin) != bool(self.token_env):
            raise ValueError("Remote control requires both staff_origin and token_env.")
        if len(set(self.channel_ids)) != len(self.channel_ids):
            raise ValueError("Configured channel IDs must be unique.")
        return self


class FederationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stations: tuple[StationPeer, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def unique_stations(self) -> FederationConfig:
        if len({peer.station_id for peer in self.stations}) != len(self.stations):
            raise ValueError("Station IDs must be unique.")
        return self


class StationSummary(BaseModel):
    station_id: str
    label: str
    public_origin: str
    channel_ids: tuple[str, ...]
    control_configured: bool


class SharedProgram(BaseModel):
    asset_id: str
    title: str
    duration_seconds: int | None
    published_at: datetime
    portal_url: str


class StationCatalog(BaseModel):
    station_id: str
    items: list[SharedProgram]
    truncated: bool


class RemoteChannel(BaseModel):
    channel_id: str
    enabled: bool
    state: str | None = None
    current_source_label: str | None = None
    last_health_at: datetime | None = None


class StationChannels(BaseModel):
    station_id: str
    channels: list[RemoteChannel]


class RemoteCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "stop"]
    confirm_stop: bool = False

    @model_validator(mode="after")
    def confirm_interruption(self) -> RemoteCommandRequest:
        if self.action == "stop" and not self.confirm_stop:
            raise ValueError("Confirm stopping the selected remote channel before continuing.")
        return self


class RemoteCommandReceipt(BaseModel):
    station_id: str
    channel_id: str
    action: Literal["start", "stop"]
    command_id: str
    queued: Literal[True] = True
    message: str = "Queued on remote station; refresh status to confirm execution."
