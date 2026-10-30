# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Cloud egress guard — the INVERSE of the local loopback guard (S13).

The local Ollama client (:mod:`civiccast.ai_runtime.ollama_client`) is loopback
ONLY: it refuses any host that is not ``127.0.0.1`` / ``localhost`` / ``::1``, and
that loopback check is what justifies its ``# noqa: S310`` suppressions. We do NOT
loosen that client and we do NOT route cloud calls through it — loosening it would
break the S310 safety justification for every local call site and could let the
default local path silently reach the network (the regression spec line 30
forbids).

Instead, credentialed cloud traffic flows through this separate module with its
own guard, :func:`require_cloud_https`:

* scheme MUST be ``https`` (never plain http for a credentialed cloud call);
* hostname MUST NOT be loopback (cloud is off-box by definition);
* hostname MUST be on a hard-coded per-provider allowlist (decision A applied to
  egress) so a misconfigured / hostile base URL can never exfiltrate to an
  arbitrary host.

The cloud adapters use ``urllib.request`` with the same ``# noqa: S310``
discipline, justified here by "host allowlisted + https-enforced by
``require_cloud_https``" instead of "loopback".
"""

from __future__ import annotations

import urllib.request  # re-exported so adapters + tests share one urlopen seam
from typing import Any
from urllib.parse import urlparse

# Loopback hosts that belong to the LOCAL client only — never a cloud target.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow any HTTP 3xx redirect on a credentialed cloud call.

    The pre-flight :func:`require_cloud_https` allowlist only validates the INITIAL
    URL. urllib's default opener installs an :class:`urllib.request.HTTPRedirectHandler`
    that silently follows 3xx to a new host and (in common cases) re-sends the
    ``Authorization: Bearer <token>`` header — so a 302 from an allowlisted host (a
    compromised provider response or an on-path attacker) to ``http://169.254.169.254/``
    or ``https://attacker.example/`` would defeat the allowlist and leak the provider
    credential and/or prompt content. Both provider endpoints are direct ``POST`` APIs
    that must not 3xx, so the safe, simple rule is: never follow a redirect.

    Returning ``None`` from :meth:`redirect_request` tells urllib not to issue the
    redirected request; urllib then raises the original 3xx as an ``HTTPError`` (an
    ``OSError`` subclass) which the adapters map to ``CloudRuntimeUnavailableError``.
    """

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


# A single dedicated opener (no redirect following) shared by BOTH cloud adapters via
# :func:`urlopen` below. We do NOT call ``urllib.request.install_opener`` — that would
# mutate the process-wide default opener and could change behavior for unrelated callers.
# Instead the cloud egress seam owns its own opener so the no-redirect guard is contained
# to cloud traffic.
_CLOUD_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def urlopen(request: urllib.request.Request, *, timeout: float) -> Any:
    """Open a cloud request through the no-redirect cloud opener (the egress seam).

    The single ``urlopen`` both cloud adapters call. Tests stub THIS symbol
    (``egress.urlopen``) to inject canned responses / errors without real network.
    Redirects are refused here (see :class:`_NoRedirectHandler`).
    """

    # Allowlisted + https-enforced by ``require_cloud_https`` at the call site; the
    # opener additionally refuses to follow any redirect off the allowlisted host.
    return _CLOUD_OPENER.open(request, timeout=timeout)


class CloudEgressError(RuntimeError):
    """Raised when a cloud URL fails the https / off-box / allowlist guard."""


class CloudConsentRequiredError(RuntimeError):
    """Raised when a cloud adapter is constructed without recorded operator consent."""


class CloudCredentialError(RuntimeError):
    """Raised when a cloud adapter cannot resolve its provider API credential."""


class CloudRuntimeUnavailableError(RuntimeError):
    """Raised when a cloud provider request fails (network / malformed response)."""


def require_cloud_https(url: str, *, allowed_hosts: frozenset[str]) -> None:
    """Refuse any URL that is not https, off-box, and on the provider allowlist.

    The mirror image of the local client's loopback guard: cloud MUST leave the
    box, MUST use TLS, and MUST target a known provider host.
    """

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise CloudEgressError(
            f"Cloud egress requires https; refusing scheme {parsed.scheme!r} for {url}."
        )
    host = parsed.hostname
    if host is None:
        raise CloudEgressError(f"Cloud egress URL has no host: {url}.")
    if host in _LOOPBACK_HOSTS:
        raise CloudEgressError(
            f"Cloud egress must be off-box; {host!r} is loopback (use the local client)."
        )
    if host not in allowed_hosts:
        raise CloudEgressError(
            f"Cloud host {host!r} is not on the provider allowlist {sorted(allowed_hosts)!r}."
        )


__all__ = [
    "CloudConsentRequiredError",
    "CloudCredentialError",
    "CloudEgressError",
    "CloudRuntimeUnavailableError",
    "require_cloud_https",
    "urllib",
    "urlopen",
]
