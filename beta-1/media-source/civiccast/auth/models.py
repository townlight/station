# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Auth data contracts for staff-route identity binding."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OperatorRole = Literal[
    "setup_admin",
    "meeting_operator",
    "records_clerk",
    "publish_operator",
    "support_admin",
]


class OperatorIdentity(BaseModel):
    """Server-verified operator identity attached to staff requests."""

    model_config = ConfigDict(extra="forbid")

    operator_id: Annotated[str, Field(min_length=1, max_length=160)]
    operator_display_name: Annotated[str, Field(min_length=1, max_length=200)]
    token_id: str | None = None
    scopes: tuple[str, ...] = Field(default_factory=tuple)


class StaffIdentityResponse(BaseModel):
    """Staff identity and v1.4 product roles returned to the operator console."""

    model_config = ConfigDict(extra="forbid")

    operator_id: Annotated[str, Field(min_length=1, max_length=160)]
    operator_display_name: Annotated[str, Field(min_length=1, max_length=200)]
    token_id: str | None = None
    scopes: tuple[str, ...] = Field(default_factory=tuple)
    roles: tuple[OperatorRole, ...] = Field(default_factory=tuple)


class StaffSignOutResponse(BaseModel):
    """Result of ending the calling browser's own staff session."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["signed_out"]
    # False only for an env-configured (CIVICCAST_STAFF_TOKENS) token, which
    # has no server-side revocation record: the browser still forgets it.
    session_revoked: bool
    message: Annotated[str, Field(min_length=1)]
    next_step: Annotated[str, Field(min_length=1)]


class StaffLoginRequest(BaseModel):
    """Credentials for a station-local staff account."""

    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=1, max_length=80)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class StaffLoginResponse(BaseModel):
    """One-time bearer credential returned after a successful staff login."""

    model_config = ConfigDict(extra="forbid")

    access_token: Annotated[str, Field(min_length=24)]
    operator_id: Annotated[str, Field(min_length=1, max_length=160)]
    operator_display_name: Annotated[str, Field(min_length=1, max_length=200)]
    roles: tuple[OperatorRole, ...] = Field(default_factory=tuple)
    issued_at: str
    expires_at: str


class StaffAccountCreateRequest(BaseModel):
    """Create one local staff login with an explicit non-empty role set."""

    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")]
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    password: Annotated[str, Field(min_length=12, max_length=256)]
    roles: tuple[OperatorRole, ...] = Field(min_length=1)


class StaffAccountUpdateRequest(BaseModel):
    """Change mutable staff identity, role, or enabled state."""

    model_config = ConfigDict(extra="forbid")

    display_name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    roles: tuple[OperatorRole, ...] | None = Field(default=None, min_length=1)
    enabled: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> StaffAccountUpdateRequest:
        if self.display_name is None and self.roles is None and self.enabled is None:
            raise ValueError("Provide a display_name, roles, or enabled change.")
        return self


class StaffPasswordResetRequest(BaseModel):
    """Set a new password for a staff account."""

    model_config = ConfigDict(extra="forbid")

    new_password: Annotated[str, Field(min_length=12, max_length=256)]


class StaffAccountResponse(BaseModel):
    """Public account metadata; password hashes never leave station state."""

    model_config = ConfigDict(extra="forbid")

    operator_id: Annotated[str, Field(min_length=1, max_length=160)]
    username: Annotated[str, Field(min_length=1, max_length=80)]
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    roles: tuple[OperatorRole, ...]
    enabled: bool
    account_type: Literal["first_admin", "staff"]
    created_at: str | None = None
    updated_at: str | None = None
    immutable: bool = False


class StaffSessionResponse(BaseModel):
    """Safe session metadata without a token, salt, or hash."""

    model_config = ConfigDict(extra="forbid")

    session_id: Annotated[str, Field(min_length=1, max_length=120)]
    operator_id: Annotated[str, Field(min_length=1, max_length=160)]
    operator_display_name: Annotated[str, Field(min_length=1, max_length=200)]
    roles: tuple[OperatorRole, ...] = Field(default_factory=tuple)
    source: str
    issued_at: str
    expires_at: str
    expired: bool = False
    current: bool = False
