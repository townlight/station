# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The pure two-tier authorization decision for the D7 control pipe.

The control pipe authorizes PER COMMAND, not per pipe (spec D7): the server
impersonates the client, extracts the token's group memberships, and this pure
function decides whether the command is allowed. Keeping the decision pure and
separate from the impersonation I/O means AC-N1 -- "an unprivileged INTERACTIVE
token: ``status`` succeeds, ``stop`` is DENIED" -- is a CI-testable property, not
something only observable on a live Windows box.

Two tiers (D7):

* **status tier** -- ``status`` / ``version``: require Authenticated Users.
* **admin tier** -- ``start`` / ``stop`` / ``restart`` / ``drain`` /
  ``runtime_set``: require ``BUILTIN\\Administrators`` OR ``SYSTEM``.

"INTERACTIVE alone gets read-only" is a consequence, not a separate rule:
INTERACTIVE membership never satisfies the admin tier, and the status tier is
satisfied by Authenticated Users (which an interactive logon token also
carries), so an interactive non-admin caller can read and cannot mutate. Fail
closed: an unrecognized command is denied, and a caller carrying none of the
known groups is denied even the status tier.

``is_mutating`` names the commands D7 requires to be audit-logged with the
caller SID -- the logging itself is I/O the pipe server does; the
classification is pure and lives here so the two never drift.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

Command = Literal["status", "version", "start", "stop", "restart", "drain", "runtime_set"]
"""Every command the control pipe accepts. Read tier: status, version. Admin
tier (mutating): start, stop, restart, drain, runtime_set."""

AuthzTier = Literal["status", "admin", "none"]
"""``status`` = Authenticated-Users read tier; ``admin`` = Administrators/SYSTEM
mutating tier; ``none`` = the caller satisfies no tier (denied)."""

# Well-known group tokens the caller's impersonated token may carry. These are
# the identifiers ``win_probes``/the pipe server maps real SIDs onto; the pure
# function is defined over this vocabulary so tests never need a real token.
#   authenticated_users -> S-1-5-11
#   administrators       -> S-1-5-32-544 (BUILTIN\Administrators)
#   system               -> S-1-5-18
#   interactive          -> S-1-5-4
KnownGroup = Literal[
    "authenticated_users", "administrators", "system", "interactive", "station_service"
]

_ADMIN_COMMANDS: frozenset[str] = frozenset({"start", "stop", "restart", "drain", "runtime_set"})
_READ_COMMANDS: frozenset[str] = frozenset({"status", "version"})
_ADMIN_GROUPS: frozenset[str] = frozenset({"administrators", "system", "station_service"})


class AuthzDecision(BaseModel):
    """The result of :func:`authorize` -- allow/deny plus the tiers and a
    reason, and whether the command is mutating (audit-log requirement)."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool
    command: str
    required_tier: AuthzTier
    caller_tier: AuthzTier
    is_mutating: bool
    reason: str


def _caller_tier(groups: frozenset[str]) -> AuthzTier:
    """The highest tier the caller's groups satisfy. Admin groups also satisfy
    the status tier, so an admin/SYSTEM caller is ``admin`` (which dominates)."""

    if groups & _ADMIN_GROUPS:
        return "admin"
    if "authenticated_users" in groups:
        return "status"
    return "none"


def authorize(command: str, groups: frozenset[str]) -> AuthzDecision:
    """Decide whether ``command`` is allowed for a caller whose impersonated
    token carries ``groups``. Total and fail-closed: an unrecognized command is
    denied with ``required_tier="none"``.
    """

    caller = _caller_tier(groups)

    if command in _ADMIN_COMMANDS:
        required: AuthzTier = "admin"
        allowed = caller == "admin"
        is_mutating = True
    elif command in _READ_COMMANDS:
        required = "status"
        # admin dominates status, so an admin/SYSTEM caller is also allowed to read.
        allowed = caller in ("status", "admin")
        is_mutating = False
    else:
        # Unknown command -> deny, fail closed. The pipe server also rejects
        # malformed frames upstream; this is the authorization backstop.
        return AuthzDecision(
            allowed=False,
            command=command,
            required_tier="none",
            caller_tier=caller,
            is_mutating=False,
            reason=f"unrecognized command {command!r}; denied (fail-closed).",
        )

    reason = (
        f"{command}: requires {required} tier, caller is {caller} tier -> "
        f"{'allowed' if allowed else 'DENIED'}."
    )
    return AuthzDecision(
        allowed=allowed,
        command=command,
        required_tier=required,
        caller_tier=caller,
        is_mutating=is_mutating,
        reason=reason,
    )


def _all_commands() -> tuple[Command, ...]:
    """All valid Command values (drives the exhaustive authorization tests)."""

    return get_args(Command)


__all__ = [
    "AuthzDecision",
    "AuthzTier",
    "Command",
    "KnownGroup",
    "authorize",
]
