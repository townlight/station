# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""First-party staff authentication helpers."""

from civiccast.auth.models import OperatorIdentity
from civiccast.auth.store import InMemoryStaffTokenStore, PostgresStaffTokenStore, StaffTokenStore


def verify_bearer_token(
    authorization: str | None,
    *,
    token_store: StaffTokenStore | None = None,
) -> OperatorIdentity:
    """Lazily import token verification to avoid setup-state import cycles."""

    from civiccast.auth.tokens import verify_bearer_token as _verify_bearer_token

    return _verify_bearer_token(authorization, token_store=token_store)


__all__ = [
    "InMemoryStaffTokenStore",
    "OperatorIdentity",
    "PostgresStaffTokenStore",
    "verify_bearer_token",
]
