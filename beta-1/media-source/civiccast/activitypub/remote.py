# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Remote ActivityPub actor fetching and signed delivery."""

from __future__ import annotations

import contextvars
import ipaddress
import json
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urldefrag, urlparse

import httpx

from civiccast.activitypub.config import ActivityPubConfig
from civiccast.activitypub.keys import load_activitypub_private_key
from civiccast.activitypub.signatures import signed_request_headers

_PinnedDns = tuple[str, str] | None
_pinned_dns: contextvars.ContextVar[_PinnedDns] = contextvars.ContextVar(
    "civiccast_activitypub_pinned_dns",
    default=None,
)
_ORIGINAL_GETADDRINFO = socket.getaddrinfo


def _safe_getaddrinfo(
    host: bytes | str | None,
    port: bytes | str | int | None,
    family: int = 0,
    type: int = 0,  # noqa: A002 - mirrors socket.getaddrinfo's public signature.
    proto: int = 0,
    flags: int = 0,
) -> list[tuple[Any, ...]]:
    pinned = _pinned_dns.get()
    if isinstance(host, bytes):
        host_key = host.decode("idna").lower()
    elif isinstance(host, str):
        host_key = host.lower()
    else:
        host_key = None
    if pinned is not None and host_key == pinned[0]:
        return cast(
            list[tuple[Any, ...]],
            _ORIGINAL_GETADDRINFO(pinned[1], port, family, type, proto, flags),
        )
    return cast(
        list[tuple[Any, ...]],
        _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags),
    )


if socket.getaddrinfo is not _safe_getaddrinfo:
    socket.getaddrinfo = cast(Any, _safe_getaddrinfo)


@contextmanager
def _pin_dns(hostname: str, ip_address: str) -> Iterator[None]:
    token = _pinned_dns.set((hostname.lower(), ip_address))
    try:
        yield
    finally:
        _pinned_dns.reset(token)


class ActivityPubRemoteError(ValueError):
    """Raised when a remote actor or inbox cannot be used safely."""


@dataclass(frozen=True)
class RemoteActor:
    actor_id: str
    inbox: str
    public_key_id: str
    public_key_pem: str
    shared_inbox: str | None = None


@dataclass(frozen=True)
class DeliveryResult:
    inbox_url: str
    status_code: int
    response_body: str
    delivered_at: datetime


class RemoteActorFetcher(Protocol):
    def fetch(self, actor_url: str) -> RemoteActor: ...


class ActivityPubDeliveryClient(Protocol):
    def deliver(self, *, inbox_url: str, activity: dict[str, Any]) -> DeliveryResult: ...


class HttpxRemoteActorFetcher:
    """Fetch remote actor documents with SSRF guardrails."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        allow_http: bool = False,
        allow_local: bool = False,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._allow_http = allow_http
        self._allow_local = allow_local

    def fetch(self, actor_url: str) -> RemoteActor:
        actor_document_url = urldefrag(actor_url)[0]
        pinned_ip = _validate_remote_url(
            actor_document_url,
            allow_http=self._allow_http,
            allow_local=self._allow_local,
        )
        hostname = _hostname(actor_document_url)
        try:
            with _pin_dns(hostname, pinned_ip):
                response = httpx.get(
                    actor_document_url,
                    headers={"Accept": "application/activity+json, application/ld+json"},
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            raise ActivityPubRemoteError(f"Could not fetch ActivityPub actor {actor_url}.") from exc
        if not isinstance(payload, dict):
            raise ActivityPubRemoteError("Remote ActivityPub actor document must be an object.")
        return remote_actor_from_document(
            payload,
            expected_actor_url=actor_document_url,
            allow_http=self._allow_http,
            allow_local=self._allow_local,
        )


class HttpxActivityPubDeliveryClient:
    """Deliver signed ActivityPub activities to remote inboxes."""

    def __init__(
        self,
        *,
        config: ActivityPubConfig,
        timeout_seconds: float = 5.0,
        allow_http: bool = False,
        allow_local: bool = False,
    ) -> None:
        self._config = config
        self._timeout_seconds = timeout_seconds
        self._allow_http = allow_http
        self._allow_local = allow_local

    def deliver(self, *, inbox_url: str, activity: dict[str, Any]) -> DeliveryResult:
        if not self._config.private_key_path:
            raise ActivityPubRemoteError("ActivityPub delivery requires a private key path.")
        pinned_ip = _validate_remote_url(
            inbox_url,
            allow_http=self._allow_http,
            allow_local=self._allow_local,
        )
        hostname = _hostname(inbox_url)
        body = json.dumps(activity, separators=(",", ":"), sort_keys=True).encode("utf-8")
        local_actor = f"{self._config.base_url}/ap/actor"
        key = load_activitypub_private_key(Path(self._config.private_key_path))
        headers = signed_request_headers(
            method="POST",
            url=inbox_url,
            body=body,
            private_key=key,
            key_id=f"{local_actor}#main-key",
        )
        try:
            with _pin_dns(hostname, pinned_ip):
                response = httpx.post(
                    inbox_url,
                    content=body,
                    headers=headers,
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                )
        except httpx.HTTPError as exc:
            return DeliveryResult(
                inbox_url=inbox_url,
                status_code=0,
                response_body=f"ActivityPub delivery failed: {exc}"[:2000],
                delivered_at=datetime.now(UTC),
            )
        return DeliveryResult(
            inbox_url=inbox_url,
            status_code=response.status_code,
            response_body=response.text[:2000],
            delivered_at=datetime.now(UTC),
        )


def remote_actor_from_document(
    document: dict[str, Any],
    *,
    expected_actor_url: str | None = None,
    allow_http: bool = False,
    allow_local: bool = False,
) -> RemoteActor:
    actor_id = document.get("id")
    inbox = document.get("inbox")
    public_key = document.get("publicKey")
    if (
        not isinstance(actor_id, str)
        or not isinstance(inbox, str)
        or not isinstance(public_key, dict)
    ):
        raise ActivityPubRemoteError("Remote actor must expose id, inbox, and publicKey.")
    if expected_actor_url and actor_id.rstrip("/") != expected_actor_url.rstrip("/"):
        raise ActivityPubRemoteError("Remote actor id does not match the fetched actor URL.")
    key_id = public_key.get("id")
    public_key_pem = public_key.get("publicKeyPem")
    if not isinstance(key_id, str) or not isinstance(public_key_pem, str):
        raise ActivityPubRemoteError("Remote actor publicKey must expose id and publicKeyPem.")
    endpoints = document.get("endpoints")
    shared_inbox = None
    if isinstance(endpoints, dict) and isinstance(endpoints.get("sharedInbox"), str):
        shared_inbox = str(endpoints["sharedInbox"])
    _validate_remote_url(inbox, allow_http=allow_http, allow_local=allow_local)
    if shared_inbox:
        _validate_remote_url(shared_inbox, allow_http=allow_http, allow_local=allow_local)
    return RemoteActor(
        actor_id=actor_id,
        inbox=inbox,
        shared_inbox=shared_inbox,
        public_key_id=key_id,
        public_key_pem=public_key_pem,
    )


def _validate_remote_url(url: str, *, allow_http: bool, allow_local: bool = False) -> str:
    parsed = urlparse(url)
    allowed_schemes = {"https"} | ({"http"} if allow_http else set())
    if parsed.scheme not in allowed_schemes or not parsed.hostname:
        raise ActivityPubRemoteError("ActivityPub remote URL must be absolute HTTPS.")
    hostname = parsed.hostname.lower()

    if allow_local and _is_local_hostname(hostname):
        return hostname

    if not allow_local and _is_local_hostname(hostname):
        raise ActivityPubRemoteError("ActivityPub remote URL cannot target local hostnames.")
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        if not allow_local:
            return _validate_resolved_addresses(hostname)
    else:
        if not allow_local:
            _reject_private_ip(ip)
        return str(ip)

    return hostname


def _validate_resolved_addresses(hostname: str) -> str:
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ActivityPubRemoteError(f"Could not resolve ActivityPub remote URL: {exc}") from exc
    addresses: list[str] = []
    for info in infos:
        address = info[4][0]
        if not isinstance(address, str):
            continue
        if address in addresses:
            continue
        _reject_private_ip(ipaddress.ip_address(address))
        addresses.append(address)
    if addresses:
        return addresses[0]
    raise ActivityPubRemoteError("Could not resolve ActivityPub remote URL to a valid IP.")


def _hostname(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ActivityPubRemoteError("ActivityPub remote URL must be absolute HTTPS.")
    return parsed.hostname.lower()


def _is_local_hostname(hostname: str) -> bool:
    return hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local")


def _reject_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise ActivityPubRemoteError("ActivityPub remote URL resolved to a non-public address.")
