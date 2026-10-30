# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Embed-iframe HTML builder.

Produces the snippet returned by ``GET /api/public/embed/{asset_id}``.
The snippet is intentionally minimal and self-contained: a single iframe
with explicit width/height, ``loading="lazy"``, and the
``allow="autoplay; fullscreen; picture-in-picture"`` permissions needed
for HLS.js playback in a sandboxed iframe.
"""

from __future__ import annotations

from html import escape


def build_embed_html(
    portal_url: str,
    title: str,
    *,
    width: int = 640,
    height: int = 360,
) -> str:
    """Return an iframe HTML snippet pointing at ``portal_url``.

    All inputs are HTML-escaped. The caller is expected to pass a portal
    URL that already includes any required query string (e.g.,
    ``?manifest=...``).
    """
    safe_url = escape(portal_url, quote=True)
    safe_title = escape(title, quote=True)
    return (
        f'<iframe src="{safe_url}" '
        f'title="{safe_title}" '
        f'width="{width}" height="{height}" '
        f'loading="lazy" '
        f'style="width:100%;aspect-ratio:16/9;height:auto;border:0" '
        f'referrerpolicy="no-referrer" '
        f'sandbox="allow-scripts allow-same-origin allow-presentation" '
        f'allow="autoplay; fullscreen; picture-in-picture" '
        f"allowfullscreen></iframe>"
    )
