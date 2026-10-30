# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Podcast RSS rendering for v0.8."""

from __future__ import annotations

from collections.abc import Mapping
from email.utils import format_datetime
from html import escape

from civiccast.podcast.models import PodcastEpisode


def render_podcast_rss(
    *,
    channel_id: str,
    episodes: list[PodcastEpisode],
    enclosure_bytes: Mapping[str, int],
    feed_url: str | None = None,
) -> str:
    item_xml = "\n".join(
        _episode_xml(episode, enclosure_bytes[episode.asset_id]) for episode in episodes
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">\n'
        "  <channel>\n"
        f"    <title>CivicCast {escape(channel_id)} podcast</title>\n"
        f"    <link>{escape(feed_url or f'https://portal.example/podcast/{channel_id}.xml')}</link>\n"
        "    <description>Approved CivicCast meeting audio episodes.</description>\n"
        "    <itunes:explicit>false</itunes:explicit>\n"
        f"{item_xml}\n"
        "  </channel>\n"
        "</rss>\n"
    )


def _episode_xml(episode: PodcastEpisode, audio_bytes: int) -> str:
    if audio_bytes <= 0:
        raise ValueError("Podcast enclosure must have a positive byte length.")
    description_parts = [episode.summary or "Approved meeting audio."]
    if episode.transcript_url:
        description_parts.append(f"Transcript: {episode.transcript_url}")
    if episode.chapters:
        chapter_text = "; ".join(
            f"{chapter.t:.0f}s {chapter.title}" for chapter in episode.chapters
        )
        description_parts.append(f"Chapters: {chapter_text}")
    description = " ".join(description_parts)
    return (
        "    <item>\n"
        f"      <title>{escape(episode.title)}</title>\n"
        f"      <link>{escape(episode.audio_url)}</link>\n"
        f'      <guid isPermaLink="false">{escape(episode.rss_guid)}</guid>\n'
        f"      <pubDate>{format_datetime(episode.published_at)}</pubDate>\n"
        f"      <description>{escape(description)}</description>\n"
        f'      <enclosure url="{escape(episode.audio_url)}" length="{audio_bytes}" type="audio/mpeg" />\n'
        "    </item>"
    )


def validate_podcast_rss(xml: str) -> list[str]:
    required = [
        '<rss version="2.0"',
        "xmlns:itunes=",
        "<itunes:explicit>false</itunes:explicit>",
        "<enclosure ",
        '<guid isPermaLink="false">',
    ]
    return [f"missing {marker}" for marker in required if marker not in xml]
