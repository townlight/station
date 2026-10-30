# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI router for the public VOD embed API."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from civiccast.egress.router import get_egress_store
from civiccast.live.router import get_current_live_session, get_live_session_store
from civiccast.live.surge_service import get_surge_switch_service
from civiccast.paywall.router import _station_id, get_paywall_service
from civiccast.paywall.service import PaywallService
from civiccast.platform.stores import resolve_app_store
from civiccast.vod.embed import build_embed_html
from civiccast.vod.models import AssetEmbedResponse, public_manifest_reference
from civiccast.vod.store import AssetStore


def get_store(request: Request) -> AssetStore:
    """FastAPI dependency for the active asset store."""
    return cast(AssetStore, resolve_app_store(request, "asset_store", surface="Public asset store"))


router = APIRouter(prefix="/api/public", tags=["public"])


@router.get(
    "/embed/{asset_id}",
    response_model=AssetEmbedResponse,
    summary="Get embed snippet + manifest URL for a public asset",
    responses={
        403: {"description": "Private or paywalled playback cannot be embedded"},
        404: {"description": "Asset not found or unpublished"},
        503: {"description": "Access policy is unavailable"},
    },
)
def get_embed(
    asset_id: str,
    request: Request,
    store: AssetStore = Depends(get_store),
    paywall: PaywallService | None = Depends(get_paywall_service),
) -> AssetEmbedResponse:
    """Resolve a currently published, anonymously accessible recording by identity."""
    asset = store.get(asset_id)
    if asset is None or asset.published_at is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Asset not found: {asset_id}",
        )

    _require_embed_access(paywall, "asset", asset_id)
    manifest_reference = public_manifest_reference(asset.asset_id, asset.manifest_url)
    portal_url = f"{str(request.base_url).rstrip('/')}/api/public/embed-player/asset/{quote(asset_id, safe='')}"

    embed_html = build_embed_html(
        portal_url=portal_url,
        title=asset.title,
    )

    return AssetEmbedResponse(
        asset_id=asset.asset_id,
        title=asset.title,
        manifest_url=manifest_reference,
        poster_url=asset.poster_url,
        portal_url=portal_url,  # type: ignore[arg-type]
        embed_html=embed_html,
    )


class ChannelEmbedResponse(BaseModel):
    channel_id: str
    title: str
    manifest_url: str | None
    portal_url: str
    embed_html: str


def _require_embed_access(paywall: PaywallService | None, kind: str, identity: str) -> None:
    if paywall is None:
        raise HTTPException(
            503, "Playback access policy is unavailable. Retry after station storage recovers."
        )
    if not paywall.has_access(
        _station_id(), None, "asset" if kind == "asset" else "series", identity
    ):
        raise HTTPException(
            403, "Restricted playback cannot be embedded. Open the station portal to sign in."
        )


@router.get("/embed/channel/{channel_id}", response_model=ChannelEmbedResponse)
def get_channel_embed(
    channel_id: str,
    request: Request,
    paywall: PaywallService | None = Depends(get_paywall_service),
    egress_store: Any = Depends(get_egress_store),
    live_session_store: Any = Depends(get_live_session_store),
    surge: Any = Depends(get_surge_switch_service),
) -> ChannelEmbedResponse:
    _require_embed_access(paywall, "channel", channel_id)
    if egress_store is None or egress_store.get_config(channel_id) is None:
        raise HTTPException(404, "Channel not found.")
    live = get_current_live_session(
        request,
        channel_id=channel_id,
        manifest_url=None,
        live_session_store=live_session_store,
        egress_store=egress_store,
        surge=surge,
    )
    portal_url = f"{str(request.base_url).rstrip('/')}/api/public/embed-player/channel/{quote(channel_id, safe='')}"
    title = live.title or "Live channel"
    return ChannelEmbedResponse(
        channel_id=channel_id,
        title=title,
        manifest_url=live.manifest_url,
        portal_url=portal_url,
        embed_html=build_embed_html(portal_url=portal_url, title=title),
    )


@router.get("/embed-player/{kind}/{identity}", response_class=FileResponse, include_in_schema=False)
def embed_player_page(
    kind: Literal["asset", "channel"],
    identity: str,
    store: AssetStore = Depends(get_store),
    paywall: PaywallService | None = Depends(get_paywall_service),
    egress_store: Any = Depends(get_egress_store),
) -> FileResponse:
    _require_embed_access(paywall, kind, identity)
    if kind == "asset":
        asset = store.get(identity)
        if asset is None or asset.published_at is None:
            raise HTTPException(404, "Recording not found.")
    elif egress_store is None or egress_store.get_config(identity) is None:
        raise HTTPException(404, "Channel not found.")
    root = os.environ.get("CIVICCAST_PUBLIC_PORTAL_DIST")
    index = Path(root) / "index.html" if root else None
    if index is None or not index.is_file():
        raise HTTPException(
            503, "The resident player is unavailable. Repair the station portal bundle."
        )
    return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-store"})
