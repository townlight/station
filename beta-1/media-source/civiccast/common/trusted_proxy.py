# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Trusted-proxy / X-Forwarded-For resolution for CDN-fronted deployments.

When CivicCast sits behind a CDN (BunnyCDN, Cloudflare, etc.) every request
arrives with ``request.client.host == <cdn edge IP>``. The paywall rate
limiter, the recording cross-station guard, the audit logger, the analytics
trusted-proxy filter, and any other "real client IP" consumer must walk the
``X-Forwarded-For`` chain back to a non-trusted hop — that hop is the real
client. Trusting the chain when the immediate peer is NOT a configured
trusted proxy is the well-known IP-spoof attack; trusting blindly when the
peer IS a trusted proxy is the only way to make CDN deployments work.

This module is the single canonical resolver. Callers wire it in via
``resolve_client_ip(request)`` instead of dipping into ``request.client.host``
or building their own parser.

## Configuration

Three env vars + one provider preset:

* ``CIVICCAST_TRUSTED_PROXY_CIDRS`` — comma-separated CIDR list. Wins over
  every other source. Use this when the operator has explicit knowledge
  of the proxy fleet (e.g. an internal load balancer subnet).
* ``CIVICCAST_TRUSTED_PROXY_CIDRS_EXACT`` — comma-separated CIDR list used by
  the native reverse-proxy integration. When present (including as an empty
  string), it is the complete trust boundary: provider presets and private
  proxy opt-in are ignored. This prevents an edge-proxy setting from silently
  inheriting wider trust from a CDN setting used by a different subsystem.
* ``CIVICCAST_CDN_PROVIDER`` (already used by ``civiccast.stream.cdn``) —
  when set to ``bunny`` or ``cloudflare_r2`` (or ``cloudflare``), the
  resolver UNIONS the provider's published edge CIDR list with the
  explicit ``CIVICCAST_TRUSTED_PROXY_CIDRS`` value. The published list is
  a snapshot in ``_PROVIDER_CIDR_PRESETS`` below; operators with a
  patched provider deployment can override with the explicit env var.
* ``CIVICCAST_TRUST_PRIVATE_PROXIES`` (``false`` by default) — whether
  RFC1918 / loopback / link-local hops are considered trusted. QA-4:
  defaulting this on let a same-peer caller pick its own rate-limit bucket
  by sending an arbitrary ``X-Forwarded-For`` value from loopback/RFC1918,
  since every such peer was implicitly trusted with nothing configured.
  Default is now OFF. Deployments that genuinely run "uvicorn behind
  nginx" on ``127.0.0.1`` (or any other private-address reverse proxy)
  MUST opt in explicitly by setting ``CIVICCAST_TRUST_PRIVATE_PROXIES=true``
  (or by configuring ``CIVICCAST_TRUSTED_PROXY_CIDRS`` with the proxy's
  real address) — otherwise X-Forwarded-For is ignored and the proxy's own
  peer IP is used, which is safe but breaks real-client-IP attribution
  behind that proxy.

The provider preset CIDR snapshots are intentionally LOW-PRECISION — they
cover the publicly documented edge networks at the time the snapshot was
taken (BunnyCDN's ``edge.bunnycdn.com`` netblocks, Cloudflare's published
``cloudflare.com/ips-v4`` + ``ips-v6`` listing). Operators should validate
the snapshots against the CDN provider's CURRENT documentation before
shipping to production. The lab/dev path defaults to no CDN and no trusted
private proxies — every IP is treated as an untrusted direct peer unless a
CDN provider or explicit CIDR/private-proxy opt-in is configured.
"""

from __future__ import annotations

import os
from functools import lru_cache
from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request


# Snapshot of well-known CDN provider edge CIDRs. Operators should
# reconcile these against the provider's published list at deploy time;
# we do not auto-update them at runtime.
_PROVIDER_CIDR_PRESETS: dict[str, tuple[str, ...]] = {
    "bunny": (
        # BunnyCDN edge — published at https://docs.bunny.net/docs/ip-blocks
        # (snapshot 2026-06-19; reconcile before production deploy).
        "23.227.38.0/24",
        "104.243.96.0/19",
        "108.59.0.0/19",
        "169.150.196.0/22",
        "185.59.221.0/24",
        "185.93.0.0/22",
    ),
    "cloudflare": (
        # Cloudflare edge v4 — published at https://www.cloudflare.com/ips-v4
        # (snapshot 2026-06-19; reconcile before production deploy).
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "104.16.0.0/12",
        "108.162.192.0/18",
        "131.0.72.0/22",
        "141.101.64.0/18",
        "162.158.0.0/15",
        "172.64.0.0/13",
        "173.245.48.0/20",
        "188.114.96.0/20",
        "190.93.240.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        # v6
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    ),
}
# Alias: cloudflare_r2 (the CDN setting variant) maps to the same edge list.
_PROVIDER_CIDR_PRESETS["cloudflare_r2"] = _PROVIDER_CIDR_PRESETS["cloudflare"]


def _truthy(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@lru_cache(maxsize=1)
def _trusted_networks() -> tuple[IPv4Network | IPv6Network, ...]:
    """Compute + cache the configured trusted-proxy network list.

    Reset the cache by calling ``_trusted_networks.cache_clear()`` (useful
    in tests when the env vars change between cases).
    """
    networks: list[IPv4Network | IPv6Network] = []

    exact = os.environ.get("CIVICCAST_TRUSTED_PROXY_CIDRS_EXACT")
    if exact is not None:
        for entry in exact.split(","):
            token = entry.strip()
            if not token:
                continue
            try:
                networks.append(ip_network(token, strict=False))
            except ValueError:
                continue
        return tuple(networks)

    # Explicit operator list takes precedence — additive with the provider preset.
    raw = os.environ.get("CIVICCAST_TRUSTED_PROXY_CIDRS", "")
    for entry in raw.split(","):
        token = entry.strip()
        if not token:
            continue
        try:
            networks.append(ip_network(token, strict=False))
        except ValueError:
            # Silently skip; operator gets the per-request safety net via
            # the private-IP allowance in resolve_client_ip().
            continue

    # Auto-include the CDN provider's edge list when one is configured.
    provider = (os.environ.get("CIVICCAST_CDN_PROVIDER") or "").strip().lower()
    for cidr in _PROVIDER_CIDR_PRESETS.get(provider, ()):
        try:
            networks.append(ip_network(cidr, strict=False))
        except ValueError:
            continue

    return tuple(networks)


@lru_cache(maxsize=1)
def _trust_private() -> bool:
    if os.environ.get("CIVICCAST_TRUSTED_PROXY_CIDRS_EXACT") is not None:
        return False
    # QA-4: was default=True. A same-peer caller (loopback/RFC1918) could pick
    # its own rate-limit bucket via a spoofed X-Forwarded-For with nothing
    # configured. Deployments that need private-proxy trust set
    # CIVICCAST_TRUST_PRIVATE_PROXIES=true explicitly.
    return _truthy(os.environ.get("CIVICCAST_TRUST_PRIVATE_PROXIES"), default=False)


def reset_trusted_proxy_cache() -> None:
    """Drop the cached env-derived state. Call in tests after monkeypatching."""
    _trusted_networks.cache_clear()
    _trust_private.cache_clear()


def is_trusted_proxy(peer_ip: IPv4Address | IPv6Address | str) -> bool:
    """Return True when ``peer_ip`` is in the configured trusted-proxy set.

    Accepts an ``IPv4Address``/``IPv6Address`` or a string. Strings that
    don't parse as IPs return False (defense-in-depth — never trust a
    malformed value).
    """
    if isinstance(peer_ip, str):
        try:
            peer_ip = ip_address(peer_ip)
        except ValueError:
            return False
    if _trust_private() and (
        peer_ip.is_loopback
        or peer_ip.is_private
        or peer_ip.is_link_local
        or peer_ip.is_reserved
        or peer_ip.is_unspecified
    ):
        return True
    return any(peer_ip in net for net in _trusted_networks())


def _parse_forwarded_for(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    return [entry.strip() for entry in header_value.split(",") if entry.strip()]


def resolve_client_ip(request: Request) -> str:
    """Walk ``X-Forwarded-For`` back from the immediate peer to the first
    non-trusted hop. That hop is the real client.

    Behavior:
    * If the immediate peer is not in the trusted-proxy set, return the
      peer IP verbatim — we IGNORE the X-Forwarded-For header to defeat
      header-spoof attacks. This is the most important invariant; do not
      remove it without thinking through the CDN bypass surface.
    * If the immediate peer IS trusted, walk the X-Forwarded-For chain
      from right to left. The first IP we encounter that is NOT itself a
      trusted proxy is the real client. If every hop is trusted, return
      the LEFTMOST IP (which by convention is the originating client).
    * If ``request.client`` is None (unlikely, but possible in some test
      transports), return ``"unknown"`` rather than crashing — the caller
      typically uses this for rate limiting where ``"unknown"`` is a safe
      bucket key.
    """
    client = request.client
    peer_host = client.host if client else "unknown"
    if peer_host == "unknown":
        return peer_host
    if not is_trusted_proxy(peer_host):
        return peer_host

    forwarded = request.headers.get("X-Forwarded-For")
    chain = _parse_forwarded_for(forwarded)
    for entry in reversed(chain):
        try:
            entry_ip = ip_address(entry)
        except ValueError:
            continue
        if not is_trusted_proxy(entry_ip):
            return str(entry_ip)
    # Every hop was trusted — fall back to the leftmost entry (origin client).
    if chain:
        try:
            return str(ip_address(chain[0]))
        except ValueError:
            pass
    return peer_host


__all__ = [
    "is_trusted_proxy",
    "reset_trusted_proxy_cache",
    "resolve_client_ip",
]
