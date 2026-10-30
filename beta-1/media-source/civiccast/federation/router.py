# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Role-gated trusted-station catalogue and central channel controls."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from civiccast.auth.roles import require_any_role
from civiccast.federation.models import (
    RemoteCommandReceipt,
    RemoteCommandRequest,
    StationCatalog,
    StationChannels,
    StationSummary,
)
from civiccast.federation.service import (
    FederationError,
    FederationService,
    configured_federation_service,
)

router = APIRouter(
    prefix="/api/staff/federation",
    tags=["staff", "federation"],
    dependencies=[
        Depends(
            require_any_role("setup_admin", "meeting_operator", "support_admin", "publish_operator")
        )
    ],
)
_LOG = logging.getLogger(__name__)


def get_federation_service(request: Request) -> FederationService:
    bound = getattr(request.app.state, "federation_service", None)
    try:
        return bound if isinstance(bound, FederationService) else configured_federation_service()
    except FederationError as exc:
        raise HTTPException(exc.status, str(exc)) from None


@router.get("/stations", response_model=list[StationSummary])
def stations(service: FederationService = Depends(get_federation_service)) -> list[StationSummary]:
    return service.stations()


@router.get("/stations/{station_id}/catalog", response_model=StationCatalog)
async def catalog(
    station_id: str, service: FederationService = Depends(get_federation_service)
) -> StationCatalog:
    try:
        return await service.catalog(station_id)
    except FederationError as exc:
        raise HTTPException(exc.status, str(exc)) from None


@router.get("/stations/{station_id}/channels", response_model=StationChannels)
async def channels(
    station_id: str, service: FederationService = Depends(get_federation_service)
) -> StationChannels:
    try:
        return await service.channels(station_id)
    except FederationError as exc:
        raise HTTPException(exc.status, str(exc)) from None


@router.post(
    "/stations/{station_id}/channels/{channel_id}/commands",
    response_model=RemoteCommandReceipt,
    status_code=202,
    dependencies=[Depends(require_any_role("setup_admin", "meeting_operator"))],
)
async def command(
    station_id: str,
    channel_id: str,
    payload: RemoteCommandRequest,
    request: Request,
    service: FederationService = Depends(get_federation_service),
) -> RemoteCommandReceipt:
    try:
        result = await service.command(station_id, channel_id, payload)
    except FederationError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    identity = request.state.operator_identity
    _LOG.info(
        "Federation command queued: station=%s channel=%s action=%s command=%s operator=%s",
        result.station_id,
        result.channel_id,
        result.action,
        result.command_id,
        identity.operator_id,
    )
    return result
