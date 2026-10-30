# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI middleware for first-party staff bearer authentication."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request, Response, status
from starlette.responses import JSONResponse

from civiccast.auth.rate_limit import (
    AuthRateLimiter,
    auth_rate_limit_config,
    client_ip,
)
from civiccast.auth.tokens import (
    StaffAuthError,
    StaffAuthMissingCredentialError,
    token_matches_exactly,
    verify_bearer_token,
)

_MISSING_CREDENTIAL_DETAIL = "Missing Authorization header. Use Bearer <staff-token>."


async def staff_auth_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Authenticate every /api/staff/* request and attach operator identity.

    Failed verifications are rate-limited per client IP (audit item #27):
    without this, /api/staff/* is an unthrottled bearer-token oracle. Only
    FAILURES count -- valid requests are never recorded against the limit, so
    a noisy client sharing the operator's proxy/NAT cannot deny access. The
    key is the client IP alone, NOT ip+token-prefix: per-prefix buckets hand a
    token-rotating brute-forcer a fresh budget per guess. Once saturated,
    exact full-token matches bypass the unknown-token budget. Exact misses
    receive 429 plus Retry-After without expensive verification or a queue in
    front of a valid operator.
    State is process-local, so ingress controls remain required for distributed
    or multi-worker lookup and request load.

    OWNER DECISION 2026-08-30 (audit finding #1, day-one-lockout fix): a
    request carrying NO Authorization header at all is not a failed
    credential guess -- it is the ordinary state of a signed-out browser
    loading any staff-console page, and every one of those page loads calls
    this same middleware. Before this fix, a missing header was verified via
    the same code path as a wrong token, so it both consumed the failure
    budget (a handful of ordinary, credential-free page loads exhausted it)
    and was itself blocked by the saturation pre-check below once that
    budget ran out -- a brand-new operator who had never typed a password
    could 429-lock themselves out just by opening the console. A missing
    header is now recognized before either the pre-check or the budget is
    touched, and always gets a plain 401 with no rate-limit interaction. A
    present-but-wrong, malformed, revoked, or expired token still counts,
    exactly as before.
    """

    syncer = getattr(request.app.state, "sync_durable_storage", None)
    if callable(syncer):
        syncer()
    if not request.url.path.startswith("/api/staff/"):
        return await call_next(request)
    if request.method == "POST" and request.url.path == "/api/staff/auth/login":
        # Password login is the sole unauthenticated staff-auth route. Its
        # handler applies the same failure-only limiter used by setup login.
        return await call_next(request)

    rejected = authenticate_staff_request(request)
    if rejected is not None:
        return rejected
    return await call_next(request)


def authenticate_staff_request(request: Request) -> JSONResponse | None:
    """Verify a request's staff bearer and attach the identity, or return the refusal.

    The one token-checking routine every staff-token-gated surface shares:
    :func:`staff_auth_middleware` for ``/api/staff/*`` and the LAN-only
    ``/openapi.json`` route in :mod:`civiccast.app` (MINOR-3, hostile review
    of PR #215: that route verified the bearer itself and never touched the
    limiter, an unthrottled token oracle next to throttled ones). Returns
    ``None`` after setting ``request.state.operator_identity``, or the 401/429
    :class:`JSONResponse` to send instead. Same accounting as documented on
    the middleware: a missing header is a budget-free 401; a present-but-wrong
    token spends one ``staff-auth-fail:<ip>`` hit; a saturated budget answers
    429 unless the token matches exactly.
    """

    authorization = request.headers.get("Authorization")
    if not authorization:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": _MISSING_CREDENTIAL_DETAIL},
            headers={"WWW-Authenticate": "Bearer"},
        )

    limiter = getattr(request.app.state, "auth_rate_limiter", None)
    limit, window_seconds = auth_rate_limit_config()
    key = f"staff-auth-fail:{client_ip(request)}"
    token_store = getattr(request.app.state, "staff_token_store", None)
    if (
        isinstance(limiter, AuthRateLimiter)
        and limiter.saturated(key, limit=limit, window_seconds=window_seconds)
        and not token_matches_exactly(authorization, token_store=token_store)
    ):
        return _staff_rate_limited_response(
            limiter,
            key=key,
            window_seconds=window_seconds,
        )
    try:
        request.state.operator_identity = verify_bearer_token(
            authorization,
            token_store=token_store,
        )
    except StaffAuthMissingCredentialError as exc:
        # Unreachable in practice (the early return above already handles a
        # missing header) but kept so this branch fails safe -- never
        # spending budget on a missing credential -- if verify_bearer_token
        # is ever reordered or reused ahead of that check.
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        )
    except StaffAuthError as exc:
        if isinstance(limiter, AuthRateLimiter) and not limiter.allow(
            key, limit=limit, window_seconds=window_seconds
        ):
            return _staff_rate_limited_response(
                limiter,
                key=key,
                window_seconds=window_seconds,
            )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": str(exc)},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None


def _staff_rate_limited_response(
    limiter: AuthRateLimiter,
    *,
    key: str,
    window_seconds: int,
) -> JSONResponse:
    retry_after = limiter.retry_after_seconds(key, window_seconds=window_seconds)
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "Too many failed staff authentication attempts. Wait and retry."},
        headers={"Retry-After": str(retry_after)},
    )
