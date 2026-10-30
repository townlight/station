# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API for the CG bulletin-board designer (S6 V1 — build step 7, slice 3b).

Thin HTTP shell over :class:`~civiccast.cg.board_service.CgBoardService`. Writes
require ``publish_operator`` / ``setup_admin``; reads also allow
``support_admin`` (S6 §4). ``operator_id`` is read from a *verified* token
identity (``request.state.operator_identity``) — never a request body — so the
audit trail can't be spoofed (mirrors the playout/takeover routers). The
``get_cg_board_service`` DI seam returns ``None`` until the app factory wires the
durable store; handlers translate ``None`` into HTTP 503.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from civiccast.auth.models import OperatorIdentity
from civiccast.auth.roles import require_any_role
from civiccast.cable.channel import get_channel_profile
from civiccast.cg.board_models import (
    CgBoard,
    CgBoardAuditEvent,
    CgFeedItemApproval,
    CgFeedSource,
    CgZoneConfig,
)
from civiccast.cg.board_resolver import ResolvedBoard
from civiccast.cg.board_runtime import build_board_snapshot_from_store
from civiccast.cg.board_service import (
    BoardNotFoundError,
    BoardView,
    CgBoardService,
    FeedInput,
    FeedNotFoundError,
    FeedUpdateInput,
    ServiceValidationError,
    ZoneInput,
    ZoneNotFoundError,
    ZoneUpdateInput,
)
from civiccast.cg.depth_models import BulletinAudio
from civiccast.cg.depth_store import CgDepthStore
from civiccast.cg.models import CgFeedItem
from civiccast.egress.board_compositor import ImageResolver, build_board_preview_args
from civiccast.egress.runtime import FfmpegRunner
from civiccast.stream._ffmpeg import run_ffmpeg

_LOG = logging.getLogger(__name__)

_WRITE_ROLES = ("publish_operator", "setup_admin")
_READ_ROLES = ("publish_operator", "setup_admin", "support_admin")

_DB_NOT_READY_DESCRIPTION = "Durable storage not ready -- run Setup storage or set DATABASE_URL"
_DB_NOT_READY_DETAIL = (
    "Durable storage is not ready. Open Setup and choose Prepare storage, "
    "or set DATABASE_URL for a technical deployment."
)

_PREVIEW_BG_COLOR = "#1a2744"
_PREVIEW_HEIGHT = 720
_PREVIEW_WIDTH = 1280
_PREVIEW_FRAME_RATE = 30
_PREVIEW_SEGMENT_SECONDS = 1.0

board_staff_router = APIRouter(prefix="/api/staff/cg", tags=["staff", "cg"])


def get_cg_board_service() -> Any:
    """DI seam: the app factory overrides this with a real CgBoardService.

    Returns None when durable storage is not active; handlers map None to 503.
    Typed as Any to avoid a router->service->store import cycle.
    """


def get_cg_depth_store() -> CgDepthStore | None:
    """Durable channel audio configuration, wired by the application factory."""


def get_cg_audio_asset_resolver() -> ImageResolver | None:
    """Resolve an uploaded asset with an audio stream inside the upload root."""


class BoardCreateRequest(BaseModel):
    """Body for ``POST /channels/{channel_id}/board``."""

    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(..., min_length=1, max_length=120)


class BoardUpdateRequest(BaseModel):
    """Body for ``PATCH /channels/{channel_id}/board``."""

    model_config = ConfigDict(extra="forbid")

    template_id: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None


class BoardAudioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_ref: str = Field(..., min_length=1, max_length=120)
    gain_db: float = Field(default=0.0, ge=-36.0, le=0.0, allow_inf_nan=False)


def _service_or_503(service: Any) -> CgBoardService:
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY_DETAIL
        )
    return cast(CgBoardService, service)


def _operator_id(request: Request) -> str:
    identity = getattr(request.state, "operator_identity", None)
    if not isinstance(identity, OperatorIdentity):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Staff identity is required for this action.",
        )
    return identity.operator_id


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


def _unprocessable(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


def _preview_ffmpeg_runner() -> FfmpegRunner:
    """Dependency seam for preview rendering tests."""

    return run_ffmpeg


def _preview_branding(channel_id: str) -> tuple[str, str]:
    profile = get_channel_profile(channel_id)
    if profile is None:
        return _PREVIEW_BG_COLOR, ""
    return profile.branding.color, profile.branding.short_name


def _preview_board_snapshot(service: Any, channel_id: str) -> ResolvedBoard | None:
    resolved_service = _service_or_503(service)
    store = getattr(resolved_service, "_store", None)
    if store is not None:
        return build_board_snapshot_from_store(
            store,
            channel_id,
            now=datetime.now(UTC),
        )
    return resolved_service.preview(channel_id)


def _preview_image_resolver(service: Any) -> ImageResolver:
    """Resolve image zones for the preview exactly as the on-air path does.

    Passing a null resolver made the preview silently omit logo/image zones that
    DO render on air — breaking the one promise the rendered preview exists to
    make (gate finding: preview parity). Falls back to a null resolver only when
    no session factory is reachable.
    """

    from civiccast.egress.bulletin_filler import _default_image_resolver

    store = getattr(_service_or_503(service), "_store", None)
    session_factory = getattr(store, "_session_factory", None)
    if session_factory is None:
        return lambda _ref: None
    return _default_image_resolver(session_factory)


def _render_board_preview(
    channel_id: str,
    board: ResolvedBoard,
    image_resolver: ImageResolver,
    *,
    ffmpeg_runner: FfmpegRunner,
) -> Response:
    channel_background_color, station_short_name = _preview_branding(channel_id)
    for include_text in (True, False):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            out_path = Path(tmp.name)
        try:
            args = build_board_preview_args(
                board=board,
                bulletin=None,
                width=_PREVIEW_WIDTH,
                height=_PREVIEW_HEIGHT,
                frame_rate=_PREVIEW_FRAME_RATE,
                background_color=channel_background_color,
                station_short_name=station_short_name,
                out_path=out_path,
                include_text=include_text,
                now=datetime.now(UTC),
                image_resolver=image_resolver,
            )
            result = ffmpeg_runner(args)
            if result.returncode != 0:
                if include_text:
                    # Mirror the on-air degradation-warning pattern in
                    # bulletin_filler.py: log loudly, naming the channel and
                    # board, before silently retrying image-only (gate finding
                    # M-1 -- the operator was previously given no signal that
                    # the preview no longer matches what they designed).
                    _LOG.warning(
                        "channel %s board %s: preview text render failed; "
                        "retrying image-only. The preview will NOT match the "
                        "designed board (text zones omitted) until the host's "
                        "fonts are fixed.",
                        channel_id,
                        board.board_id,
                    )
                continue
            image = out_path.read_bytes()
            response = Response(content=image, media_type="image/png")
            if not include_text:
                # Flag the degradation on the response itself so the UI can
                # tell the operator this preview omits text zones, rather than
                # silently rendering an image that doesn't match the design.
                response.headers["X-CivicCast-Preview-Degraded"] = "text-omitted"
            return response
        finally:
            with contextlib.suppress(FileNotFoundError):
                out_path.unlink()
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=(
            "Could not render the board preview. This usually means required "
            "fonts are missing or misconfigured on this host; contact your "
            "system administrator if this persists."
        ),
    )


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------


@board_staff_router.get(
    "/channels/{channel_id}/board",
    response_model=BoardView | None,
    summary="Read the active CG board for a channel (board + zones + feeds)",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    responses={
        200: {
            "description": (
                "The active board, or JSON null when the channel has no board "
                "yet (a normal pre-setup state, not an error)"
            )
        },
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_board(channel_id: str, service: Any = Depends(get_cg_board_service)) -> BoardView | None:
    # "No board yet" is the normal state of every channel before an operator
    # creates one — it is NOT an error, so it must not be a 404. A 404 here
    # made the browser log a red network error on every open of the CG Board /
    # CG Designer screens even though the UI rendered the empty state fine.
    # The client (getCgBoard in portal-operator's api/client.ts) is typed
    # Promise<BoardView | null> and the designer screen renders null as the
    # empty state, so 200 + JSON null is the contract both sides expect.
    return _service_or_503(service).get_board_view(channel_id)


@board_staff_router.post(
    "/channels/{channel_id}/board",
    response_model=CgBoard,
    status_code=status.HTTP_201_CREATED,
    summary="Create (and activate) a CG board for a channel",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def create_board(
    channel_id: str,
    body: BoardCreateRequest,
    request: Request,
    service: Any = Depends(get_cg_board_service),
) -> CgBoard:
    return _service_or_503(service).create_board(
        channel_id, template_id=body.template_id, operator_id=_operator_id(request)
    )


@board_staff_router.patch(
    "/channels/{channel_id}/board",
    response_model=CgBoard,
    summary="Update the active CG board (template / active)",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={
        404: {"description": "No active board"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def update_board(
    channel_id: str,
    body: BoardUpdateRequest,
    request: Request,
    service: Any = Depends(get_cg_board_service),
) -> CgBoard:
    try:
        return _service_or_503(service).update_board(
            channel_id,
            template_id=body.template_id,
            active=body.active,
            operator_id=_operator_id(request),
        )
    except BoardNotFoundError as exc:
        raise _not_found(exc) from exc


def _audio_store_or_503(store: CgDepthStore | None) -> CgDepthStore:
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY_DETAIL)
    return store


@board_staff_router.get(
    "/channels/{channel_id}/board/audio",
    response_model=BulletinAudio | None,
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
)
def get_board_audio(
    channel_id: str, store: CgDepthStore | None = Depends(get_cg_depth_store)
) -> BulletinAudio | None:
    audio = _audio_store_or_503(store).list_audio(scope="channel", target_id=channel_id)
    return audio[-1] if audio else None


@board_staff_router.put(
    "/channels/{channel_id}/board/audio",
    response_model=BulletinAudio,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
)
def set_board_audio(
    channel_id: str,
    body: BoardAudioRequest,
    store: CgDepthStore | None = Depends(get_cg_depth_store),
    audio_resolver: ImageResolver | None = Depends(get_cg_audio_asset_resolver),
) -> BulletinAudio:
    resolved = _audio_store_or_503(store)
    if audio_resolver is None or audio_resolver(body.asset_ref) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Choose an uploaded CivicCast asset with an audio track and measured duration.",
        )
    audio_id = "channel-board-" + hashlib.sha256(channel_id.encode("utf-8")).hexdigest()[:24]
    existing = resolved.list_audio(scope="channel", target_id=channel_id)
    saved = resolved.set_audio(
        BulletinAudio(
            audio_id=audio_id,
            scope="channel",
            target_id=channel_id,
            asset_ref=body.asset_ref,
            gain_db=body.gain_db,
            created_at=next(
                (item.created_at for item in existing if item.audio_id == audio_id),
                datetime.now(UTC),
            ),
        )
    )
    for item in existing:
        if item.audio_id != audio_id:
            resolved.delete_audio(item.audio_id)
    return saved


@board_staff_router.delete(
    "/channels/{channel_id}/board/audio",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
)
def delete_board_audio(
    channel_id: str, store: CgDepthStore | None = Depends(get_cg_depth_store)
) -> Response:
    resolved = _audio_store_or_503(store)
    for item in resolved.list_audio(scope="channel", target_id=channel_id):
        resolved.delete_audio(item.audio_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Zones
# ---------------------------------------------------------------------------


@board_staff_router.post(
    "/channels/{channel_id}/zones",
    response_model=CgZoneConfig,
    status_code=status.HTTP_201_CREATED,
    summary="Add a zone to the active board",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={
        404: {"description": "No active board"},
        422: {"description": "Zone violates a content-source rule"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def add_zone(
    channel_id: str,
    body: ZoneInput,
    request: Request,
    service: Any = Depends(get_cg_board_service),
) -> CgZoneConfig:
    try:
        return _service_or_503(service).add_zone(
            channel_id, payload=body, operator_id=_operator_id(request)
        )
    except BoardNotFoundError as exc:
        raise _not_found(exc) from exc
    except ServiceValidationError as exc:
        raise _unprocessable(exc) from exc


@board_staff_router.patch(
    "/channels/{channel_id}/zones/{zone_id}",
    response_model=CgZoneConfig,
    summary="Update a zone on the active board",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={
        404: {"description": "Zone not on the active board"},
        422: {"description": "Zone violates a content-source rule"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def update_zone(
    channel_id: str,
    zone_id: str,
    body: ZoneUpdateInput,
    request: Request,
    service: Any = Depends(get_cg_board_service),
) -> CgZoneConfig:
    try:
        return _service_or_503(service).update_zone(
            channel_id, zone_id, payload=body, operator_id=_operator_id(request)
        )
    except (BoardNotFoundError, ZoneNotFoundError) as exc:
        raise _not_found(exc) from exc
    except ServiceValidationError as exc:
        raise _unprocessable(exc) from exc


@board_staff_router.delete(
    "/channels/{channel_id}/zones/{zone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a zone from the active board",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={
        404: {"description": "Zone not on the active board"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def delete_zone(
    channel_id: str,
    zone_id: str,
    request: Request,
    service: Any = Depends(get_cg_board_service),
) -> Response:
    try:
        _service_or_503(service).delete_zone(channel_id, zone_id, operator_id=_operator_id(request))
    except (BoardNotFoundError, ZoneNotFoundError) as exc:
        raise _not_found(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Feed sources
# ---------------------------------------------------------------------------


@board_staff_router.get(
    "/channels/{channel_id}/feeds",
    response_model=list[CgFeedSource],
    summary="List the channel's registered CG feed sources",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def list_feeds(channel_id: str, service: Any = Depends(get_cg_board_service)) -> list[CgFeedSource]:
    return _service_or_503(service).list_feeds(channel_id)


@board_staff_router.post(
    "/channels/{channel_id}/feeds",
    response_model=CgFeedSource,
    status_code=status.HTTP_201_CREATED,
    summary="Register a CG feed source",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={
        422: {"description": "Feed violates a trust-tier rule"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def create_feed(
    channel_id: str,
    body: FeedInput,
    request: Request,
    service: Any = Depends(get_cg_board_service),
) -> CgFeedSource:
    try:
        return _service_or_503(service).add_feed(
            channel_id, payload=body, operator_id=_operator_id(request)
        )
    except ServiceValidationError as exc:
        raise _unprocessable(exc) from exc


@board_staff_router.patch(
    "/channels/{channel_id}/feeds/{feed_source_id}",
    response_model=CgFeedSource,
    summary="Update a CG feed source",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={
        404: {"description": "Feed not on this channel"},
        422: {"description": "Feed violates a trust-tier rule"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def update_feed(
    channel_id: str,
    feed_source_id: str,
    body: FeedUpdateInput,
    request: Request,
    service: Any = Depends(get_cg_board_service),
) -> CgFeedSource:
    try:
        return _service_or_503(service).update_feed(
            channel_id, feed_source_id, payload=body, operator_id=_operator_id(request)
        )
    except FeedNotFoundError as exc:
        raise _not_found(exc) from exc
    except ServiceValidationError as exc:
        raise _unprocessable(exc) from exc


@board_staff_router.delete(
    "/channels/{channel_id}/feeds/{feed_source_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a CG feed source",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={
        404: {"description": "Feed not on this channel"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def delete_feed(
    channel_id: str,
    feed_source_id: str,
    request: Request,
    service: Any = Depends(get_cg_board_service),
) -> Response:
    try:
        _service_or_503(service).delete_feed(
            channel_id, feed_source_id, operator_id=_operator_id(request)
        )
    except FeedNotFoundError as exc:
        raise _not_found(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@board_staff_router.post(
    "/channels/{channel_id}/feeds/{feed_source_id}/items/{item_id:path}/approve",
    response_model=CgFeedItemApproval,
    status_code=status.HTTP_201_CREATED,
    summary="Approve a feed item for an approval-gated zone",
    dependencies=[Depends(require_any_role(*_WRITE_ROLES))],
    responses={
        404: {"description": "Feed not on this channel"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def approve_feed_item(
    channel_id: str,
    feed_source_id: str,
    item_id: str,
    request: Request,
    service: Any = Depends(get_cg_board_service),
) -> CgFeedItemApproval:
    try:
        return _service_or_503(service).approve_feed_item(
            channel_id,
            feed_source_id=feed_source_id,
            item_id=item_id,
            operator_id=_operator_id(request),
        )
    except FeedNotFoundError as exc:
        raise _not_found(exc) from exc


_FEED_FETCH_TIMEOUT = 10.0  # seconds; monkeypatchable in tests


@board_staff_router.get(
    "/channels/{channel_id}/feeds/{feed_source_id}/items",
    response_model=list[CgFeedItem],
    summary="List a feed's current items with approval status (the review queue)",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    responses={
        404: {"description": "Feed not on this channel"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
async def list_feed_items_for_review(
    channel_id: str,
    feed_source_id: str,
    service: Any = Depends(get_cg_board_service),
) -> list[CgFeedItem]:
    # Fetches the feed live (SSRF-guarded) and stamps each item's real
    # approved/pending status. Runs in a thread so the sync fetch doesn't
    # block the event loop. asyncio.wait_for bounds how long THIS request
    # waits (504 on timeout) — it does NOT cancel the worker thread, which
    # runs to completion. The real backstop against a wedged feed is the
    # fetch's own socket timeout + response-size cap in the service layer;
    # wait_for keeps one slow feed from blocking the caller indefinitely,
    # not from briefly occupying a threadpool slot.
    svc = _service_or_503(service)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                svc.list_feed_items_for_review,
                channel_id,
                feed_source_id=feed_source_id,
            ),
            timeout=_FEED_FETCH_TIMEOUT,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Feed fetch timed out after {_FEED_FETCH_TIMEOUT:.0f}s.",
        ) from exc
    except FeedNotFoundError as exc:
        raise _not_found(exc) from exc


# ---------------------------------------------------------------------------
# Preview + audit
# ---------------------------------------------------------------------------


@board_staff_router.get(
    "/channels/{channel_id}/preview",
    response_model=ResolvedBoard,
    summary="Render a live preview snapshot of the active board",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    responses={
        400: {"description": "Unsupported preview format"},
        404: {"description": "No active board"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def preview_board(
    channel_id: str,
    format_: Annotated[str | None, Query(alias="format")] = None,
    ffmpeg_runner: FfmpegRunner = Depends(_preview_ffmpeg_runner),
    service: Any = Depends(get_cg_board_service),
) -> Response | ResolvedBoard:
    resolved = _preview_board_snapshot(service, channel_id)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active CG board for channel {channel_id!r}.",
        )
    if format_ is not None:
        if format_.lower() == "png":
            return _render_board_preview(
                channel_id,
                resolved,
                _preview_image_resolver(service),
                ffmpeg_runner=ffmpeg_runner,
            )
        if format_.lower() != "json":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unsupported preview format. Use 'json' or 'png'.",
            )
    return resolved


@board_staff_router.get(
    "/channels/{channel_id}/preview.png",
    summary="Render a PNG preview snapshot of the active board",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    responses={
        404: {"description": "No active board"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def preview_board_png(
    channel_id: str,
    ffmpeg_runner: FfmpegRunner = Depends(_preview_ffmpeg_runner),
    service: Any = Depends(get_cg_board_service),
) -> Response:
    resolved = _preview_board_snapshot(service, channel_id)
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No active CG board for channel {channel_id!r}.",
        )
    return _render_board_preview(
        channel_id,
        resolved,
        _preview_image_resolver(service),
        ffmpeg_runner=ffmpeg_runner,
    )


@board_staff_router.get(
    "/channels/{channel_id}/board/audit",
    response_model=list[CgBoardAuditEvent],
    summary="List board-lifecycle audit events (newest first)",
    dependencies=[Depends(require_any_role(*_READ_ROLES))],
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def board_audit(
    channel_id: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    service: Any = Depends(get_cg_board_service),
) -> list[CgBoardAuditEvent]:
    return _service_or_503(service).list_audit(channel_id, limit=limit, offset=offset)
