# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Config-driven CDN adapter selection (Stage C).

ADR 0006 designed the ``CDNAdapter`` protocol with a config key choosing the
active adapter; until this stage nothing read any such key and the only
construction site was a hard-instantiated R2 probe in the CLI. The selector is
``CIVICCAST_CDN_PROVIDER``:

- ``off`` (default): no CDN configured; :func:`build_cdn_adapter` returns None.
- ``bunny``: :class:`~civiccast.stream.cdn.bunny.BunnyCDNAdapter` from
  ``CIVICCAST_BUNNY_{STORAGE_ZONE,ACCESS_KEY,CDN_HOSTNAME}``.
- ``cloudflare_r2``: :class:`~civiccast.stream.cdn.cloudflare_r2.CloudflareR2Adapter`
  from the five ``CIVICCAST_R2_*`` variables (requires the ``cloudflare-r2``
  optional extra).
- ``fastly`` / ``akamai`` (ADR 0020): the generic
  :class:`~civiccast.stream.cdn.s3_compatible.S3CompatibleCDNAdapter` from the
  five ``CIVICCAST_FASTLY_*`` / ``CIVICCAST_AKAMAI_*`` variables (``REGION``,
  ``ACCESS_KEY_ID``, ``SECRET_ACCESS_KEY``, ``BUCKET``, ``PUBLIC_BASE_URL``);
  requires the ``s3-cdn`` optional extra.
- ``stub``: :class:`~civiccast.stream.cdn.stub.StubCDNAdapter` writing under
  ``CIVICCAST_CDN_STUB_ROOT`` (tests/proofs only).

Invalid values and missing credentials raise ``ValueError`` with the exact
variable names — fail fast at startup, never silently degrade.

Operator note (Stage A reconciliation): when a CDN fronts the public portal,
visitor requests reach CivicCast from the CDN edge and the analytics rate
limiter must trust those hops via ``CIVICCAST_ANALYTICS_TRUSTED_PROXY_CIDRS``,
or it will key rate limits on edge IPs. See
``docs/ops/cdn-and-providers.md``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from civiccast.stream.cdn import CDNAdapter

CDN_PROVIDER_OFF = "off"
CDN_PROVIDER_BUNNY = "bunny"
CDN_PROVIDER_CLOUDFLARE_R2 = "cloudflare_r2"
CDN_PROVIDER_FASTLY = "fastly"
CDN_PROVIDER_AKAMAI = "akamai"
CDN_PROVIDER_STUB = "stub"

CDN_PROVIDERS: tuple[str, ...] = (
    CDN_PROVIDER_OFF,
    CDN_PROVIDER_BUNNY,
    CDN_PROVIDER_CLOUDFLARE_R2,
    CDN_PROVIDER_FASTLY,
    CDN_PROVIDER_AKAMAI,
    CDN_PROVIDER_STUB,
)

# S3-compatible object-storage endpoints by provider region (ADR 0020). Both
# Fastly Object Storage and Akamai (Linode) Object Storage speak the S3 API, so
# they share the generic S3CompatibleCDNAdapter; only the endpoint differs.
_FASTLY_ENDPOINT = "https://{region}.object.fastlystorage.app"
_AKAMAI_ENDPOINT = "https://{region}.linodeobjects.com"

# A region is interpolated into the endpoint host, so it must be a bare label.
# Reject anything that could break out of the host (`/`, `@`, `.`, whitespace)
# and redirect the S3 endpoint -- which carries the station's real credentials
# -- to an attacker-controlled host. The adapter's `https://` check does not
# catch host substitution, so this guard is the one that does.
_SAFE_REGION = re.compile(r"^[a-z0-9-]+$")


def _validated_region(region: str, *, provider: str) -> str:
    if not _SAFE_REGION.match(region):
        raise ValueError(
            f"{provider} region must match [a-z0-9-] (got {region!r}); a malformed "
            "region could redirect the storage endpoint to another host."
        )
    return region


__all__ = [
    "CDN_CREDENTIAL_PROVIDER_IDS",
    "CDN_PROVIDERS",
    "CDN_PROVIDER_AKAMAI",
    "CDN_PROVIDER_BUNNY",
    "CDN_PROVIDER_CLOUDFLARE_R2",
    "CDN_PROVIDER_FASTLY",
    "CDN_PROVIDER_OFF",
    "CDN_PROVIDER_STUB",
    "CdnSettings",
    "build_cdn_adapter",
    "build_cdn_adapter_from_credentials",
]

# Installer/setup-wizard provider ids that map to a live CDN adapter. These are
# the hyphenated ids the setup wizard stores (see installer.service
# `_PROVIDER_CREDENTIAL_FIELDS`), distinct from the ``CIVICCAST_CDN_PROVIDER``
# env selector values above.
CDN_CREDENTIAL_PROVIDER_IDS: tuple[str, ...] = ("cloudflare-r2", "bunny", "fastly", "akamai")


@dataclass(frozen=True)
class CdnSettings:
    """The selected CDN provider, read from ``CIVICCAST_CDN_PROVIDER``."""

    provider: str = CDN_PROVIDER_OFF

    def __post_init__(self) -> None:
        # Fail fast on every construction path, not just from_env(), so a direct
        # CdnSettings("typo") can never reach build_cdn_adapter as a KeyError.
        if self.provider not in CDN_PROVIDERS:
            raise ValueError(
                f"CIVICCAST_CDN_PROVIDER must be one of {', '.join(CDN_PROVIDERS)}; "
                f"got {self.provider!r}."
            )

    @classmethod
    def from_env(cls) -> CdnSettings:
        raw = os.environ.get("CIVICCAST_CDN_PROVIDER", CDN_PROVIDER_OFF).strip().lower()
        return cls(provider=raw)


def _require_env(names: tuple[str, ...], *, provider: str) -> dict[str, str]:
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(
            f"CIVICCAST_CDN_PROVIDER={provider} requires {', '.join(missing)} "
            "to be set. See docs/ops/cdn-and-providers.md."
        )
    return values


def build_cdn_adapter(settings: CdnSettings) -> CDNAdapter | None:
    """Construct the selected CDN adapter, or None when provider is ``off``."""

    if settings.provider == CDN_PROVIDER_OFF:
        return None
    if settings.provider == CDN_PROVIDER_STUB:
        root = os.environ.get("CIVICCAST_CDN_STUB_ROOT", "").strip()
        if not root:
            raise ValueError(
                "CIVICCAST_CDN_PROVIDER=stub requires CIVICCAST_CDN_STUB_ROOT "
                "(an absolute directory the stub writes into)."
            )
        from civiccast.stream.cdn.stub import StubCDNAdapter

        return StubCDNAdapter(Path(root))
    if settings.provider == CDN_PROVIDER_BUNNY:
        values = _require_env(
            (
                "CIVICCAST_BUNNY_STORAGE_ZONE",
                "CIVICCAST_BUNNY_ACCESS_KEY",
                "CIVICCAST_BUNNY_CDN_HOSTNAME",
            ),
            provider=CDN_PROVIDER_BUNNY,
        )
        from civiccast.stream.cdn.bunny import BunnyCDNAdapter

        return BunnyCDNAdapter(
            storage_zone_name=values["CIVICCAST_BUNNY_STORAGE_ZONE"],
            access_key=values["CIVICCAST_BUNNY_ACCESS_KEY"],
            cdn_hostname=values["CIVICCAST_BUNNY_CDN_HOSTNAME"],
        )
    if settings.provider == CDN_PROVIDER_CLOUDFLARE_R2:
        values = _require_env(
            (
                "CIVICCAST_R2_ACCOUNT_ID",
                "CIVICCAST_R2_ACCESS_KEY_ID",
                "CIVICCAST_R2_SECRET_ACCESS_KEY",
                "CIVICCAST_R2_BUCKET",
                "CIVICCAST_R2_PUBLIC_BASE_URL",
            ),
            provider=CDN_PROVIDER_CLOUDFLARE_R2,
        )
        from civiccast.stream.cdn.cloudflare_r2 import CloudflareR2Adapter

        return CloudflareR2Adapter(
            account_id=values["CIVICCAST_R2_ACCOUNT_ID"],
            access_key_id=values["CIVICCAST_R2_ACCESS_KEY_ID"],
            secret_access_key=values["CIVICCAST_R2_SECRET_ACCESS_KEY"],
            bucket=values["CIVICCAST_R2_BUCKET"],
            public_base_url=values["CIVICCAST_R2_PUBLIC_BASE_URL"],
        )

    # Fastly + Akamai: S3-compatible object storage (ADR 0020). Same generic
    # adapter; the endpoint is built from the provider's region.
    endpoint_template = {
        CDN_PROVIDER_FASTLY: _FASTLY_ENDPOINT,
        CDN_PROVIDER_AKAMAI: _AKAMAI_ENDPOINT,
    }[settings.provider]
    prefix = f"CIVICCAST_{settings.provider.upper()}_"
    values = _require_env(
        (
            f"{prefix}REGION",
            f"{prefix}ACCESS_KEY_ID",
            f"{prefix}SECRET_ACCESS_KEY",
            f"{prefix}BUCKET",
            f"{prefix}PUBLIC_BASE_URL",
        ),
        provider=settings.provider,
    )
    from civiccast.stream.cdn.s3_compatible import S3CompatibleCDNAdapter

    region = _validated_region(values[f"{prefix}REGION"], provider=settings.provider)
    return S3CompatibleCDNAdapter(
        endpoint_url=endpoint_template.format(region=region),
        access_key_id=values[f"{prefix}ACCESS_KEY_ID"],
        secret_access_key=values[f"{prefix}SECRET_ACCESS_KEY"],
        bucket=values[f"{prefix}BUCKET"],
        public_base_url=values[f"{prefix}PUBLIC_BASE_URL"],
        region=region,
    )


def build_cdn_adapter_from_credentials(provider_id: str, fields: Mapping[str, str]) -> CDNAdapter:
    """Build a live CDN adapter from operator-entered setup-wizard credentials.

    ``provider_id`` is the installer's hyphenated id (``cloudflare-r2``,
    ``bunny``, ``fastly``, or ``akamai``) the setup wizard stores; ``fields``
    are that provider's saved credential fields. This is the bridge that makes
    portal-entered credentials actually configure the live CDN -- the setup
    wizard persists them, and this turns them into the same adapter
    ``build_cdn_adapter`` would from env.

    Raises ``ValueError`` for a non-CDN or unknown provider, or a malformed
    region. The adapter constructors raise ``ValueError`` for missing/blank
    required fields, and the boto3-backed adapters raise
    ``CloudflareR2NotInstalledError`` / ``S3CDNNotInstalledError`` when their
    optional extra is absent -- callers translate these into operator-facing
    messages.
    """
    if provider_id == "cloudflare-r2":
        from civiccast.stream.cdn.cloudflare_r2 import CloudflareR2Adapter

        return CloudflareR2Adapter(
            account_id=fields.get("account_id", ""),
            access_key_id=fields.get("access_key_id", ""),
            secret_access_key=fields.get("secret_access_key", ""),
            bucket=fields.get("bucket", ""),
            public_base_url=fields.get("public_base_url", ""),
        )
    if provider_id == "bunny":
        from civiccast.stream.cdn.bunny import BunnyCDNAdapter

        return BunnyCDNAdapter(
            storage_zone_name=fields.get("storage_zone_name", ""),
            access_key=fields.get("access_key", ""),
            cdn_hostname=fields.get("cdn_hostname", ""),
        )
    if provider_id in ("fastly", "akamai"):
        from civiccast.stream.cdn.s3_compatible import S3CompatibleCDNAdapter

        template = _FASTLY_ENDPOINT if provider_id == "fastly" else _AKAMAI_ENDPOINT
        region = fields.get("region", "")
        if region:
            region = _validated_region(region, provider=provider_id)
        return S3CompatibleCDNAdapter(
            # Empty region -> empty endpoint -> the adapter's own "incomplete
            # credentials" ValueError (so a missing region reads cleanly).
            endpoint_url=template.format(region=region) if region else "",
            access_key_id=fields.get("access_key_id", ""),
            secret_access_key=fields.get("secret_access_key", ""),
            bucket=fields.get("bucket", ""),
            public_base_url=fields.get("public_base_url", ""),
            region=region or "us-east-1",
        )
    raise ValueError(f"Not a CDN credential provider: {provider_id!r}")
