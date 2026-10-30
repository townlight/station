# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Fixed-route peer access: credentials stay local, remote auth remains authoritative."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter, ValidationError

from civiccast.egress.router import EgressCommandResponse, StaffEgressChannelSummary
from civiccast.federation.models import (
    FederationConfig,
    RemoteChannel,
    RemoteCommandReceipt,
    RemoteCommandRequest,
    SharedProgram,
    StationCatalog,
    StationChannels,
    StationPeer,
    StationSummary,
)
from civiccast.vod.models import AssetMetadata

_MAX_RESPONSE_BYTES = 1_048_576


class FederationError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class FederationService:
    def __init__(
        self, config: FederationConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.config = config
        self._transport = transport

    def stations(self) -> list[StationSummary]:
        return [
            StationSummary(
                station_id=peer.station_id,
                label=peer.label,
                public_origin=peer.public_origin,
                channel_ids=peer.channel_ids,
                control_configured=bool(peer.token_env and os.environ.get(peer.token_env)),
            )
            for peer in self.config.stations
        ]

    def _peer(self, station_id: str) -> StationPeer:
        for peer in self.config.stations:
            if peer.station_id == station_id:
                return peer
        raise FederationError(404, "Configured station not found.")

    async def _request(
        self,
        peer: StationPeer,
        path: str,
        *,
        control: bool = False,
        body: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        origin = peer.public_origin
        if control:
            token = os.environ.get(peer.token_env or "", "")
            if (
                not peer.staff_origin
                or not token
                or any(ord(c) <= 32 or ord(c) == 127 for c in token)
            ):
                raise FederationError(
                    503, "Remote staff credential is not configured for this station."
                )
            origin = peer.staff_origin
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with asyncio.timeout(12):
                async with httpx.AsyncClient(
                    transport=self._transport, follow_redirects=False, trust_env=False, timeout=8
                ) as client:
                    async with client.stream(
                        "POST" if body is not None else "GET",
                        origin + path,
                        headers=headers,
                        json=body,
                    ) as response:
                        if response.status_code in {401, 403}:
                            raise FederationError(
                                502,
                                "Remote station refused authorization; check its staff credential and roles.",
                            )
                        if not response.is_success or response.status_code != (
                            202 if body is not None else 200
                        ):
                            raise FederationError(
                                502,
                                "Remote station did not accept the request. No command completion is confirmed.",
                            )
                        payload = bytearray()
                        async for chunk in response.aiter_bytes():
                            payload.extend(chunk)
                            if len(payload) > _MAX_RESPONSE_BYTES:
                                raise FederationError(
                                    502, "Remote station response exceeded the supported size."
                                )
                        return json.loads(payload)
        except FederationError:
            raise
        except (httpx.HTTPError, TimeoutError):
            raise FederationError(
                502,
                "Remote station could not be reached. A submitted command may still be queued; refresh status before retrying.",
            ) from None
        except (ValueError, TypeError):
            raise FederationError(502, "Remote station returned an invalid response.") from None

    async def catalog(self, station_id: str) -> StationCatalog:
        peer = self._peer(station_id)
        payload = await self._request(peer, "/api/public/assets")
        try:
            assets = TypeAdapter(list[AssetMetadata]).validate_python(payload)
        except ValidationError:
            raise FederationError(
                502, "Remote station returned an invalid published catalog."
            ) from None
        published = [asset for asset in assets if asset.published_at is not None]
        return StationCatalog(
            station_id=station_id,
            truncated=len(published) > 100,
            items=[
                SharedProgram(
                    asset_id=asset.asset_id,
                    title=asset.title,
                    duration_seconds=asset.duration_seconds,
                    published_at=asset.published_at,
                    portal_url=f"{peer.public_origin}/#/watch/{quote(asset.asset_id, safe='')}",
                )
                for asset in published[:100]
                if asset.published_at is not None
            ],
        )

    async def channels(self, station_id: str) -> StationChannels:
        peer = self._peer(station_id)
        payload = await self._request(peer, "/api/staff/egress/channels", control=True)
        try:
            rows = TypeAdapter(list[StaffEgressChannelSummary]).validate_python(payload)
        except ValidationError:
            raise FederationError(502, "Remote station returned invalid channel status.") from None
        return StationChannels(
            station_id=station_id,
            channels=[
                RemoteChannel(
                    channel_id=row.channel_id,
                    enabled=row.enabled,
                    state=row.state.state if row.state else None,
                    current_source_label=row.state.current_source_label if row.state else None,
                    last_health_at=row.latest_health.sampled_at if row.latest_health else None,
                )
                for row in rows
                if row.channel_id in peer.channel_ids
            ],
        )

    async def command(
        self, station_id: str, channel_id: str, request: RemoteCommandRequest
    ) -> RemoteCommandReceipt:
        peer = self._peer(station_id)
        if channel_id not in peer.channel_ids:
            raise FederationError(404, "Channel is not enabled for management on this station.")
        payload = await self._request(
            peer,
            f"/api/staff/egress/channels/{quote(channel_id, safe='')}/commands",
            control=True,
            body={"action": request.action},
        )
        try:
            result = EgressCommandResponse.model_validate(payload)
            if (
                not result.queued
                or result.command.channel_id != channel_id
                or result.command.action != request.action
            ):
                raise ValueError("Mismatched acknowledgement")
        except (ValidationError, ValueError):
            raise FederationError(
                502, "Remote command acknowledgement was invalid; refresh status before retrying."
            ) from None
        return RemoteCommandReceipt(
            station_id=station_id,
            channel_id=channel_id,
            action=request.action,
            command_id=result.command.command_id,
        )


def configured_federation_service() -> FederationService:
    from civiccast.installer.station_state import station_state_path

    path = Path(
        os.environ.get(
            "CIVICCAST_FEDERATION_CONFIG", str(station_state_path().with_name("federation.json"))
        )
    )
    try:
        config = (
            FederationConfig.model_validate_json(path.read_bytes())
            if path.exists()
            else FederationConfig()
        )
    except (OSError, ValidationError):
        raise FederationError(
            503,
            "Station federation configuration is invalid; ask the station administrator to repair it.",
        ) from None
    return FederationService(config)
