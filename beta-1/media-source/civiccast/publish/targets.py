# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The one canonical publication-target resolver (WP-05 plan items 1-2).

A "publication target" is a ``(target_type, target_id)`` pair a resident can
subscribe to: ``("channel", "government")`` or ``("meeting_body", "planning")``.
Two very different surfaces need the same answer for the same asset:

* subscriber delivery -- which confirmed subscriptions a publish run must
  notify (:mod:`civiccast.publish.notifications`);
* the public subscription RSS feed -- which published recordings belong in
  ``/api/public/subscribe/rss/{target_type}/{target_id}.xml``
  (:mod:`civiccast.subscribe.router`).

Before WP-05 neither surface resolved anything: delivery hardcoded
``channel/government`` and RSS emitted an invented example item. Resolving
them in two places would guarantee they drift, so both call
:func:`resolve_publication_targets` and nothing else.

``StaffAssetRow`` has no ``channel_id`` column, so the channel is *derived*:

1. a live-finalized asset carries ``source_live_session_id`` -> the
   ``live_sessions`` row's ``channel_id``;
2. otherwise a scheduled asset has a ``schedule_items`` row -> its
   ``channel_id``;
3. otherwise (a legacy uploaded asset that was never scheduled) the station
   profile's ``default_channel_id``.

The asset's ``meeting_body`` tag, when set, adds a second target. A
subscription reached through both targets is deduplicated by the caller
(see :func:`civiccast.publish.notifications.deliver_publication_notifications`);
this module's own contract is only "which targets does this asset publish to",
returned in a stable, deterministic order.

Nothing here writes. Every lookup is a read of rows another module owns
(``civiccast.live.models.LiveSession``, ``civiccast.schedule.models
.ScheduleItem``) plus the station profile JSON.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.schedule.models import StaffAssetRow

__all__ = [
    "DEFAULT_CHANNEL_ID_FALLBACK",
    "ChannelAssociationLookup",
    "PublicationTarget",
    "SqlChannelAssociationLookup",
    "StaticChannelAssociationLookup",
    "publication_id_for_asset",
    "resolve_public_base_url",
    "resolve_publication_targets",
    "resolve_station_default_channel_id",
]

SessionFactory = Callable[[], AbstractContextManager[Session]]

TargetTypeValue = Literal["channel", "meeting_body"]
TargetSourceValue = Literal[
    "live_session",
    "schedule",
    "station_default",
    "asset_meeting_body",
]

#: Last-resort channel id. Matches ``civiccast.installer.station_state
#: ._FALLBACK_DEFAULT_CHANNEL_ID`` and the ``"government"`` target the publish
#: readiness module has always evaluated against, so a station that has not
#: completed first-admin setup keeps the behaviour it had before WP-05.
DEFAULT_CHANNEL_ID_FALLBACK = "government"

# Any non-empty string satisfies StationSetupState's validator; this call site
# only wants the profile, never the console URL it echoes back.
_PROFILE_READ_CONSOLE_URL = "local"


@dataclass(frozen=True, order=True)
class PublicationTarget:
    """One subscribable target an asset publishes to.

    ``order=True`` on ``(target_type, target_id, source)`` is load-bearing:
    delivery keys, the dedupe rule and the persisted per-delivery summary all
    depend on a deterministic target order, so the same asset resolves to the
    same first-listed target on every run.
    """

    target_type: TargetTypeValue
    target_id: str
    source: TargetSourceValue


class ChannelAssociationLookup(Protocol):
    """Resolves an asset's channel from the schedule/live association."""

    def channel_id_for_asset(self, asset: StaffAssetRow) -> str | None: ...

    def channel_ids_for_assets(self, assets: Sequence[StaffAssetRow]) -> dict[str, str]: ...


class StaticChannelAssociationLookup:
    """In-memory association for tests and no-durable-storage app instances.

    An app running without durable storage has no ``schedule_items`` or
    ``live_sessions`` table to read, so it resolves every asset to the station
    default rather than guessing -- this class with no arguments is exactly
    that behaviour, and is what the ephemeral app factory wires.
    """

    def __init__(
        self,
        *,
        by_asset_id: dict[str, str] | None = None,
        by_live_session_id: dict[str, str] | None = None,
    ) -> None:
        self._by_asset_id = dict(by_asset_id or {})
        self._by_live_session_id = dict(by_live_session_id or {})

    def channel_id_for_asset(self, asset: StaffAssetRow) -> str | None:
        if asset.source_live_session_id:
            found = self._by_live_session_id.get(asset.source_live_session_id)
            if found:
                return found
        return self._by_asset_id.get(asset.asset_id)

    def channel_ids_for_assets(self, assets: Sequence[StaffAssetRow]) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for asset in assets:
            channel_id = self.channel_id_for_asset(asset)
            if channel_id:
                resolved[asset.asset_id] = channel_id
        return resolved


class SqlChannelAssociationLookup:
    """Reads the real schedule/live-finalization association.

    Batched (:meth:`channel_ids_for_assets`) because the public RSS feed
    resolves a page of published assets at once; a per-asset query there would
    grow with the station's whole recording history, the same shape as the
    publish dashboard's PE-1 finding.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def channel_id_for_asset(self, asset: StaffAssetRow) -> str | None:
        return self.channel_ids_for_assets([asset]).get(asset.asset_id)

    def channel_ids_for_assets(self, assets: Sequence[StaffAssetRow]) -> dict[str, str]:
        if not assets:
            return {}
        # Imported here, not at module import time: civiccast.live.models and
        # civiccast.schedule.models both register ORM tables on Base.metadata,
        # and civiccast.publish.service imports this module at import time.
        from civiccast.live.models import LiveSession
        from civiccast.schedule.models import ScheduleItem

        live_session_ids = [
            asset.source_live_session_id for asset in assets if asset.source_live_session_id
        ]
        asset_ids = [asset.asset_id for asset in assets]
        resolved: dict[str, str] = {}
        with self._session_factory() as session:
            by_live_session: dict[str, str] = {}
            if live_session_ids:
                by_live_session = {
                    row.live_session_id: row.channel_id
                    for row in session.execute(
                        select(LiveSession.live_session_id, LiveSession.channel_id).where(
                            LiveSession.live_session_id.in_(sorted(set(live_session_ids)))
                        )
                    ).all()
                }
            # Deterministic pick when an asset was scheduled more than once:
            # the earliest non-cancelled airing is the publication's channel.
            by_asset: dict[str, str] = {}
            for row in session.execute(
                select(ScheduleItem.asset_id, ScheduleItem.channel_id)
                .where(
                    ScheduleItem.asset_id.in_(sorted(set(asset_ids))),
                    ScheduleItem.state != "cancelled",
                )
                .order_by(ScheduleItem.scheduled_at.asc(), ScheduleItem.channel_id.asc())
            ).all():
                by_asset.setdefault(row.asset_id, row.channel_id)
        for asset in assets:
            channel_id = None
            if asset.source_live_session_id:
                channel_id = by_live_session.get(asset.source_live_session_id)
            if not channel_id:
                channel_id = by_asset.get(asset.asset_id)
            if channel_id:
                resolved[asset.asset_id] = channel_id
        return resolved


def _station_profile() -> object | None:
    """Return the persisted station profile, or ``None`` before setup."""

    try:
        from civiccast.installer.station_state import read_station_setup_state

        return read_station_setup_state(operator_console_url=_PROFILE_READ_CONSOLE_URL).profile
    except Exception:  # pragma: no cover - unreadable/absent state is "no profile"
        return None


def resolve_station_default_channel_id() -> str:
    """Effective default channel id: persisted profile, else the fallback.

    Mirrors the precedence the station-state module already applies to the
    timezone and display name. There is deliberately no env override: the
    default channel is an operator-set profile field, not a deployment knob.
    """

    profile = _station_profile()
    candidate = getattr(profile, "default_channel_id", None)
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return DEFAULT_CHANNEL_ID_FALLBACK


def resolve_public_base_url() -> str | None:
    """Effective public portal base URL, or ``None`` when unconfigured.

    Precedence: ``CIVICCAST_PUBLIC_BASE_URL`` (the same variable the
    ActivityPub config already reads) > the station profile's
    ``public_base_url`` > ``None``. Callers that must still return a valid
    document (the public RSS feed) fall back to the request's own base URL
    rather than inventing a hostname.
    """

    env_value = os.environ.get("CIVICCAST_PUBLIC_BASE_URL", "").strip()
    if env_value:
        return env_value.rstrip("/")
    profile = _station_profile()
    candidate = getattr(profile, "public_base_url", None)
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip().rstrip("/")
    return None


def publication_id_for_asset(asset_id: str) -> str:
    """Stable publication identity for one recording.

    One asset is one publication. This is what makes re-approval idempotent
    (WP-05 plan item 6): approving the same recording twice resolves to the
    same publication id, therefore the same logical delivery keys, therefore
    the already-sent recipients are recognised and skipped rather than mailed
    a second time.
    """

    return f"pub:{asset_id}"


def resolve_publication_targets(
    asset: StaffAssetRow,
    *,
    lookup: ChannelAssociationLookup | None = None,
    default_channel_id: str | None = None,
    channel_id: str | None = None,
) -> tuple[PublicationTarget, ...]:
    """Return every subscribable target ``asset`` publishes to.

    ``channel_id`` short-circuits the association lookup (used by the batched
    RSS path, which resolves a page of assets in one query). ``lookup`` is the
    schedule/live association reader; ``None`` means "no association source
    wired", which resolves to the station default rather than failing.

    Always returns at least one target: a station always has a default
    channel, so a publish run can never silently reach nobody because the
    resolver returned nothing.
    """

    resolved_channel = channel_id
    source: TargetSourceValue
    if resolved_channel:
        source = "live_session" if asset.source_live_session_id else "schedule"
    else:
        if lookup is not None:
            resolved_channel = lookup.channel_id_for_asset(asset)
        if resolved_channel:
            source = "live_session" if asset.source_live_session_id else "schedule"
        else:
            resolved_channel = default_channel_id or resolve_station_default_channel_id()
            source = "station_default"

    targets: list[PublicationTarget] = [
        PublicationTarget(target_type="channel", target_id=resolved_channel, source=source)
    ]
    meeting_body = (asset.meeting_body or "").strip()
    if meeting_body:
        targets.append(
            PublicationTarget(
                target_type="meeting_body",
                target_id=meeting_body,
                source="asset_meeting_body",
            )
        )
    return tuple(_deduplicated(targets))


def _deduplicated(targets: Iterable[PublicationTarget]) -> list[PublicationTarget]:
    seen: set[tuple[str, str]] = set()
    unique: list[PublicationTarget] = []
    for target in sorted(targets):
        key = (target.target_type, target.target_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(target)
    return unique
