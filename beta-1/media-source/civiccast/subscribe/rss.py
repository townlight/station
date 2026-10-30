# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""RSS rendering helpers for subscription feeds."""

from __future__ import annotations

from email.utils import format_datetime
from html import escape

from civiccast.subscribe.models import RssItem


def render_rss(*, title: str, link: str, description: str, items: list[RssItem]) -> str:
    """Render a small RSS 2.0 document with stable GUIDs."""
    item_xml = "\n".join(
        [
            "    <item>\n"
            f"      <title>{escape(item.title)}</title>\n"
            f"      <link>{escape(item.link)}</link>\n"
            f'      <guid isPermaLink="false">{escape(item.guid)}</guid>\n'
            f"      <pubDate>{format_datetime(item.published_at)}</pubDate>\n"
            f"      <description>{escape(item.description)}</description>\n"
            "    </item>"
            for item in items
        ]
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        "  <channel>\n"
        f"    <title>{escape(title)}</title>\n"
        f"    <link>{escape(link)}</link>\n"
        f"    <description>{escape(description)}</description>\n"
        f"{item_xml}\n"
        "  </channel>\n"
        "</rss>\n"
    )


def validate_rss(xml: str) -> list[str]:
    """Lightweight local validation for CI; external validators are not required."""
    required = ['<rss version="2.0">', "<channel>", "<title>", "<link>", "<description>"]
    return [f"missing {marker}" for marker in required if marker not in xml]
