# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API for validated station listener and reverse-proxy settings."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from civiccast.auth.roles import require_any_role
from civiccast.native.network import (
    NetworkSettings,
    NetworkSettingsStore,
    default_network_settings_path,
    render_caddyfile,
)

router = APIRouter(prefix="/api/staff/network", tags=["staff", "network"])


class NetworkSettingsResponse(BaseModel):
    """Persisted settings and the Caddy config the operator can deploy."""

    model_config = ConfigDict(extra="forbid")

    settings: NetworkSettings
    caddyfile: str
    restart_required: bool


def get_network_settings_store(request: Request) -> NetworkSettingsStore:
    store = getattr(request.app.state, "network_settings_store", None)
    if isinstance(store, NetworkSettingsStore):
        return store
    try:
        store = NetworkSettingsStore(default_network_settings_path())
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Station network settings are unavailable.",
        ) from exc
    request.app.state.network_settings_store = store
    return store


def _response(
    settings: NetworkSettings, *, active_settings: NetworkSettings
) -> NetworkSettingsResponse:
    return NetworkSettingsResponse(
        settings=settings,
        caddyfile=render_caddyfile(settings),
        restart_required=settings != active_settings,
    )


def _active_network_settings(request: Request) -> NetworkSettings:
    active = getattr(request.app.state, "network_active_settings", None)
    return active if isinstance(active, NetworkSettings) else NetworkSettings()


@router.get(
    "/settings",
    response_model=NetworkSettingsResponse,
    summary="Read station listener and proxy settings",
    dependencies=[Depends(require_any_role("setup_admin", "support_admin"))],
)
def read_network_settings(
    request: Request,
    store: Annotated[NetworkSettingsStore, Depends(get_network_settings_store)],
) -> NetworkSettingsResponse:
    try:
        return _response(store.load(), active_settings=_active_network_settings(request))
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Station network settings could not be read. Restore a valid settings file or contact support.",
        ) from exc


@router.put(
    "/settings",
    response_model=NetworkSettingsResponse,
    summary="Save station listener and proxy settings",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def save_network_settings(
    request: Request,
    settings: NetworkSettings,
    store: Annotated[NetworkSettingsStore, Depends(get_network_settings_store)],
) -> NetworkSettingsResponse:
    try:
        store.save(settings)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Station network settings could not be saved. Check station storage and try again.",
        ) from exc
    return _response(settings, active_settings=_active_network_settings(request))


__all__ = ["NetworkSettingsResponse", "get_network_settings_store", "router"]
