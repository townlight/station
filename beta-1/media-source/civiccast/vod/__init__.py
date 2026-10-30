# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""VOD asset metadata and the public embed-widget API.

Sprint 0.2 ships an in-memory asset store and the public embed endpoint.
The real database-backed asset store lands at rung 0.3 with the schedule
module. The Protocol-based design here means callers (router, portal,
embed widget consumers) do not change when the store implementation is
swapped.
"""

from civiccast.vod.embed import build_embed_html
from civiccast.vod.models import AssetEmbedResponse, AssetMetadata
from civiccast.vod.store import AssetStore, InMemoryAssetStore

__all__ = [
    "AssetEmbedResponse",
    "AssetMetadata",
    "AssetStore",
    "InMemoryAssetStore",
    "build_embed_html",
]
