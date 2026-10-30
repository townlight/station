# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Public and staff playback policy routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError

from civiccast.auth.roles import require_any_role
from civiccast.playback_policy.entitlements import (
    ViewerTokenError,
    issue_viewer_token,
    viewer_from_token,
)
from civiccast.playback_policy.models import (
    PlaybackPolicyAuditLog,
    PlaybackPolicyConfig,
    PlaybackPolicyEvaluation,
    PlaybackPolicyEvaluationRequest,
    PlaybackPolicyUpdate,
    PublicPlaybackPolicyEvaluationRequest,
    ViewerTokenRequest,
    ViewerTokenResponse,
)
from civiccast.playback_policy.store import PlaybackPolicyStore, default_playback_policy_state_path

public_router = APIRouter(prefix="/api/public/playback-policy", tags=["public", "playback-policy"])
staff_router = APIRouter(prefix="/api/staff/playback-policy", tags=["staff", "playback-policy"])


def get_playback_policy_store(request: Request) -> PlaybackPolicyStore:
    store = getattr(request.app.state, "playback_policy_store", None)
    if isinstance(store, PlaybackPolicyStore):
        return store
    store = PlaybackPolicyStore(default_playback_policy_state_path())
    request.app.state.playback_policy_store = store
    return store


@public_router.post(
    "/evaluate",
    response_model=PlaybackPolicyEvaluation,
    summary="Evaluate playback access and preroll policy",
)
def evaluate_playback_policy(
    payload: PublicPlaybackPolicyEvaluationRequest,
    store: PlaybackPolicyStore = Depends(get_playback_policy_store),
) -> PlaybackPolicyEvaluation:
    viewer = None
    if payload.viewer_token:
        try:
            viewer = viewer_from_token(payload.viewer_token)
        except (ValueError, ViewerTokenError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
            ) from exc
    request = PlaybackPolicyEvaluationRequest(
        asset_id=payload.asset_id,
        channel_id=payload.channel_id,
        viewer=viewer,
    )
    return store.evaluate(request)


@staff_router.get(
    "/audit/events",
    response_model=PlaybackPolicyAuditLog,
    summary="Read playback policy decision audit log",
)
def read_playback_policy_audit_log(
    store: PlaybackPolicyStore = Depends(get_playback_policy_store),
) -> PlaybackPolicyAuditLog:
    return store.audit_log()


@staff_router.post(
    "/viewer-tokens",
    response_model=ViewerTokenResponse,
    summary="Issue a signed resident playback entitlement token",
    dependencies=[Depends(require_any_role("publish_operator", "support_admin"))],
)
def issue_playback_viewer_token(payload: ViewerTokenRequest) -> ViewerTokenResponse:
    return issue_viewer_token(payload)


@staff_router.post(
    "/{subject_type}/{subject_id}",
    response_model=PlaybackPolicyConfig,
    summary="Update one channel or asset playback policy",
    dependencies=[Depends(require_any_role("publish_operator", "support_admin"))],
)
def update_playback_policy(
    subject_type: Literal["channel", "asset"],
    subject_id: str,
    payload: PlaybackPolicyUpdate,
    store: PlaybackPolicyStore = Depends(get_playback_policy_store),
) -> PlaybackPolicyConfig:
    try:
        return store.upsert_policy(subject_type, subject_id, payload)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@staff_router.get(
    "/{subject_type}/{subject_id}",
    response_model=PlaybackPolicyConfig,
    summary="Read one channel or asset playback policy",
)
def read_playback_policy(
    subject_type: Literal["channel", "asset"],
    subject_id: str,
    store: PlaybackPolicyStore = Depends(get_playback_policy_store),
) -> PlaybackPolicyConfig:
    return store.get_policy(subject_type, subject_id)
