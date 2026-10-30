# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Cloudflare R2 concierge: turn one pasted API token into working CDN credentials.

PEG stations and schools do not arrive holding a Cloudflare account id, an R2
access key, a secret key, a bucket, and a public URL -- the five fields
``_PROVIDER_CREDENTIAL_FIELDS["cloudflare-r2"]`` (installer.service) normally
asks an operator to paste by hand. This module collapses that to one token:

1. Verify the token (``/user/tokens/verify``) -- also gives us the token's own
   id, which doubles as the R2 S3 access key id (see step 5).
2. Resolve the Cloudflare account the token can see.
3. Create the R2 bucket (idempotent -- "already exists" is fine).
4. Enable the bucket's managed ``pub-<hash>.r2.dev`` public domain.
5. Derive the S3-compatible keypair Cloudflare documents for R2 tokens:
   access_key_id = the token's own id, secret_access_key = the lowercase hex
   SHA-256 of the token *value*. One token, no second minting step.

The pasted token is used in-memory only for these calls and is never logged,
returned, or persisted -- only the derived keypair + ids (the same shape
``build_cdn_adapter_from_credentials`` already expects) are handed back for
the caller to store via the existing credential-store path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal

import httpx

__all__ = ["ConciergeErrorCode", "ConciergeResult", "provision_r2"]

_API_BASE = "https://api.cloudflare.com/client/v4"
_R2_ENABLE_DEEP_LINK = "https://dash.cloudflare.com/?to=/:account/r2"
_R2_NOT_ENABLED_CODE = 10042

# ponytail: Cloudflare does not publicly document the exact error code for
# "bucket already exists" (checked developers.cloudflare.com 2026-07-07); a
# message substring match is the robust check regardless of the exact code.
# Upgrade to an exact code set once observed against the live API.
_BUCKET_EXISTS_HINT = "already exists"

ConciergeErrorCode = Literal[
    "invalid_token",
    "r2_not_enabled",
    "no_account",
    "bucket_error",
    "domain_error",
]


@dataclass(frozen=True)
class ConciergeResult:
    """Outcome of :func:`provision_r2`.

    ``credential_fields`` uses exactly the field ids
    ``installer.service._PROVIDER_CREDENTIAL_FIELDS["cloudflare-r2"]`` expects
    (``account_id``, ``access_key_id``, ``secret_access_key``, ``bucket``,
    ``public_base_url``), so a caller can pass it straight to
    ``save_provider_credentials`` / ``ProviderCredentialSetupRequest.values``.
    """

    status: Literal["success", "error"]
    message: str
    error_code: ConciergeErrorCode | None = None
    deep_link: str | None = None
    account_id: str | None = None
    bucket: str | None = None
    public_base_url: str | None = None
    credential_fields: dict[str, str] = field(default_factory=dict)


def _error(
    code: ConciergeErrorCode, message: str, *, deep_link: str | None = None
) -> ConciergeResult:
    return ConciergeResult(status="error", error_code=code, message=message, deep_link=deep_link)


def _cf_errors(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors")
    return [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []


def _cf_error_summary(payload: object) -> str:
    messages = [str(e.get("message", "")) for e in _cf_errors(payload)]
    return "; ".join(m for m in messages if m) or "Cloudflare returned an error."


def provision_r2(
    token: str,
    *,
    bucket_name: str,
    http: httpx.Client,
    account_id: str | None = None,
) -> ConciergeResult:
    """Provision an R2 bucket + public domain from one Cloudflare API token.

    ``http`` is an injected ``httpx.Client`` (real, or one built on a mock
    transport in tests) so no network call happens without the caller's
    say-so. ``account_id`` may be passed explicitly when the token can see
    more than one account; otherwise the token must see exactly one.
    """
    token = token.strip()
    if not token:
        return _error("invalid_token", "Paste a Cloudflare API token.")
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Verify the token and capture its own id (-> S3 access_key_id).
    try:
        verify_resp = http.get(f"{_API_BASE}/user/tokens/verify", headers=headers)
    except httpx.HTTPError as exc:
        return _error("invalid_token", f"Could not reach Cloudflare to verify the token: {exc}.")
    try:
        verify_payload = verify_resp.json()
    except ValueError:
        verify_payload = {}
    if verify_resp.status_code != 200 or not verify_payload.get("success"):
        if verify_resp.status_code == 429 or verify_resp.status_code >= 500:
            return _error(
                "invalid_token",
                "Cloudflare is temporarily unavailable. Please try again in a moment.",
            )
        return _error(
            "invalid_token",
            "Cloudflare rejected this token. Create a new token scoped to R2 Edit and paste it again.",
        )
    result = verify_payload.get("result")
    token_id = result.get("id") if isinstance(result, dict) else None
    if not token_id:
        return _error("invalid_token", "Cloudflare did not return a token id for this token.")
    token_status = result.get("status") if isinstance(result, dict) else None
    if token_status is not None and token_status != "active":  # noqa: S105 - status enum value, not a secret
        return _error(
            "invalid_token",
            f"This token is {token_status}, not active. Create a new token and paste it again.",
        )

    # 2. Resolve the account the token can see.
    resolved_account_id = account_id
    if resolved_account_id is None:
        try:
            accounts_resp = http.get(f"{_API_BASE}/accounts", headers=headers)
        except httpx.HTTPError as exc:
            return _error(
                "no_account", f"Could not list Cloudflare accounts for this token: {exc}."
            )
        try:
            accounts_payload = accounts_resp.json()
        except ValueError:
            accounts_payload = {}
        if accounts_resp.status_code != 200 or not accounts_payload.get("success"):
            if accounts_resp.status_code == 429 or accounts_resp.status_code >= 500:
                return _error(
                    "no_account",
                    "Cloudflare is temporarily unavailable while listing accounts. "
                    "Please try again in a moment.",
                )
            return _error("no_account", _cf_error_summary(accounts_payload))
        accounts = accounts_payload.get("result")
        accounts = accounts if isinstance(accounts, list) else []
        if len(accounts) == 0:
            return _error(
                "no_account",
                "This token cannot see any Cloudflare account. Check the token's account scope.",
            )
        if len(accounts) > 1:
            names = ", ".join(
                str(a.get("name", a.get("id", "?"))) for a in accounts if isinstance(a, dict)
            )
            return _error(
                "no_account",
                f"This token can see {len(accounts)} Cloudflare accounts ({names}). "
                "Provision again with a specific account.",
            )
        first_account = accounts[0] if isinstance(accounts[0], dict) else {}
        resolved_account_id = str(first_account.get("id") or "")
        if not resolved_account_id:
            return _error("no_account", "Cloudflare did not return an account id for this token.")

    # 3. Create the bucket (idempotent: already-exists is fine).
    try:
        bucket_resp = http.post(
            f"{_API_BASE}/accounts/{resolved_account_id}/r2/buckets",
            headers=headers,
            json={"name": bucket_name},
        )
    except httpx.HTTPError as exc:
        return _error("bucket_error", f"Could not create the R2 bucket: {exc}.")
    try:
        bucket_payload = bucket_resp.json()
    except ValueError:
        bucket_payload = {}
    if not bucket_payload.get("success"):
        errors = _cf_errors(bucket_payload)
        if any(e.get("code") == _R2_NOT_ENABLED_CODE for e in errors):
            return _error(
                "r2_not_enabled",
                "R2 is not enabled on this Cloudflare account yet. This is a one-time, "
                "free-tier-available step -- Cloudflare may still ask for a payment method "
                "even at $0. Enable it, then retry.",
                deep_link=_R2_ENABLE_DEEP_LINK,
            )
        summary = _cf_error_summary(bucket_payload)
        if _BUCKET_EXISTS_HINT not in summary.lower():
            return _error("bucket_error", f"Could not create the R2 bucket: {summary}.")
        # Already exists under this token/account -- treat as done.

    # 4. Enable the managed public r2.dev domain.
    try:
        domain_resp = http.put(
            f"{_API_BASE}/accounts/{resolved_account_id}/r2/buckets/{bucket_name}/domains/managed",
            headers=headers,
            json={"enabled": True},
        )
    except httpx.HTTPError as exc:
        return _error("domain_error", f"Could not enable the public R2 domain: {exc}.")
    try:
        domain_payload = domain_resp.json()
    except ValueError:
        domain_payload = {}
    if not domain_payload.get("success"):
        return _error(
            "domain_error",
            f"Could not enable the public R2 domain: {_cf_error_summary(domain_payload)}.",
        )
    domain_result = domain_payload.get("result")
    public_domain = domain_result.get("domain") if isinstance(domain_result, dict) else None
    if not public_domain:
        return _error("domain_error", "Cloudflare did not return a public domain for this bucket.")

    # 5. Derive the R2 S3-compatible keypair from the one pasted token.
    access_key_id = str(token_id)
    secret_access_key = sha256(token.encode("utf-8")).hexdigest()
    public_base_url = f"https://{public_domain}"

    credential_fields = {
        "account_id": resolved_account_id,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "bucket": bucket_name,
        "public_base_url": public_base_url,
    }
    return ConciergeResult(
        status="success",
        message="R2 storage is ready: bucket created and its public domain enabled.",
        account_id=resolved_account_id,
        bucket=bucket_name,
        public_base_url=public_base_url,
        credential_fields=credential_fields,
    )
