# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Server-signed resident playback entitlement tokens."""

from __future__ import annotations

from datetime import UTC, datetime

from civiccast.playback_policy.models import ViewerAccount, ViewerTokenRequest, ViewerTokenResponse
from civiccast.subscribe.crypto import signed_token, verify_token
from civiccast.subscribe.secrets import load_subscription_secrets

_TOKEN_ACTION = "playback-entitlement"  # noqa: S105 - token audience label, not a secret.


class ViewerTokenError(ValueError):
    """Raised when a resident playback entitlement token is invalid."""


def issue_viewer_token(request: ViewerTokenRequest) -> ViewerTokenResponse:
    viewer = ViewerAccount(
        account_id=request.account_id,
        display_name=request.display_name,
        invite_groups=request.invite_groups,
        oidc_subject=request.oidc_subject,
    )
    payload: dict[str, object] = {
        "action": _TOKEN_ACTION,
        "account_id": viewer.account_id,
        "display_name": viewer.display_name,
        "invite_groups": list(viewer.invite_groups),
    }
    if viewer.oidc_subject:
        payload["oidc_subject"] = viewer.oidc_subject
    if request.expires_at is not None:
        payload["expires_at"] = request.expires_at.astimezone(UTC).isoformat()
    token = signed_token(payload, load_subscription_secrets().token_secret)
    return ViewerTokenResponse(viewer=viewer, token=token, expires_at=request.expires_at)


def viewer_from_token(token: str) -> ViewerAccount:
    payload = verify_token(token, load_subscription_secrets().token_secret)
    if payload.get("action") != _TOKEN_ACTION:
        raise ViewerTokenError("Playback entitlement token is not valid for playback.")
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, str):
        expires = datetime.fromisoformat(expires_at)
        if expires.astimezone(UTC) < datetime.now(UTC):
            raise ViewerTokenError("Playback entitlement token has expired.")
    invite_groups = payload.get("invite_groups", [])
    if not isinstance(invite_groups, list) or not all(
        isinstance(group, str) for group in invite_groups
    ):
        raise ViewerTokenError("Playback entitlement token has invalid invite groups.")
    account_id = payload.get("account_id")
    display_name = payload.get("display_name")
    oidc_subject = payload.get("oidc_subject")
    if not isinstance(account_id, str) or not isinstance(display_name, str):
        raise ViewerTokenError("Playback entitlement token is missing viewer identity.")
    if oidc_subject is not None and not isinstance(oidc_subject, str):
        raise ViewerTokenError("Playback entitlement token has invalid OIDC subject.")
    return ViewerAccount(
        account_id=account_id,
        display_name=display_name,
        invite_groups=invite_groups,
        oidc_subject=oidc_subject,
    )
