# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Local product-role checks for staff routes."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from fastapi import Request, status
from starlette.exceptions import HTTPException

from civiccast.auth.models import OperatorIdentity, OperatorRole

ALL_OPERATOR_ROLES: tuple[OperatorRole, ...] = (
    "setup_admin",
    "meeting_operator",
    "records_clerk",
    "publish_operator",
    "support_admin",
)

_ROLE_ALIASES: dict[str, tuple[OperatorRole, ...]] = {
    "admin": ALL_OPERATOR_ROLES,
    "operator": ALL_OPERATOR_ROLES,
    "setup": ("setup_admin",),
    "setup-admin": ("setup_admin",),
    "setup_admin": ("setup_admin",),
    "meeting": ("meeting_operator",),
    "meeting-operator": ("meeting_operator",),
    "meeting_operator": ("meeting_operator",),
    "records": ("records_clerk",),
    "records-clerk": ("records_clerk",),
    "records_clerk": ("records_clerk",),
    "publish": ("publish_operator",),
    "publish-operator": ("publish_operator",),
    "publish_operator": ("publish_operator",),
    "support": ("support_admin",),
    "support-admin": ("support_admin",),
    "support_admin": ("support_admin",),
}


def roles_for_identity(identity: OperatorIdentity) -> set[OperatorRole]:
    """Expand token scopes into v1.4 product roles.

    Fail-closed: an identity with no scopes gets **no** roles. (Until the
    Stage B+D audit, QA-002, empty scopes silently expanded to all five roles —
    a fail-open boundary on every role-gated route. Every legitimate identity
    source names its scopes: env tokens are validated at config load, the DB
    token store defaults to ``("operator",)``, and the station first-admin
    path issues ``("admin",)``.)
    """

    roles: set[OperatorRole] = set()
    for scope in identity.scopes:
        roles.update(_ROLE_ALIASES.get(scope.strip().lower(), ()))
    return roles


def unknown_role_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    """Return token scopes that do not map to any CivicCast product role."""

    unknown: list[str] = []
    for scope in scopes:
        key = scope.strip().lower()
        if key and key not in _ROLE_ALIASES:
            unknown.append(scope)
    return tuple(unknown)


def _normalize_required_roles(roles: Iterable[str]) -> set[OperatorRole]:
    normalized: set[OperatorRole] = set()
    for role in roles:
        key = role.strip().lower()
        if key in _ROLE_ALIASES:
            normalized.update(_ROLE_ALIASES[key])
    return normalized


def require_any_role(*required_roles: str) -> Callable[[Request], None]:
    """FastAPI dependency factory that requires at least one product role."""

    required = _normalize_required_roles(required_roles)

    def dependency(request: Request) -> None:
        identity = getattr(request.state, "operator_identity", None)
        if not isinstance(identity, OperatorIdentity):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Staff identity is required for this action.",
            )
        granted = roles_for_identity(identity)
        if not granted.intersection(required):
            labels = ", ".join(sorted(required))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires one of these CivicCast roles: {labels}.",
            )

    return dependency


def has_any_role(identity: OperatorIdentity, roles: Iterable[str]) -> bool:
    """Return whether an identity has at least one requested product role."""

    return bool(roles_for_identity(identity).intersection(_normalize_required_roles(roles)))
