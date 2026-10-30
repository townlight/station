# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Platform-owned broker contract for CivicCast event publication."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from inspect import Signature
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from civiccast.platform.broker_config import BROKER_SUBJECT_REGISTRY

DOCUMENTED_BROKER_SUBJECTS = frozenset({"publish.asset.approved"})


@dataclass(frozen=True)
class BrokerEvent:
    """One documented broker event published through the platform substrate."""

    subject: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class BrokerPublishReceipt:
    """Immutable receipt proving a broker event was accepted by the substrate."""

    subject: str
    message_id: str
    published_at: datetime
    provider_stream: str | None = None
    provider_sequence: int | None = None
    provider_domain: str | None = None


@runtime_checkable
class BrokerClient(Protocol):
    """Small platform broker surface used by CivicCast modules."""

    def publish(self, event: BrokerEvent) -> BrokerPublishReceipt:
        """Publish a documented event and return an immutable receipt."""

    def replay(self, subject: str) -> list[BrokerEvent]:
        """Return recorded events for a documented subject."""

    def subscribe(self, subject: str, handler: Callable[[BrokerEvent], None]) -> None:
        """Register a handler for future documented events on a subject."""


def validate_broker_subject(subject: str) -> None:
    """Fail before publish when a module attempts an undocumented subject."""

    try:
        BROKER_SUBJECT_REGISTRY.require_subject(subject)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


class InProcessBrokerClient:
    """In-process broker adapter used for local tests and v1.2 evidence."""

    def __init__(self) -> None:
        self._events: dict[str, list[BrokerEvent]] = {}
        self._subscribers: dict[str, list[Callable[[BrokerEvent], None]]] = {}

    def publish(self, event: BrokerEvent) -> BrokerPublishReceipt:
        validate_broker_subject(event.subject)
        self._events.setdefault(event.subject, []).append(event)
        for handler in self._subscribers.get(event.subject, []):
            handler(event)
        return BrokerPublishReceipt(
            subject=event.subject,
            message_id=str(uuid4()),
            published_at=datetime.now(UTC),
        )

    def replay(self, subject: str) -> list[BrokerEvent]:
        validate_broker_subject(subject)
        return list(self._events.get(subject, []))

    def subscribe(self, subject: str, handler: Callable[[BrokerEvent], None]) -> None:
        validate_broker_subject(subject)
        self._subscribers.setdefault(subject, []).append(handler)


_default_broker_client: BrokerClient = InProcessBrokerClient()


def get_broker_client() -> BrokerClient:
    """FastAPI dependency returning the app's broker client seam.

    NATS JetStream was removed from the product (owner decision 2026-08-20;
    see ADR 0023, which supersedes ADR 0001) -- it never did real production
    work here, since this dependency always returned the in-process adapter
    below. :class:`InProcessBrokerClient` is now the only ``BrokerClient``
    implementation; the ``BrokerClient`` Protocol seam stays so a future
    adapter can still be swapped in behind it.
    """

    return _default_broker_client


get_broker_client.__signature__ = Signature()  # type: ignore[attr-defined]
