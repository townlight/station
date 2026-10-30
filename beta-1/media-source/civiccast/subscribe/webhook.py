# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Real outbound webhook client (issue #111).

POSTs the canonical signed JSON notification body with an HMAC signature
header. Selected with ``CIVICCAST_PROVIDER_WEBHOOK=real``; the in-memory
``LocalWebhookClient`` mock remains the default. Per-subscription secrets are
passed in by the caller (they live sealed in the subscriptions table), so this
module reads no credentials from the environment — only the request timeout.

The signed bytes are exactly the bytes on the wire: canonical JSON with sorted
keys and compact separators, the same body :func:`sign_payload` HMACs, so the
mock's recorded signature and a real delivery's signature agree for the same
payload and secret.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass

import httpx

from civiccast.subscribe.crypto import sign_payload
from civiccast.subscribe.models import NotificationPayload

__all__ = ["HttpWebhookClient", "WebhookSettings"]


def validate_webhook_url(url: str) -> httpx.URL:
    """Reject unsafe URL forms before either a challenge or a notification."""
    parsed = httpx.URL(url)
    if parsed.scheme != "https" or not parsed.host or parsed.userinfo or parsed.fragment:
        raise ValueError(
            "Webhook destination must be an HTTPS URL without credentials or fragment."
        )
    host = parsed.host.lower()
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("Webhook destination must use a public address.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global or address.is_multicast:
            raise ValueError("Webhook destination must use a public address.")
    return parsed


class PublicWebhookTransport(httpx.HTTPTransport):
    """Pin each connection to validated DNS results, preserving Host and TLS SNI.

    Rewriting only the connection URL prevents a second DNS lookup from choosing
    an internal address. httpcore's sni_hostname retains certificate verification
    against the original hostname. No environment proxy can bypass this path.
    """

    def __init__(self) -> None:
        super().__init__(trust_env=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = validate_webhook_url(str(request.url))
        try:
            answers = socket.getaddrinfo(url.host, url.port or 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("Webhook destination could not be resolved.") from exc
        addresses = [str(answer[4][0]) for answer in answers]
        if not addresses or any(
            not ipaddress.ip_address(address).is_global
            or ipaddress.ip_address(address).is_multicast
            for address in addresses
        ):
            raise ValueError("Webhook destination must resolve only to public addresses.")
        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode("ascii")
        pinned = httpx.Request(
            request.method,
            url.copy_with(host=addresses[0]),
            headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": url.host},
        )
        return super().handle_request(pinned)


@dataclass(frozen=True)
class WebhookSettings:
    """Outbound webhook delivery configuration, read from the environment."""

    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> WebhookSettings:
        raw = os.environ.get("CIVICCAST_WEBHOOK_TIMEOUT_SECONDS", "").strip()
        if not raw:
            return cls()
        try:
            timeout = float(raw)
        except ValueError as exc:
            raise ValueError(
                f"CIVICCAST_WEBHOOK_TIMEOUT_SECONDS must be a number; got {raw!r}."
            ) from exc
        if timeout <= 0:
            raise ValueError(f"CIVICCAST_WEBHOOK_TIMEOUT_SECONDS must be positive; got {raw!r}.")
        return cls(timeout_seconds=timeout)


class HttpWebhookClient:
    """Webhook provider satisfying the same protocol as ``LocalWebhookClient``."""

    def __init__(
        self,
        settings: WebhookSettings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings or WebhookSettings()
        self._transport = transport

    def post(self, *, url: str, payload: NotificationPayload, secret: str) -> str:
        data = payload.model_dump(mode="json")
        signature = sign_payload(data, secret)
        body = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        self._post(
            url,
            body,
            {
                "content-type": "application/json",
                "x-civiccast-signature": f"sha256={signature}",
                "x-civiccast-asset-id": payload.asset_id,
            },
        )
        return signature

    def send_confirmation(self, *, url: str, confirmation_token: str, secret: str) -> None:
        """Provision the random secret only to the HTTPS endpoint's owner."""
        body = json.dumps(
            {
                "type": "civiccast.subscription_confirmation",
                "confirmation_token": confirmation_token,
                "signing_secret": secret,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self._post(url, body, {"content-type": "application/json"})

    def _post(self, url: str, body: bytes, headers: dict[str, str]) -> None:
        validate_webhook_url(url)
        with httpx.Client(
            transport=self._transport or PublicWebhookTransport(),
            timeout=self._settings.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.post(
                url,
                content=body,
                headers=headers,
            )
            response.raise_for_status()
