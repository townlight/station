# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Resolve a durable CG board into a renderable snapshot (S6 V1 — build step 7).

The board designer (:mod:`civiccast.cg.board_store`) persists a board, its
zones, and its feed sources. This module turns the active board for a channel
into a :class:`~civiccast.cg.models.MultiZoneCgSnapshot` — the contract the
GStreamer engine (S15) composites and the portal renders. It is **pure given
its inputs**: it reads the store (zones, feeds, approvals) and an
already-fetched ``feed_items_by_source`` map; it does no network I/O (the feed
fetcher, slice 2b, owns that).

Graceful degradation (no-FK soft refs):
* A feed-sourced zone whose feed was deleted or disabled renders empty and is
  reported in ``degraded_zone_ids`` — it never raises.
* An approval-gated feed zone shows only operator-approved items.
* A board missing one of the snapshot's required zone kinds
  (primary/ticker/schedule/logo) gets a minimal default zone back-filled so the
  contract is always satisfiable; the back-filled kinds are reported so the
  designer UI can prompt the operator to configure them.

Bulletin time-window helpers (S6 §6) live here too; the filler (slice 4) reuses
them so preview and on-air agree on what is airable *now*.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from civiccast.cg.board_models import CgZoneConfig
from civiccast.cg.board_store import CgBoardStore
from civiccast.cg.models import (
    CgBulletinSubmission,
    CgFeedItem,
    CgTemplate,
    CgTemplateLibrary,
    CgZone,
    MultiZoneCgSnapshot,
    ZoneKind,
)
from civiccast.cg.service import build_template_library

# The snapshot contract (MultiZoneCgSnapshot._required_zones_present) demands
# these four kinds; a board without them still resolves via back-fill.
_REQUIRED_KINDS: tuple[ZoneKind, ...] = ("primary", "ticker", "schedule", "logo")

_AIRABLE_STATES = frozenset({"accepted", "scheduled"})

__all__ = [
    "ResolvedBoard",
    "airable_bulletins",
    "bulletin_is_airable",
    "coming_up_next",
    "resolve_board",
]


class ResolvedBoard(BaseModel):
    """A resolved board: the renderable snapshot plus resolution diagnostics."""

    model_config = ConfigDict(extra="forbid")

    board_id: str
    snapshot: MultiZoneCgSnapshot
    backfilled_kinds: list[ZoneKind] = Field(default_factory=list)
    degraded_zone_ids: list[str] = Field(default_factory=list)


def resolve_board(
    store: CgBoardStore,
    channel_id: str,
    *,
    now: datetime,
    feed_items_by_source: Mapping[str, list[CgFeedItem]] | None = None,
    template_library: CgTemplateLibrary | None = None,
    upcoming: list[tuple[datetime, str]] | None = None,
) -> ResolvedBoard | None:
    """Resolve the channel's active board into a snapshot, or ``None`` if none.

    ``upcoming`` is the channel's next program-log entries (``(starts_at,
    title)``); when given, ``schedule`` zones render a real "coming up next"
    interstitial (S18 gap 6 / DC-CG4).
    """

    board = store.get_active_board(channel_id)
    if board is None:
        return None

    library = template_library or build_template_library(channel_id)
    template = _resolve_template(library, board.template_id)
    feed_items = feed_items_by_source or {}

    rendered: list[CgZone] = []
    degraded: list[str] = []
    seen_kinds: set[str] = set()
    used_ids: set[str] = set()

    for cfg in store.list_zones(board.board_id):
        zone, is_degraded = _render_zone(
            cfg, store=store, feed_items=feed_items, now=now, upcoming=upcoming
        )
        rendered.append(zone)
        used_ids.add(zone.zone_id)
        seen_kinds.add(zone.kind)
        if is_degraded:
            degraded.append(zone.zone_id)

    backfilled: list[ZoneKind] = []
    for kind in _REQUIRED_KINDS:
        if kind in seen_kinds:
            continue
        default_zone = _default_zone(kind, used_ids)
        rendered.append(default_zone)
        used_ids.add(default_zone.zone_id)
        backfilled.append(kind)

    snapshot = MultiZoneCgSnapshot(
        snapshot_id=f"{channel_id}-community-board",
        generated_at=now.replace(microsecond=0),
        channel_id=channel_id,
        template=template,
        zones=rendered,
        hls_render_path=f"/api/public/cg/channels/{channel_id}/stream.m3u8",
        portal_render_path=f"/api/public/cg/channels/{channel_id}/snapshot",
        proof_boundary="software-cg-snapshot-to-portal-and-hls-render-path",
    )
    return ResolvedBoard(
        board_id=board.board_id,
        snapshot=snapshot,
        backfilled_kinds=backfilled,
        degraded_zone_ids=degraded,
    )


def _resolve_template(library: CgTemplateLibrary, template_id: str) -> CgTemplate:
    for template in library.templates:
        if template.template_id == template_id:
            return template
    # The designer only offers built-in template ids; if a board names one that
    # no longer exists, fall back to the library's active template so resolution
    # never fails. (The library guarantees active_template_id resolves.)
    return next(
        template
        for template in library.templates
        if template.template_id == library.active_template_id
    )


def _render_zone(
    cfg: CgZoneConfig,
    *,
    store: CgBoardStore,
    feed_items: Mapping[str, list[CgFeedItem]],
    now: datetime,
    upcoming: list[tuple[datetime, str]] | None,
) -> tuple[CgZone, bool]:
    """Render one configured zone to a snapshot zone; second value = degraded."""

    degraded = False
    content: dict[str, object]
    title: str | None = None

    if cfg.content_source == "feed_adapter":
        feed = store.get_feed(cfg.feed_source_id) if cfg.feed_source_id else None
        if feed is None or not feed.enabled:
            # Soft ref: the named feed was deleted or disabled -> render empty.
            degraded = True
            content = {"items": [], "degraded": True}
        else:
            feed_source_id = cfg.feed_source_id
            assert feed_source_id is not None
            items = list(feed_items.get(feed_source_id, []))
            if cfg.approval_required:
                approved = store.list_approved_item_ids(
                    channel_id=feed.channel_id, feed_source_id=feed.feed_source_id
                )
                items = [item for item in items if item.item_id in approved]
            if cfg.allowed_tags:
                # CG depth (DC-CG3): a tagged zone shows only items carrying one
                # of its allowed tags.
                allowed = set(cfg.allowed_tags)
                items = [item for item in items if allowed & set(item.tags)]
            title = feed.label
            content = {"items": [_feed_item_dict(item) for item in items]}
    elif cfg.content_source == "manual":
        content = {"text": cfg.manual_text or ""}
    elif cfg.content_source == "image":
        content = {"image_asset_ref": cfg.image_asset_ref}
    elif cfg.content_source == "live_video":
        content = {"live_source_id": cfg.live_source_id}
    elif cfg.content_source == "schedule":
        # CG depth (DC-CG4): a program-aware "coming up next" interstitial when
        # the caller supplies the channel's upcoming program-log entries.
        content = (
            {"items": coming_up_next(upcoming, now=now)} if upcoming else {"source": "schedule"}
        )
    elif cfg.content_source == "emergency":
        content = {"active": False}
    else:  # clock
        content = {"mode": "clock"}

    return (
        CgZone(
            zone_id=cfg.zone_id,
            kind=cfg.zone_kind,
            title=title,
            source=cfg.content_source,
            content=content,
            refresh_seconds=cfg.refresh_seconds,
            approved=True,
        ),
        degraded,
    )


def _feed_item_dict(item: CgFeedItem) -> dict[str, object]:
    out: dict[str, object] = {"item_id": item.item_id, "title": item.title}
    if item.summary is not None:
        out["summary"] = item.summary
    if item.url is not None:
        out["url"] = item.url
    if item.starts_at is not None:
        out["starts_at"] = item.starts_at.isoformat()
    return out


_DEFAULT_ZONE_SPECS: dict[ZoneKind, tuple[str, str, dict[str, object]]] = {
    "primary": ("schedule", "Now showing", {"headline": "Community programming"}),
    "ticker": ("manual", "Community updates", {"items": []}),
    "schedule": ("schedule", "Coming up next", {"items": []}),
    "logo": ("station-branding", "Station identity", {}),
}


def _default_zone(kind: ZoneKind, used_ids: set[str]) -> CgZone:
    source, title, content = _DEFAULT_ZONE_SPECS[kind]
    zone_id = f"_default_{kind}"
    suffix = 1
    while zone_id in used_ids:  # avoid an unlikely collision with an operator zone
        zone_id = f"_default_{kind}_{suffix}"
        suffix += 1
    return CgZone(
        zone_id=zone_id,
        kind=kind,
        title=title,
        source=source,
        content=dict(content),
        approved=True,
    )


# ---------------------------------------------------------------------------
# Bulletin time-window helpers (S6 §6) — shared by preview + the filler
# ---------------------------------------------------------------------------


def bulletin_is_airable(bulletin: CgBulletinSubmission, *, now: datetime) -> bool:
    """True if an accepted/scheduled bulletin is inside its [start, end) window."""

    if bulletin.state not in _AIRABLE_STATES:
        return False
    if bulletin.requested_start is not None and bulletin.requested_start > now:
        return False  # scheduled for later
    # Airable until the half-open window end: now must be < requested_end.
    return bulletin.requested_end is None or now < bulletin.requested_end


def airable_bulletins(
    bulletins: list[CgBulletinSubmission], *, now: datetime
) -> list[CgBulletinSubmission]:
    """Filter to the bulletins airable right now, preserving input order."""

    return [b for b in bulletins if bulletin_is_airable(b, now=now)]


def coming_up_next(
    entries: list[tuple[datetime, str]], *, now: datetime, count: int = 4
) -> list[dict[str, str]]:
    """Format the next program-log entries for a "coming up next" zone (DC-CG4).

    ``entries`` is ``(starts_at, title)``; only future starts are kept, sorted
    earliest first, capped at ``count``, each rendered as ``{time, title}``.
    """

    upcoming = sorted((e for e in entries if e[0] >= now), key=lambda e: e[0])
    return [
        {"time": starts.strftime("%H:%M"), "title": title} for starts, title in upcoming[:count]
    ]
