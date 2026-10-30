# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff token lifecycle storage and verification.

The v1.2 token lifecycle keeps bearer secrets out of durable storage. The
operator sees the token secret once at issuance or rotation; the database keeps
only a per-token salt and PBKDF2 hash, a SHA-256 fingerprint of the generated
high-entropy secret, revocation metadata, last-use metadata, and append-only
lifecycle audit rows. The fingerprint enables exact pre-verification admission;
it does not replace the authoritative PBKDF2 comparison.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import itertools
import json
import secrets
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.auth.models import OperatorIdentity

SessionFactory = Callable[[], AbstractContextManager[Session]]

TOKEN_HASH_ALGORITHM = "pbkdf2-sha256"
TOKEN_HASH_ITERATIONS = 210_000
TOKEN_PREFIX = "ccst"
DEFAULT_SCOPES = ("operator",)


class StaffTokenLifecycleError(RuntimeError):
    """Base class for staff token lifecycle failures."""


class StaffTokenNotFoundError(StaffTokenLifecycleError):
    """Raised when a token id is not present in the lifecycle store."""


class StaffTokenRevokedError(StaffTokenLifecycleError):
    """Raised when a revoked token is used for authentication."""


class StaffTokenInvalidError(StaffTokenLifecycleError):
    """Raised when a bearer token does not match the stored hash."""


class StaffTokenUpgradeRequiredError(StaffTokenLifecycleError):
    """Raised when a pre-fingerprint active token must be rotated."""


@dataclass(frozen=True)
class StaffTokenMetadata:
    """Public metadata for a staff token. Never includes the bearer secret."""

    token_id: str
    operator_id: str
    operator_display_name: str
    scopes: tuple[str, ...]
    issued_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    revocation_reason: str | None
    rotated_from_token_id: str | None


@dataclass(frozen=True)
class IssuedStaffToken:
    """Token issuance result. ``secret`` is shown once and never stored."""

    secret: str
    metadata: StaffTokenMetadata


@dataclass(frozen=True)
class StaffTokenAuditEvent:
    """Lifecycle audit event bound to a token id and operator identity."""

    event_id: str
    token_id: str | None
    operator_id: str | None
    event_type: str
    created_at: datetime
    details: dict[str, object]


@dataclass(frozen=True)
class _StoredStaffToken:
    metadata: StaffTokenMetadata
    token_hash: str
    salt_b64: str


class StaffTokenStore(Protocol):
    """Storage contract used by middleware and CLI token management."""

    def issue_token(
        self,
        *,
        operator_id: str,
        operator_display_name: str,
        scopes: Sequence[str] = DEFAULT_SCOPES,
        rotated_from_token_id: str | None = None,
    ) -> IssuedStaffToken: ...

    def verify_token(self, secret: str) -> OperatorIdentity: ...

    def matches_token_fingerprint(self, secret: str) -> bool:
        """Cheap exact match for active fingerprint-era lifecycle tokens."""

        ...

    def list_tokens(self) -> list[StaffTokenMetadata]: ...

    def revoke_token(self, token_id: str, *, reason: str) -> StaffTokenMetadata: ...

    def rotate_token(self, token_id: str) -> IssuedStaffToken: ...

    def audit_events(self) -> list[StaffTokenAuditEvent]: ...


def make_token_secret(token_id: str) -> str:
    """Return a newly generated bearer secret carrying its public token id."""

    return f"{TOKEN_PREFIX}_{token_id}_{secrets.token_urlsafe(32)}"


def extract_token_id(secret: str) -> str | None:
    """Extract the public token id from a v1.2 bearer secret."""

    prefix = f"{TOKEN_PREFIX}_"
    if not secret.startswith(prefix):
        return None
    remainder = secret[len(prefix) :]
    token_id, sep, _private = remainder.partition("_")
    if not sep or not token_id:
        return None
    return token_id


def hash_token(secret: str, salt: bytes) -> str:
    """Derive the durable token hash with the project's v1.2 parameters."""

    digest = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt,
        TOKEN_HASH_ITERATIONS,
    )
    return base64.b64encode(digest).decode("ascii")


def token_fingerprint(secret: str) -> str:
    """Fingerprint a generated high-entropy bearer token for fast exact matching."""

    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _enriched_token_hash(secret: str, pbkdf2_hash: str) -> str:
    if "$sha256$" in pbkdf2_hash:
        return pbkdf2_hash
    return f"{pbkdf2_hash}$sha256${token_fingerprint(secret)}"


def _pbkdf2_component(stored_hash: str) -> str:
    return stored_hash.partition("$sha256$")[0]


def _fingerprint_component(stored_hash: str) -> str | None:
    _pbkdf2, separator, fingerprint = stored_hash.partition("$sha256$")
    return fingerprint if separator and fingerprint else None


def new_token_id() -> str:
    """Return a URL-safe public token id."""

    return "st" + secrets.token_hex(12)


_AUDIT_EVENT_SEQ = itertools.count()


def new_audit_event_id() -> str:
    """Return a URL-safe, time-ordered audit event id.

    The id embeds a zero-padded microsecond wall timestamp plus a per-process
    monotonic sequence so that ``ORDER BY created_at, event_id`` is a stable
    insertion order even when consecutive events share a ``created_at`` value.
    The Windows wall clock quantizes at up to ~15.6 ms on Python 3.12, so
    issue/use/revoke inside one request routinely collide on ``created_at``;
    with a purely random tiebreaker the audit order flipped
    nondeterministically (caught by the real-Postgres run of
    ``test_database_lifecycle_never_persists_bearer_secret`` — SQLite masked
    it with stable scan order). Cross-process ordering inside a single clock
    quantum remains arbitrary, which no id scheme can fix without a DB-side
    sequence.
    """

    micros = int(time.time() * 1_000_000)
    seq = next(_AUDIT_EVENT_SEQ) % 100_000_000
    suffix = secrets.token_urlsafe(6).replace("-", "").replace("_", "")
    return f"sta_{micros:020d}{seq:08d}{suffix}"


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(scope.strip() for scope in scopes if scope.strip()))
    return normalized or DEFAULT_SCOPES


def _new_stored_token(
    *,
    operator_id: str,
    operator_display_name: str,
    scopes: Sequence[str],
    rotated_from_token_id: str | None,
) -> tuple[IssuedStaffToken, _StoredStaffToken]:
    token_id = new_token_id()
    secret = make_token_secret(token_id)
    salt = secrets.token_bytes(24)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    issued_at = _now()
    metadata = StaffTokenMetadata(
        token_id=token_id,
        operator_id=operator_id,
        operator_display_name=operator_display_name,
        scopes=_normalize_scopes(scopes),
        issued_at=issued_at,
        last_used_at=None,
        revoked_at=None,
        revocation_reason=None,
        rotated_from_token_id=rotated_from_token_id,
    )
    stored = _StoredStaffToken(
        metadata=metadata,
        token_hash=_enriched_token_hash(secret, hash_token(secret, salt)),
        salt_b64=salt_b64,
    )
    return IssuedStaffToken(secret=secret, metadata=metadata), stored


def _verify_stored_token(secret: str, stored: _StoredStaffToken) -> OperatorIdentity:
    if stored.metadata.revoked_at is not None:
        raise StaffTokenRevokedError("Staff bearer token has been revoked.")
    if _fingerprint_component(stored.token_hash) is None:
        raise StaffTokenUpgradeRequiredError(
            f"Staff token {stored.metadata.token_id!r} predates exact-token admission. "
            "Rotate it with `civiccast token rotate <token-id>` and use the new secret."
        )
    salt = base64.b64decode(stored.salt_b64.encode("ascii"))
    candidate = hash_token(secret, salt)
    if not hmac.compare_digest(candidate, _pbkdf2_component(stored.token_hash)):
        raise StaffTokenInvalidError("Invalid staff bearer token.")
    return OperatorIdentity(
        operator_id=stored.metadata.operator_id,
        operator_display_name=stored.metadata.operator_display_name,
        token_id=stored.metadata.token_id,
        scopes=stored.metadata.scopes,
    )


class InMemoryStaffTokenStore:
    """In-memory lifecycle store for tests and explicit local injection."""

    def __init__(self) -> None:
        self._tokens: dict[str, _StoredStaffToken] = {}
        self._audit_events: list[StaffTokenAuditEvent] = []

    def issue_token(
        self,
        *,
        operator_id: str,
        operator_display_name: str,
        scopes: Sequence[str] = DEFAULT_SCOPES,
        rotated_from_token_id: str | None = None,
    ) -> IssuedStaffToken:
        issued, stored = _new_stored_token(
            operator_id=operator_id,
            operator_display_name=operator_display_name,
            scopes=scopes,
            rotated_from_token_id=rotated_from_token_id,
        )
        self._tokens[stored.metadata.token_id] = stored
        self._append_audit(
            token_id=stored.metadata.token_id,
            operator_id=operator_id,
            event_type="issued",
            details={"scopes": list(stored.metadata.scopes)},
        )
        return issued

    def verify_token(self, secret: str) -> OperatorIdentity:
        token_id = extract_token_id(secret)
        if token_id is None or token_id not in self._tokens:
            raise StaffTokenInvalidError("Invalid staff bearer token.")
        stored = self._tokens[token_id]
        identity = _verify_stored_token(secret, stored)
        used_at = _now()
        self._tokens[token_id] = _StoredStaffToken(
            metadata=_replace_metadata(stored.metadata, last_used_at=used_at),
            token_hash=_enriched_token_hash(secret, stored.token_hash),
            salt_b64=stored.salt_b64,
        )
        self._append_audit(
            token_id=token_id,
            operator_id=identity.operator_id,
            event_type="used",
            details={},
        )
        return identity

    def matches_token_fingerprint(self, secret: str) -> bool:
        token_id = extract_token_id(secret)
        if token_id is None:
            return False
        stored = self._tokens.get(token_id)
        if stored is None or stored.metadata.revoked_at is not None:
            return False
        fingerprint = _fingerprint_component(stored.token_hash)
        if fingerprint is None:
            return False
        return hmac.compare_digest(fingerprint, token_fingerprint(secret))

    def list_tokens(self) -> list[StaffTokenMetadata]:
        return [
            stored.metadata
            for stored in sorted(self._tokens.values(), key=lambda item: item.metadata.issued_at)
        ]

    def revoke_token(self, token_id: str, *, reason: str) -> StaffTokenMetadata:
        stored = self._tokens.get(token_id)
        if stored is None:
            raise StaffTokenNotFoundError(f"Unknown staff token id: {token_id}")
        metadata = _replace_metadata(
            stored.metadata,
            revoked_at=stored.metadata.revoked_at or _now(),
            revocation_reason=reason,
        )
        self._tokens[token_id] = _StoredStaffToken(
            metadata=metadata,
            token_hash=stored.token_hash,
            salt_b64=stored.salt_b64,
        )
        self._append_audit(
            token_id=token_id,
            operator_id=metadata.operator_id,
            event_type="revoked",
            details={"reason": reason},
        )
        return metadata

    def rotate_token(self, token_id: str) -> IssuedStaffToken:
        stored = self._tokens.get(token_id)
        if stored is None:
            raise StaffTokenNotFoundError(f"Unknown staff token id: {token_id}")
        self.revoke_token(token_id, reason="rotated")
        issued = self.issue_token(
            operator_id=stored.metadata.operator_id,
            operator_display_name=stored.metadata.operator_display_name,
            scopes=stored.metadata.scopes,
            rotated_from_token_id=token_id,
        )
        self._append_audit(
            token_id=issued.metadata.token_id,
            operator_id=issued.metadata.operator_id,
            event_type="rotated",
            details={"rotated_from_token_id": token_id},
        )
        return issued

    def audit_events(self) -> list[StaffTokenAuditEvent]:
        return list(self._audit_events)

    def _append_audit(
        self,
        *,
        token_id: str | None,
        operator_id: str | None,
        event_type: str,
        details: dict[str, object],
    ) -> None:
        self._audit_events.append(
            StaffTokenAuditEvent(
                event_id=new_audit_event_id(),
                token_id=token_id,
                operator_id=operator_id,
                event_type=event_type,
                created_at=_now(),
                details=details,
            )
        )


class PostgresStaffTokenStore:
    """SQLAlchemy-backed staff token store for Postgres and SQLite tests."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def issue_token(
        self,
        *,
        operator_id: str,
        operator_display_name: str,
        scopes: Sequence[str] = DEFAULT_SCOPES,
        rotated_from_token_id: str | None = None,
    ) -> IssuedStaffToken:
        for _attempt in range(3):
            issued, stored = _new_stored_token(
                operator_id=operator_id,
                operator_display_name=operator_display_name,
                scopes=scopes,
                rotated_from_token_id=rotated_from_token_id,
            )
            with self._session_factory() as session:
                table = self._table_prefix(session)
                try:
                    self._insert_token(session, table, stored)
                    self._insert_audit(
                        session,
                        table,
                        token_id=stored.metadata.token_id,
                        operator_id=operator_id,
                        event_type="issued",
                        details={"scopes": list(stored.metadata.scopes)},
                    )
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    continue
            return issued
        raise StaffTokenLifecycleError("Could not allocate a unique staff token id.")

    def verify_token(self, secret: str) -> OperatorIdentity:
        token_id = extract_token_id(secret)
        if token_id is None:
            raise StaffTokenInvalidError("Invalid staff bearer token.")
        with self._session_factory() as session:
            table = self._table_prefix(session)
            stored = self._load_token(session, table, token_id)
            if stored is None:
                raise StaffTokenInvalidError("Invalid staff bearer token.")
            identity = _verify_stored_token(secret, stored)
            used_at = _now()
            session.execute(
                text(
                    f"UPDATE {table}staff_tokens SET last_used_at = :last_used_at, "  # nosec B608
                    "token_hash = :token_hash "
                    "WHERE token_id = :token_id"
                ),
                {
                    "last_used_at": _store_datetime(session, used_at),
                    "token_hash": _enriched_token_hash(secret, stored.token_hash),
                    "token_id": token_id,
                },
            )
            self._insert_audit(
                session,
                table,
                token_id=token_id,
                operator_id=identity.operator_id,
                event_type="used",
                details={},
            )
            session.commit()
            return identity

    def matches_token_fingerprint(self, secret: str) -> bool:
        token_id = extract_token_id(secret)
        if token_id is None:
            return False
        with self._session_factory() as session:
            table = self._table_prefix(session)
            stored = self._load_token(session, table, token_id)
        if stored is None or stored.metadata.revoked_at is not None:
            return False
        fingerprint = _fingerprint_component(stored.token_hash)
        if fingerprint is None:
            return False
        return hmac.compare_digest(fingerprint, token_fingerprint(secret))

    def list_tokens(self) -> list[StaffTokenMetadata]:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            rows = session.execute(
                text(
                    f"SELECT token_id, operator_id, operator_display_name, scopes_json, "  # nosec B608
                    f"issued_at, last_used_at, revoked_at, revocation_reason, "
                    f"rotated_from_token_id FROM {table}staff_tokens "
                    "ORDER BY issued_at ASC, token_id ASC"
                )
            ).fetchall()
            return [_metadata_from_row(row) for row in rows]

    def revoke_token(self, token_id: str, *, reason: str) -> StaffTokenMetadata:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            stored = self._load_token(session, table, token_id)
            if stored is None:
                raise StaffTokenNotFoundError(f"Unknown staff token id: {token_id}")
            revoked_at = stored.metadata.revoked_at or _now()
            session.execute(
                text(
                    f"UPDATE {table}staff_tokens SET revoked_at = :revoked_at, "  # nosec B608
                    "revocation_reason = :revocation_reason WHERE token_id = :token_id"
                ),
                {
                    "revoked_at": _store_datetime(session, revoked_at),
                    "revocation_reason": reason,
                    "token_id": token_id,
                },
            )
            self._insert_audit(
                session,
                table,
                token_id=token_id,
                operator_id=stored.metadata.operator_id,
                event_type="revoked",
                details={"reason": reason},
            )
            session.commit()
            return _replace_metadata(
                stored.metadata,
                revoked_at=revoked_at,
                revocation_reason=reason,
            )

    def rotate_token(self, token_id: str) -> IssuedStaffToken:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            stored = self._load_token(session, table, token_id)
            if stored is None:
                raise StaffTokenNotFoundError(f"Unknown staff token id: {token_id}")
            session.execute(
                text(
                    f"UPDATE {table}staff_tokens SET revoked_at = :revoked_at, "  # nosec B608
                    "revocation_reason = :revocation_reason WHERE token_id = :token_id"
                ),
                {
                    "revoked_at": _store_datetime(session, stored.metadata.revoked_at or _now()),
                    "revocation_reason": "rotated",
                    "token_id": token_id,
                },
            )
            issued, new_stored = _new_stored_token(
                operator_id=stored.metadata.operator_id,
                operator_display_name=stored.metadata.operator_display_name,
                scopes=stored.metadata.scopes,
                rotated_from_token_id=token_id,
            )
            self._insert_token(session, table, new_stored)
            self._insert_audit(
                session,
                table,
                token_id=token_id,
                operator_id=stored.metadata.operator_id,
                event_type="revoked",
                details={"reason": "rotated"},
            )
            self._insert_audit(
                session,
                table,
                token_id=issued.metadata.token_id,
                operator_id=issued.metadata.operator_id,
                event_type="rotated",
                details={"rotated_from_token_id": token_id},
            )
            session.commit()
            return issued

    def audit_events(self) -> list[StaffTokenAuditEvent]:
        with self._session_factory() as session:
            table = self._table_prefix(session)
            rows = session.execute(
                text(
                    f"SELECT event_id, token_id, operator_id, event_type, created_at, "  # nosec B608
                    f"details_json FROM {table}staff_token_audit_events "
                    "ORDER BY created_at ASC, event_id ASC"
                )
            ).fetchall()
            return [
                StaffTokenAuditEvent(
                    event_id=str(row.event_id),
                    token_id=str(row.token_id) if row.token_id is not None else None,
                    operator_id=str(row.operator_id) if row.operator_id is not None else None,
                    event_type=str(row.event_type),
                    created_at=_coerce_datetime(row.created_at),
                    details=json.loads(row.details_json),
                )
                for row in rows
            ]

    def _insert_token(
        self,
        session: Session,
        table: str,
        stored: _StoredStaffToken,
    ) -> None:
        session.execute(
            text(
                f"INSERT INTO {table}staff_tokens "  # nosec B608
                "(token_id, operator_id, operator_display_name, token_hash, salt_b64, "
                "hash_algorithm, hash_iterations, scopes_json, issued_at, last_used_at, "
                "revoked_at, revocation_reason, rotated_from_token_id) "
                "VALUES (:token_id, :operator_id, :operator_display_name, :token_hash, "
                ":salt_b64, :hash_algorithm, :hash_iterations, :scopes_json, "
                ":issued_at, :last_used_at, :revoked_at, :revocation_reason, "
                ":rotated_from_token_id)"
            ),
            {
                "token_id": stored.metadata.token_id,
                "operator_id": stored.metadata.operator_id,
                "operator_display_name": stored.metadata.operator_display_name,
                "token_hash": stored.token_hash,
                "salt_b64": stored.salt_b64,
                "hash_algorithm": TOKEN_HASH_ALGORITHM,
                "hash_iterations": TOKEN_HASH_ITERATIONS,
                "scopes_json": json.dumps(list(stored.metadata.scopes), sort_keys=True),
                "issued_at": _store_datetime(session, stored.metadata.issued_at),
                "last_used_at": _store_optional_datetime(session, stored.metadata.last_used_at),
                "revoked_at": _store_optional_datetime(session, stored.metadata.revoked_at),
                "revocation_reason": stored.metadata.revocation_reason,
                "rotated_from_token_id": stored.metadata.rotated_from_token_id,
            },
        )

    def _load_token(
        self,
        session: Session,
        table: str,
        token_id: str,
    ) -> _StoredStaffToken | None:
        row = session.execute(
            text(
                f"SELECT token_id, operator_id, operator_display_name, token_hash, "  # nosec B608
                f"salt_b64, scopes_json, issued_at, last_used_at, revoked_at, "
                f"revocation_reason, rotated_from_token_id "
                f"FROM {table}staff_tokens WHERE token_id = :token_id"
            ),
            {"token_id": token_id},
        ).first()
        if row is None:
            return None
        return _StoredStaffToken(
            metadata=_metadata_from_row(row),
            token_hash=str(row.token_hash),
            salt_b64=str(row.salt_b64),
        )

    def _insert_audit(
        self,
        session: Session,
        table: str,
        *,
        token_id: str | None,
        operator_id: str | None,
        event_type: str,
        details: dict[str, object],
    ) -> None:
        session.execute(
            text(
                f"INSERT INTO {table}staff_token_audit_events "  # nosec B608
                "(event_id, token_id, operator_id, event_type, created_at, details_json) "
                "VALUES (:event_id, :token_id, :operator_id, :event_type, "
                ":created_at, :details_json)"
            ),
            {
                "event_id": new_audit_event_id(),
                "token_id": token_id,
                "operator_id": operator_id,
                "event_type": event_type,
                "created_at": _store_datetime(session, _now()),
                "details_json": json.dumps(details, sort_keys=True),
            },
        )

    @staticmethod
    def _table_prefix(session: Session) -> str:
        bind = session.get_bind()
        return "" if bind.dialect.name == "sqlite" else "civiccast."


def _metadata_from_row(row: object) -> StaffTokenMetadata:
    mapping = row._mapping  # type: ignore[attr-defined]
    return StaffTokenMetadata(
        token_id=str(mapping["token_id"]),
        operator_id=str(mapping["operator_id"]),
        operator_display_name=str(mapping["operator_display_name"]),
        scopes=tuple(json.loads(mapping["scopes_json"])),
        issued_at=_coerce_datetime(mapping["issued_at"]),
        last_used_at=_coerce_optional_datetime(mapping["last_used_at"]),
        revoked_at=_coerce_optional_datetime(mapping["revoked_at"]),
        revocation_reason=(
            str(mapping["revocation_reason"]) if mapping["revocation_reason"] is not None else None
        ),
        rotated_from_token_id=(
            str(mapping["rotated_from_token_id"])
            if mapping["rotated_from_token_id"] is not None
            else None
        ),
    )


def _coerce_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _coerce_datetime(value)


def _coerce_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"Unsupported datetime value: {value!r}")


def _store_optional_datetime(session: Session, value: datetime | None) -> datetime | str | None:
    if value is None:
        return None
    return _store_datetime(session, value)


def _store_datetime(session: Session, value: datetime) -> datetime | str:
    bind = session.get_bind()
    if bind.dialect.name == "sqlite":
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return normalized.isoformat()
    return value


def _replace_metadata(metadata: StaffTokenMetadata, **updates: object) -> StaffTokenMetadata:
    values = {
        "token_id": metadata.token_id,
        "operator_id": metadata.operator_id,
        "operator_display_name": metadata.operator_display_name,
        "scopes": metadata.scopes,
        "issued_at": metadata.issued_at,
        "last_used_at": metadata.last_used_at,
        "revoked_at": metadata.revoked_at,
        "revocation_reason": metadata.revocation_reason,
        "rotated_from_token_id": metadata.rotated_from_token_id,
    }
    values.update(updates)
    return StaffTokenMetadata(**values)  # type: ignore[arg-type]
