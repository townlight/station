# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CG board runtime helpers shared by preview and on-air composers.

The shared bridge is intentionally narrow: a given channel's active board is
resolved through the store and with fetched feeds. Using one helper avoids

drift between runtime paths that compose the same CG snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from civiccast.cg.board_resolver import ResolvedBoard, resolve_board
from civiccast.cg.board_store import CgBoardStore
from civiccast.cg.feed_fetcher import FeedCache, FeedFetch, default_http_fetch, fetch_all
from civiccast.cg.models import CgFeedItem, CgTemplateLibrary

__all__ = ["build_board_snapshot_from_store", "fetch_board_feed_items"]


def fetch_board_feed_items(
    store: CgBoardStore,
    channel_id: str,
    *,
    now: datetime,
    fetch: FeedFetch = default_http_fetch,
    cache: FeedCache | None = None,
) -> dict[str, list[CgFeedItem]]:
    """Fetch all enabled feed items for the channel's board zones using caching."""

    return fetch_all(
        store.list_feeds(channel_id, enabled_only=True),
        store,
        fetch=fetch,
        now=now,
        cache=cache,
    )


def build_board_snapshot_from_store(
    store: CgBoardStore,
    channel_id: str,
    *,
    now: datetime,
    feed_items_by_source: Mapping[str, list[CgFeedItem]] | None = None,
    template_library: CgTemplateLibrary | None = None,
    upcoming: list[tuple[datetime, str]] | None = None,
    fetch: FeedFetch = default_http_fetch,
    cache: FeedCache | None = None,
) -> ResolvedBoard | None:
    """Resolve a live board snapshot from store and fetched feed items."""

    feed_items = feed_items_by_source
    if feed_items is None:
        feed_items = fetch_board_feed_items(
            store,
            channel_id,
            now=now,
            fetch=fetch,
            cache=cache,
        )
    return resolve_board(
        store,
        channel_id,
        now=now,
        feed_items_by_source=feed_items,
        template_library=template_library,
        upcoming=upcoming,
    )
