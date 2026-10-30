# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Explicit, restrictive CORS policy.

CivicCast's operator console and public portal are served same-origin from
this same FastAPI app (``_mount_packaged_portals``), and every first-party
fetch call uses a relative URL. There is no legitimate cross-origin browser
use case out of the box, so the default is **deny all cross-origin
requests** — no wildcard, no origin reflection. A station operator who
genuinely needs a separate origin (e.g. a dashboard on another host hitting
this API) can opt in with ``CIVICCAST_CORS_ALLOWED_ORIGINS``, a comma-
separated allowlist of exact origins (scheme+host+port, no wildcards).
"""

from __future__ import annotations

import os


def cors_allowed_origins() -> list[str]:
    """Read the explicit CORS allowlist from the environment.

    Empty by default (no cross-origin access at all). Any entry containing
    ``*`` is a startup error, not a silent no-op: CORSMiddleware matches
    origins exactly, so a typo like ``*.example.com`` would never match
    anything and the operator would chase phantom CORS failures — and a
    bare ``*`` on a possibly LAN-exposed station box is exactly the
    misconfiguration CORS exists to prevent. Fail fast at boot (same
    posture as ``validate_staff_token_config``, QA-002) so the config
    error is seen immediately instead of discovered in production.
    """

    raw = os.environ.get("CIVICCAST_CORS_ALLOWED_ORIGINS", "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    for origin in origins:
        if "*" in origin:
            raise ValueError(
                "CIVICCAST_CORS_ALLOWED_ORIGINS must list exact origins "
                f"(scheme://host[:port]); wildcard entry {origin!r} is not "
                "supported and would never match. Remove it or spell out "
                "each origin."
            )
    return origins
