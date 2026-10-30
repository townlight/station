# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The documented broker subject registry.

NATS JetStream was removed from the product (owner decision 2026-08-20; see
ADR 0023, which supersedes ADR 0001). It never did real production work --
``civiccast.platform.broker.get_broker_client`` always returned the
in-process adapter -- so this module no longer carries a broker "mode",
production NATS URL/stream/mTLS configuration, or a JetStream readiness
check. What remains is the closed registry of logical broker subjects
CivicCast modules may publish/replay/subscribe to, which
:class:`civiccast.platform.broker.InProcessBrokerClient` validates against
before every operation.
"""

from __future__ import annotations

from dataclasses import dataclass


class BrokerConfigurationError(ValueError):
    """Raised when a module attempts to use an undocumented broker subject."""


@dataclass(frozen=True)
class BrokerSubjectEntry:
    """One platform-owned documented broker subject."""

    subject: str


class BrokerSubjectRegistry:
    """Closed registry of broker subjects that CivicCast modules may publish."""

    def __init__(self, entries: tuple[BrokerSubjectEntry, ...]) -> None:
        self._entries = {entry.subject: entry for entry in entries}

    def require_subject(self, subject: str) -> BrokerSubjectEntry:
        try:
            return self._entries[subject]
        except KeyError as exc:
            allowed = ", ".join(sorted(self._entries))
            raise BrokerConfigurationError(
                f"{subject!r} is not a documented broker subject. Use one of: {allowed}."
            ) from exc


BROKER_SUBJECT_REGISTRY = BrokerSubjectRegistry(
    (BrokerSubjectEntry(subject="publish.asset.approved"),)
)


__all__ = [
    "BROKER_SUBJECT_REGISTRY",
    "BrokerConfigurationError",
    "BrokerSubjectEntry",
    "BrokerSubjectRegistry",
]
