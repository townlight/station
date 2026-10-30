# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Route a composed remote-guest programme through CivicCast live takeover."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from civiccast.egress.models import EgressProofEvent, redact_source_uri
from civiccast.egress.takeover_service import TakeoverService
from civiccast.live.contribution.bridge import guest_stream_id
from civiccast.live.contribution.mixer import (
    GuestFeed,
    GuestMixHandle,
    probe_guest_feed,
    start_guest_mix,
    wait_for_guest_mix,
)
from civiccast.live.contribution.models import ContributionRoom, RemoteGuestSession
from civiccast.live.contribution.store import ContributionStore
from civiccast.live.models import LiveSourceCreate, LiveSourceUpdate
from civiccast.live.readiness_service import LiveSourceReadinessService
from civiccast.live.store import LiveSourceStore

_LOG = logging.getLogger(__name__)


@dataclass
class _MixRun:
    handle: GuestMixHandle
    read_uri: str
    room_id: str


class GuestProgramRouter:
    """One controlled media transition at a time for each station process."""

    def __init__(
        self,
        contribution_store: ContributionStore,
        source_store: LiveSourceStore,
        takeover_service: Callable[[], TakeoverService],
        *,
        rtsp_base: str,
        srt_base: str,
        proof_events: Callable[[str, int], list[EgressProofEvent]] | None = None,
    ) -> None:
        rtsp = urlsplit(rtsp_base)
        srt = urlsplit(srt_base)
        if rtsp.scheme not in {"rtsp", "rtsps"} or not rtsp.hostname:
            raise ValueError("guest relay needs an RTSP read address")
        if srt.scheme != "srt" or not srt.hostname:
            raise ValueError("guest relay needs an SRT publish/read address")
        if any((rtsp.username, rtsp.password, rtsp.query, rtsp.fragment)):
            raise ValueError("guest RTSP address must not carry credentials or a query")
        if any((srt.username, srt.password, srt.query, srt.fragment, srt.path)):
            raise ValueError("guest SRT address must be a plain server base URL")
        self._contribution_store = contribution_store
        self._source_store = source_store
        self._takeover_service = takeover_service
        self._rtsp_base = rtsp_base.rstrip("/")
        self._srt_base = srt_base.rstrip("/")
        self._proof_events = proof_events
        self._lock = threading.RLock()
        self._runs: dict[str, _MixRun] = {}
        self._retry_after: dict[str, float] = {}

    @staticmethod
    def _source_id(channel_id: str) -> str:
        digest = hashlib.sha256(channel_id.encode("utf-8")).hexdigest()[:24]
        return f"remote-guest-{digest}"

    def on_air(self, _guest: RemoteGuestSession, room: ContributionRoom) -> None:
        self._transition(room)

    def off_air(self, guest: RemoteGuestSession, room: ContributionRoom) -> None:
        self._transition(room, excluding=guest.session_id)

    def audio_mute(self, guest: RemoteGuestSession, room: ContributionRoom, muted: bool) -> None:
        self._transition(room, mute_override=(guest.session_id, muted))

    def _sessions(
        self,
        room: ContributionRoom,
        *,
        excluding: str | None,
        mute_override: tuple[str, bool] | None,
    ) -> list[tuple[RemoteGuestSession, bool]]:
        sessions = self._contribution_store.list_sessions(room_id=room.room_id, active_only=True)
        result = []
        for guest in sessions:
            if guest.session_id == excluding or guest.state not in {"on_air", "muted"}:
                continue
            muted = guest.state == "muted"
            if mute_override is not None and guest.session_id == mute_override[0]:
                muted = mute_override[1]
            result.append((guest, muted))
        return result

    def _feeds(self, sessions: list[tuple[RemoteGuestSession, bool]]) -> list[GuestFeed]:
        def read_feed(item: tuple[RemoteGuestSession, bool]) -> GuestFeed:
            guest, muted = item
            invite = self._contribution_store.get_invite(guest.invite_id)
            if invite is None:
                raise RuntimeError("guest invite no longer exists")
            endpoint = f"{self._rtsp_base}/{guest_stream_id(invite.invite_token)}"
            return probe_guest_feed(endpoint, muted=muted)

        with ThreadPoolExecutor(max_workers=min(4, len(sessions))) as workers:
            return list(workers.map(read_feed, sessions))

    def _uri(self, action: str, stream_id: str) -> str:
        return f"{self._srt_base}?streamid={action}:{stream_id}&pkt_size=1316"

    def _await_on_air(self, channel: str, read_uri: str, since: datetime) -> None:
        """Observe the daemon's actual handoff before retiring the previous mix."""
        if self._proof_events is None:
            return  # Unit fakes have no egress daemon; production supplies this reader.
        expected = redact_source_uri(read_uri)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for event in self._proof_events(channel, 10):
                observed = event.observed_at
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=UTC)
                if (
                    event.state == "ON_AIR"
                    and event.proof_boundary == "civiccast-egress-handoff-boundary"
                    and event.source_path == expected
                    and observed >= since
                ):
                    return
            time.sleep(0.5)
        raise RuntimeError("egress did not confirm the remote guest feed on air")

    def _await_handback(self, channel: str, source_id: str, since: datetime) -> None:
        """Keep the final guest feed alive until scheduled output takes over."""
        if self._proof_events is None:
            return
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for event in self._proof_events(channel, 10):
                observed = event.observed_at
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=UTC)
                if (
                    event.state in {"ON_AIR", "FALLBACK_SLATE"}
                    and event.proof_boundary == "civiccast-egress-handoff-boundary"
                    and event.source_ref != source_id
                    and observed >= since
                ):
                    return
            time.sleep(0.5)
        raise RuntimeError("egress did not confirm handback from remote guests")

    def _transition(
        self,
        room: ContributionRoom,
        *,
        excluding: str | None = None,
        mute_override: tuple[str, bool] | None = None,
    ) -> None:
        with self._lock:
            channel = room.channel_id
            source_id = self._source_id(channel)
            takeover = self._takeover_service()
            active = takeover.state(channel).active_session
            if active is not None and active.source_ref != source_id:
                raise RuntimeError("another live source currently owns this channel")
            sessions = self._sessions(room, excluding=excluding, mute_override=mute_override)
            previous = self._runs.get(channel)
            if previous is not None and previous.room_id != room.room_id:
                raise RuntimeError("another remote guest room currently owns this channel")
            if not sessions:
                if active is not None:
                    switch_at = datetime.now(UTC)
                    takeover.handback(
                        channel_id=channel,
                        operator_id="remote-contribution",
                        notes="remote guests returned to program",
                    )
                    self._await_handback(channel, source_id, switch_at)
                if previous is not None:
                    previous.handle.terminate()
                    self._runs.pop(channel, None)
                return

            feeds = self._feeds(sessions)
            stream_id = f"ccguest-{uuid.uuid4().hex}"
            read_uri = self._uri("read", stream_id)
            candidate = start_guest_mix(feeds, self._uri("publish", stream_id))
            old_source = self._source_store.get(source_id)
            returned_old = False
            takeover_sent = False
            try:
                wait_for_guest_mix(candidate, read_uri)
                if active is not None:
                    takeover.handback(
                        channel_id=channel,
                        operator_id="remote-contribution",
                        notes="switching remote guest composition",
                    )
                    returned_old = True
                if old_source is None:
                    self._source_store.create(
                        LiveSourceCreate(
                            live_source_id=source_id,
                            channel_id=channel,
                            name="Remote guest program",
                            source_type="srt",
                            endpoint_url=read_uri,
                        )
                    )
                else:
                    self._source_store.update(
                        source_id,
                        LiveSourceUpdate(
                            endpoint_url=read_uri,
                            expected_row_version=old_source.row_version,
                        ),
                    )
                _, observation, _ = LiveSourceReadinessService(self._source_store).probe(source_id)
                if not observation.ok:
                    raise RuntimeError("composed guest feed failed live-source readiness")
                switch_at = datetime.now(UTC)
                takeover.take(
                    channel_id=channel,
                    operator_id="remote-contribution",
                    reason="remote guest composition on air",
                    path_id=source_id,
                    duration_seconds=12 * 3600,
                )
                takeover_sent = True
                self._await_on_air(channel, read_uri, switch_at)
            except Exception:
                if takeover_sent:
                    takeover.handback(
                        channel_id=channel,
                        operator_id="remote-contribution",
                        notes="remote guest handoff was not confirmed",
                    )
                if (
                    returned_old
                    and previous is not None
                    and previous.handle.poll() is None
                    and old_source is not None
                ):
                    current = self._source_store.get(source_id)
                    if current is not None:
                        self._source_store.update(
                            source_id,
                            LiveSourceUpdate(
                                endpoint_url=old_source.endpoint_url,
                                expected_row_version=current.row_version,
                            ),
                        )
                        LiveSourceReadinessService(self._source_store).probe(source_id)
                        takeover.take(
                            channel_id=channel,
                            operator_id="remote-contribution",
                            reason="restore previous remote guest composition",
                            path_id=source_id,
                            duration_seconds=12 * 3600,
                        )
                candidate.terminate()
                raise
            self._runs[channel] = _MixRun(candidate, read_uri, room.room_id)
            if previous is not None:
                previous.handle.terminate()

    def check_health(self) -> None:
        """Restart a failed active mixer without changing guest lifecycle records."""
        with self._lock:
            for channel, run in list(self._runs.items()):
                if run.handle.poll() is None or time.monotonic() < self._retry_after.get(
                    channel, 0
                ):
                    continue
                room = self._contribution_store.get_room(run.room_id)
                if room is None:
                    continue
                try:
                    self._transition(room)
                    self._retry_after.pop(channel, None)
                except Exception:
                    self._retry_after[channel] = time.monotonic() + 60
                    _LOG.exception("Remote guest mix for channel %s failed recovery", channel)

    def recover(self) -> None:
        """Rebuild active compositions after a station-service restart."""
        for room in self._contribution_store.list_rooms():
            if room.state != "live" or room.channel_id in self._runs:
                continue
            if self._sessions(room, excluding=None, mute_override=None):
                self._transition(room)

    def close(self) -> None:
        with self._lock:
            for run in self._runs.values():
                run.handle.terminate()
            self._runs.clear()
