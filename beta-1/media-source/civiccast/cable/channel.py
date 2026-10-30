# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Software channel operations contracts for v1.6."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from civiccast.egress.models import EgressCaptionProofSample, EgressProofEvent, EgressStateRow
from civiccast.schedule.models import ScheduleItemResponse

ChannelKind = Literal["public", "education", "government", "community"]
OutputKind = Literal["hls", "rtmp", "srt", "ndi-plan"]
PlayoutKind = Literal["live", "file", "slate", "bulletin", "rerun", "fallback"]
PlayoutStatus = Literal["scheduled", "playing", "completed", "failed", "fallback"]


HLS_OUTPUT_LABEL = "Resident and CTV HLS"
HLS_OUTPUT_NOT_ENABLED_BOUNDARY = "hls-output-not-enabled"
HLS_OUTPUT_ENABLED_BOUNDARY = "hls-sink-configured"
HLS_OUTPUT_NOT_ENABLED_NEXT_STEP = (
    "HLS web output is not enabled for this channel. Apply the "
    "'Local rehearsal (web preview, HLS)' preset under Cable headend delivery, "
    "or add an hls sink to the channel's egress config, and this URL starts serving."
)
HLS_OUTPUT_ENABLED_NEXT_STEP = (
    "Serves the channel's hls egress sink while the channel is on air; the resident "
    "portal resolves this same URL from /api/public/live/current."
)
# Operator-set absolute media origin. Unset (the stock install) means the
# local live manifest URL is site-relative -- the resident portal and the
# media router share an origin, so a relative URL resolves to whatever host
# the resident actually reached. Only a deployer whose media origin differs
# from the portal's sets this. Read on every call so tests can monkeypatch it.
LOCAL_MEDIA_BASE_URL_ENV = "CIVICCAST_LOCAL_MEDIA_BASE_URL"


def public_live_manifest_path(channel_id: str) -> str:
    """The advertised public HLS URL for a channel.

    Served by ``civiccast.cable.router`` as a redirect to
    :func:`local_live_manifest_path` when the channel has an ``hls`` egress
    sink, and as a 404 whose ``detail`` names the fix otherwise — so the URL
    printed on the Channels screen and in the CTV feed is never a dead link.
    """
    return f"/api/public/channels/{channel_id}/live.m3u8"


def local_live_manifest_path(channel_id: str) -> str:
    """Where ``civiccast.stream.media_router`` serves a channel's hls sink.

    The ONE helper that spells this URL: the Channels screen, the CTV feed,
    the ``live.m3u8`` redirect and ``/api/public/live/current`` all print it
    (review round 2 delta, MINOR 2: two spellings had drifted -- one applied
    ``CIVICCAST_LOCAL_MEDIA_BASE_URL`` and ``quote()``, one did neither).
    Site-relative unless the operator set an absolute base.
    """
    path = f"/media/live/{quote(channel_id, safe='')}/playlist.m3u8"
    base_url = os.environ.get(LOCAL_MEDIA_BASE_URL_ENV, "").strip()
    if not base_url:
        return path
    return f"{base_url.rstrip('/')}{path}"


class ChannelOutput(BaseModel):
    """One software output target for a linear channel.

    ``enabled`` is ``False`` when the output is advertised by the product but
    not wired on this station (today: the HLS web output of a channel with no
    ``hls`` egress sink). ``next_step`` then says what turns it on, and UIs must
    show that sentence instead of the ``target`` link.
    """

    model_config = ConfigDict(extra="forbid")

    kind: OutputKind
    label: str = Field(min_length=1, max_length=80)
    target: str = Field(min_length=1, max_length=500)
    proof_boundary: str = Field(min_length=1, max_length=120)
    next_step: str = Field(min_length=1)
    enabled: bool = True


def hls_channel_output(channel_id: str, *, hls_sink_configured: bool) -> ChannelOutput:
    """The channel's HLS web output, truthful about whether it is wired.

    With an ``hls`` sink the target is the real ``/media/live`` manifest the
    media router serves; without one the target is the public redirect URL
    (which answers 404 + fix instructions, see :func:`public_live_manifest_path`)
    and ``enabled`` is ``False``.
    """
    if hls_sink_configured:
        return ChannelOutput(
            kind="hls",
            label=HLS_OUTPUT_LABEL,
            target=local_live_manifest_path(channel_id),
            proof_boundary=HLS_OUTPUT_ENABLED_BOUNDARY,
            next_step=HLS_OUTPUT_ENABLED_NEXT_STEP,
            enabled=True,
        )
    return ChannelOutput(
        kind="hls",
        label=HLS_OUTPUT_LABEL,
        target=public_live_manifest_path(channel_id),
        proof_boundary=HLS_OUTPUT_NOT_ENABLED_BOUNDARY,
        next_step=HLS_OUTPUT_NOT_ENABLED_NEXT_STEP,
        enabled=False,
    )


def channel_has_hls_sink(channel_id: str, egress_store: Any) -> bool:
    """True when the channel's egress config carries an ``hls`` sink."""
    if egress_store is None:
        return False
    config = egress_store.get_config(channel_id)
    return config is not None and any(sink.kind == "hls" for sink in config.sinks)


def resolve_live_outputs(profiles: list[ChannelProfile], egress_store: Any) -> list[ChannelProfile]:
    """Replace each profile's static HLS output with the station's real state."""
    resolved: list[ChannelProfile] = []
    for profile in profiles:
        outputs = [
            hls_channel_output(
                profile.channel_id,
                hls_sink_configured=channel_has_hls_sink(profile.channel_id, egress_store),
            )
            if output.kind == "hls"
            else output
            for output in profile.outputs
        ]
        resolved.append(profile.model_copy(update={"outputs": outputs}))
    return resolved


class ChannelBranding(BaseModel):
    """Resident and CTV-visible channel identity."""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    short_name: str = Field(min_length=1, max_length=40)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    logo_text: str = Field(min_length=1, max_length=40)


class ChannelProfile(BaseModel):
    """Industry-standard software channel profile."""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1, max_length=80)
    slug: str = Field(min_length=1, max_length=80)
    kind: ChannelKind
    branding: ChannelBranding
    programming_rules: list[str] = Field(default_factory=list)
    fallback_behavior: str = Field(min_length=1)
    default_slate_asset_id: str | None = Field(default=None, max_length=120)
    outputs: list[ChannelOutput] = Field(default_factory=list)


class PlayoutBlock(BaseModel):
    """One scheduled or actual linear-channel playout block."""

    model_config = ConfigDict(extra="forbid")

    block_id: str = Field(min_length=1, max_length=120)
    channel_id: str = Field(min_length=1, max_length=80)
    kind: PlayoutKind
    title: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    duration_seconds: int = Field(gt=0)
    source_ref: str = Field(min_length=1, max_length=500)
    status: PlayoutStatus = "scheduled"
    caption_refs: list[str] = Field(default_factory=list)
    failover_from: str | None = Field(default=None, max_length=120)
    failover_reason: str | None = Field(default=None, max_length=500)


class ChannelNowNext(BaseModel):
    """Resident and operator now/next projection for one channel."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    channel: ChannelProfile
    # ``None`` when nothing is on air: the outgoing feed is stopped or has no
    # daemon state row yet. The console renders "No program on air" for it
    # rather than a fabricated block (beta.5 walkthrough F-27).
    current: PlayoutBlock | None
    next: PlayoutBlock | None
    fallback_active: bool
    proof_boundary: str
    # Set when a scheduled block covers "now" but the egress daemon reports a
    # different source on air (live takeover, manual start, bulletin fill).
    # The daemon is the authority on what is airing; this names the
    # disagreement instead of rendering the scheduled title as "Playing".
    schedule_note: str | None = Field(default=None, max_length=500)


class ChannelProofEvent(BaseModel):
    """Operator-readable and machine-readable playout proof event."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=120)
    observed_at: datetime
    channel_id: str = Field(min_length=1, max_length=80)
    # ``None`` when the daemon put something on air that no schedule block
    # claimed (operator start, takeover, fallback slate).
    scheduled_block_id: str | None = Field(default=None, min_length=1, max_length=120)
    actual_kind: PlayoutKind
    actual_status: PlayoutStatus
    title: str = Field(min_length=1, max_length=200)
    source_ref: str = Field(min_length=1, max_length=500)
    failover_from: str | None = Field(default=None, max_length=120)
    failover_reason: str | None = Field(default=None, max_length=500)
    # Joined from the daemon's CEA-608/708 caption decode-back proof samples:
    # ``True`` when the nearest sample within CAPTION_PROOF_JOIN_WINDOW_SECONDS
    # of the event is a PASS, ``False`` on a FAIL, ``None`` ("not verified")
    # when no sample covers the event. The console must not print "Attached"
    # for captions nobody proved (F-27).
    captions_attached: bool | None
    machine_summary: str = Field(min_length=1)


class ChannelProofLog(BaseModel):
    """Playout proof log for one channel."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    channel: ChannelProfile
    events: list[ChannelProofEvent]
    export_formats: list[str]
    not_claimed: list[str]


class ChannelPlayoutPlan(BaseModel):
    """Operator playout plan derived from the schedule lane."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    channel: ChannelProfile
    source: Literal["schedule-store", "sample-contract"]
    # Empty when nothing is scheduled. The operator plan never falls back to
    # sample-contract blocks; that source is reserved for test fixtures.
    blocks: list[PlayoutBlock]
    gap_blocks: list[PlayoutBlock]
    export_formats: list[str]
    proof_boundary: str
    not_claimed: list[str]


class CtvFeedItem(BaseModel):
    """Roku/reference CTV feed item."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    type: Literal["live", "vod"]
    title: str = Field(min_length=1, max_length=200)
    channel_id: str | None = Field(default=None, max_length=80)
    stream_url: str = Field(min_length=1, max_length=500)
    captions_url: str | None = Field(default=None, max_length=500)
    content_id: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=1000)


class CtvFeed(BaseModel):
    """Stable public feed for reference connected-TV clients."""

    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    station_name: str = Field(min_length=1, max_length=160)
    items: list[CtvFeedItem]
    browse_facets: list[str]
    proof_boundary: str


def default_channel_profiles() -> list[ChannelProfile]:
    """Return the default PEG-style software channel lineup."""

    return [
        _profile(
            channel_id="public",
            slug="public",
            kind="public",
            display_name="Public Channel",
            short_name="Public",
            color="#2458A6",
            logo_text="PUBLIC",
            default_slate_asset_id="slate-public",
            programming_rules=[
                "Live meetings take priority over file playback.",
                "Meeting reruns may fill gaps between live events.",
                "Fallback to station slate when no approved program is available.",
            ],
        ),
        _profile(
            channel_id="education",
            slug="education",
            kind="education",
            display_name="Education Channel",
            short_name="Education",
            color="#1B7F5F",
            logo_text="EDU",
            default_slate_asset_id="slate-education",
            programming_rules=[
                "School-board meetings and education programs are preferred.",
                "Bulletin boards may fill short gaps.",
                "Fallback to education slate when playback underruns.",
            ],
        ),
        _profile(
            channel_id="government",
            slug="government",
            kind="government",
            display_name="Government Channel",
            short_name="Gov",
            color="#7A4E9D",
            logo_text="GOV",
            default_slate_asset_id="slate-government",
            programming_rules=[
                "Council, board, and commission meetings take priority.",
                "Emergency bulletin blocks may interrupt scheduled playback.",
                "Fallback to government slate when a live source fails.",
            ],
        ),
    ]


def get_channel_profile(channel_id: str) -> ChannelProfile | None:
    """Return one default channel profile by id or slug."""

    normalized = channel_id.strip().lower()
    for profile in default_channel_profiles():
        if normalized in {profile.channel_id, profile.slug}:
            return profile
    return None


_ON_AIR_EGRESS_STATES: frozenset[str] = frozenset(
    {"ON_AIR", "TRANSITIONING", "FALLBACK_SLATE", "DRAINING"}
)

NOW_NEXT_PROOF_BOUNDARY = "egress-state-and-schedule-store"
PLAYOUT_PLAN_PROOF_BOUNDARY = "software-schedule-to-playout-plan"
SAMPLE_PROOF_BOUNDARY = "sample-contract"

# Free text from the egress daemon (``last_error`` is up to 1000 chars and
# carries ``str(exc)`` / a child-stderr tail) is cut to the contract field's
# ``max_length`` at this boundary. Without it the now/next endpoints raised a
# pydantic ValidationError -- a 500 -- exactly during a fallback incident.
OPERATOR_REASON_MAX_CHARS = 500
_ELLIPSIS = "\u2026"

# A caption decode-back proof sample counts for a proof event only when it was
# taken within this many seconds of the event; matches the daemon's own
# caption freshness window (``civiccast.egress.caption_proof``).
CAPTION_PROOF_JOIN_WINDOW_SECONDS = 120

_PROOF_LOG_NOT_CLAIMED = [
    "SDI or DeckLink output",
    "Comcast/headend delivery proof",
    "Roku Channel Store publication",
    (
        "caption verdicts more than "
        f"{CAPTION_PROOF_JOIN_WINDOW_SECONDS}s from the event (reported as not verified)"
    ),
]


def operator_reason(text: str | None, limit: int = OPERATOR_REASON_MAX_CHARS) -> str | None:
    """Cut daemon free text to ``limit`` characters with a trailing ellipsis."""

    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + _ELLIPSIS


_PLAYOUT_PLAN_NOT_CLAIMED = [
    "hardware playout device control",
    "SDI or DeckLink output",
    "Comcast/headend delivery proof",
]


def build_channel_now_next(
    channel_id: str,
    *,
    now: datetime | None = None,
    schedule_items: list[ScheduleItemResponse] | None = None,
    egress_state: EgressStateRow | None = None,
    include_operator_detail: bool = True,
    current_source_ref: str | None = None,
) -> ChannelNowNext:
    """Build now/next from what the egress daemon and schedule store really report.

    ``current`` is only populated while the daemon reports the feed on air
    (``ON_AIR``/``TRANSITIONING``/``FALLBACK_SLATE``/``DRAINING``). A stopped
    feed, a missing state row, or a channel with no daemon at all yields
    ``current=None`` -- the console says "No program on air".

    ``next`` is the first scheduled premiere whose ``starts_at`` is after
    ``now``, or ``None``. A premiere that already started but is not what the
    daemon airs is named in ``schedule_note``; it is never re-advertised as
    "Next" with a start time in the past (hostile review B4).

    ``current_source_ref`` is the asset id the daemon's own proof event
    recorded for the source it handed off (``EgressProofEvent.source_ref``,
    joined by the router on the state row). It is the only thing that lets a
    scheduled block lend its block id, timing and caption refs to the on-air
    block; identity is never inferred from a substring of the free-text
    label (hostile review M5). With no proof event, an exact whole-label
    match lends the slot but never the caption refs.

    ``include_operator_detail=False`` is the unauthenticated public
    projection: the daemon's ``last_error`` (raw ``str(exc)`` / stderr with
    file paths and headend host:port) and its free-text source label never
    leave the station. The public block's title is the scheduled program's
    title when the daemon is proven to be airing that scheduled asset (the
    same string the public schedule routes already serve), the channel's
    display name when the daemon airs something else or reports no label,
    or "Fallback slate"; it carries no ``failover_reason``. ``schedule_note``
    is withheld because it repeats the daemon label.
    """

    profile = _require_profile(channel_id)
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    scheduled = _blocks_from_schedule(profile, schedule_items or [])
    upcoming = [
        block
        for block in scheduled
        if block.starts_at + timedelta(seconds=block.duration_seconds) > current_time
    ]
    current: PlayoutBlock | None = None
    schedule_note: str | None = None
    if egress_state is not None and egress_state.state in _ON_AIR_EGRESS_STATES:
        current, schedule_note, scheduled_title = _block_from_egress_state(
            profile, egress_state, upcoming, current_time, current_source_ref
        )
        if not include_operator_detail:
            current = _public_projection(profile, current, scheduled_title)
            schedule_note = None
    # B4: only a premiere that has not started yet is "Next". A block whose
    # window already covers ``now`` is either what is airing (borrowed into
    # ``current``) or what the schedule claims against the daemon (named in
    # ``schedule_note``); either way it is not coming up.
    next_block = next((block for block in upcoming if block.starts_at > current_time), None)
    return ChannelNowNext(
        generated_at=current_time,
        channel=profile,
        current=current,
        next=next_block,
        fallback_active=egress_state is not None and egress_state.state == "FALLBACK_SLATE",
        proof_boundary=NOW_NEXT_PROOF_BOUNDARY,
        schedule_note=schedule_note,
    )


def build_channel_proof_log(
    channel_id: str,
    *,
    now: datetime | None = None,
    proof_events: list[EgressProofEvent] | None = None,
    caption_proof_samples: list[EgressCaptionProofSample] | None = None,
) -> ChannelProofLog:
    """Build the operator proof log from persisted egress daemon proof events.

    No daemon events means an empty log -- the console says "No proof events
    yet". Nothing here is synthesised from the plan. ``captions_attached`` is
    joined from ``caption_proof_samples`` (the daemon's decode-back verdicts):
    the nearest sample within :data:`CAPTION_PROOF_JOIN_WINDOW_SECONDS` of the
    event decides PASS -> ``True`` / FAIL -> ``False``; no sample in the window
    leaves ``None`` ("Not verified").
    """

    profile = _require_profile(channel_id)
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    samples = [
        sample
        for sample in (caption_proof_samples or [])
        if sample.channel_id == profile.channel_id
    ]
    events = [
        _proof_event_from_egress(profile, event, samples)
        for event in sorted(proof_events or [], key=lambda row: row.observed_at, reverse=True)
        if event.channel_id == profile.channel_id
    ]
    return ChannelProofLog(
        generated_at=current_time,
        channel=profile,
        events=events,
        export_formats=["json", "csv-ready"],
        not_claimed=_PROOF_LOG_NOT_CLAIMED,
    )


def build_channel_playout_plan(
    channel_id: str,
    *,
    schedule_items: list[ScheduleItemResponse] | None = None,
    now: datetime | None = None,
) -> ChannelPlayoutPlan:
    """Build a software playout plan from scheduled rows only.

    An empty schedule yields an empty plan (``blocks == []``); the operator
    view never receives sample-contract blocks.
    """

    profile = _require_profile(channel_id)
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    blocks = _blocks_from_schedule(profile, schedule_items or [])
    return ChannelPlayoutPlan(
        generated_at=current_time,
        channel=profile,
        source="schedule-store",
        blocks=blocks,
        gap_blocks=_gap_blocks(profile, blocks),
        export_formats=["json", "csv-ready"],
        proof_boundary=PLAYOUT_PLAN_PROOF_BOUNDARY,
        not_claimed=_PLAYOUT_PLAN_NOT_CLAIMED,
    )


def build_sample_channel_now_next(
    channel_id: str, *, now: datetime | None = None
) -> ChannelNowNext:
    """Deterministic sample now/next for fixtures and the seeded app-platform feed.

    Never wire this into the operator console: it invents a playing block.
    """

    profile = _require_profile(channel_id)
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    blocks = _sample_playout_blocks(profile, current_time)
    current = blocks[0]
    return ChannelNowNext(
        generated_at=current_time,
        channel=profile,
        current=current,
        next=blocks[1] if len(blocks) > 1 else None,
        fallback_active=current.status == "fallback",
        proof_boundary=SAMPLE_PROOF_BOUNDARY,
    )


def build_sample_channel_proof_log(
    channel_id: str, *, now: datetime | None = None
) -> ChannelProofLog:
    """Deterministic sample proof log for fixtures. Not for the operator console."""

    profile = _require_profile(channel_id)
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    blocks = _sample_playout_blocks(profile, current_time)
    events = [
        ChannelProofEvent(
            event_id=f"proof-{block.block_id}",
            observed_at=max(block.starts_at, current_time),
            channel_id=profile.channel_id,
            scheduled_block_id=block.block_id,
            actual_kind=block.kind,
            actual_status=block.status,
            title=block.title,
            source_ref=block.source_ref,
            failover_from=block.failover_from,
            failover_reason=block.failover_reason,
            captions_attached=bool(block.caption_refs),
            machine_summary=(
                f"{profile.channel_id}:{block.block_id}:{block.kind}:{block.status}:"
                f"{'captions' if block.caption_refs else 'no-captions'}"
            ),
        )
        for block in blocks
    ]
    return ChannelProofLog(
        generated_at=current_time,
        channel=profile,
        events=events,
        export_formats=["json", "csv-ready"],
        not_claimed=_PROOF_LOG_NOT_CLAIMED,
    )


def build_sample_channel_playout_plan(
    channel_id: str, *, now: datetime | None = None
) -> ChannelPlayoutPlan:
    """Deterministic sample plan (``source="sample-contract"``) for fixtures only."""

    profile = _require_profile(channel_id)
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    blocks = _sample_playout_blocks(profile, current_time)
    return ChannelPlayoutPlan(
        generated_at=current_time,
        channel=profile,
        source="sample-contract",
        blocks=blocks,
        gap_blocks=_gap_blocks(profile, blocks),
        export_formats=["json", "csv-ready"],
        proof_boundary=SAMPLE_PROOF_BOUNDARY,
        not_claimed=_PLAYOUT_PLAN_NOT_CLAIMED,
    )


def _block_from_egress_state(
    profile: ChannelProfile,
    state: EgressStateRow,
    upcoming: list[PlayoutBlock],
    now: datetime,
    source_ref: str | None = None,
) -> tuple[PlayoutBlock, str | None, str | None]:
    """Project the daemon's on-air state onto a playout block.

    The daemon is the authority on what is on air. Its ``current_source_label``
    is the title. A scheduled block covering ``now`` lends its block id,
    timing and caption refs only when the daemon's own proof event names that
    block's asset (``source_ref``). Without a proof event, a label that equals
    the block's title whole lends the slot but not the caption refs -- a
    title is a heuristic, and a caption claim on the proof surface must not
    rest on one (hostile review M5). Identity is never a substring of the
    label: asset id ``live`` does not own "Live takeover from Studio B".

    When the schedule says one thing and the daemon another (live takeover,
    manual start, bulletin fill), the block is built from the daemon row
    alone and the second return value names the disagreement. A daemon row
    without a label never adopts the scheduled title as "Playing" -- that was
    the F-27 fabrication in a new coat. The third return value is the
    scheduled program's title when the block was borrowed, for the public
    projection (M6).
    """

    fallback = state.state == "FALLBACK_SLATE"
    label = state.current_source_label
    reason = operator_reason(state.last_error) if fallback else None
    covering = next((block for block in upcoming if block.starts_at <= now), None)
    proven = (
        covering is not None and source_ref is not None and _block_airs_asset(covering, source_ref)
    )
    same_title = (
        covering is not None
        and source_ref is None
        and label is not None
        and _label_equals_title(covering, label)
    )
    if covering is not None and (proven or same_title):
        return (
            covering.model_copy(
                update={
                    "title": label or covering.title,
                    "status": "fallback" if fallback else "playing",
                    "kind": "fallback" if fallback else covering.kind,
                    "failover_from": covering.source_ref if fallback else None,
                    "failover_reason": reason,
                    "caption_refs": list(covering.caption_refs) if proven else [],
                }
            ),
            None,
            covering.title,
        )
    started = state.updated_at.astimezone(UTC)
    elapsed = max(1, int((now - started).total_seconds()))
    title = label or f"{profile.branding.short_name} outgoing feed"
    block = PlayoutBlock(
        block_id=f"{profile.channel_id}-egress-{state.state.lower()}",
        channel_id=profile.channel_id,
        kind="fallback" if fallback else "live",
        title=title,
        starts_at=started,
        duration_seconds=elapsed,
        source_ref=label or f"egress-{profile.channel_id}",
        status="fallback" if fallback else "playing",
        failover_reason=reason,
    )
    note: str | None = None
    if covering is not None:
        airing = f"'{label}'" if label else "an unlabelled source"
        note = operator_reason(
            f"Schedule lists '{covering.title}' from "
            f"{covering.starts_at.strftime('%H:%M')} UTC, but the outgoing feed "
            f"reports {airing} on air. The schedule is not what is airing."
        )
    return block, note, None


def _block_airs_asset(block: PlayoutBlock, source_ref: str) -> bool:
    """True when the daemon's proof-event ``source_ref`` is this block's asset."""

    asset_id = source_ref.strip()
    return bool(asset_id) and block.source_ref == f"asset-{asset_id}"


def _label_equals_title(block: PlayoutBlock, label: str) -> bool:
    """True only when the whole label is the block's title (never a substring)."""

    folded = label.strip().casefold()
    return bool(folded) and folded == block.title.strip().casefold()


def _public_projection(
    profile: ChannelProfile, block: PlayoutBlock, scheduled_title: str | None
) -> PlayoutBlock:
    """Resident-safe copy of an on-air block: no daemon free text.

    ``scheduled_title`` is the schedule store's title for the block the daemon
    is proven to be airing -- already public via the schedule routes -- so
    the resident surface names the program instead of blanking it to the
    channel name and contradicting ``next`` (hostile review M6).
    """

    fallback = block.status == "fallback"
    title = "Fallback slate" if fallback else (scheduled_title or profile.branding.display_name)
    return block.model_copy(
        update={
            "title": title,
            "source_ref": f"egress-{profile.channel_id}",
            "failover_from": None,
            "failover_reason": None,
        }
    )


def _captions_verdict(
    event: EgressProofEvent, samples: list[EgressCaptionProofSample]
) -> bool | None:
    window = timedelta(seconds=CAPTION_PROOF_JOIN_WINDOW_SECONDS)
    observed = event.observed_at.astimezone(UTC)
    nearest: EgressCaptionProofSample | None = None
    nearest_delta: timedelta | None = None
    for sample in samples:
        delta = abs(sample.sampled_at.astimezone(UTC) - observed)
        if delta > window:
            continue
        if nearest_delta is None or delta < nearest_delta:
            nearest, nearest_delta = sample, delta
    if nearest is None:
        return None
    return nearest.status == "PASS"


def _proof_event_from_egress(
    profile: ChannelProfile,
    event: EgressProofEvent,
    caption_samples: list[EgressCaptionProofSample] | None = None,
) -> ChannelProofEvent:
    fallback = event.state == "FALLBACK_SLATE"
    on_air = event.state in _ON_AIR_EGRESS_STATES
    status: PlayoutStatus = "fallback" if fallback else ("playing" if on_air else "completed")
    if event.state == "ERROR":
        status = "failed"
    return ChannelProofEvent(
        event_id=event.event_id,
        observed_at=event.observed_at,
        channel_id=profile.channel_id,
        scheduled_block_id=None,
        actual_kind="fallback" if fallback else "live",
        actual_status=status,
        title=event.source_label,
        source_ref=event.source_ref or event.source_label,
        captions_attached=_captions_verdict(event, caption_samples or []),
        machine_summary=event.machine_summary,
    )


def build_ctv_feed(*, station_name: str = "CivicCast Test Station") -> CtvFeed:
    """Build the stable public feed for Roku/reference CTV clients."""

    generated_at = datetime.now(UTC)
    items = [
        CtvFeedItem(
            id=f"live-{profile.channel_id}",
            type="live",
            title=profile.branding.display_name,
            channel_id=profile.channel_id,
            stream_url=public_live_manifest_path(profile.channel_id),
            captions_url=f"/api/public/channels/{profile.channel_id}/captions.vtt",
            content_id=f"civiccast-live-{profile.channel_id}",
            description=f"Live and scheduled programming for {profile.branding.display_name}.",
        )
        for profile in default_channel_profiles()
    ]
    items.append(
        CtvFeedItem(
            id="vod-recent-meetings",
            type="vod",
            title="Recent meetings",
            stream_url="/api/public/assets",
            content_id="civiccast-vod-recent-meetings",
            description="Reference VOD collection for meetings published through CivicCast.",
        )
    )
    return CtvFeed(
        generated_at=generated_at,
        station_name=station_name,
        items=items,
        browse_facets=["channel", "meeting-body", "series", "date", "topic"],
        proof_boundary="reference-feed-api-not-channel-store-publication",
    )


def _profile(
    *,
    channel_id: str,
    slug: str,
    kind: ChannelKind,
    display_name: str,
    short_name: str,
    color: str,
    logo_text: str,
    default_slate_asset_id: str,
    programming_rules: list[str],
) -> ChannelProfile:
    return ChannelProfile(
        channel_id=channel_id,
        slug=slug,
        kind=kind,
        branding=ChannelBranding(
            display_name=display_name,
            short_name=short_name,
            color=color,
            logo_text=logo_text,
        ),
        programming_rules=programming_rules,
        fallback_behavior=(
            "Use the channel slate immediately when a live source, file playback, "
            "or bulletin block is unavailable."
        ),
        default_slate_asset_id=default_slate_asset_id,
        outputs=[
            # Static default = not wired; ``resolve_live_outputs`` upgrades it
            # from the station's egress config at request time.
            hls_channel_output(channel_id, hls_sink_configured=False),
            ChannelOutput(
                kind="ndi-plan",
                label="NDI command plan",
                target=f"CivicCast {display_name}",
                proof_boundary="command-plan-only",
                next_step="Run receiver-side NDI proof before claiming NDI delivery.",
            ),
        ],
    )


def _require_profile(channel_id: str) -> ChannelProfile:
    profile = get_channel_profile(channel_id)
    if profile is None:
        raise ValueError(f"Unknown channel profile: {channel_id}")
    return profile


def _sample_playout_blocks(profile: ChannelProfile, now: datetime) -> list[PlayoutBlock]:
    started = now.replace(second=0, microsecond=0)
    failed_source = f"live-source-{profile.channel_id}"
    current_kind: PlayoutKind = "live" if profile.kind != "education" else "file"
    current_status: PlayoutStatus = "playing"
    failover_from = None
    failover_reason = None
    source_ref = failed_source
    if profile.kind == "government":
        current_kind = "fallback"
        current_status = "fallback"
        failover_from = failed_source
        failover_reason = "live source missing heartbeat"
        source_ref = profile.default_slate_asset_id or f"slate-{profile.channel_id}"
    return [
        PlayoutBlock(
            block_id=f"{profile.channel_id}-now",
            channel_id=profile.channel_id,
            kind=current_kind,
            title=f"{profile.branding.short_name} live programming",
            starts_at=started,
            duration_seconds=1800,
            source_ref=source_ref,
            status=current_status,
            caption_refs=[f"{profile.channel_id}-live.vtt"] if current_kind != "fallback" else [],
            failover_from=failover_from,
            failover_reason=failover_reason,
        ),
        PlayoutBlock(
            block_id=f"{profile.channel_id}-next",
            channel_id=profile.channel_id,
            kind="rerun",
            title=f"{profile.branding.short_name} meeting replay",
            starts_at=started + timedelta(minutes=30),
            duration_seconds=3600,
            source_ref=f"asset-{profile.channel_id}-recent-meeting",
            status="scheduled",
            caption_refs=[f"{profile.channel_id}-replay.vtt"],
        ),
    ]


def _blocks_from_schedule(
    profile: ChannelProfile,
    schedule_items: list[ScheduleItemResponse],
) -> list[PlayoutBlock]:
    blocks: list[PlayoutBlock] = []
    for item in sorted(schedule_items, key=lambda row: row.scheduled_at):
        if item.mode != "premiere" or item.state != "scheduled":
            continue
        title = item.asset_title or item.asset_id
        blocks.append(
            PlayoutBlock(
                block_id=f"schedule-{item.id}",
                channel_id=profile.channel_id,
                kind="file",
                title=title,
                starts_at=item.scheduled_at.astimezone(UTC),
                duration_seconds=item.duration_seconds or 1,
                source_ref=f"asset-{item.asset_id}",
                status="scheduled",
                caption_refs=[f"{item.asset_id}.vtt"],
            )
        )
    # An empty schedule is an empty plan. The old "channel slate" placeholder
    # block made a bare channel look programmed (beta.5 walkthrough F-27).
    return blocks


def _gap_blocks(profile: ChannelProfile, blocks: list[PlayoutBlock]) -> list[PlayoutBlock]:
    gaps: list[PlayoutBlock] = []
    ordered = sorted(blocks, key=lambda block: block.starts_at)
    for index, left in enumerate(ordered[:-1]):
        right = ordered[index + 1]
        left_end = left.starts_at + timedelta(seconds=left.duration_seconds)
        gap_seconds = int((right.starts_at - left_end).total_seconds())
        if gap_seconds <= 0:
            continue
        gaps.append(
            PlayoutBlock(
                block_id=f"gap-{left.block_id}-to-{right.block_id}",
                channel_id=profile.channel_id,
                kind="slate",
                title=f"{profile.branding.short_name} slate gap",
                starts_at=left_end,
                duration_seconds=gap_seconds,
                source_ref=profile.default_slate_asset_id or f"slate-{profile.channel_id}",
                status="scheduled",
            )
        )
    return gaps
