# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Real SMTP mailbox for subscriber mail (Beta sprint B5, decision #6).

Sends through the station's SMTP relay with ``smtplib``. Selected with
``CIVICCAST_PROVIDER_MAIL=real``; the in-memory ``LocalMailbox`` remains the
default. Settings are validated fail-fast at resolution: host and from-address
are required, STARTTLS is on by default, and credentials are optional but must
come as a pair.

Subjects and bodies match :class:`~civiccast.subscribe.delivery.LocalMailbox`
exactly, so proofs and real deliveries say the same thing. No credentials live
in code or tests; contract tests run an in-process SMTP server on a loopback
socket.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from dataclasses import dataclass, field
from email.headerregistry import HeaderRegistry
from email.message import EmailMessage
from email.policy import SMTP, Policy
from email.utils import make_msgid
from typing import Any

from civiccast.platform.provider_settings import setting
from civiccast.subscribe.delivery import notification_body
from civiccast.subscribe.models import NotificationPayload

__all__ = ["SmtpMailbox", "SmtpSettings"]


class _VerbatimHeader:
    """A header whose value is emitted exactly as given.

    ``EmailMessage``'s default policy treats any header it does not recognise
    as unstructured display text, so a long ``List-Unsubscribe`` URL comes out
    RFC 2047 q-encoded and line-folded:
    ``List-Unsubscribe: =?utf-8?q?=3Chttps=3A//...``. That is not a valid
    RFC 2369 header value -- no mail client will parse it, and the one-click
    Unsubscribe control silently never appears. Caught by
    ``tests/publish/test_subscriber_notification_delivery.py``'s wire-level
    assertion; the header only *looked* set.

    These values are generated ASCII URLs and tokens, never user text, so
    emitting them verbatim is safe.
    """

    max_count = 1

    @classmethod
    def parse(cls, value: object, kwds: dict[str, object]) -> None:
        kwds["parse_tree"] = None
        kwds["decoded"] = str(value)
        kwds["defects"] = []

    def fold(self, *, policy: Policy[Any]) -> str:
        # ``name`` and ``__str__`` come from ``BaseHeader``, which the registry
        # mixes in when it builds the concrete header class.
        return f"{self.name}: {self}{policy.linesep}"  # type: ignore[attr-defined]


def _mail_policy() -> Policy[Any]:
    registry = HeaderRegistry()
    for header in ("List-Unsubscribe", "List-Unsubscribe-Post"):
        # The registry mixes this class with BaseHeader at map time, which is
        # exactly the documented extension point; the stub only admits an
        # already-complete BaseHeader subclass.
        registry.map_to_type(header, _VerbatimHeader)  # type: ignore[arg-type]
    return SMTP.clone(header_factory=registry)


_MAIL_POLICY = _mail_policy()

SmtpFactory = Callable[[str, int], smtplib.SMTP]


@dataclass(frozen=True)
class SmtpSettings:
    """Station SMTP relay configuration, read from the environment."""

    host: str
    from_address: str
    port: int = 587
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    use_starttls: bool = True

    @classmethod
    def from_env(cls) -> SmtpSettings:
        host = setting("subscriber-notifications", "smtp_host", "CIVICCAST_SMTP_HOST", "").strip()
        from_address = setting(
            "subscriber-notifications", "smtp_from", "CIVICCAST_SMTP_FROM", ""
        ).strip()
        missing = [
            name
            for name, value in (
                ("CIVICCAST_SMTP_HOST", host),
                ("CIVICCAST_SMTP_FROM", from_address),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "CIVICCAST_PROVIDER_MAIL=real requires "
                f"{', '.join(missing)} to be set (see docs/ops/cdn-and-providers.md)."
            )
        port_raw = setting(
            "subscriber-notifications", "smtp_port", "CIVICCAST_SMTP_PORT", ""
        ).strip()
        try:
            port = int(port_raw) if port_raw else 587
        except ValueError as exc:
            raise ValueError(f"CIVICCAST_SMTP_PORT must be an integer; got {port_raw!r}.") from exc
        username = (
            setting(
                "subscriber-notifications", "smtp_username", "CIVICCAST_SMTP_USERNAME", ""
            ).strip()
            or None
        )
        password = (
            setting(
                "subscriber-notifications", "smtp_password", "CIVICCAST_SMTP_PASSWORD", ""
            ).strip()
            or None
        )
        if (username is None) != (password is None):
            raise ValueError(
                "CIVICCAST_SMTP_USERNAME and CIVICCAST_SMTP_PASSWORD must be "
                "set together (or both left unset for an unauthenticated relay)."
            )
        use_starttls = (
            setting(
                "subscriber-notifications", "smtp_starttls", "CIVICCAST_SMTP_STARTTLS", "1"
            ).strip()
            != "0"
        )
        return cls(
            host=host,
            from_address=from_address,
            port=port,
            username=username,
            password=password,
            use_starttls=use_starttls,
        )


class SmtpMailbox:
    """Mail provider satisfying the same protocol as ``LocalMailbox``.

    ``smtp_factory`` exists for tests (and unusual relays): it must return a
    connected ``smtplib.SMTP``-compatible object for ``(host, port)``.
    """

    def __init__(
        self,
        settings: SmtpSettings,
        *,
        smtp_factory: SmtpFactory | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._settings = settings
        self._smtp_factory = smtp_factory
        self._timeout_seconds = timeout_seconds

    def _connect(self) -> smtplib.SMTP:
        if self._smtp_factory is not None:
            return self._smtp_factory(self._settings.host, self._settings.port)
        return smtplib.SMTP(self._settings.host, self._settings.port, timeout=self._timeout_seconds)

    def send_confirmation(self, *, email: str, confirmation_url: str) -> str:
        return self._send(
            to=email,
            subject="Confirm your CivicCast subscription",
            body=f"Confirm your subscription: {confirmation_url}",
        )

    def send_magic_link(self, *, email: str, link_url: str) -> str:
        return self._send(
            to=email,
            subject="Your CivicCast sign-in link",
            body=f"Open this single-use sign-in link: {link_url}",
        )

    def send_notification(self, *, email: str, payload: NotificationPayload) -> str:
        return self._send(
            to=email,
            subject=f"New CivicCast recording: {payload.title}",
            body=notification_body(payload),
            unsubscribe_url=payload.unsubscribe_url,
        )

    def _send(self, *, to: str, subject: str, body: str, unsubscribe_url: str | None = None) -> str:
        message = EmailMessage(policy=_MAIL_POLICY)
        message["From"] = self._settings.from_address
        message["To"] = to
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(domain=None)
        if unsubscribe_url:
            # RFC 2369 / RFC 8058. Mail clients render the header as a
            # one-click "Unsubscribe" control, and mailbox providers weigh its
            # presence when deciding whether a bulk sender is legitimate -- so
            # this is deliverability as well as consent. The one-click POST
            # variant is advertised because
            # ``POST /api/public/subscribe/unsubscribe`` exists for exactly
            # this purpose; the same URL also answers GET for a human click.
            message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
            message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        message.set_content(body)
        with self._connect() as smtp:
            smtp.ehlo()
            if self._settings.use_starttls:
                smtp.starttls()
                smtp.ehlo()
            if self._settings.username and self._settings.password:
                smtp.login(self._settings.username, self._settings.password)
            smtp.send_message(message)
        return str(message["Message-ID"])
