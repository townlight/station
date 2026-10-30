# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Source-plan providers for channel egress."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import os
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from civiccast.egress.env_vars import resolve_renamed_env
from civiccast.egress.errors import SourcePrepareError
from civiccast.egress.models import (
    MAX_PLAYLIST_SUBCHAINS,
    EgressConfig,
    EgressSourcePlan,
    EgressSourceSegment,
)
from civiccast.egress.runtime import FfmpegRunner
from civiccast.schedule.models import (
    ASSET_STATE_RECORDED,
    ASSET_STATE_VALIDATED,
    SCHEDULE_MODE_PREMIERE,
    SCHEDULE_STATE_PUBLISHED,
    ScheduleItemResponse,
    StaffAssetRow,
)
from civiccast.stream._ffmpeg import probe_media_duration_seconds, run_ffmpeg

_LOG = logging.getLogger(__name__)

AssetResolver = Callable[[str], StaffAssetRow | None]
ScheduleItemsProvider = Callable[[str], Sequence[ScheduleItemResponse]]
#: U36 (2026-09-25): the media's OWN playable length, in seconds, from its
#: path -- or None when it cannot be measured. The production resolver is
#: ``_default_media_duration_resolver`` below (a memoized ffprobe).
MediaDurationResolver = Callable[[Path], float | None]
_PLAYABLE_ASSET_STATES = {ASSET_STATE_VALIDATED, ASSET_STATE_RECORDED}

#: D45 fix (2026-09-05): D43 (#170) set this to 1800.0 to lengthen a
#: schedule-derived plan for the sake of the ROLLOVER cadence (see
#: ``automation._check_plan_rollover``'s D43 comments). Real-hardware soak
#: evidence (3 GStreamer channels, a schedule of 30-second items
#: back-to-back) then measured the actual cost of that: chasing 1800s of
#: planned duration out of 30-second slots builds ~60 segments per plan, and
#: ``bridge.graph_from_config`` builds ONE filesrc->decodebin->videoconvert->
#: videoscale->videorate sub-chain PER segment in a SINGLE pipeline that is
#: set to PLAYING all at once (``engine._build_playlist``) -- avdec_h264 at
#: its default max-threads=0 spins up ~20 threads per sub-chain, so 60
#: sub-chains produced ~1200 threads and ~3.5 GB on one worker, with no TS
#: buffer landing inside the 10s stall watchdog
#: (``engine.py``'s ``CTRL stall: no output for 10s``). Every worker
#: relaunched roughly every 30s. The pipeline SHAPE (segment count) has to
#: be bounded on its own terms -- CPU-only stations building 1200 decoder
#: threads is unsafe regardless of how "correct" the resulting wall-clock
#: window is -- so this is back to 0.0 (no duration floor at all;
#: ``max_segments`` alone decides how many sub-chains a plan gets). The
#: rollover-cadence problem D43 was actually solving is fixed at its own
#: layer instead: ``ChannelAutomationService._rollover_min_interval_seconds``
#: (automation.py) now derives its per-channel dispatch floor from the
#: ON-AIR plan's own duration rather than a fixed 300s, so a short plan still
#: gets rolled over comfortably inside its own lifetime.
PLAN_MIN_SECONDS = 0.0
SLATE_RENDER_VERSION = 1
# A single GStreamer decoder chain can safely carry a long bounded slice. Keep
# the slice at 30 minutes so ordinary long-form programming does not force a
# reload every minute; the caller still uses ``max_segments=1`` so this does
# not create a large multi-chain pipeline.
DEFAULT_GSTREAMER_SOURCE_SEGMENT_SECONDS = 1800.0

#: U26 (2026-09-25): the size of schedule gap this provider will absorb.
#:
#: The gap this covers is a HOLE, not a short item: an instant that falls after
#: one item's window closed and before the next one's opened resolves to
#: nothing, and the rollover then fills it with filler -- the channel leaves its
#: program for a slate epoch, and the switch back runs through FALLBACK_SLATE's
#: immediate-reload path (daemon F3(b): worker exit, relay replacement).
#: Resolving the boundary straight to the item due within this window removes
#: the slate epoch entirely: the deferred switch is then program -> program,
#: in-worker, with no exit. Proven at this provider's boundary by probe
#: (reports/U26.md item 3): two items 0.5s apart resolve to None at 0.0 and to
#: the next item, 0.5s early, at 30.0.
#:
#: The two archived live incidents (government 02:13 / 02:58 MDT) measured a
#: related shortfall at a different layer, and this constant is not what
#: repairs them: the channel left its program with 11.0s of the CLOSING item's
#: own recorded plan end still to run (02:58:30.392844 - 02:58:19.385; ~9.6s at
#: 02:13), and the boundary provider answered
#: with that still-open item -- no filler plan and no ``target=filler`` rollover
#: appears in either log. What removed their second restart is the daemon-side
#: floor below (``daemon._SCHEDULE_TAIL_FLOOR_SECONDS``), which re-resolves 1s
#: past the closing item's plan end. See reports/U26.md items 1-2.
#:
#: ``30.0`` matches ``daemon._SCHEDULE_TAIL_FLOOR_SECONDS``, which refuses to
#: build a separate leg for a sub-30s remainder at all -- below that size a
#: slate epoch plus an exit/restart costs the station more than starting the
#: next item early by the gap. The two constants are complementary, not
#: duplicates: this one acts at plan BUILD time, before the channel ever leaves
#: its program, and only when the plan has nothing to air; the daemon floor
#: acts at RELOAD time for a plan resolved at the wall clock (slate replan,
#: operator reload), where only the daemon knows the channel is already on
#: slate and no live item can be cut short. A real gap (beyond this window)
#: keeps the old behavior: filler, then F3(b)'s restart detour for the cut.
SCHEDULE_GAP_ABSORB_SECONDS = 30.0

#: BETA.10 U03: the spelling the station's service registry actually sets (the
#: ``Environment`` REG_MULTI_SZ under
#: ``HKLM\SYSTEM\CurrentControlSet\Services\CivicCastSupervisor``, written by
#: the native installer). This reader used one C, so the registry's ``=1800``
#: never reached it. The one-C name below stays readable as a legacy fallback
#: -- see ``civiccast.egress.env_vars.resolve_renamed_env``.
GSTREAMER_SOURCE_SEGMENT_SECONDS_ENV = "CIVICCAST_GSTREAMER_SOURCE_SEGMENT_SECONDS"
LEGACY_GSTREAMER_SOURCE_SEGMENT_SECONDS_ENV = "CIVICAST_GSTREAMER_SOURCE_SEGMENT_SECONDS"

#: One-time-warning latch for the legacy/conflict messages -- see
#: ``env_vars.resolve_renamed_env``'s ``warned`` parameter.
_RENAMED_ENV_WARNED: set[str] = set()


def gstreamer_source_segment_seconds_from_env() -> float:
    """Return the bounded GStreamer preparation horizon.

    The GStreamer path pre-conforms schedule-derived media when the source
    cannot be trimmed at playout. A multi-hour schedule item must not become
    one multi-hour conform job before a channel can air, so the default plan
    horizon is bounded at 30 minutes. The caller's one-segment pipeline shape
    keeps this from multiplying decoder chains, while the longer slice avoids
    turning normal operation into a minute-by-minute reload stress test.
    Operators may tune the horizon for their encoder hardware, but non-positive
    or malformed values fall back to the safe default rather than disabling the
    bound.

    BETA.10 U03: reads ``GSTREAMER_SOURCE_SEGMENT_SECONDS_ENV``, falling back
    to the legacy ``LEGACY_GSTREAMER_SOURCE_SEGMENT_SECONDS_ENV`` spelling --
    see ``env_vars``. The station's service registry sets the two-C name, which
    this reader used to miss entirely.
    """

    resolved = resolve_renamed_env(
        name=GSTREAMER_SOURCE_SEGMENT_SECONDS_ENV,
        legacy_name=LEGACY_GSTREAMER_SOURCE_SEGMENT_SECONDS_ENV,
        logger=_LOG,
        warned=_RENAMED_ENV_WARNED,
    )
    if resolved is None:
        return DEFAULT_GSTREAMER_SOURCE_SEGMENT_SECONDS
    env_name, raw = resolved
    try:
        value = float(raw)
    except ValueError:
        _LOG.warning(
            "Invalid %s=%r; using %.1fs.",
            env_name,
            raw,
            DEFAULT_GSTREAMER_SOURCE_SEGMENT_SECONDS,
        )
        return DEFAULT_GSTREAMER_SOURCE_SEGMENT_SECONDS
    if value <= 0:
        _LOG.warning(
            "%s must be positive; using %.1fs.",
            env_name,
            DEFAULT_GSTREAMER_SOURCE_SEGMENT_SECONDS,
        )
        return DEFAULT_GSTREAMER_SOURCE_SEGMENT_SECONDS
    return value


def schedule_loop_enabled_from_env() -> bool:
    """Return whether the finite published schedule should repeat cyclically.

    The default remains the existing finite schedule semantics.  A station or
    controlled soak opts in explicitly with ``CIVICCAST_SCHEDULE_LOOP=1`` (or
    ``true``, ``yes``, or ``on``); this avoids silently changing a real
    operator's end-of-schedule policy.
    """

    return os.environ.get("CIVICCAST_SCHEDULE_LOOP", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class SlateSourceGenerator:
    """Generate a pre-conformed MPEG-TS slate source for an egress channel."""

    def __init__(
        self,
        *,
        work_dir: Path,
        duration_seconds: int = 30,
        ffmpeg_runner: FfmpegRunner = run_ffmpeg,
        target_fill_seconds: int = 3600,
    ) -> None:
        self._work_dir = work_dir
        self._duration_seconds = duration_seconds
        self._ffmpeg_runner = ffmpeg_runner
        # CA-8 finding: a single-segment plan relaunched the encoder (and
        # reset the TS session) every `duration_seconds` during slate
        # periods — a headend monitor logs CC errors at every reset. The
        # fix for that was a longer horizon, not a longer pipeline: U27
        # made it ONE pre-conformed file covering the whole horizon (see
        # `_render_fill`), which keeps a single decoder chain and still
        # never relaunches the encoder inside the horizon. A due scheduled
        # item still replaces slate through the normal fallback replan path.
        self._target_fill_seconds = target_fill_seconds

    def __call__(self, config: EgressConfig) -> EgressSourcePlan:
        slate_dir = self._work_dir / config.channel_id
        slate_dir.mkdir(parents=True, exist_ok=True)
        cache_key = self._cache_key(config, self._duration_seconds)
        output_path = slate_dir / f"slate-{cache_key}.ts"
        if not output_path.exists() or output_path.stat().st_size == 0:
            staging = slate_dir / f".{cache_key}.{uuid.uuid4().hex}.partial.ts"
            try:
                args = build_slate_source_args(
                    output_path=staging,
                    config=config,
                    duration_seconds=self._duration_seconds,
                )
                result = self._ffmpeg_runner(args)
                if result.returncode != 0:
                    args = build_slate_source_args(
                        output_path=staging,
                        config=config,
                        duration_seconds=self._duration_seconds,
                        include_text=False,
                    )
                    result = self._ffmpeg_runner(args)
                if result.returncode != 0:
                    raise SourcePrepareError(
                        "Could not generate the egress slate source; inspect FFmpeg output before retrying."
                    )
                staging.replace(output_path)
            finally:
                with contextlib.suppress(OSError):
                    staging.unlink(missing_ok=True)
        repeats = max(1, -(-self._target_fill_seconds // self._duration_seconds))
        fill_seconds = repeats * self._duration_seconds
        segment = EgressSourceSegment(
            label="CivicCast slate",
            path=str(
                output_path
                if repeats == 1
                else self._render_fill(
                    source=output_path,
                    cache_key=cache_key,
                    fill_seconds=fill_seconds,
                )
            ),
            duration_seconds=fill_seconds,
            kind="slate",
            source_ref="civiccast-slate",
        )
        return EgressSourcePlan(
            channel_id=config.channel_id,
            segments=[segment],
        )

    def _render_fill(self, *, source: Path, cache_key: str, fill_seconds: int) -> Path:
        """Concatenate the rendered slate into the ONE file the slate leg plays.

        U27 finding (reproduced off-live through the relay's own argv): a plan
        of N repeats of one finite file makes the bridge build N decoder
        sub-chains feeding the two concat aggregators. On the live caption
        shape the hand-off between sub-chains withholds the outgoing chain's
        video branch while audio keeps flowing, so the mux hands the relay
        about one slate duration of audio with no video; the relay's
        ``-fflags +genpts`` restarts its timeline at that discontinuity and
        every 2s output segment afterwards carries ~20s of a/v offset until
        the channel is restarted. Stream-copying the same pre-conformed slate
        into a single file leaves one decoder chain -- nothing to hand off,
        no 12-subchain teardown -- and one continuous timeline with no
        per-seam PTS reset.

        Deliberately does NOT fall back to the multi-segment plan on failure:
        that plan is the defect. A failed fill is a ``SourcePrepareError``,
        exactly like a failed slate render.
        """

        fill_dir = source.parent
        fill_key = self._fill_cache_key(cache_key, fill_seconds, source)
        fill_path = fill_dir / f"slate-fill-{fill_key}.ts"
        if fill_path.exists() and fill_path.stat().st_size > 0:
            return fill_path
        manifest = fill_dir / f".{fill_key}.{uuid.uuid4().hex}.concat.txt"
        staging = fill_dir / f".{fill_key}.{uuid.uuid4().hex}.partial.ts"
        try:
            manifest.write_text(
                "".join(
                    f"file '{_escape_concat_path(str(source))}'\n"
                    for _ in range(fill_seconds // self._duration_seconds)
                ),
                encoding="utf-8",
            )
            result = self._ffmpeg_runner(concat_copy_args(manifest=manifest, destination=staging))
            if result.returncode != 0:
                raise SourcePrepareError(
                    "Could not assemble the egress slate fill file; inspect FFmpeg output before retrying."
                )
            staging.replace(fill_path)
        finally:
            with contextlib.suppress(OSError):
                manifest.unlink(missing_ok=True)
                staging.unlink(missing_ok=True)
        return fill_path

    @staticmethod
    def _fill_cache_key(cache_key: str, fill_seconds: int, source: Path) -> str:
        digest = hashlib.sha256()
        for part in (cache_key, str(fill_seconds), str(source.stat().st_size)):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()[:24]

    @staticmethod
    def _cache_key(config: EgressConfig, duration_seconds: int) -> str:
        digest = hashlib.sha256()
        for part in (
            str(SLATE_RENDER_VERSION),
            str(duration_seconds),
            config.slate_message,
            config.canonical_profile.model_dump_json(),
        ):
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()[:24]


def _default_media_duration_resolver() -> MediaDurationResolver:
    """The production :data:`MediaDurationResolver`: a memoized media probe.

    U36 (2026-09-25). The planner used to take an item's playable length
    entirely from the database rows (``asset.duration_seconds`` and the trim
    window), so a row that overstated the media by a few seconds let a plan end
    past the file's real end -- see ``_playable_duration``. Measuring the file
    is the only way to know.

    Memoized on ``(path, mtime_ns, size)`` because the automation loop builds a
    plan every ~2 seconds and the media lives on the station's storage (often a
    NAS): one ffprobe per file VERSION, re-probed when the file is replaced,
    never a blocking probe per poll. A changed ``st_mtime_ns``/``st_size`` is
    the cache key rather than the file's identity so a re-uploaded asset of the
    same length is still re-measured.

    Returns None (fail-open: the rows stay authoritative, exactly as before
    this change) when the file cannot be measured -- see
    ``_segment_from_item``.
    """

    cache: dict[tuple[str, int, int], float | None] = {}

    def resolve(path: Path) -> float | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if key not in cache:
            cache[key] = probe_media_duration_seconds(path)
        return cache[key]

    return resolve


class ScheduleSourcePlanProvider:
    """Build concrete egress source plans from scheduled local media."""

    def __init__(
        self,
        *,
        schedule_items_provider: ScheduleItemsProvider,
        asset_resolver: AssetResolver,
        now_provider: Callable[[], datetime] | None = None,
        max_segments: int = 8,
        min_plan_seconds: float = PLAN_MIN_SECONDS,
        segment_cap: int = MAX_PLAYLIST_SUBCHAINS,
        gap_tolerance_seconds: float = 1.0,
        loop_schedule: bool = False,
        max_segment_seconds: float | None = None,
        gap_absorb_seconds: float = 0.0,
        media_duration_resolver: MediaDurationResolver | None = None,
    ) -> None:
        if max_segments <= 0:
            raise ValueError("max_segments must be greater than zero.")
        if gap_tolerance_seconds < 0:
            raise ValueError("gap_tolerance_seconds must be zero or greater.")
        if max_segment_seconds is not None and max_segment_seconds <= 0:
            raise ValueError("max_segment_seconds must be greater than zero when set.")
        if gap_absorb_seconds < 0:
            raise ValueError("gap_absorb_seconds must be zero or greater.")
        self._schedule_items_provider = schedule_items_provider
        self._asset_resolver = asset_resolver
        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._max_segments = max_segments
        self._min_plan_seconds = min_plan_seconds
        self._segment_cap = segment_cap
        self._gap_tolerance = timedelta(seconds=gap_tolerance_seconds)
        self._loop_schedule = loop_schedule
        self._max_segment_seconds = max_segment_seconds
        self._gap_absorb_seconds = gap_absorb_seconds
        # U36: the provider is where the media probe is wired in, because it is
        # the shape both production sites construct (automation.py:2628,
        # cli.py:1150) -- and it is the SAME provider instance that serves the
        # rollover-time plan and the relaunch plan, so both get truthful
        # durations. The bare ``build_source_plan_from_schedule`` stays
        # row-only by default so its own callers remain deterministic.
        self._media_duration_resolver = (
            media_duration_resolver
            if media_duration_resolver is not None
            else _default_media_duration_resolver()
        )

    def __call__(self, channel_id: str) -> EgressSourcePlan | None:
        # Ordinary starts and slate polling must wait for the published start.
        # Absorbing a future item here airs it early, then the next boundary
        # resolves that same occurrence again and replays it.
        return self.plan_at(channel_id, self._now_provider(), gap_absorb_seconds=0.0)

    def plan_at(
        self,
        channel_id: str,
        boundary_at: datetime,
        *,
        min_plan_seconds: float | None = None,
        gap_absorb_seconds: float | None = None,
    ) -> EgressSourcePlan | None:
        """Build the plan active at an explicit scheduled boundary.

        Automation uses this separate entry point to prepare the item due at a
        future rollover boundary. Ordinary starts continue through ``__call__``
        and therefore retain wall-clock join-in-progress behavior.

        The configured ``gap_absorb_seconds`` applies by default to this explicit
        boundary; ordinary ``__call__`` resolution overrides it to zero.
        With ``gap_absorb_seconds`` set (U26), a boundary that lands in a small
        gap between two items -- or a few seconds inside the next item's slot,
        when the published rows overlap (U52) -- resolves to that item rather
        than to None/filler: see ``build_source_plan_from_schedule``.

        ``min_plan_seconds`` (U41) overrides the provider's own horizon for this
        call only. A deferred rollover is prepared at dispatch but takes air at
        the outgoing item's own end, so the daemon measures the horizon it wants
        from the SWITCH -- the plan must still have that much life left when it
        finally goes on air, not when it was built. The plan still ends on item
        boundaries: the horizon widens it with whole following items, it never
        splits one.
        """

        return build_source_plan_from_schedule(
            channel_id=channel_id,
            schedule_items=self._schedule_items_provider(channel_id),
            asset_resolver=self._asset_resolver,
            now=boundary_at,
            max_segments=self._max_segments,
            min_plan_seconds=(
                self._min_plan_seconds if min_plan_seconds is None else min_plan_seconds
            ),
            segment_cap=self._segment_cap,
            gap_tolerance=self._gap_tolerance,
            loop_schedule=self._loop_schedule,
            max_segment_seconds=self._max_segment_seconds,
            gap_absorb_seconds=(
                self._gap_absorb_seconds if gap_absorb_seconds is None else gap_absorb_seconds
            ),
            media_duration_resolver=self._media_duration_resolver,
        )

    def next_item_start_at(self, channel_id: str, after: datetime) -> datetime | None:
        """Start instant of the next playable item strictly after ``after``.

        U53 item 1: a rollover whose target is filler needs to know how far
        ahead the next scheduled programme is due, so the filler can be trimmed
        to exactly that gap and the programme chained behind it in the same
        plan. ``plan_at`` cannot answer this -- it answers "what is active at
        this instant", which is None for the whole gap. This is the same
        playable-item set ``plan_at`` plans from, so the two can never
        disagree about which item comes next.
        """

        return next_playable_item_start(
            channel_id=channel_id,
            schedule_items=self._schedule_items_provider(channel_id),
            after=after,
            loop_schedule=self._loop_schedule,
        )


def _playable_items(
    channel_id: str,
    schedule_items: Sequence[ScheduleItemResponse],
    *,
    loop_schedule: bool = False,
    now: datetime | None = None,
) -> list[ScheduleItemResponse]:
    """The channel's airable items, sorted by (start, id) -- and, when looping,
    shifted into the cycle containing ``now``.

    ONE definition of "playable", shared by ``build_source_plan_from_schedule``
    and ``next_playable_item_start``: a plan may never be built from a
    different item set than the "next item" lookup walks (U53 item 1)."""

    items = [
        item
        for item in schedule_items
        if item.channel_id == channel_id
        # Commit-to-Air gate: only ``published`` items are airable -- a
        # ``scheduled`` premiere has not been approved to air yet (via
        # commit, or auto-approval by autoschedule).
        and item.state == SCHEDULE_STATE_PUBLISHED
        and item.mode == SCHEDULE_MODE_PREMIERE
        and item.duration_seconds is not None
    ]
    items.sort(key=lambda item: (item.scheduled_at, str(item.id)))
    if loop_schedule and items:
        items = _repeat_schedule_cycle(items, _as_utc(now or datetime.now(UTC)))
    return items


def next_playable_item_start(
    *,
    channel_id: str,
    schedule_items: Sequence[ScheduleItemResponse],
    after: datetime,
    loop_schedule: bool = False,
) -> datetime | None:
    """The start instant of the first playable item due strictly after ``after``.

    None means "nothing further is scheduled" (or, under ``loop_schedule``, that
    ``after`` falls past the end of the cycle the loop could shift forward):
    the caller must then keep whatever it would have done without a next item.
    """

    after_utc = _as_utc(after)
    for item in _playable_items(
        channel_id, schedule_items, loop_schedule=loop_schedule, now=after_utc
    ):
        starts_at = _as_utc(item.scheduled_at)
        if starts_at > after_utc:
            # Sorted by start: the first one still ahead is the next due.
            return starts_at
    return None


def build_source_plan_from_schedule(
    *,
    channel_id: str,
    schedule_items: Sequence[ScheduleItemResponse],
    asset_resolver: AssetResolver,
    now: datetime | None = None,
    max_segments: int = 8,
    min_plan_seconds: float = PLAN_MIN_SECONDS,
    segment_cap: int = MAX_PLAYLIST_SUBCHAINS,
    gap_tolerance: timedelta = timedelta(seconds=1),
    loop_schedule: bool = False,
    max_segment_seconds: float | None = None,
    gap_absorb_seconds: float = 0.0,
    media_duration_resolver: MediaDurationResolver | None = None,
) -> EgressSourcePlan | None:
    """Return the currently playable egress source plan, or None for slate fallback.

    The window is bounded by COUNT first (``max_segments``, 8 by default) --
    each segment becomes its own decoder sub-chain in the egress pipeline
    (``bridge.graph_from_config``), so the segment count IS the pipeline's
    shape, not just a wall-clock convenience. ``min_plan_seconds`` (D45: 0.0
    by default) is an OPTIONAL additional duration floor for a caller that
    explicitly wants a longer window and can bear a bigger pipeline; segments
    are appended until there are at least ``max_segments`` of them AND the
    plan spans at least ``min_plan_seconds``, whichever condition is
    satisfied later. ``segment_cap`` is the hard upper bound on the segment
    count regardless of either.

    Each segment is clipped to its SCHEDULE SLOT (D42): the slot is the
    contract, so long media in a 30-second slot plays for 30 seconds, not
    for its full asset length. When the media is SHORTER than its slot the
    plan stops at that item: the remainder of the slot belongs to the
    channel's fill policy (``bulletin_filler.FillerSourceProvider`` --
    bulletins or slate), reached through the daemon's FALLBACK_SLATE
    gap-replan path, exactly as an already-aired current item does below.
    Nothing here loops or stretches media to cover a slot, and no following
    item is ever started early -- the one exception is ``gap_absorb_seconds``
    below, which only applies when there is nothing at all to air.

    ``gap_absorb_seconds`` (U26, 2026-09-25; off by default) absorbs a small
    schedule gap. When the instant this plan is built for has NOTHING to air --
    it falls between two items, or the item covering it has already aired all
    of its media -- and the next item is due within this many seconds of it, the
    plan is built from that next item instead: the caller starts it at this
    instant, early by at most the gap. It exists because automation's own
    rollover resolves a boundary in such a gap to ``None``, rolls the channel
    onto filler, and must then take F3(b)'s exit-and-restart detour to get back
    to a program; absorbing the gap keeps the channel on its program and makes
    the switch an ordinary deferred in-worker reload. The window is two-sided
    (U52, 2026-09-26): published rows can OVERLAP, so the boundary taken at the
    closing item's plan end lands a few seconds INSIDE the next item's slot, and
    that just-started item is the boundary's program -- the one-sided form
    answered filler there and produced 3m48s of slate before an exit-and-restart
    (reports/U52.md item 1). A gap LARGER than this window still returns None
    (filler is the honest answer there), a started item whose own slot has
    closed is never resurrected, and a plan that has anything to air is never
    replaced: a join-in-progress resume of a live item is content, not a stub,
    so it is never cut short to start the next item early.
    ``SCHEDULE_GAP_ABSORB_SECONDS`` is the value the station wires in.

    ``max_segment_seconds`` is an optional preparation horizon.  The
    GStreamer path uses it because that engine must pre-conform a source that
    cannot be trimmed in place; bounding the current segment lets a channel
    reach air quickly and lets the existing rollover machinery prepare the
    next join-in-progress slice while the current slice is playing.  It does
    not change the schedule's wall-clock position or the media trim window.

    ``media_duration_resolver`` (U36, 2026-09-25) measures the media FILE, and
    every segment is capped at what the file really has. Until this change the
    plan's durations came only from the database rows (``item.duration_seconds``
    -- the slot -- and ``asset.duration_seconds``/the trim window), so a row
    that overstated the media made the plan end past the file's real end. On
    the station's engine every plan is one leg (``max_segments=1``), so the
    leg's true EOS ended the pipeline: the worker exited 0 while ON_AIR and the
    relaunch re-resolved the same overstated clock into a sub-2-second sliver
    of a program that had already finished. With the media measured, such an
    item is recognised as exhausted like any other, so the U26 gap-absorb below
    hands the boundary to the next program (when it is due within the window)
    instead of re-airing a tail.

    Left ``None`` (the default) the builder keeps its row-only behaviour, which
    is what every direct caller wants: their fixtures are not real media, and a
    probe per plan build would make them non-deterministic. ``None`` from a
    resolver means the file could not be measured, and the rows stay
    authoritative -- fail-open, so a station without ffprobe is unaffected.
    """

    if max_segments <= 0:
        raise ValueError("max_segments must be greater than zero.")
    if min_plan_seconds < 0:
        raise ValueError("min_plan_seconds must be zero or greater.")
    if gap_tolerance < timedelta(0):
        raise ValueError("gap_tolerance must be zero or greater.")
    if max_segment_seconds is not None and max_segment_seconds <= 0:
        raise ValueError("max_segment_seconds must be greater than zero when set.")
    if gap_absorb_seconds < 0:
        raise ValueError("gap_absorb_seconds must be zero or greater.")
    # Hostile-review fix (2026-09-05): validate the CALLER's raw pair first,
    # before either is touched by the pipeline-shape clamp below. A caller
    # that explicitly asks for an inconsistent pair (e.g. max_segments=20,
    # segment_cap=15) is asking for something that was never sensible on its
    # own terms -- that is a caller bug to surface loudly, not something the
    # clamp below should silently paper over by coincidentally shrinking
    # both values into agreement (20/15 clamped to 12/12 would look "fixed"
    # while hiding that the caller's own request was self-contradictory).
    if segment_cap < max_segments:
        raise ValueError("segment_cap must be at least max_segments.")
    # The cap that bounds the pipeline SHAPE has to live here, at the plan's
    # only producer, not in ``bridge.graph_from_config`` alone. A
    # caller-side ``max_segments``/``segment_cap`` above
    # ``MAX_PLAYLIST_SUBCHAINS`` used to build a plan every OTHER consumer
    # (``automation.py``'s rollover-horizon tracking, ``daemon.py``'s
    # dispatched-plan bookkeeping, ``continuity.py``, ``preparer.py``)
    # trusted at its full, uncapped size, while the bridge silently played
    # only the first ``MAX_PLAYLIST_SUBCHAINS`` of it -- the pipeline would
    # reach EOS long before automation's tracked horizon expected it to,
    # restarting the worker on a cadence the rest of the system had no idea
    # was coming. Clamping the inputs here instead means every consumer
    # reads the SAME plan the pipeline will actually play; a plan returned
    # by this function can never disagree with what ``graph_from_config``
    # builds from it. This runs AFTER the raw-pair validation above: a
    # caller whose own numbers already agree with each other (e.g. 20/20)
    # gets both quietly clamped down together, in step, rather than raising
    # over a cap the caller had no way to know about at the call site.
    if max_segments > MAX_PLAYLIST_SUBCHAINS:
        _LOG.warning(
            "build_source_plan_from_schedule(%s): max_segments=%d exceeds "
            "MAX_PLAYLIST_SUBCHAINS=%d (the hard per-pipeline decoder-chain cap); "
            "clamping to %d.",
            channel_id,
            max_segments,
            MAX_PLAYLIST_SUBCHAINS,
            MAX_PLAYLIST_SUBCHAINS,
        )
        max_segments = MAX_PLAYLIST_SUBCHAINS
    if segment_cap > MAX_PLAYLIST_SUBCHAINS:
        _LOG.warning(
            "build_source_plan_from_schedule(%s): segment_cap=%d exceeds "
            "MAX_PLAYLIST_SUBCHAINS=%d (the hard per-pipeline decoder-chain cap); "
            "clamping to %d.",
            channel_id,
            segment_cap,
            MAX_PLAYLIST_SUBCHAINS,
            MAX_PLAYLIST_SUBCHAINS,
        )
        segment_cap = MAX_PLAYLIST_SUBCHAINS
    current_time = _as_utc(now or datetime.now(UTC))
    playable_items = _playable_items(
        channel_id, schedule_items, loop_schedule=loop_schedule, now=current_time
    )

    def _plan_from(start_index: int, start_elapsed: float) -> list[EgressSourceSegment]:
        """Build the contiguous plan starting at ``start_index``.

        ``start_elapsed`` is the offset already aired of the item at
        ``start_index`` (CA-2 join-in-progress); it is 0.0 for an item taken at
        its own start by the gap-absorb path below. An empty result means the
        item at ``start_index`` has none of its own media left to air.
        """

        start_item = playable_items[start_index]
        built: list[EgressSourceSegment] = []
        built_seconds = 0.0
        cursor_end = _as_utc(start_item.scheduled_at) + timedelta(
            seconds=start_item.duration_seconds or 0
        )
        for item in playable_items[start_index:]:
            starts_at = _as_utc(item.scheduled_at)
            if built and starts_at > cursor_end + gap_tolerance:
                break
            item_elapsed = start_elapsed if item is start_item else 0.0
            segment = _segment_from_item(
                item,
                asset_resolver,
                elapsed_seconds=item_elapsed,
                max_duration_seconds=max_segment_seconds,
                media_duration_resolver=media_duration_resolver,
            )
            if segment is None:
                # The item's media has fully aired (media shorter than its
                # slot, or a late rejoin past the end). Honest behavior is
                # slate until the next item is due — never an early start.
                # ``_segment_from_item`` answers None either for an item that
                # has already aired part of itself (elapsed > 0) or (U36) for
                # one whose media really ends at or before the point this
                # segment would start; so in practice this is always the FIRST
                # iteration and ``built`` is empty, and the gap-absorb below
                # gets its chance to start the next program. A later item's
                # media is resolved whole, and a broken or missing one raises
                # SourcePrepareError instead.
                break
            built.append(segment)
            built_seconds += segment.duration_seconds
            cursor_end = max(
                cursor_end,
                starts_at + timedelta(seconds=item.duration_seconds or segment.duration_seconds),
            )
            if len(built) >= segment_cap:
                break
            if not _covers_slot(
                item,
                segment,
                elapsed_seconds=item_elapsed,
                tolerance_seconds=gap_tolerance.total_seconds(),
            ):
                # D42: this item's media runs out before its slot does. The
                # rest of the slot is the fill policy's (bulletins/slate) —
                # stop the plan here rather than starting the NEXT item early,
                # the same honest answer the fully-aired branch above gives.
                break
            if len(built) >= max_segments and built_seconds >= min_plan_seconds:
                break
        return built

    current_index = _current_item_index(playable_items, current_time)
    segments: list[EgressSourceSegment] = []
    if current_index is not None:
        current_item = playable_items[current_index]
        # CA-2 join-in-progress: a (re)start mid-program resumes the CURRENT
        # item at the wall-clock offset so the channel stays on its published
        # log instead of replaying the program from the top.
        elapsed_seconds = max(
            0.0, (current_time - _as_utc(current_item.scheduled_at)).total_seconds()
        )
        segments = _plan_from(current_index, elapsed_seconds)

    # U26 gap absorb: when there is nothing to air at this instant -- it falls
    # in the gap between two scheduled items, or the item covering it has
    # already aired all of its media -- and the next item is due within
    # ``gap_absorb_seconds`` of this instant, take that item now instead of
    # going to slate for the gap. The caller starts it at this instant, early
    # by at most the gap; automation's rollover then prepares a PROGRAM for the
    # boundary rather than filler, so the channel never leaves its program and
    # its deferred switch stays an ordinary in-worker reload. The window is
    # two-sided (U52): overlapping published rows mean the boundary can land a
    # few seconds INSIDE the next item's slot, and the item that just started
    # is the boundary's program -- see ``_gap_absorb_candidate_index``. A real
    # gap (beyond the window) still returns None: filler is the honest answer
    # there.
    absorb_index = _gap_absorb_candidate_index(
        playable_items,
        current_time,
        current_index=current_index,
        gap_absorb_seconds=gap_absorb_seconds,
    )
    if absorb_index is not None and not segments:
        absorbed = _plan_from(absorb_index, 0.0)
        if absorbed:
            segments = absorbed

    if not segments:
        return None
    return EgressSourcePlan(channel_id=channel_id, segments=segments)


def _gap_absorb_candidate_index(
    items: Sequence[ScheduleItemResponse],
    current_time: datetime,
    *,
    current_index: int | None,
    gap_absorb_seconds: float,
) -> int | None:
    """Return the index of the item this instant's boundary belongs to.

    ``items`` is the sorted (and, when looping, repeated) playable list;
    ``current_index`` is ``_current_item_index``'s answer for ``current_time``,
    or None when that instant falls in a gap between two items.

    The candidate is the first item at or after ``current_index`` that is due
    within ``gap_absorb_seconds`` of ``current_time`` -- on EITHER side of it:

    * due slightly AHEAD (the bare-gap case): the caller starts it early by at
      most the window, so automation's rollover prepares a program for the
      boundary instead of filler;
    * started slightly BEHIND, while its own slot is still open (the live
      2026-09-26 education shape, reports/U52.md item 1): published rows
      OVERLAP, so the boundary taken at the closing item's plan end lands a few
      seconds inside the next item's slot. That item IS the boundary's program;
      it is taken whole from its beginning, late by at most the window. The
      one-sided form this replaces skipped exactly that item and answered
      filler for the program's whole preparation window -- 3m48s of slate, then
      the F3(b) exit-and-restart.

    A started item whose own slot has ALREADY closed is never resurrected --
    it should have finished, and airing it now would be a late replay rather
    than the boundary's program. None is also returned when the absorb is
    disabled, when the instant is BEFORE the published log begins (a station
    that came up early waits for its schedule's first item rather than airing
    it early -- there is no ending item whose end it is at), and when the next
    item is further out than ``gap_absorb_seconds``.
    """

    if gap_absorb_seconds <= 0.0:
        return None
    if not any(_as_utc(item.scheduled_at) <= current_time for item in items):
        return None
    start_index = 0 if current_index is None else current_index + 1
    for index in range(start_index, len(items)):
        item = items[index]
        starts_at = _as_utc(item.scheduled_at)
        delta = (starts_at - current_time).total_seconds()
        if delta > gap_absorb_seconds:
            # Sorted by start time: the first item still ahead of
            # ``current_time`` is the next one due, and it is already too far
            # out.
            return None
        if delta < -gap_absorb_seconds:
            # Started longer ago than the window: not this boundary's item.
            continue
        if delta < 0.0:
            ends_at = starts_at + timedelta(seconds=item.duration_seconds or 0)
            if ends_at <= current_time:
                # Its own slot is closed too: a late replay, not a program.
                continue
        return index
    return None


def _contiguous_schedule_run(
    items: Sequence[ScheduleItemResponse], gap_tolerance: timedelta
) -> list[ScheduleItemResponse]:
    """Return the LAST contiguous run of ``items`` (sorted by start).

    A real station's published schedule accumulates in separate contiguous
    passes: a pass, then a multi-day gap, then another pass.  The loop must
    repeat ONE pass, not the whole history - otherwise the cycle boundary
    swallows the inter-pass gaps and a wrapped ``current_time`` lands in a gap
    (no plan, channel to slate).

    "Contiguous" means the next item starts no later than the previous item's
    end plus ``gap_tolerance``; the first break ends the scan and the run after
    the final break is returned.  A schedule with a single pass returns it
    unchanged, so this is a no-op for the simple case.
    """

    ordered = sorted(items, key=lambda item: (_as_utc(item.scheduled_at), str(item.id)))
    start = 0
    for index in range(1, len(ordered)):
        previous = ordered[index - 1]
        previous_end = _as_utc(previous.scheduled_at) + timedelta(
            seconds=previous.duration_seconds or 0
        )
        if _as_utc(ordered[index].scheduled_at) > previous_end + gap_tolerance:
            start = index
    return ordered[start:]


def _repeat_schedule_cycle(
    items: Sequence[ScheduleItemResponse],
    current_time: datetime,
    *,
    gap_tolerance: timedelta = timedelta(seconds=1),
) -> list[ScheduleItemResponse]:
    """Shift a finite schedule into the cycle containing ``current_time``.

    The cycle is ONE contiguous pass of the published schedule -- the LAST such
    pass -- not the entire published history.  Repeated passes of the same
    sequence are joined by multi-day gaps, and anchoring the period on the
    whole history would fold those gaps into the "cycle", dropping a wrapped
    ``current_time`` into dead air.

    Within the chosen pass the first item's start is the cycle anchor and the
    end of the last slot is the cycle boundary; gaps between items inside the
    pass remain gaps (the normal filler/slate policy owns them).  After the
    final slot the same pass starts again.  Item IDs/assets are unchanged so
    the loop is a playout policy, not a second set of schedule records.
    """

    ordered = sorted(items, key=lambda item: (_as_utc(item.scheduled_at), str(item.id)))
    if not ordered:
        return []
    cycle_items = _contiguous_schedule_run(ordered, gap_tolerance)
    anchor = _as_utc(cycle_items[0].scheduled_at)
    cycle_end = max(
        _as_utc(item.scheduled_at) + timedelta(seconds=item.duration_seconds or 0)
        for item in cycle_items
    )
    period_seconds = (cycle_end - anchor).total_seconds()
    if period_seconds <= 0 or current_time < anchor:
        return list(cycle_items)
    cycle_index = math.floor((current_time - anchor).total_seconds() / period_seconds)
    offset = timedelta(seconds=cycle_index * period_seconds)
    return [
        item.model_copy(update={"scheduled_at": _as_utc(item.scheduled_at) + offset})
        for item in cycle_items
    ]


def build_slate_source_args(
    *,
    output_path: Path,
    config: EgressConfig,
    duration_seconds: int,
    include_text: bool = True,
) -> list[str]:
    """Build FFmpeg args for a canonical MPEG-TS slate source."""

    profile = config.canonical_profile
    video_input = (
        f"color=c=0x1a2744:size={profile.width}x{profile.height}"
        f":rate={profile.fps}:duration={duration_seconds}"
    )
    args = [
        "-f",
        "lavfi",
        "-i",
        video_input,
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={profile.audio_sample_rate}:cl=stereo",
    ]
    if include_text:
        args.extend(
            [
                "-vf",
                (
                    f"drawtext=expansion=none:text='{_escape_drawtext(config.slate_message)}':"
                    "fontsize=28:fontcolor=white:box=1:boxcolor=black@0.4:boxborderw=8:"
                    "x=(w-text_w)/2:y=(h-text_h)/2"
                ),
            ]
        )
    args.extend(
        [
            "-c:v",
            profile.video_codec,
            "-b:v",
            f"{profile.video_bitrate_kbps}k",
            "-g",
            str(profile.gop_size),
            "-c:a",
            profile.audio_codec,
            "-b:a",
            f"{profile.audio_bitrate_kbps}k",
            "-ar",
            str(profile.audio_sample_rate),
            "-ac",
            str(profile.audio_channels),
            "-shortest",
            "-f",
            "mpegts",
            str(output_path),
        ]
    )
    return args


def _escape_drawtext(value: str) -> str:
    """Escape a string for safe use inside a single-quoted ffmpeg drawtext value.

    This is the ONE shared escaping implementation for every ``drawtext=`` call
    site in the egress package (source_plan.py, bulletin_filler.py, and
    board_compositor.py all import this rather than keeping their own copy).
    Two independent copies previously existed and had already drifted once in
    call order (gate finding F-3) -- do not add a third; import this instead.

    The backslash MUST be escaped first, before the quote and colon, or a
    literal backslash introduced by a later replacement would itself get
    re-escaped. Every ``drawtext`` build site also passes ``expansion=none``,
    which is a separate, load-bearing control that disables ffmpeg's own
    ``%{...}``-style value expansion inside drawtext text (gate finding F-1 --
    without it, community-submitted text could reach ffmpeg's expansion
    syntax). Escaping here and ``expansion=none`` at the call site are both
    required; neither substitutes for the other.
    """
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _escape_concat_path(path: str) -> str:
    """Quote one absolute path for FFmpeg's concat demuxer manifest.

    This is the ONE shared implementation for every ``-f concat`` manifest in
    the egress package (``SlateSourceGenerator`` and
    ``BulletinFillerSourceGenerator`` both use it) -- do not add a second copy.

    A backslash inside a single-quoted ffconcat token is literal. Close the
    quote, escape the apostrophe outside it, then reopen the quote.
    """

    return Path(path).resolve().as_posix().replace("'", "'\\''")


def concat_copy_args(*, manifest: Path, destination: Path) -> list[str]:
    """FFmpeg args that concatenate a manifest's files into one continuous file.

    The sources are already-conformed MPEG-TS, so this is a stream copy: no
    re-encode, no generation loss, and the demuxer's output timeline is
    continuous across every seam (measured: 12 x 30s slate copies -> one
    360.20s video span / 360.23s audio span, 0 non-monotonic PTS pairs).
    """

    return ["-f", "concat", "-safe", "0", "-i", str(manifest), "-c", "copy", str(destination)]


def _current_item_index(
    items: Sequence[ScheduleItemResponse],
    current_time: datetime,
) -> int | None:
    for index, item in enumerate(items):
        starts_at = _as_utc(item.scheduled_at)
        assert item.duration_seconds is not None
        ends_at = starts_at + timedelta(seconds=item.duration_seconds)
        if starts_at <= current_time < ends_at:
            return index
    return None


def _segment_from_item(
    item: ScheduleItemResponse,
    asset_resolver: AssetResolver,
    *,
    elapsed_seconds: float = 0.0,
    max_duration_seconds: float | None = None,
    media_duration_resolver: MediaDurationResolver | None = None,
) -> EgressSourceSegment | None:
    """Build the segment for one scheduled item.

    ``elapsed_seconds`` > 0 means a join-in-progress rejoin of the current
    item: playback resumes that far into the (trimmed) media. Returns None
    when the elapsed time exceeds the playable media — the item has fully
    aired and contributes nothing (the caller falls back to slate).

    ``media_duration_resolver`` (U36) measures the media file itself; the
    item's playable length is then capped by it as well as by the slot and the
    trim window, so a segment never runs past the media's real end. Its
    ``None`` answer (unreadable media, no ffprobe) leaves the rows
    authoritative, exactly as before this parameter existed.
    """

    asset = asset_resolver(item.asset_id)
    if asset is None:
        raise SourcePrepareError(
            f"Scheduled asset {item.asset_id!r} is not in the local asset library."
        )
    if asset.state not in _PLAYABLE_ASSET_STATES:
        raise SourcePrepareError(
            f"Scheduled asset {item.asset_id!r} is {asset.state!r}, not ready for egress."
        )
    if not asset.file_path:
        raise SourcePrepareError(
            f"Scheduled asset {item.asset_id!r} has no local media file path for egress."
        )
    media_path = Path(asset.file_path).expanduser()
    if not media_path.exists() or not media_path.is_file():
        raise SourcePrepareError(
            f"Scheduled asset {item.asset_id!r} local media file is missing: {media_path}."
        )
    inpoint = asset.trim_in_seconds
    outpoint = asset.trim_out_seconds
    media_end = media_duration_resolver(media_path) if media_duration_resolver is not None else None
    playable_duration = _playable_duration(
        asset,
        inpoint=asset.trim_in_seconds,
        outpoint=asset.trim_out_seconds,
        media_end=media_end,
    )
    duration = _segment_duration(
        item, asset, inpoint=inpoint, outpoint=outpoint, media_end=media_end
    )
    if duration <= 0:
        if media_end is not None and media_end <= (inpoint or 0.0):
            # U36 (2026-09-25): the rows say this item has media left, but the
            # media itself ends at or before the point this segment would start
            # from — there is nothing left of this item to air. That is an
            # EXHAUSTED item (the caller advances to the next one / falls back),
            # not a misconfigured trim window: the live defect was a 2-second
            # sliver of a program whose file had already ended.
            return None
        raise SourcePrepareError(
            f"Scheduled asset {item.asset_id!r} has an invalid trim window for egress."
        )
    if elapsed_seconds > 0:
        if elapsed_seconds >= duration:
            return None
        inpoint = (inpoint or 0.0) + elapsed_seconds
        duration = duration - elapsed_seconds
    if max_duration_seconds is not None:
        duration = min(duration, max_duration_seconds)
    if outpoint is not None:
        # D42: ``duration`` may now be the SLOT rather than the whole trim
        # window, so the out-point has to follow it — a stale outpoint would
        # let a trim-aware consumer (preparer._emit_prepared_from_cache with
        # ``playout_trim_supported``, build_conform_source_args) emit more
        # media than the slot actually bought.
        outpoint = min(outpoint, (inpoint or 0.0) + duration)
    return EgressSourceSegment(
        label=asset.title or item.asset_title or item.asset_id,
        path=str(media_path),
        duration_seconds=duration,
        playable_duration_seconds=(
            playable_duration if playable_duration is not None and playable_duration > 0 else None
        ),
        kind="program",
        source_ref=item.asset_id,
        schedule_occurrence_id=str(item.id),
        inpoint_seconds=inpoint,
        outpoint_seconds=outpoint,
    )


def _covers_slot(
    item: ScheduleItemResponse,
    segment: EgressSourceSegment,
    *,
    elapsed_seconds: float,
    tolerance_seconds: float,
) -> bool:
    """Whether this item's media fills its whole scheduled slot.

    ``elapsed_seconds`` is the join-in-progress offset already consumed from
    the slot, so the comparison is against the slot as a whole, not the
    remaining part of it. ``tolerance_seconds`` (the caller's gap tolerance,
    1s by default) keeps a 29.97s asset in a 30s slot from being treated as
    an under-fill on every single plan.
    """

    if item.duration_seconds is None:
        return True
    slot_seconds = float(item.duration_seconds)
    aired_seconds = elapsed_seconds + segment.duration_seconds
    return aired_seconds + tolerance_seconds >= slot_seconds


def _segment_duration(
    item: ScheduleItemResponse,
    asset: StaffAssetRow,
    *,
    inpoint: float | None,
    outpoint: float | None,
    media_end: float | None = None,
) -> float:
    """The airtime this item contributes: its SLOT, capped by playable media.

    D42 (real-hardware soak, 2026-09-05): this used to return the asset's own
    playable length and ignore ``item.duration_seconds`` entirely, so a
    30-second schedule slot holding an hour-long recording aired for the
    whole hour — the published schedule was not honoured at all — while a
    schedule of short assets built a plan far shorter than its own slots.
    The slot is the contract; the media can only ever cut it short.

    ``media_end`` (U36) is the media's real length from the file itself; it
    caps the playable length, so D42's "the media can only ever cut the slot
    short" is finally measured against the media rather than against a row
    that can be wrong.
    """

    playable = _playable_duration(asset, inpoint=inpoint, outpoint=outpoint, media_end=media_end)
    if item.duration_seconds is None:
        # No slot on record (should not happen: the caller filters these out)
        # — fall back to whatever the media offers.
        return playable if playable is not None else 0.0
    slot = float(item.duration_seconds)
    if playable is None:
        # Un-probed asset with no trim window: the slot is the only number
        # available, exactly as before this change.
        return slot
    return min(slot, playable)


def _playable_duration(
    asset: StaffAssetRow,
    *,
    inpoint: float | None,
    outpoint: float | None,
    media_end: float | None = None,
) -> float | None:
    """Seconds of media playable from ``inpoint``, or None when unknowable.

    A non-positive result (an inverted trim window) is returned as-is so the
    caller raises the existing ``invalid trim window`` SourcePrepareError.

    ``media_end`` (U36, 2026-09-25) is the media's real length, measured from
    the file. It only ever LOWERS the end — the trim out-point keeps its
    precedence over the row's ``duration_seconds``, because a trim is an
    operator's explicit instruction while the row is a record that can be
    wrong — and it is the only source of an end when the rows can supply
    none at all. ``None`` (a probe that could not answer) leaves the rows
    authoritative, exactly as before this change.
    """

    end: float | None
    if inpoint is not None and outpoint is not None:
        end = float(outpoint)
    elif asset.duration_seconds is not None:
        end = float(asset.duration_seconds)
        if outpoint is not None:
            end = min(end, float(outpoint))
    elif outpoint is not None:
        end = float(outpoint)
    else:
        end = None
    if media_end is not None:
        end = media_end if end is None else min(end, media_end)
    if end is None:
        return None
    return end - (inpoint or 0.0)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
