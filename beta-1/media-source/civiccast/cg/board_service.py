# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CG board-designer service (S6 V1 — build step 7, slice 3a).

The operator-facing orchestration over :class:`~civiccast.cg.board_store.CgBoardStore`:
server-owned id generation, an append-only audit trail on every mutation, and a
live preview that reuses the resolver so preview and on-air agree. The router
(slice 3b) is a thin HTTP shell over this; keeping the logic here makes it unit
testable without FastAPI.

Audit history is board-scoped (S6 §3). Board and zone mutations audit against
their board id; feed and approval mutations audit against the channel's active
board when one exists (a feed registered before any board exists simply has no
board history to attach to yet). ``operator_id`` always comes from the router's
*verified* token identity — never a request body — so the trail can't be spoofed.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from civiccast.cg.board_models import (
    CgBoard,
    CgBoardAuditEvent,
    CgFeedItemApproval,
    CgFeedSource,
    CgZoneConfig,
    ZoneContentSource,
)
from civiccast.cg.board_resolver import ResolvedBoard
from civiccast.cg.board_runtime import build_board_snapshot_from_store
from civiccast.cg.board_store import CgBoardStore
from civiccast.cg.feed_fetcher import (
    FeedCache,
    FeedFetch,
    default_http_fetch,
    fetch_all,
    fetch_feed_items,
)
from civiccast.cg.models import (
    CgFeedAdapter,
    CgFeedCatalog,
    CgFeedItem,
    FeedKind,
    FeedTrustTier,
    TemplateRegion,
    ZoneKind,
)

__all__ = [
    "BoardNotFoundError",
    "BoardView",
    "CgBoardService",
    "CgBoardServiceError",
    "FeedInput",
    "FeedNotFoundError",
    "FeedUpdateInput",
    "ServiceValidationError",
    "ZoneInput",
    "ZoneNotFoundError",
    "ZoneUpdateInput",
]


# ---------------------------------------------------------------------------
# Errors (router maps to HTTP status)
# ---------------------------------------------------------------------------


class CgBoardServiceError(Exception):
    """Base for board-service errors."""


class BoardNotFoundError(CgBoardServiceError):
    """No active board for the channel."""


class ZoneNotFoundError(CgBoardServiceError):
    """Zone missing on the channel's active board."""


class FeedNotFoundError(CgBoardServiceError):
    """Feed source missing on the channel."""


class ServiceValidationError(CgBoardServiceError):
    """A mutation would violate a domain rule (router -> 422)."""


# ---------------------------------------------------------------------------
# Input + view models (shared with the router request/response bodies)
# ---------------------------------------------------------------------------


class ZoneInput(BaseModel):
    """Add-zone payload."""

    model_config = ConfigDict(extra="forbid")

    region: TemplateRegion
    zone_kind: ZoneKind
    content_source: ZoneContentSource
    feed_source_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    refresh_seconds: Annotated[int | None, Field(default=None, gt=0, le=86400)] = None
    approval_required: bool = False
    manual_text: Annotated[str | None, Field(default=None, max_length=500)] = None
    image_asset_ref: Annotated[str | None, Field(default=None, max_length=120)] = None
    live_source_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    allowed_tags: list[str] = Field(default_factory=list)


class ZoneUpdateInput(BaseModel):
    """Patch-zone payload (only set fields are applied)."""

    model_config = ConfigDict(extra="forbid")

    region: TemplateRegion | None = None
    zone_kind: ZoneKind | None = None
    content_source: ZoneContentSource | None = None
    feed_source_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    refresh_seconds: Annotated[int | None, Field(default=None, gt=0, le=86400)] = None
    approval_required: bool | None = None
    manual_text: Annotated[str | None, Field(default=None, max_length=500)] = None
    image_asset_ref: Annotated[str | None, Field(default=None, max_length=120)] = None
    live_source_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    allowed_tags: list[str] | None = None


class FeedInput(BaseModel):
    """Register-feed payload."""

    model_config = ConfigDict(extra="forbid")

    kind: FeedKind
    label: Annotated[str, Field(min_length=1, max_length=160)]
    source_url: Annotated[str, Field(min_length=1, max_length=500)]
    trust_tier: FeedTrustTier
    refresh_seconds: Annotated[int, Field(gt=0, le=86400)] = 900
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)


class FeedUpdateInput(BaseModel):
    """Patch-feed payload (only set fields are applied)."""

    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    source_url: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    trust_tier: FeedTrustTier | None = None
    refresh_seconds: Annotated[int, Field(gt=0, le=86400)] | None = None
    enabled: bool | None = None
    tags: list[str] | None = None


class BoardView(BaseModel):
    """The active board plus its zones and the channel's feed sources."""

    model_config = ConfigDict(extra="forbid")

    board: CgBoard
    zones: list[CgZoneConfig]
    feeds: list[CgFeedSource]


class CgBoardService:
    """Operator orchestration over the durable board-designer store."""

    def __init__(
        self,
        store: CgBoardStore,
        *,
        clock: Callable[[], datetime] | None = None,
        upcoming_reader: Callable[[str, datetime], list[tuple[datetime, str]]] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        # Returns the channel's upcoming (starts_at, title) program-log entries
        # for the "coming up next" interstitial (CG depth, DC-CG4). None when the
        # program log is unavailable (preview then shows a plain schedule zone).
        self._upcoming_reader = upcoming_reader
        # Long-lived per-service feed cache (WP-06): the public feed catalog is
        # polled on a UI interval, so this reuses each feed's own TTL
        # (refresh_seconds) instead of hitting the network on every request --
        # the same pattern bulletin_filler.py uses for the on-air path.
        self._feed_cache = FeedCache()

    # -- ids + audit -------------------------------------------------------

    @staticmethod
    def _new_id(prefix: str) -> str:
        token = secrets.token_urlsafe(9).replace("-", "").replace("_", "")
        return f"{prefix}_{token}"

    def _audit(
        self,
        *,
        board_id: str,
        channel_id: str,
        event_kind: str,
        operator_id: str | None,
        details: dict[str, object] | None = None,
    ) -> None:
        self._store.append_audit(
            CgBoardAuditEvent(
                audit_id=self._new_id("cgaud"),
                board_id=board_id,
                channel_id=channel_id,
                event_kind=event_kind,
                operator_id=operator_id,
                occurred_at=self._clock(),
                details=details or {},
            )
        )

    def _require_active_board(self, channel_id: str) -> CgBoard:
        board = self._store.get_active_board(channel_id)
        if board is None:
            raise BoardNotFoundError(f"channel {channel_id!r} has no active board")
        return board

    # -- Board -------------------------------------------------------------

    def create_board(self, channel_id: str, *, template_id: str, operator_id: str) -> CgBoard:
        now = self._clock()
        board = CgBoard(
            board_id=self._new_id("cgb"),
            channel_id=channel_id,
            template_id=template_id,
            active=True,
            created_by=operator_id,
            created_at=now,
            updated_at=now,
        )
        stored = self._store.upsert_board(board)
        self._audit(
            board_id=stored.board_id,
            channel_id=channel_id,
            event_kind="board_created",
            operator_id=operator_id,
            details={"template_id": template_id},
        )
        return stored

    def update_board(
        self,
        channel_id: str,
        *,
        template_id: str | None = None,
        active: bool | None = None,
        operator_id: str,
    ) -> CgBoard:
        board = self._require_active_board(channel_id)
        updated = board.model_copy(
            update={
                "template_id": template_id if template_id is not None else board.template_id,
                "active": active if active is not None else board.active,
            }
        )
        stored = self._store.upsert_board(updated)
        self._audit(
            board_id=stored.board_id,
            channel_id=channel_id,
            event_kind="board_updated",
            operator_id=operator_id,
            details={"template_id": stored.template_id, "active": stored.active},
        )
        return stored

    def get_board_view(self, channel_id: str) -> BoardView | None:
        board = self._store.get_active_board(channel_id)
        if board is None:
            return None
        return BoardView(
            board=board,
            zones=self._store.list_zones(board.board_id),
            feeds=self._store.list_feeds(channel_id),
        )

    # -- Zones -------------------------------------------------------------

    def add_zone(self, channel_id: str, *, payload: ZoneInput, operator_id: str) -> CgZoneConfig:
        board = self._require_active_board(channel_id)
        try:
            zone = CgZoneConfig(
                zone_id=self._new_id("cgz"),
                board_id=board.board_id,
                created_at=self._clock(),
                **payload.model_dump(),
            )
        except ValidationError as exc:
            raise ServiceValidationError(str(exc)) from exc
        stored = self._store.upsert_zone(zone)
        self._audit(
            board_id=board.board_id,
            channel_id=channel_id,
            event_kind="zone_added",
            operator_id=operator_id,
            details={"zone_id": stored.zone_id, "zone_kind": stored.zone_kind},
        )
        return stored

    def update_zone(
        self, channel_id: str, zone_id: str, *, payload: ZoneUpdateInput, operator_id: str
    ) -> CgZoneConfig:
        board = self._require_active_board(channel_id)
        existing = self._store.get_zone(zone_id)
        if existing is None or existing.board_id != board.board_id:
            raise ZoneNotFoundError(f"zone {zone_id!r} not on the active board")
        changes = payload.model_dump(exclude_unset=True)
        try:
            updated = CgZoneConfig.model_validate(existing.model_copy(update=changes).model_dump())
        except ValidationError as exc:
            raise ServiceValidationError(str(exc)) from exc
        stored = self._store.upsert_zone(updated)
        self._audit(
            board_id=board.board_id,
            channel_id=channel_id,
            event_kind="zone_updated",
            operator_id=operator_id,
            details={"zone_id": zone_id},
        )
        return stored

    def delete_zone(self, channel_id: str, zone_id: str, *, operator_id: str) -> bool:
        board = self._require_active_board(channel_id)
        existing = self._store.get_zone(zone_id)
        if existing is None or existing.board_id != board.board_id:
            raise ZoneNotFoundError(f"zone {zone_id!r} not on the active board")
        removed = self._store.delete_zone(zone_id)
        if removed:
            self._audit(
                board_id=board.board_id,
                channel_id=channel_id,
                event_kind="zone_removed",
                operator_id=operator_id,
                details={"zone_id": zone_id},
            )
        return removed

    # -- Feed sources ------------------------------------------------------

    def list_feeds(self, channel_id: str) -> list[CgFeedSource]:
        return self._store.list_feeds(channel_id)

    def add_feed(self, channel_id: str, *, payload: FeedInput, operator_id: str) -> CgFeedSource:
        try:
            feed = CgFeedSource(
                feed_source_id=self._new_id("cgfeed"),
                channel_id=channel_id,
                created_by=operator_id,
                created_at=self._clock(),
                **payload.model_dump(),
            )
        except ValidationError as exc:
            raise ServiceValidationError(str(exc)) from exc
        stored = self._store.upsert_feed(feed)
        self._audit_channel_event(
            channel_id,
            event_kind="feed_added",
            operator_id=operator_id,
            details={"feed_source_id": stored.feed_source_id, "kind": stored.kind},
        )
        return stored

    def update_feed(
        self,
        channel_id: str,
        feed_source_id: str,
        *,
        payload: FeedUpdateInput,
        operator_id: str,
    ) -> CgFeedSource:
        existing = self._store.get_feed(feed_source_id)
        if existing is None or existing.channel_id != channel_id:
            raise FeedNotFoundError(f"feed {feed_source_id!r} not on channel {channel_id!r}")
        changes = payload.model_dump(exclude_unset=True)
        try:
            updated = CgFeedSource.model_validate(existing.model_copy(update=changes).model_dump())
        except ValidationError as exc:
            raise ServiceValidationError(str(exc)) from exc
        stored = self._store.upsert_feed(updated)
        self._audit_channel_event(
            channel_id,
            event_kind="feed_updated",
            operator_id=operator_id,
            details={"feed_source_id": feed_source_id},
        )
        return stored

    def delete_feed(self, channel_id: str, feed_source_id: str, *, operator_id: str) -> bool:
        existing = self._store.get_feed(feed_source_id)
        if existing is None or existing.channel_id != channel_id:
            raise FeedNotFoundError(f"feed {feed_source_id!r} not on channel {channel_id!r}")
        removed = self._store.delete_feed(feed_source_id)
        if removed:
            self._audit_channel_event(
                channel_id,
                event_kind="feed_removed",
                operator_id=operator_id,
                details={"feed_source_id": feed_source_id},
            )
        return removed

    def approve_feed_item(
        self, channel_id: str, *, feed_source_id: str, item_id: str, operator_id: str
    ) -> CgFeedItemApproval:
        feed = self._store.get_feed(feed_source_id)
        if feed is None or feed.channel_id != channel_id:
            raise FeedNotFoundError(f"feed {feed_source_id!r} not on channel {channel_id!r}")
        approval = self._store.approve_item(
            CgFeedItemApproval(
                approval_id=self._new_id("cgappr"),
                channel_id=channel_id,
                feed_source_id=feed_source_id,
                item_id=item_id,
                approved_by_operator=operator_id,
                approved_at=self._clock(),
            )
        )
        self._audit_channel_event(
            channel_id,
            event_kind="feed_item_approved",
            operator_id=operator_id,
            details={"feed_source_id": feed_source_id, "item_id": item_id},
        )
        return approval

    def list_feed_items_for_review(
        self,
        channel_id: str,
        *,
        feed_source_id: str,
        fetch: FeedFetch = default_http_fetch,
    ) -> list[CgFeedItem]:
        """Fetch a feed's current items and stamp each with its REAL approval
        status (cross-referenced against cg_feed_item_approvals), so the operator
        review queue shows which items are pending vs. already approved.

        A deliberate operator action (not the on-air path), so a live network
        fetch is acceptable — it is SSRF-guarded by the fetcher. Items are
        transient (never persisted); only the per-item approval rows are durable.
        A fetch/parse failure is swallowed by ``fetch_feed_items`` (returns [])."""
        feed = self._store.get_feed(feed_source_id)
        if feed is None or feed.channel_id != channel_id:
            raise FeedNotFoundError(f"feed {feed_source_id!r} not on channel {channel_id!r}")
        approved_ids = self._store.list_approved_item_ids(
            channel_id=channel_id, feed_source_id=feed_source_id
        )
        items = fetch_feed_items(feed, fetch=fetch)
        return [
            item.model_copy(update={"approved": item.item_id in approved_ids}) for item in items
        ]

    def upcoming(self, channel_id: str) -> list[tuple[datetime, str]]:
        """Return the channel's next program-log occurrences (starts_at, title)
        from the same real program-log data the "coming up next" preview zone
        (DC-CG4) and the operator Schedule / Program Guide screens read
        (``upcoming_reader``, wired to ``PostgresProgramLogStore`` in
        production -- see ``civiccast.app._cg_upcoming_reader``). WP-06
        non-negotiable follow-up: the public snapshot's schedule zone must
        source real occurrences here, never invented events. Empty when no
        reader is wired (e.g. ephemeral/no-DB mode)."""

        if self._upcoming_reader is None:
            return []
        return self._upcoming_reader(channel_id, self._clock())

    def feed_catalog(
        self, channel_id: str, *, fetch: FeedFetch = default_http_fetch
    ) -> CgFeedCatalog:
        """Build the public feed catalog from durable, enabled feed sources.

        WP-06: replaces the legacy deterministic ``build_feed_catalog()``
        (four hard-coded ``example.invalid`` adapters). Only feeds bound to at
        least one zone on the channel's active board are exposed -- a feed
        registered but not yet assigned to a zone has nothing to target, and
        ``CgFeedAdapter`` requires at least one target zone kind. Items from an
        approval-gated zone are filtered to operator-approved item ids only
        (S6 approval gate). A station with no board, no feeds, or no zone
        bindings yet returns an empty catalog -- the router renders that as an
        actionable empty state, never as invented content.

        Uses the service's long-lived ``FeedCache`` so a UI polling this on an
        interval reuses each feed's own ``refresh_seconds`` TTL instead of
        re-fetching every network feed on every request (mirrors
        ``egress.bulletin_filler``'s on-air fetch pattern).
        """

        now = self._clock()
        feeds = self._store.list_feeds(channel_id, enabled_only=True)
        board = self._store.get_active_board(channel_id)
        zones = self._store.list_zones(board.board_id) if board is not None else []

        target_kinds: dict[str, list[ZoneKind]] = {}
        approval_required: set[str] = set()
        for zone in zones:
            if zone.content_source != "feed_adapter" or not zone.feed_source_id:
                continue
            kinds = target_kinds.setdefault(zone.feed_source_id, [])
            if zone.zone_kind not in kinds:
                kinds.append(zone.zone_kind)
            if zone.approval_required:
                approval_required.add(zone.feed_source_id)

        fetched = fetch_all(feeds, self._store, fetch=fetch, now=now, cache=self._feed_cache)

        adapters: list[CgFeedAdapter] = []
        for feed in feeds:
            target = target_kinds.get(feed.feed_source_id)
            if not target:
                # Registered but not bound to any zone -- nothing to expose in
                # the catalog contract yet (an adapter needs >=1 target zone).
                continue
            items = list(fetched.get(feed.feed_source_id, []))
            if feed.feed_source_id in approval_required:
                approved_ids = self._store.list_approved_item_ids(
                    channel_id=channel_id, feed_source_id=feed.feed_source_id
                )
                items = [item for item in items if item.item_id in approved_ids]
            adapters.append(
                CgFeedAdapter(
                    adapter_id=feed.feed_source_id,
                    kind=feed.kind,
                    label=feed.label,
                    source_url=feed.source_url,
                    trust_tier=feed.trust_tier,
                    refresh_seconds=feed.refresh_seconds,
                    target_zone_kinds=target,
                    items=[item.model_copy(update={"approved": True}) for item in items],
                )
            )
        return CgFeedCatalog(
            generated_at=now.replace(microsecond=0),
            channel_id=channel_id,
            adapters=adapters,
            proof_boundary="configured-feed-adapters-to-approved-cg-zone-items",
        )

    def _audit_channel_event(
        self,
        channel_id: str,
        *,
        event_kind: str,
        operator_id: str,
        details: dict[str, object],
    ) -> None:
        # Channel-scoped (feed) events attach to the active board's history when
        # one exists; a feed registered before any board has no history yet.
        board = self._store.get_active_board(channel_id)
        if board is None:
            return
        self._audit(
            board_id=board.board_id,
            channel_id=channel_id,
            event_kind=event_kind,
            operator_id=operator_id,
            details=details,
        )

    # -- Preview + audit read ---------------------------------------------

    def preview(self, channel_id: str) -> ResolvedBoard | None:
        # Live preview renders the current configuration. A schedule zone renders a "coming up next"
        # interstitial from the program log when a reader is wired (DC-CG4).
        now = self._clock()
        upcoming = self._upcoming_reader(channel_id, now) if self._upcoming_reader else None
        return build_board_snapshot_from_store(self._store, channel_id, now=now, upcoming=upcoming)

    def list_audit(
        self, channel_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[CgBoardAuditEvent]:
        board = self._store.get_active_board(channel_id)
        if board is None:
            return []
        return self._store.list_audit(board_id=board.board_id, limit=limit, offset=offset)
