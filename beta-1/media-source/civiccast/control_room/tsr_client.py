# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The CivicCast-side contract for the Node TSR sidecar (S16).

CivicCast's Python control plane never speaks a device protocol directly. It
sends *desired state* to a small Node service that embeds TSR
(timeline-state-resolver, MIT) behind this CivicCast-owned REST/IPC contract;
TSR computes the device command diff and drives the device. We treat TSR as a
black-box state resolver so the public API never leaks its shape, and so no
GPL/AGPL device-control code is ever linked into this Apache tree.

This module defines the contract (the ``TsrClient`` protocol + result/error
types) and a fail-closed ``NullTsrClient`` used when the sidecar is not
configured. The real localhost HTTP client lands with the Node sidecar (slice
2d); tests inject a fake implementing this protocol.
"""

from __future__ import annotations

from collections.abc import Callable
from ipaddress import ip_address
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from civiccast.control_room.models import DeviceProfile, ProductionDevice
from civiccast.control_room.secrets import load_device_secret

SecretResolver = Callable[[str], str | None]


class TsrApplyResult(BaseModel):
    """Outcome of applying one cue's desired state through TSR."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    detail: str = ""
    # The observed device-state change, already redacted of secrets by the
    # sidecar — safe to fold into the cue-fired audit.
    device_state: dict[str, Any] = Field(default_factory=dict)


class TsrProbeResult(BaseModel):
    """Outcome of opening a control connection to verify reachability."""

    model_config = ConfigDict(extra="forbid")

    reachable: bool
    capability_map: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""


class TsrClientError(RuntimeError):
    """Raised when the Node TSR sidecar cannot be reached or returns an error.

    The message carries the failure TYPE/summary only — never device secrets or
    raw protocol payloads (the sidecar redacts before it ever reaches here)."""


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def validate_tsr_loopback_url(base_url: str) -> str:
    """Return a normalized TSR URL only when it targets a loopback HTTP endpoint."""

    candidate = base_url.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("TSR sidecar URL must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("TSR sidecar URL must not include credentials")
    if not _is_loopback_host(parsed.hostname):
        raise ValueError("TSR sidecar URL must use localhost or a loopback IP address")
    return candidate


class TsrClient(Protocol):
    """The contract CivicCast's control plane calls; the Node sidecar fulfils it."""

    def health(self) -> TsrProbeResult:
        """Check sidecar liveness without resolving device secrets or touching devices."""
        ...

    def apply_cue(
        self,
        *,
        device: ProductionDevice,
        profile: DeviceProfile | None,
        action: str,
        payload: dict[str, Any],
    ) -> TsrApplyResult:
        """Resolve + send the desired state for one cue. Raises TsrClientError
        when the sidecar is unreachable or the device rejects the command."""
        ...

    def probe_device(
        self, *, device: ProductionDevice, profile: DeviceProfile | None
    ) -> TsrProbeResult:
        """Open a control connection to verify reachability + capability map."""
        ...


class NullTsrClient:
    """Default client when the Node TSR sidecar is NOT configured.

    Fails closed: ``apply_cue`` always raises (so a cue can never silently
    'succeed' against a control plane that isn't there) and ``probe_device``
    reports unreachable. The operator console renders this as a clear
    'production control unavailable' state (S16 §5) rather than a dead UI that
    looks functional."""

    _UNAVAILABLE = "Node TSR control service is not configured"

    def __init__(self, detail: str | None = None) -> None:
        self.detail = detail or self._UNAVAILABLE

    def apply_cue(
        self,
        *,
        device: ProductionDevice,
        profile: DeviceProfile | None,
        action: str,
        payload: dict[str, Any],
    ) -> TsrApplyResult:
        raise TsrClientError(self.detail)

    def probe_device(
        self, *, device: ProductionDevice, profile: DeviceProfile | None
    ) -> TsrProbeResult:
        return TsrProbeResult(reachable=False, detail=self.detail)

    def health(self) -> TsrProbeResult:
        return TsrProbeResult(reachable=False, detail=self.detail)


class HttpTsrClient:
    """TsrClient that calls the Node TSR sidecar over localhost HTTP.

    The sidecar (``civiccast/control_room/tsr_service``) embeds TSR and binds to
    127.0.0.1, so the device secret is resolved HERE (from the keyring via the
    device's ``secret_ref``) and POSTed over loopback only — it never leaves the
    box and is never persisted in the request. Any transport / non-200 /
    unparseable response is surfaced as ``TsrClientError`` (the failure type
    only — no secret or payload echo)."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        secret_resolver: SecretResolver | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._base = validate_tsr_loopback_url(base_url)
        self._timeout = timeout
        self._resolve_secret = secret_resolver or load_device_secret
        self._client = client  # injectable httpx.Client (MockTransport in tests)

    def _device_payload(
        self, device: ProductionDevice, profile: DeviceProfile | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "device_id": device.device_id,
            "kind": device.kind,
            "host": device.host,
            "port": device.port,
            "options": dict(profile.options) if profile is not None else {},
        }
        if device.secret_ref:
            secret = self._resolve_secret(device.secret_ref)
            if secret is not None:
                body["secret"] = secret  # loopback only; resolved from the keyring
        return body

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}{path}"
        try:
            if self._client is not None:
                resp = self._client.post(url, json=payload, timeout=self._timeout)
            else:
                resp = httpx.post(url, json=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise TsrClientError("TSR sidecar unreachable") from exc
        if resp.status_code != 200:
            raise TsrClientError(f"TSR sidecar HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise TsrClientError("TSR sidecar returned an unreadable response") from exc
        if not isinstance(body, dict):
            raise TsrClientError("TSR sidecar returned a non-object response")
        return body

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self._base}{path}"
        try:
            if self._client is not None:
                resp = self._client.get(url, timeout=self._timeout)
            else:
                resp = httpx.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            raise TsrClientError("TSR sidecar unreachable") from exc
        if resp.status_code != 200:
            raise TsrClientError(f"TSR sidecar HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise TsrClientError("TSR sidecar returned an unreadable response") from exc
        if not isinstance(body, dict):
            raise TsrClientError("TSR sidecar returned a non-object response")
        return body

    def health(self) -> TsrProbeResult:
        body = self._get("/healthz")
        ok = bool(body.get("ok", False))
        return TsrProbeResult(
            reachable=ok,
            capability_map={"tsr": body.get("tsr")} if body.get("tsr") is not None else {},
            detail="TSR sidecar reachable"
            if ok
            else str(body.get("detail", "TSR sidecar not ready")),
        )

    def apply_cue(
        self,
        *,
        device: ProductionDevice,
        profile: DeviceProfile | None,
        action: str,
        payload: dict[str, Any],
    ) -> TsrApplyResult:
        body = self._post(
            "/apply-cue",
            {"device": self._device_payload(device, profile), "action": action, "payload": payload},
        )
        if not body.get("ok", False):
            raise TsrClientError(f"TSR apply rejected: {body.get('detail', 'unknown')}")
        return TsrApplyResult(
            ok=True,
            detail=str(body.get("detail", "")),
            device_state=dict(body.get("device_state", {})),
        )

    def probe_device(
        self, *, device: ProductionDevice, profile: DeviceProfile | None
    ) -> TsrProbeResult:
        body = self._post("/probe-device", {"device": self._device_payload(device, profile)})
        return TsrProbeResult(
            reachable=bool(body.get("reachable", False)),
            capability_map=dict(body.get("capability_map", {})),
            detail=str(body.get("detail", "")),
        )


__all__ = [
    "HttpTsrClient",
    "NullTsrClient",
    "SecretResolver",
    "TsrApplyResult",
    "TsrClient",
    "TsrClientError",
    "TsrProbeResult",
    "validate_tsr_loopback_url",
]
