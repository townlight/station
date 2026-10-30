# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Bearer-token verification helpers for CivicCast staff routes."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets

from civiccast.auth.models import OperatorIdentity
from civiccast.auth.roles import unknown_role_scopes
from civiccast.auth.store import (
    StaffTokenInvalidError,
    StaffTokenRevokedError,
    StaffTokenStore,
    StaffTokenUpgradeRequiredError,
)
from civiccast.installer.station_state import verify_station_operator_token


class StaffAuthError(RuntimeError):
    """Raised when a staff bearer token cannot be verified."""


class StaffAuthMissingCredentialError(StaffAuthError):
    """Raised when the request carried no Authorization header at all.

    OWNER DECISION 2026-08-30 (audit finding #1, day-one-lockout fix): this
    is a distinct condition from every other :class:`StaffAuthError`. A
    present-but-wrong, malformed, revoked, or expired token is a failed
    credential guess -- brute-force protection must count it. No credential
    at all is simply the normal state of a signed-out browser loading any
    ``/api/staff/*`` page; it is not a guess, and must never be treated as
    one. See ``civiccast.auth.middleware.staff_auth_middleware``, which
    catches this subclass separately and never spends failure-budget on it.
    """


_CONFIGURED_TOKEN_PREFIX = "ccenv1_"  # noqa: S105 - public format marker, not a secret.
_CONFIGURED_TOKEN_PATTERN = re.compile(r"^ccenv1_[A-Za-z0-9_-]{43}$")


def generate_configured_staff_token() -> str:
    """Return a versioned, high-entropy secret for the legacy environment path."""

    while True:
        token = _CONFIGURED_TOKEN_PREFIX + secrets.token_urlsafe(32)
        if _is_configured_staff_token(token):
            return token


def _is_configured_staff_token(token: str) -> bool:
    if _CONFIGURED_TOKEN_PATTERN.fullmatch(token) is None:
        return False
    random_material = token.removeprefix(_CONFIGURED_TOKEN_PREFIX)
    return len(set(random_material)) >= 8


def _configured_tokens() -> dict[str, OperatorIdentity]:
    """Return configured staff tokens.

    Deployments should set CIVICCAST_STAFF_TOKENS to semicolon-separated
    entries of ``token:operator_id:Operator Name:role[,role...]`` using the
    v1.4 product roles (or the ``operator``/``admin`` all-roles aliases).
    **Roles are required**: a token configured without roles is rejected here
    (and at app startup via :func:`validate_staff_token_config`) rather than
    silently granted full admin — the pre-2026-06-09 fail-open behavior was
    removed per the Stage B+D audit (QA-002).
    Deterministic local tests can opt in to the documented fixture token with
    CIVICCAST_ALLOW_DETERMINISTIC_STAFF_TOKEN=1.
    """

    configured = os.environ.get("CIVICCAST_STAFF_TOKENS", "").strip()
    if not configured:
        if os.environ.get("CIVICCAST_ALLOW_DETERMINISTIC_STAFF_TOKEN") == "1":
            return {
                "operator-token-a": OperatorIdentity(
                    operator_id="operator-token-a",
                    operator_display_name="Token Identity A",
                    token_id="default-local-operator",  # noqa: S106 - deterministic local test token id.
                    scopes=("operator",),
                )
            }
        raise StaffAuthError(
            "CIVICCAST_STAFF_TOKENS is not configured. Set token:operator_id:"
            "display_name:role entries before enabling staff routes."
        )
    identities: dict[str, OperatorIdentity] = {}
    for item in configured.split(";"):
        token, sep, rest = item.partition(":")
        operator_id, sep2, display_and_scopes = rest.partition(":")
        display_name, _sep3, scopes_text = display_and_scopes.partition(":")
        if not token or not sep or not operator_id or not sep2 or not display_name:
            raise StaffAuthError(
                "CIVICCAST_STAFF_TOKENS must contain "
                "token:operator_id:display_name:role[,role] entries."
            )
        scopes = tuple(scope.strip() for scope in scopes_text.split(",") if scope.strip())
        if not scopes:
            raise StaffAuthError(
                f"CIVICCAST_STAFF_TOKENS entry for operator {operator_id!r} has "
                "no roles. Name the roles the token grants, e.g. "
                "token:operator_id:Display Name:meeting_operator,setup_admin "
                "(or 'operator' for all roles). Role-less tokens were "
                "previously granted full admin silently; that fail-open "
                "behavior has been removed."
            )
        unknown_scopes = unknown_role_scopes(scopes)
        if unknown_scopes:
            labels = ", ".join(unknown_scopes)
            raise StaffAuthError(
                f"CIVICCAST_STAFF_TOKENS entry for operator {operator_id!r} has "
                f"unknown role scope(s): {labels}. Use one of setup_admin, "
                "meeting_operator, records_clerk, publish_operator, support_admin "
                "or the documented aliases admin/operator."
            )
        if (
            not _is_configured_staff_token(token)
            and os.environ.get("CIVICCAST_ALLOW_DETERMINISTIC_STAFF_TOKEN") != "1"
        ):
            raise StaffAuthError(
                "CIVICCAST_STAFF_TOKENS bearer secrets must use CivicCast's "
                "current versioned format. Generate each secret with "
                "`civiccast token generate-env` instead of writing one by hand."
            )
        identities[token] = OperatorIdentity(
            operator_id=operator_id,
            operator_display_name=display_name,
            token_id="env-" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16],
            scopes=scopes,
        )
    return identities


def validate_staff_token_config() -> None:
    """Fail fast at app startup when CIVICCAST_STAFF_TOKENS is malformed.

    Parses the configured env tokens (rejecting role-less entries, QA-002) so
    an operator sees the configuration error at boot instead of discovering it
    on the first staff request. A completely unset CIVICCAST_STAFF_TOKENS is
    fine here — deployments may use DB-issued or station-state tokens only.
    """

    if not os.environ.get("CIVICCAST_STAFF_TOKENS", "").strip():
        return
    _configured_tokens()


def verify_bearer_token(
    authorization: str | None,
    *,
    token_store: StaffTokenStore | None = None,
) -> OperatorIdentity:
    """Verify an Authorization header and return server-owned identity."""

    token = _bearer_token(authorization)
    if token_store is not None:
        try:
            return token_store.verify_token(token)
        except StaffTokenRevokedError as exc:
            raise StaffAuthError("Staff bearer token has been revoked.") from exc
        except StaffTokenUpgradeRequiredError as exc:
            raise StaffAuthError(str(exc)) from exc
        except StaffTokenInvalidError as exc:
            with_station = verify_station_operator_token(token)
            if with_station is not None:
                return with_station
            if os.environ.get("CIVICCAST_STAFF_TOKENS_FALLBACK_WITH_DB") == "1":
                with_env = _verify_configured_token(token)
                if with_env is not None:
                    return with_env
            raise StaffAuthError("Invalid staff bearer token.") from exc
    with_station = verify_station_operator_token(token)
    if with_station is not None:
        return with_station
    with_env = _verify_configured_token(token)
    if with_env is not None:
        return with_env
    raise StaffAuthError("Invalid staff bearer token.")


def token_matches_exactly(
    authorization: str | None,
    *,
    token_store: StaffTokenStore | None = None,
) -> bool:
    """Cheaply recognize exact valid tokens before rejecting saturated guesses.

    Pre-fingerprint lifecycle tokens do not bypass. Their authoritative verifier
    returns a precise rotation instruction instead of accepting an attacker-
    controlled public token ID as evidence of validity.
    """

    try:
        token = _bearer_token(authorization)
    except StaffAuthError:
        return False
    if token_store is not None:
        fingerprint_match = token_store.matches_token_fingerprint(token)
        if fingerprint_match is True:
            return True
        if verify_station_operator_token(token) is not None:
            return True
        return os.environ.get(
            "CIVICCAST_STAFF_TOKENS_FALLBACK_WITH_DB"
        ) == "1" and _configured_token_matches_without_error(token)
    if verify_station_operator_token(token) is not None:
        return True
    return _configured_token_matches_without_error(token)


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise StaffAuthMissingCredentialError(
            "Missing Authorization header. Use Bearer <staff-token>."
        )
    scheme, sep, token = authorization.partition(" ")
    if not sep or scheme.lower() != "bearer" or not token.strip():
        raise StaffAuthError("Invalid Authorization header. Use Bearer <staff-token>.")
    return token.strip()


def _configured_token_matches_without_error(token: str) -> bool:
    try:
        return _verify_configured_token(token) is not None
    except StaffAuthError:
        return False


def _verify_configured_token(token: str) -> OperatorIdentity | None:
    for candidate, identity in _configured_tokens().items():
        if hmac.compare_digest(candidate, token):
            return identity
    return None
