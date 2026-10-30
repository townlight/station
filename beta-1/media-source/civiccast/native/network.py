# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Validated listener and Caddy reverse-proxy settings for native stations.

The application listener is always loopback-only. Caddy is the supported TLS
edge: it serves only the resident portal on the public hostname and admits the
operator surface only from explicitly configured source networks.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path
from threading import RLock
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DNS_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class NetworkRuntimeValues(BaseModel):
    """Values consumed by the supervisor when it starts the app process."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    upstream_host: str
    upstream_port: int
    public_base_url: str | None
    trusted_proxy_cidrs: tuple[str, ...]
    caddy_bind_addresses: tuple[str, ...]


def _normalize_dns_hostname(value: str) -> str:
    """Normalize one DNS hostname and reject IPs, wildcards, and Caddy syntax."""
    token = value.strip().rstrip(".")
    if not token or any(char.isspace() or ord(char) < 33 for char in token):
        raise ValueError("Enter a DNS hostname without a scheme, path, or spaces.")
    try:
        ascii_host = token.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Enter a valid DNS hostname.") from exc
    try:
        ipaddress.ip_address(ascii_host)
    except ValueError:
        pass
    else:
        raise ValueError("Enter a DNS hostname, not an IP address.")
    labels = ascii_host.split(".")
    if (
        len(ascii_host) > 253
        or len(labels) < 2
        or any(not _DNS_LABEL.fullmatch(label) for label in labels)
    ):
        raise ValueError("Enter a fully qualified DNS hostname, such as watch.example.gov.")
    return ascii_host


class NetworkSettings(BaseModel):
    """Persisted station network configuration; defaults never expose a socket."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mode: Literal["loopback", "caddy"] = "loopback"
    public_base_url: str | None = Field(default=None, max_length=240)
    staff_hostname: str | None = Field(default=None, max_length=253)
    staff_allowed_cidrs: list[str] = Field(default_factory=list, max_length=32)
    caddy_bind_addresses: list[str] = Field(default_factory=list, max_length=8)
    upstream_host: str = "127.0.0.1"
    upstream_port: int = Field(default=8000, ge=1024, le=65535)

    @field_validator("upstream_host")
    @classmethod
    def _loopback_upstream_only(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("The application upstream must be a loopback IP address.") from exc
        if not address.is_loopback:
            raise ValueError("The application upstream must stay on 127.0.0.1 or ::1.")
        return str(address)

    @field_validator("public_base_url")
    @classmethod
    def _https_public_origin(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        raw = value.strip()
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Enter a valid HTTPS public URL.") from exc
        if (
            parsed.scheme.lower() != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or port not in (None, 443)
        ):
            raise ValueError("Use an HTTPS origin only, such as https://watch.example.gov.")
        hostname = _normalize_dns_hostname(parsed.hostname or "")
        return f"https://{hostname}"

    @field_validator("staff_hostname")
    @classmethod
    def _staff_dns_hostname(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _normalize_dns_hostname(value)

    @field_validator("staff_allowed_cidrs")
    @classmethod
    def _canonical_staff_networks(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            try:
                network = ipaddress.ip_network(raw.strip(), strict=False)
            except ValueError as exc:
                raise ValueError(f"Invalid staff network: {raw!r}.") from exc
            minimum_prefix = 8 if network.version == 4 else 16
            if network.prefixlen < minimum_prefix:
                raise ValueError("Staff networks must be narrower than IPv4 /8 or IPv6 /16.")
            canonical = str(network)
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    @field_validator("caddy_bind_addresses")
    @classmethod
    def _canonical_caddy_bind_addresses(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            try:
                address = ipaddress.ip_address(raw.strip())
            except ValueError as exc:
                raise ValueError(f"Invalid Caddy listener address: {raw!r}.") from exc
            if (
                address.is_unspecified
                or address.is_loopback
                or address.is_multicast
                or address.is_link_local
            ):
                raise ValueError(
                    "Caddy listener addresses must be assigned unicast LAN or public IPs."
                )
            if getattr(address, "scope_id", None) is not None:
                raise ValueError("Scoped IPv6 listener addresses are not supported.")
            canonical = str(address)
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    @model_validator(mode="after")
    def _complete_caddy_configuration(self) -> NetworkSettings:
        if self.mode == "caddy":
            if not self.public_base_url:
                raise ValueError("Caddy mode requires an HTTPS public URL.")
            if not self.staff_hostname:
                raise ValueError("Caddy mode requires a separate staff hostname.")
            public_hostname = urlsplit(self.public_base_url).hostname
            if self.staff_hostname == public_hostname:
                raise ValueError("The public and staff hostnames must be different.")
            if not self.staff_allowed_cidrs:
                raise ValueError("Caddy mode requires at least one staff network CIDR.")
        elif (
            self.public_base_url
            or self.staff_hostname
            or self.staff_allowed_cidrs
            or self.caddy_bind_addresses
        ):
            raise ValueError(
                "Caddy hostnames, staff networks, and bind addresses require Caddy mode."
            )
        return self

    def runtime_values(self) -> NetworkRuntimeValues:
        """Return exact bind/public/proxy values for supervisor integration."""
        proxy_networks: tuple[str, ...] = ()
        if self.mode == "caddy":
            address = ipaddress.ip_address(self.upstream_host)
            prefix = 32 if address.version == 4 else 128
            proxy_networks = (f"{address}/{prefix}",)
        return NetworkRuntimeValues(
            upstream_host=self.upstream_host,
            upstream_port=self.upstream_port,
            public_base_url=self.public_base_url,
            trusted_proxy_cidrs=proxy_networks,
            caddy_bind_addresses=tuple(self.caddy_bind_addresses if self.mode == "caddy" else ()),
        )


class NetworkSettingsStore:
    """Small atomic JSON store for non-secret network settings."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def load(self) -> NetworkSettings:
        with self._lock:
            if not self.path.exists():
                return NetworkSettings()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                return NetworkSettings.model_validate(raw)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"Network settings file is invalid: {self.path}") from exc

    def save(self, settings: NetworkSettings) -> bool:
        with self._lock:
            previous = self.load()
            if previous == settings:
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            contents = json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    dir=self.path.parent,
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(contents)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                if os.name != "nt":
                    temporary_path.chmod(0o600)
                temporary_path.replace(self.path)
                return True
            finally:
                if temporary_path is not None and temporary_path.exists():
                    temporary_path.unlink()


def _configured_network_settings_path() -> Path | None:
    configured = os.getenv("CIVICCAST_NETWORK_SETTINGS_PATH", "").strip()
    return Path(configured).expanduser() if configured else None


def default_network_settings_path() -> Path:
    """Return the configured network file or the station-state sibling."""
    configured = _configured_network_settings_path()
    if configured is not None:
        return configured
    from civiccast.installer.station_state import station_state_path

    return station_state_path().with_name("network-settings.json")


def _upstream_address(settings: NetworkSettings) -> str:
    host = settings.upstream_host
    if ipaddress.ip_address(host).version == 6:
        host = f"[{host}]"
    return f"{host}:{settings.upstream_port}"


def render_caddyfile(settings: NetworkSettings) -> str:
    """Render a strict, two-host Caddy config; local mode has no public edge."""
    if settings.mode != "caddy":
        return ""
    assert settings.public_base_url is not None and settings.staff_hostname is not None
    public_hostname = urlsplit(settings.public_base_url).hostname
    assert public_hostname is not None
    upstream = _upstream_address(settings)
    networks = " ".join(settings.staff_allowed_cidrs)
    bind_line = ""
    bound_http_site = ""
    if settings.caddy_bind_addresses:
        bind_addresses = " ".join(
            f"[{address}]" if ipaddress.ip_address(address).version == 6 else address
            for address in settings.caddy_bind_addresses
        )
        bind_line = f"    bind {bind_addresses}\n"
        bound_http_site = (
            f"http://{public_hostname}, http://{settings.staff_hostname} {{\n"
            f"{bind_line}"
            "    redir https://{host}{uri} permanent\n"
            "}\n\n"
        )
    proxy_headers = (
        "            header_up X-Forwarded-For {http.request.remote.host}\n"
        "            header_up X-Forwarded-Proto {http.request.scheme}\n"
        "            header_up X-Forwarded-Host {http.request.host}\n"
        "            header_up X-Real-IP {http.request.remote.host}\n"
        "            header_up -Forwarded\n"
    )
    return (
        "# Generated by CivicCast. Caddy must be directly reachable by clients.\n"
        "{\n"
        "    admin off\n"
        "}\n\n"
        f"{bound_http_site}"
        f"https://{public_hostname} {{\n"
        f"{bind_line}"
        "    route {\n"
        "        @resident path / /assets/* /favicon.svg /api/public/* /media/vod/* /media/live/*\n"
        f"        reverse_proxy @resident {upstream} {{\n"
        f"{proxy_headers}"
        "        }\n"
        '        respond "Not found" 404\n'
        "    }\n"
        "}\n\n"
        f"https://{settings.staff_hostname} {{\n"
        f"{bind_line}"
        "    route {\n"
        "        @first_setup path /api/setup /api/setup/*\n"
        '        respond @first_setup "First setup is available only on the station computer." 403\n'
        f"        @staff_access remote_ip {networks}\n"
        f"        reverse_proxy @staff_access {upstream} {{\n"
        f"{proxy_headers}"
        "        }\n"
        '        respond "Staff access is limited to approved networks." 403\n'
        "    }\n"
        "}\n"
    )


__all__ = [
    "NetworkRuntimeValues",
    "NetworkSettings",
    "NetworkSettingsStore",
    "default_network_settings_path",
    "render_caddyfile",
]
