# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Bridge operator-entered CDN credentials to the live CDN adapter.

The setup wizard persists CDN credentials (``installer.service``), but the live
CDN factory (``stream.cdn.factory``) is env-var-only. This module joins the two:
it turns saved setup-wizard credentials into a real adapter (so entering them in
the portal actually configures the CDN), and backs the wizard's "Test
connection" action with a live ``health_check``.
"""

from __future__ import annotations

from civiccast.installer.models import ProviderConnectionTestResponse
from civiccast.installer.service import stored_provider_field_values
from civiccast.stream.cdn import CDNAdapter
from civiccast.stream.cdn.cloudflare_r2 import CloudflareR2NotInstalledError
from civiccast.stream.cdn.factory import (
    CDN_CREDENTIAL_PROVIDER_IDS,
    build_cdn_adapter_from_credentials,
)
from civiccast.stream.cdn.s3_compatible import S3CDNNotInstalledError

__all__ = ["check_provider_connection", "resolve_stored_cdn_adapter"]


def resolve_stored_cdn_adapter() -> CDNAdapter | None:
    """Build a CDN adapter from the first configured setup-wizard provider.

    Returns None when no CDN provider has saved credentials. The app uses this
    as the fallback when no CDN env vars are set, so portal-entered credentials
    take effect (on the next worker build) without env configuration.
    """
    for provider_id in CDN_CREDENTIAL_PROVIDER_IDS:
        fields = stored_provider_field_values(provider_id)
        if fields:
            return build_cdn_adapter_from_credentials(provider_id, fields)
    return None


def check_provider_connection(provider_id: str) -> ProviderConnectionTestResponse:
    """Live-validate a provider's saved credentials via its adapter health check.

    Builds the adapter from the *saved* credentials (never from the request) and
    calls ``health_check()``. Never echoes credentials or a raw provider error,
    so the response is safe to show an operator. Raises ``ValueError`` for a
    non-CDN provider or when no credentials have been saved yet.
    """
    if provider_id not in CDN_CREDENTIAL_PROVIDER_IDS:
        raise ValueError(f"{provider_id} is not a CDN provider that supports a connection test.")
    fields = stored_provider_field_values(provider_id)
    if not fields:
        raise ValueError("Save the provider credentials before testing the connection.")

    try:
        adapter = build_cdn_adapter_from_credentials(provider_id, fields)
    except (CloudflareR2NotInstalledError, S3CDNNotInstalledError):
        extra = "cloudflare-r2" if provider_id == "cloudflare-r2" else "s3-cdn"
        return ProviderConnectionTestResponse(
            provider_id=provider_id,
            status="failed",
            message=(
                "CDN support for this provider is not installed on this server. "
                f"Install the '{extra}' extra and try again."
            ),
        )
    except ValueError:
        return ProviderConnectionTestResponse(
            provider_id=provider_id,
            status="failed",
            message="Saved credentials are incomplete. Re-enter them and try again.",
        )

    if adapter.health_check():
        return ProviderConnectionTestResponse(
            provider_id=provider_id,
            status="ok",
            message="Connected to the CDN with the saved credentials.",
        )
    return ProviderConnectionTestResponse(
        provider_id=provider_id,
        status="failed",
        message="Could not reach the CDN with these credentials. Check them and try again.",
    )
