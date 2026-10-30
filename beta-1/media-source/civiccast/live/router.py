# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""FastAPI staff router for the live-broadcast spine.

Sprint 0.4 Slice 1 Commit 6. Lights up the operator-facing surface for
the contracts shipped in Slice 1 Commits 3 / 4 / 5:

  - POST   /api/staff/live/sessions
        Create a live session at state ``idle``. 201 + ``LiveSessionResponse``
        on success; 409 when the ``live_session_id`` already exists; 422 on
        Pydantic validation failure; 503 when the DB is not configured.

  - GET    /api/staff/live/sessions/{live_session_id}
        Read one live session. 200 + ``LiveSessionResponse`` or 404.

  - POST   /api/staff/live/sessions/{live_session_id}/start-preflight
        State transition ``idle -> preflight``. 200 + ``LiveSessionResponse``,
        404 if the session is missing, 409 with ``current_state`` +
        ``attempted_transition`` if the session is not in ``idle``.

  - POST   /api/staff/live/sessions/{live_session_id}/preflight
        Body: ``PreflightInputs``. Runs the pre-flight evaluator and returns
        ``PreflightEvaluation``. This endpoint does NOT mutate the
        ``LiveSession`` state machine -- the operator UI may re-evaluate as
        often as it likes during the ``preflight`` window. 422 on Pydantic
        failure; 503 when the DB is not configured.

  - POST   /api/staff/live/sessions/{live_session_id}/go-on-air
        State transition ``preflight -> on_air``. Stamps ``started_at``.
        Same status code surface as ``/start-preflight``.

  - POST   /api/staff/live/sessions/{live_session_id}/end-broadcast
        State transition ``on_air -> ending``. Stamps ``ended_at``.

  - POST   /api/staff/live/sources           (create one ``LiveSource``)
  - GET    /api/staff/live/sources           (list, optional ``?channel_id=``)
  - GET    /api/staff/live/sources/{id}      (get one or 404)
  - PATCH  /api/staff/live/sources/{id}      (edit; 409 on a stale row_version)
  - POST   /api/staff/live/sources/{id}/probe (check it is delivering media now)

  - POST   /api/staff/live/recording-targets (create one ``RecordingTarget``)
  - GET    /api/staff/live/recording-targets (list every row)
  - GET    /api/staff/live/recording-targets/{id} (get one or 404)

Auth posture follows the v1.4 local operator-role model. Read routes stay
visible to staff operators, while live-room mutations require the
``meeting_operator`` role.

The dependency callables (``get_live_session_store``,
``get_preflight_evaluator``, ``get_live_source_store``,
``get_recording_target_store``) all return ``None`` by default. The
umbrella ``civiccast.app.create_app`` factory overrides them with real
``LiveSessionStore`` / ``PreflightEvaluator`` / ``LiveSourceStore`` /
``RecordingTargetStore`` instances when ``DATABASE_URL`` is set. The
endpoints translate ``None`` into HTTP 503 with an operator-readable
"Database not configured" message, matching the schedule-router
posture established in v0.3.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from civiccast.auth.roles import require_any_role
from civiccast.cable.channel import LOCAL_MEDIA_BASE_URL_ENV, local_live_manifest_path
from civiccast.common.trusted_proxy import resolve_client_ip
from civiccast.egress.headend import resolve_local_hls_directory
from civiccast.egress.router import _staff_operator, get_egress_store, get_takeover_service
from civiccast.egress.takeover_service import (
    AlreadyLiveError,
    TakeoverNotReadyError,
    TakeoverService,
)
from civiccast.live.models import (
    LIVE_SESSION_STATE_ON_AIR,
    LiveFinalizationStatusResponse,
    LiveIngestPlan,
    LiveRelayConfigCreate,
    LiveRelayConfigResponse,
    LiveRelayHealthUpdate,
    LiveSessionCreate,
    LiveSessionResponse,
    LiveSourceCreate,
    LiveSourceProbeResponse,
    LiveSourceResponse,
    LiveSourceUpdate,
    RecordingTargetCreate,
    RecordingTargetResponse,
)
from civiccast.live.preflight import PreflightEvaluation, PreflightInputs
from civiccast.live.relay import build_ingest_plan
from civiccast.live.surge_service import get_surge_switch_service

# ---------------------------------------------------------------------------
# Dependency seams (overridden by the app factory when DATABASE_URL is set)
# ---------------------------------------------------------------------------


def get_live_session_store() -> Any:
    """FastAPI dependency for the active ``LiveSessionStore``.

    Returns ``None`` when the umbrella app has not been wired with a
    real DB engine. The route handlers translate ``None`` into HTTP
    503 so the OpenAPI surface documents the deployment-time
    requirement instead of producing a 500 on first request.

    Typed ``Any`` to avoid a circular import between router and store
    -- the concrete return type at runtime under ``create_app`` is
    :class:`civiccast.live.store.LiveSessionStore`.
    """


def get_preflight_evaluator() -> Any:
    """FastAPI dependency for the active ``PreflightEvaluator``.

    Returns ``None`` when the umbrella app has not been wired with a
    real DB engine. Runtime type under ``create_app`` is
    :class:`civiccast.live.preflight.PreflightEvaluator`.
    """


def get_live_source_store() -> Any:
    """FastAPI dependency for the active ``LiveSourceStore``."""


def get_live_source_readiness_service() -> Any:
    """FastAPI dependency for the active ``LiveSourceReadinessService``.

    Returns ``None`` until the umbrella app wires durable storage, like every
    other seam in this module. Runtime type under ``create_app`` is
    :class:`civiccast.live.readiness_service.LiveSourceReadinessService`.
    """


def get_live_relay_config_store() -> Any:
    """FastAPI dependency for the active ``LiveRelayConfigStore``."""


def get_recording_target_store() -> Any:
    """FastAPI dependency for the active ``RecordingTargetStore``."""


def get_live_finalization_worker() -> Any:
    """FastAPI dependency for the recording finalization worker/status surface."""


# ---------------------------------------------------------------------------
# Router instance + helpers
# ---------------------------------------------------------------------------


staff_router = APIRouter(prefix="/api/staff/live", tags=["staff", "live"])
public_router = APIRouter(prefix="/api/public/live", tags=["public", "live"])
_DB_NOT_READY_DESCRIPTION = "Durable storage not ready -- run Setup storage or set DATABASE_URL"
_DB_NOT_READY_DETAIL = (
    "Durable storage is not ready. Open Setup and choose Prepare storage, "
    "or set DATABASE_URL for a technical deployment."
)


def _require_store(store: Any, *, surface: str) -> Any:
    """Translate a ``None`` store dependency into HTTP 503.

    The DI seams above default to ``None`` so importing the router
    module does no I/O. Each route uses this helper so the 503 detail
    string is consistent across endpoints and the operator sees the
    same actionable message regardless of which live-route they hit.
    """
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{_DB_NOT_READY_DETAIL} Surface: {surface}.",
        )
    return store


def _translate_state_error(exc: Any, *, attempted_transition: str) -> HTTPException:
    """Map ``LiveSessionStateError`` to a 409 with a structured detail body.

    The body carries ``current_state`` + ``attempted_transition`` so the
    operator console can show "session is already on-air; click End
    Broadcast instead" without re-querying.
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": (
                f"LiveSession {exc.live_session_id!r} cannot {attempted_transition} "
                f"from state {exc.current_state!r}."
            ),
            "live_session_id": exc.live_session_id,
            "current_state": exc.current_state,
            "attempted_transition": attempted_transition,
        },
    )


PUBLIC_LIVE_STATE_OFFLINE = "offline"
PUBLIC_LIVE_STATE_ON_AIR = "on_air"
# The channel's egress pipeline is on air (program or fallback slate) but no
# ``hls`` sink is configured, so there is no web-playable manifest. Distinct from
# ``offline`` so the resident portal can say "on air, web preview not enabled"
# instead of lying that the station is dark.
PUBLIC_LIVE_STATE_ON_AIR_NO_WEB_OUTPUT = "on_air_no_web_output"
# The channel's egress pipeline is up but emitting its fallback slate, not a
# program (egress ``FALLBACK_SLATE``). Reported as its own state (review round
# 2, MAJOR 3): a slate must never read "On air" to residents, and the portal
# keeps its between-streams idle page up instead of hiding it behind a player.
# ``manifest_url`` is still filled in when an ``hls`` sink exists -- the slate
# IS being served -- so a client that wants to show it can.
PUBLIC_LIVE_STATE_STANDING_BY = "standing_by"
PUBLIC_LIVE_STATE_STARTING = "starting"
PUBLIC_LIVE_STATE_FAILED = "failed"
PUBLIC_LIVE_STATE_INTERRUPTED = "interrupted"
PUBLIC_LIVE_REASON_NO_HLS_OUTPUT = "no HLS output configured"
# An ``hls`` sink IS configured but its playlist does not exist on disk yet
# (the channel is running a pipeline built before the sink was added, the
# relay has not written its first segment, or the folder is outside the
# served root). Reported instead of a manifest URL that would 404 (review
# round 2 delta, BLOCKER 1: a preset applied to a running channel used to
# hand residents a player pointed at a manifest that never existed).
PUBLIC_LIVE_REASON_HLS_NOT_SERVING = "HLS output configured but not serving yet"
PUBLIC_LIVE_REASON_FALLBACK_SLATE = "fallback slate, no program on air"
# Re-exported for callers that historically imported it from here; the one
# owner is ``civiccast.cable.channel`` (review round 2 delta, MINOR 2).
__all__ = ["LOCAL_MEDIA_BASE_URL_ENV"]
# Egress states that mean "the channel is emitting a program right now".
# ``TRANSITIONING`` is emitting (review round 2 delta, MAJOR 1): the daemon
# writes it on the restart-reload path while the outgoing worker is still on
# air, and deliberately never writes it on the seamless path because output
# stays up -- so it must not project to ``offline``. Its ``current_source_label``
# is carried over from the state it is leaving.
_EGRESS_STATES_PROGRAM = frozenset({"ON_AIR", "TRANSITIONING"})
# Egress state that means "the pipeline is up on its fallback slate".
_EGRESS_STATE_STANDING_BY = "FALLBACK_SLATE"
_EGRESS_STATES_EMITTING = _EGRESS_STATES_PROGRAM | {_EGRESS_STATE_STANDING_BY}
_LOG = logging.getLogger(__name__)


class PublicProgramTransition(BaseModel):
    title: str
    started_at: str


class PublicLiveStatus(BaseModel):
    """Resident-safe live-session projection for the public portal.

    ``state`` includes ``offline`` / ``on_air`` / ``on_air_no_web_output`` /
    ``standing_by`` / ``starting`` / ``failed`` / ``interrupted``. ``reason``
    names in plain
    words, why there is no ``manifest_url`` although the channel is on air, or
    why a channel with a manifest is not called on air (it is on its slate).
    """

    state: str
    live_session_id: str | None = None
    channel_id: str | None = None
    title: str | None = None
    started_at: str | None = None
    # Actual source handoffs are used with HLS PROGRAM-DATE-TIME to select the
    # title at the viewer's playback position. The endpoint intentionally
    # projects only resident-safe title/time fields, never proof paths.
    program_timeline: list[PublicProgramTransition] = Field(default_factory=list)
    manifest_url: str | None = None
    reason: str | None = None


@public_router.get(
    "/current",
    response_model=PublicLiveStatus,
    summary="Return the current on-air live session for residents",
    responses={
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_current_live_session(
    request: Request,
    channel_id: str | None = None,
    manifest_url: str | None = None,
    live_session_store: Any = Depends(get_live_session_store),
    egress_store: Any = Depends(get_egress_store),
    surge: Any = Depends(get_surge_switch_service),
) -> PublicLiveStatus:
    """Return the newest on-air session, else the egress pipeline's live state.

    Precedence: a live SESSION that is ``on_air`` always wins (its title and
    ``live_session_id`` are the resident-facing record). When no session is on
    air, the channel's EGRESS state is consulted: a channel whose pipeline is
    ``ON_AIR`` (scheduled playout) reports ``on_air`` when it has an ``hls``
    sink to serve residents from, and ``on_air_no_web_output`` (with
    ``reason``) when it is on air on the headend only; a channel on its
    ``FALLBACK_SLATE`` reports ``standing_by`` (with ``reason``, and with
    ``manifest_url`` when an ``hls`` sink exists) -- a slate is not a program
    and is never called on air. Before this fall-through the resident portal
    said "Offline" for a channel that was visibly on air on a UDP headend
    preset (beta.5 clean-machine walkthrough) because only session rows were
    ever consulted.

    The public contract exposes only resident-safe fields. Precedence for
    ``manifest_url`` (Sprint 0.4 Phase 2): an explicit ``manifest_url`` query
    param wins (a deployer's CDN/reverse-proxy URL); otherwise, when the
    on-air channel has a local ``hls`` egress sink configured, this defaults
    to that sink's servable local URL (``civiccast.stream.media_router``'s
    ``/media/live`` mount) so a stock install has a real, resolvable URL
    instead of ``None``. A channel with no ``hls`` sink configured still
    reports ``on_air`` with ``manifest_url=None`` — session state does not
    depend on live-HLS packaging being wired up.

    This is also the **backend half** of the surge switch's post-switch load
    signal: ``observe`` here drives the switch from ``/current`` rather than only
    from the local-manifest poll (``media_router``), which goes silent the
    instant a viewer follows the switch to the CDN. It only actually keeps the
    switch driven if the client *re-polls* ``/current`` while watching — which
    the shipped public portal does **not** yet do (it fetches ``/current`` once
    on mount). So this is a necessary-but-not-sufficient step: the periodic
    client re-resolve + player source-swap is the remaining piece (tracked in
    the 0.2.0 Deliverable 2 issue) and is a **prerequisite for enabling the
    surge switch against real traffic** — until it ships, the origin cannot see
    viewers the CDN is serving, so the release path could evict under an active
    audience. The switch is off by default for exactly this reason.
    """
    store = _require_store(live_session_store, surface="public live status")
    rows: list[LiveSessionResponse] = store.list_sessions(
        channel_id=channel_id,
        states=(LIVE_SESSION_STATE_ON_AIR,),
    )
    if not rows:
        return _egress_live_status(
            request,
            channel_id=channel_id,
            manifest_url_override=manifest_url,
            egress_store=egress_store,
            surge=surge,
        )
    current = rows[0]
    # A durable live-session row records the operator's session, not whether
    # its configured output process is still running. Once an egress config
    # exists, its state is authoritative for resident-facing availability.
    # Keep session metadata for context, but never advertise a stopped or
    # failed output as live and never return a player URL for it.
    if egress_store is not None:
        config = egress_store.get_config(current.channel_id)
        if config is not None:
            egress_state = egress_store.read_state(current.channel_id)
            if egress_state is None or egress_state.state not in _EGRESS_STATES_PROGRAM:
                if egress_state is None or egress_state.state == "STARTING":
                    public_state = PUBLIC_LIVE_STATE_STARTING
                    reason = "Broadcast output is starting or has not reported a state yet."
                elif egress_state.state == "ERROR":
                    public_state = PUBLIC_LIVE_STATE_FAILED
                    reason = "Broadcast output failed. The station has been notified."
                else:
                    public_state = PUBLIC_LIVE_STATE_INTERRUPTED
                    reason = (
                        "Broadcast output is on its fallback slate."
                        if egress_state.state == _EGRESS_STATE_STANDING_BY
                        else "Broadcast output is not running."
                    )
                return PublicLiveStatus(
                    state=public_state,
                    live_session_id=current.live_session_id,
                    channel_id=current.channel_id,
                    title=current.title,
                    started_at=current.started_at.isoformat() if current.started_at else None,
                    reason=reason,
                )
    # Feed + advance the surge switch on every resolution poll, before reading
    # the resolved URL, so the delay buffer elapsing on this very poll hands the
    # viewer the CDN URL in the same response. Unlike the local-manifest observe,
    # this touchpoint can keep firing once viewers are on the CDN -- but only if
    # the client re-polls /current while watching (see the docstring: the shipped
    # portal does not yet, which is why the switch is off by default).
    if surge is not None:
        surge.observe(current.channel_id, resolve_client_ip(request))
    # Precedence: a *validated* explicit deployer URL wins; then the surge
    # switch's CDN URL when this channel is switched under load; then the local
    # media-router URL. The override is validated because /current is public and
    # unauthenticated -- see _safe_override_url.
    resolved_manifest_url = (
        _safe_override_url(manifest_url)
        or (surge.manifest_url(current.channel_id) if surge is not None else None)
        or _local_live_manifest_url(current.channel_id, egress_store)
    )
    return PublicLiveStatus(
        state=PUBLIC_LIVE_STATE_ON_AIR,
        live_session_id=current.live_session_id,
        channel_id=current.channel_id,
        title=current.title,
        started_at=current.started_at.isoformat() if current.started_at else None,
        program_timeline=_public_program_timeline(
            _recent_public_program_events(egress_store, current.channel_id)
        ),
        manifest_url=resolved_manifest_url,
    )


def _egress_live_status(
    request: Request,
    *,
    channel_id: str | None,
    manifest_url_override: str | None,
    egress_store: Any,
    surge: Any,
) -> PublicLiveStatus:
    """Project the egress daemon's state for residents when no session is on air.

    Picks the emitting channel (``ON_AIR`` / ``FALLBACK_SLATE``) — the requested
    ``channel_id`` when given, otherwise the first such channel by id,
    preferring a channel airing a program over one on its slate, then one that
    has an ``hls`` sink so a multi-channel station with one web-enabled channel
    resolves to something playable. ``title`` carries the daemon's current
    source label (asset name or slate) and ``started_at`` the moment the
    pipeline entered its current state (``EgressStateRow.updated_at`` is
    written only on state transitions). ``FALLBACK_SLATE`` projects to
    ``standing_by``, never ``on_air``: the slate is not a program.
    """
    if egress_store is None:
        return PublicLiveStatus(state=PUBLIC_LIVE_STATE_OFFLINE)
    if channel_id is not None:
        config = egress_store.get_config(channel_id)
        configs = [config] if config is not None else []
    else:
        configs = sorted(egress_store.list_configs(), key=lambda cfg: cfg.channel_id)
    candidates: list[tuple[bool, bool, Any, Any]] = []
    for config in configs:
        row = egress_store.read_state(config.channel_id)
        if row is None or row.state not in _EGRESS_STATES_EMITTING:
            continue
        has_hls = any(sink.kind == "hls" for sink in config.sinks)
        is_program = row.state in _EGRESS_STATES_PROGRAM
        candidates.append((is_program, has_hls, config, row))
    if not candidates:
        return PublicLiveStatus(state=PUBLIC_LIVE_STATE_OFFLINE)
    # Program-airing channels first, then web-playable ones; the sort above
    # already fixed the id order for ties.
    is_program, has_hls, config, row = max(candidates, key=lambda item: (item[0], item[1]))
    if surge is not None and has_hls:
        surge.observe(config.channel_id, resolve_client_ip(request))
    resolved_manifest_url = (
        _safe_override_url(manifest_url_override)
        or (surge.manifest_url(config.channel_id) if surge is not None and has_hls else None)
        or _local_live_manifest_url(config.channel_id, egress_store)
    )
    program_events = _recent_public_program_events(egress_store, config.channel_id)
    timeline = _public_program_timeline(program_events)
    # EgressStateRow.updated_at is a worker-heartbeat timestamp: the daemon
    # refreshes it on every healthy poll. Use the actual source-handoff proof
    # for the public start marker instead of presenting that heartbeat as the
    # program's start time.
    current_transition = next(
        (
            event
            for event in program_events
            if event.state in {"ON_AIR", "FALLBACK_SLATE"}
            and event.proof_boundary == "civiccast-egress-handoff-boundary"
            and event.event_id == row.current_proof_event_id
        ),
        None,
    )
    started_at = current_transition.observed_at.isoformat() if current_transition else None
    if not is_program:
        return PublicLiveStatus(
            state=PUBLIC_LIVE_STATE_STANDING_BY,
            channel_id=config.channel_id,
            title=row.current_source_label,
            started_at=started_at,
            program_timeline=timeline,
            manifest_url=resolved_manifest_url,
            reason=PUBLIC_LIVE_REASON_FALLBACK_SLATE,
        )
    if resolved_manifest_url is None:
        return PublicLiveStatus(
            state=PUBLIC_LIVE_STATE_ON_AIR_NO_WEB_OUTPUT,
            channel_id=config.channel_id,
            title=row.current_source_label,
            started_at=started_at,
            program_timeline=timeline,
            reason=(
                PUBLIC_LIVE_REASON_HLS_NOT_SERVING if has_hls else PUBLIC_LIVE_REASON_NO_HLS_OUTPUT
            ),
        )
    return PublicLiveStatus(
        state=PUBLIC_LIVE_STATE_ON_AIR,
        channel_id=config.channel_id,
        title=row.current_source_label,
        started_at=started_at,
        program_timeline=timeline,
        manifest_url=resolved_manifest_url,
    )


def _recent_public_program_events(egress_store: Any, channel_id: str) -> list[Any]:
    """Read recent source handoffs for a resident-safe projection."""
    recent = getattr(egress_store, "recent_proof_events", None)
    if recent is None:
        return []
    return [
        event
        for event in recent(channel_id, 120)
        if event.event_id.startswith("egress-proof-")
        and event.state in {"ON_AIR", "FALLBACK_SLATE"}
        and event.proof_boundary == "civiccast-egress-handoff-boundary"
    ]


def _public_program_timeline(events: list[Any]) -> list[PublicProgramTransition]:
    """Project actual on-air handoffs without exposing source paths.

    HLS segments carry PROGRAM-DATE-TIME. The resident player maps that media
    clock to these proof-backed transition times so a buffered frame keeps its
    own program title while the encoder has already moved to the next source.
    """
    return [
        PublicProgramTransition(
            title=event.source_label,
            started_at=event.observed_at.isoformat(),
        )
        for event in reversed(events)
    ]


def _safe_override_url(url: str | None) -> str | None:
    """Validate a client-supplied ``?manifest_url=`` override before echoing it.

    ``/api/public/live/current`` is public and unauthenticated, so any caller can
    set this query param, and its value is reflected verbatim in the
    resident-facing ``manifest_url`` field (the documented "deployer's CDN /
    reverse-proxy URL" override). Only honor it when it is a well-formed absolute
    ``http(s)`` URL -- never a ``javascript:``/``data:``/relative/malformed value
    that would ride back as if the station vouched for it. On rejection we return
    ``None`` so resolution falls through to the surge/local URL (fail safe), not
    an error: a bad override simply does not win.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme in ("http", "https") and parts.netloc:
        return url
    return None


def _local_live_manifest_url(channel_id: str, egress_store: Any) -> str | None:
    """The channel's local live-HLS manifest URL, or ``None`` if not servable.

    Servable means all of: an ``hls`` sink is configured, its folder is inside
    the root ``civiccast.stream.media_router`` is allowed to serve (the same
    :func:`resolve_local_hls_directory` check the media router runs, so this
    never advertises a folder that route would 404), and ``playlist.m3u8``
    exists there right now. The last check is what keeps the URL honest when
    the sink was added to a channel whose running pipeline was built without
    it (review round 2 delta, BLOCKER 1): until a pipeline actually writes
    the manifest, residents get ``on_air_no_web_output`` -- never a player
    pointed at a 404. The writers never remove the playlist themselves, so
    the daemon removes it on an operator ``stop`` and on a start that finds
    no live relay (``daemon._discard_stale_hls_playlists``, review round 3
    delta, MINOR 3); what remains uncovered is a daemon that died uncleanly
    and has not yet restarted the channel -- its last playlist is advertised
    until the channel's next start removes it, and a player that loads it
    stalls on the missing segments rather than 404ing the manifest.

    The URL itself is spelled by :func:`civiccast.cable.channel.
    local_live_manifest_path` -- site-relative by default, absolute only with
    an operator-set ``CIVICCAST_LOCAL_MEDIA_BASE_URL`` (review round 2,
    BLOCKER 1: an absolute ``http://127.0.0.1:8000`` default sent every LAN
    resident to their own loopback).
    """
    if egress_store is None:
        return None
    config = egress_store.get_config(channel_id)
    if config is None:
        return None
    hls_sink = next((sink for sink in config.sinks if sink.kind == "hls"), None)
    if hls_sink is None:
        return None
    try:
        directory = resolve_local_hls_directory(hls_sink.uri)
    except ValueError as exc:
        _LOG.warning(
            "channel %s: hls sink folder is not servable, no manifest advertised: %s",
            channel_id,
            exc,
        )
        return None
    if not (directory / "playlist.m3u8").is_file():
        return None
    return local_live_manifest_path(channel_id)


# ===========================================================================
# Session create + read
# ===========================================================================


@staff_router.post(
    "/sessions",
    response_model=LiveSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a live session (staff)",
    dependencies=[Depends(require_any_role("meeting_operator"))],
    responses={
        409: {"description": "live_session_id already exists"},
        422: {"description": "Invalid payload"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def create_session(
    payload: LiveSessionCreate,
    live_session_store: Any = Depends(get_live_session_store),
) -> LiveSessionResponse:
    """Persist a new ``LiveSession`` at state ``idle``.

    No auth in this rung -- same posture as the rest of ``/api/staff/*``.
    Pydantic validation (slug pattern, max lengths) surfaces as 422 before
    this handler runs; duplicate-id collisions surface as 409.
    """
    store = _require_store(live_session_store, surface="live session creation")

    # Local import to avoid a circular dependency between router and store
    # (router -> store -> models -> router would surface otherwise).
    from civiccast.live.store import LiveSessionAlreadyExistsError

    try:
        return _cast_response(store.create_session(payload), LiveSessionResponse)
    except LiveSessionAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"LiveSession already exists: {exc.live_session_id}",
        ) from exc


@staff_router.get(
    "/sessions/{live_session_id}",
    response_model=LiveSessionResponse,
    summary="Get a live session by id (staff)",
    responses={
        404: {"description": "LiveSession not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_session(
    live_session_id: str,
    live_session_store: Any = Depends(get_live_session_store),
) -> LiveSessionResponse:
    """Return one ``LiveSession`` projection or 404."""
    store = _require_store(live_session_store, surface="live session reads")
    result = store.get_session(live_session_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"LiveSession not found: {live_session_id}",
        )
    return _cast_response(result, LiveSessionResponse)


# ===========================================================================
# State transitions
# ===========================================================================


@staff_router.post(
    "/sessions/{live_session_id}/start-preflight",
    response_model=LiveSessionResponse,
    summary="Transition idle -> preflight",
    dependencies=[Depends(require_any_role("meeting_operator"))],
    responses={
        404: {"description": "LiveSession not found"},
        409: {
            "description": (
                "Session is not in 'idle' state -- detail carries current_state + "
                "attempted_transition for operator copy."
            )
        },
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def start_preflight(
    live_session_id: str,
    live_session_store: Any = Depends(get_live_session_store),
) -> LiveSessionResponse:
    """Advance the session from ``idle`` to ``preflight``.

    This transition only moves the LiveSession state machine; it does
    NOT run the pre-flight evaluator. The evaluator runs against
    ``POST /sessions/{id}/preflight`` so the operator UI can re-evaluate
    as inputs (network probe, storage probe, operator-confirm flip)
    change without re-transitioning state.
    """
    store = _require_store(live_session_store, surface="live session transitions")

    from civiccast.live.store import LiveSessionNotFoundError, LiveSessionStateError

    try:
        return _cast_response(store.start_preflight(live_session_id), LiveSessionResponse)
    except LiveSessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"LiveSession not found: {exc.live_session_id}",
        ) from exc
    except LiveSessionStateError as exc:
        raise _translate_state_error(exc, attempted_transition="start_preflight") from exc


@staff_router.post(
    "/sessions/{live_session_id}/preflight",
    response_model=PreflightEvaluation,
    summary="Run the pre-flight checklist evaluator",
    dependencies=[Depends(require_any_role("meeting_operator"))],
    responses={
        422: {"description": "Invalid PreflightInputs payload"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def evaluate_preflight(
    live_session_id: str,
    payload: PreflightInputs,
    preflight_evaluator: Any = Depends(get_preflight_evaluator),
) -> PreflightEvaluation:
    """Run the nine-check pre-flight evaluator.

    The ``live_session_id`` in the URL path MUST match the
    ``live_session_id`` in the request body. Mismatch surfaces as 422
    so the contract is explicit; the evaluator never silently picks
    the path or the body when they disagree.

    No state-machine side effect. Operator clicks "Re-run" -> hits this
    endpoint -> sees the refreshed ``PreflightEvaluation``.
    """
    evaluator = _require_store(preflight_evaluator, surface="pre-flight evaluation")
    if payload.live_session_id != live_session_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Path live_session_id {live_session_id!r} does not match body "
                f"live_session_id {payload.live_session_id!r}."
            ),
        )
    result: PreflightEvaluation = evaluator.evaluate(payload)
    return result


@staff_router.post(
    "/sessions/{live_session_id}/go-on-air",
    response_model=LiveSessionResponse,
    summary="Transition preflight -> on_air (stamps started_at)",
    dependencies=[Depends(require_any_role("meeting_operator"))],
    responses={
        404: {"description": "LiveSession not found"},
        409: {
            "description": (
                "Session is not in preflight state, or a fresh source-bound server-side "
                "pre-flight evaluation did not pass; no broadcast starts."
            )
        },
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def go_on_air(
    live_session_id: str,
    payload: PreflightInputs,
    request: Request,
    takeover_service: TakeoverService | None = Depends(get_takeover_service),
    live_session_store: Any = Depends(get_live_session_store),
    preflight_evaluator: Any = Depends(get_preflight_evaluator),
) -> LiveSessionResponse:
    """Advance the session from ``preflight`` to ``on_air``.

    The store stamps ``started_at`` server-side; the operator UI shows
    "On air for HH:MM:SS" by diffing this timestamp against client time.
    """
    store = _require_store(live_session_store, surface="live session transitions")
    evaluator = _require_store(preflight_evaluator, surface="pre-flight evaluation")
    if payload.live_session_id != live_session_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Path live_session_id {live_session_id!r} does not match body "
                f"live_session_id {payload.live_session_id!r}."
            ),
        )
    current = store.get_session(live_session_id)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"LiveSession not found: {live_session_id}",
        )
    if current.state != "preflight":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "live_session_id": live_session_id,
                "current_state": current.state,
                "attempted_transition": "go_on_air",
            },
        )
    evaluation: PreflightEvaluation = evaluator.evaluate(payload)
    if not evaluation.ready:
        failed_checks = [
            {
                "name": check.name,
                "reason_code": check.reason_code,
                "message": check.message,
            }
            for check in evaluation.checks
            if check.status == "fail"
        ]
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    "Go on air blocked: a fresh source-bound server-side pre-flight did not "
                    "pass. No broadcast was started. Correct the failed checks and run "
                    "pre-flight again."
                ),
                "failed_checks": failed_checks,
            },
        )

    from civiccast.live.store import LiveSessionNotFoundError, LiveSessionStateError

    service = _require_store(takeover_service, surface="live broadcast dispatch")
    identity = _staff_operator(request)
    try:
        takeover = service.prepare_take(
            channel_id=current.channel_id,
            operator_id=identity.operator_id,
            operator_name=identity.operator_display_name,
            path_id=payload.live_source_id,
            reason=f"Live Room session {live_session_id}",
        )
        return _cast_response(
            store.go_on_air(live_session_id, takeover=takeover), LiveSessionResponse
        )
    except AlreadyLiveError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except TakeoverNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except LiveSessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"LiveSession not found: {exc.live_session_id}",
        ) from exc
    except LiveSessionStateError as exc:
        raise _translate_state_error(exc, attempted_transition="go_on_air") from exc


@staff_router.post(
    "/sessions/{live_session_id}/end-broadcast",
    response_model=LiveSessionResponse,
    summary="Transition on_air -> ending (stamps ended_at)",
    dependencies=[Depends(require_any_role("meeting_operator"))],
    responses={
        404: {"description": "LiveSession not found"},
        409: {"description": "Session is not in 'on_air' state"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def end_broadcast(
    live_session_id: str,
    request: Request,
    live_session_store: Any = Depends(get_live_session_store),
) -> LiveSessionResponse:
    """Advance the session from ``on_air`` to ``ending``.

    The router triggers the state advance + ``ended_at`` stamp only. The
    finalization worker (run by the app lifespan in ``inline`` mode, or as a
    separate process in ``external`` mode -- see
    ``docs/ops/finalization-worker-runbook.md``) observes the ``ending``
    session, waits for the recording file to settle, finalizes it into an
    asset at ``recorded``, and packages it. Progress is visible at
    ``GET /api/staff/live/sessions/{live_session_id}/finalization``.
    """
    store = _require_store(live_session_store, surface="live session transitions")

    from civiccast.live.store import LiveSessionNotFoundError, LiveSessionStateError

    try:
        return _cast_response(
            store.end_broadcast(live_session_id, operator_id=_staff_operator(request).operator_id),
            LiveSessionResponse,
        )
    except LiveSessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"LiveSession not found: {exc.live_session_id}",
        ) from exc
    except LiveSessionStateError as exc:
        raise _translate_state_error(exc, attempted_transition="end_broadcast") from exc


# ===========================================================================
# Recording finalization worker status
# ===========================================================================


@staff_router.get(
    "/finalizations",
    response_model=list[LiveFinalizationStatusResponse],
    summary="List recording finalization worker statuses",
    responses={503: {"description": _DB_NOT_READY_DESCRIPTION}},
)
def list_finalization_statuses(
    finalization_worker: Any = Depends(get_live_finalization_worker),
) -> list[LiveFinalizationStatusResponse]:
    """Return operator-visible finalization worker rows."""
    worker = _require_store(finalization_worker, surface="live recording finalization status")
    rows: list[LiveFinalizationStatusResponse] = worker.list_statuses()
    return rows


@staff_router.get(
    "/sessions/{live_session_id}/finalization",
    response_model=LiveFinalizationStatusResponse,
    summary="Get recording finalization status for one session",
    responses={
        404: {"description": "Finalization status not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_finalization_status(
    live_session_id: str,
    finalization_worker: Any = Depends(get_live_finalization_worker),
) -> LiveFinalizationStatusResponse:
    """Return one finalization status row or 404."""
    worker = _require_store(finalization_worker, surface="live recording finalization status")
    status_row = worker.get_status(live_session_id)
    if status_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Finalization status not found: {live_session_id}",
        )
    return _cast_response(status_row, LiveFinalizationStatusResponse)


@staff_router.post(
    "/sessions/{live_session_id}/finalization/retry",
    response_model=LiveFinalizationStatusResponse,
    summary="Re-queue a failed recording finalization",
    dependencies=[Depends(require_any_role("meeting_operator"))],
    responses={
        404: {"description": "Finalization status not found"},
        409: {"description": "Finalization is running or already completed"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def retry_finalization(
    live_session_id: str,
    finalization_worker: Any = Depends(get_live_finalization_worker),
) -> LiveFinalizationStatusResponse:
    """Operator repair surface (Beta B2): give a failed finalization —
    retrying or terminal — a fresh attempt budget. The worker re-attempts it
    on its next scan through the normal settle/retry machinery; fix the
    underlying cause first (see ``failure_reason``)."""

    from civiccast.live.finalization_worker import FinalizationRetryConflictError

    worker = _require_store(finalization_worker, surface="live recording finalization status")
    try:
        status_row = worker.request_retry(live_session_id)
    except FinalizationRetryConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Finalization for {live_session_id} is {exc.state!r}; retry applies "
                "only to failed finalizations."
            ),
        ) from exc
    if status_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Finalization status not found: {live_session_id}",
        )
    return _cast_response(status_row, LiveFinalizationStatusResponse)


# ===========================================================================
# LiveSource CRUD
# ===========================================================================


@staff_router.post(
    "/sources",
    response_model=LiveSourceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a configured live source (RTMP / RTSP / NDI / SRT)",
    dependencies=[Depends(require_any_role("setup_admin"))],
    responses={
        409: {"description": "live_source_id already exists"},
        422: {"description": "Invalid payload"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def create_source(
    payload: LiveSourceCreate,
    live_source_store: Any = Depends(get_live_source_store),
) -> LiveSourceResponse:
    """Persist a new ``LiveSource`` row."""
    store = _require_store(live_source_store, surface="live source CRUD")

    from civiccast.live.store import LiveSourceAlreadyExistsError

    try:
        return _cast_response(store.create(payload), LiveSourceResponse)
    except LiveSourceAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"LiveSource already exists: {exc.live_source_id}",
        ) from exc


@staff_router.get(
    "/sources",
    response_model=list[LiveSourceResponse],
    summary="List configured live sources",
    responses={
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def list_sources(
    channel_id: str | None = None,
    live_source_store: Any = Depends(get_live_source_store),
) -> list[LiveSourceResponse]:
    """Return every ``LiveSource``, optionally filtered by ``channel_id``."""
    store = _require_store(live_source_store, surface="live source CRUD")
    rows: list[LiveSourceResponse] = store.list(channel_id=channel_id)
    return rows


@staff_router.get(
    "/sources/{live_source_id}",
    response_model=LiveSourceResponse,
    summary="Get one live source",
    responses={
        404: {"description": "LiveSource not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_source(
    live_source_id: str,
    live_source_store: Any = Depends(get_live_source_store),
) -> LiveSourceResponse:
    """Return one ``LiveSource`` by id or 404."""
    store = _require_store(live_source_store, surface="live source CRUD")
    result = store.get(live_source_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"LiveSource not found: {live_source_id}",
        )
    return _cast_response(result, LiveSourceResponse)


@staff_router.patch(
    "/sources/{live_source_id}",
    response_model=LiveSourceResponse,
    summary="Edit a configured live source",
    dependencies=[Depends(require_any_role("setup_admin"))],
    responses={
        404: {"description": "LiveSource not found"},
        409: {"description": "The source changed since it was loaded for editing"},
        422: {"description": "Invalid payload, or an endpoint that does not match the type"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def update_source(
    live_source_id: str,
    payload: LiveSourceUpdate,
    live_source_store: Any = Depends(get_live_source_store),
) -> LiveSourceResponse:
    """Apply an operator edit to one configured source.

    Same endpoint/type/credential validation as create, applied to the merged
    row -- changing only ``source_type`` is checked against the endpoint the
    row already holds. Any change to what would actually be probed (endpoint,
    source type, channel, credential reference) clears the source's readiness
    in the same transaction, so an edited source cannot inherit the previous
    address's "ready" and cannot take air until it is checked again.
    """
    store = _require_store(live_source_store, surface="live source CRUD")

    from civiccast.live.store import LiveSourceConcurrencyError, LiveSourceNotFoundError

    try:
        return _cast_response(store.update(live_source_id, payload), LiveSourceResponse)
    except LiveSourceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"LiveSource not found: {live_source_id}",
        ) from exc
    except LiveSourceConcurrencyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"{live_source_id} was changed by someone else while you were editing "
                    "it. Reload the source and reapply your change."
                ),
                "live_source_id": live_source_id,
                "expected_row_version": exc.expected,
                "current_row_version": exc.actual,
            },
        ) from exc
    except ValueError as exc:
        # Endpoint/type/credential validation raised from the merged row rather
        # than from Pydantic's parse of the request body, so FastAPI has not
        # already turned it into a 422.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc


@staff_router.post(
    "/sources/{live_source_id}/probe",
    response_model=LiveSourceProbeResponse,
    summary="Check whether a live source is delivering media right now",
    dependencies=[Depends(require_any_role("meeting_operator", "setup_admin"))],
    responses={
        404: {"description": "LiveSource not found"},
        409: {"description": "The source changed while it was being checked"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def probe_source(
    live_source_id: str,
    readiness_service: Any = Depends(get_live_source_readiness_service),
) -> LiveSourceProbeResponse:
    """Run one bounded server-side media probe and persist what it saw.

    Returns 200 for a failed check, not an error status: "this camera is not
    answering" is a result the operator needs rendered on the source card,
    with its reason and its next action, not an exception that leaves the
    screen showing the previous state. Only a missing source (404), a
    conflicting concurrent edit (409), or missing durable storage (503) is an
    error here.
    """
    service = _require_store(readiness_service, surface="live source readiness")

    from civiccast.live.store import LiveSourceNotFoundError, LiveSourceProbeConflictError

    try:
        source, observation, probed_at = service.probe(live_source_id)
    except LiveSourceNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"LiveSource not found: {live_source_id}",
        ) from exc
    except LiveSourceProbeConflictError as exc:
        # An edit landed while this probe was running. The edit's own write
        # already reset readiness to ``never_probed`` when it changed
        # anything probe-relevant; persisting this probe's verdict on top of
        # that would silently overwrite the newer row with an answer about
        # the address that used to be there. Refuse it the same way a
        # concurrent PATCH-vs-PATCH conflict is refused.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": (
                    f"{live_source_id} was changed while CivicCast was checking it. "
                    "Reload the source and choose Check source again."
                ),
                "live_source_id": live_source_id,
                "reason": exc.reason,
            },
        ) from exc
    return LiveSourceProbeResponse(
        source=_cast_response(source, LiveSourceResponse),
        probed_at=probed_at,
        ok=observation.ok,
        error_code=observation.error_code,
        detail=observation.detail,
    )


# ===========================================================================
# Remote ingest / relay target CRUD
# ===========================================================================


@staff_router.post(
    "/relay-configs",
    response_model=LiveRelayConfigResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an optional remote ingest or relay target",
    dependencies=[Depends(require_any_role("setup_admin"))],
    responses={
        409: {"description": "relay_config_id already exists"},
        422: {"description": "Invalid payload"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def create_relay_config(
    payload: LiveRelayConfigCreate,
    live_relay_config_store: Any = Depends(get_live_relay_config_store),
) -> LiveRelayConfigResponse:
    """Persist an optional relay config.

    No row is required for the default local RTMP workflow. Operators add rows
    only when they intentionally configure outbound cloud relay or direct
    syndication.
    """
    store = _require_store(live_relay_config_store, surface="live relay config CRUD")

    from civiccast.live.store import LiveRelayConfigAlreadyExistsError

    try:
        return _cast_response(store.create(payload), LiveRelayConfigResponse)
    except LiveRelayConfigAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"LiveRelayConfig already exists: {exc.relay_config_id}",
        ) from exc


@staff_router.get(
    "/ingest-plan",
    response_model=LiveIngestPlan,
    summary="Build the current live ingest plan for a channel",
    responses={
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_ingest_plan(
    channel_id: str,
    live_relay_config_store: Any = Depends(get_live_relay_config_store),
    live_source_store: Any = Depends(get_live_source_store),
) -> LiveIngestPlan:
    """Return the operator's configured sources plus optional relay/direct paths.

    Bug B5: this used to return only ``local_default`` (a hardcoded, never-
    listening RTMP placeholder) plus relay rows -- the channel's actual
    ``LiveSource`` rows (what Run Meeting and pre-flight already treat as
    the station's real inputs) were never consulted, so live-takeover could
    never select what an operator had actually configured.
    """
    relay_store = _require_store(live_relay_config_store, surface="live ingest plan")
    relay_rows: list[LiveRelayConfigResponse] = relay_store.list(
        channel_id=channel_id, enabled=True
    )
    source_store = _require_store(live_source_store, surface="live ingest plan")
    source_rows = source_store.list(channel_id=channel_id)
    return build_ingest_plan(channel_id, relay_rows, live_sources=source_rows)


@staff_router.get(
    "/relay-configs",
    response_model=list[LiveRelayConfigResponse],
    summary="List optional remote ingest or relay targets",
    responses={
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def list_relay_configs(
    channel_id: str | None = None,
    enabled: bool | None = None,
    live_relay_config_store: Any = Depends(get_live_relay_config_store),
) -> list[LiveRelayConfigResponse]:
    """Return relay configs, optionally filtered by channel or enabled state."""
    store = _require_store(live_relay_config_store, surface="live relay config CRUD")
    rows: list[LiveRelayConfigResponse] = store.list(channel_id=channel_id, enabled=enabled)
    return rows


@staff_router.get(
    "/relay-configs/{relay_config_id}",
    response_model=LiveRelayConfigResponse,
    summary="Get one remote ingest or relay target",
    responses={
        404: {"description": "LiveRelayConfig not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_relay_config(
    relay_config_id: str,
    live_relay_config_store: Any = Depends(get_live_relay_config_store),
) -> LiveRelayConfigResponse:
    """Return one relay config by id or 404."""
    store = _require_store(live_relay_config_store, surface="live relay config CRUD")
    result = store.get(relay_config_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"LiveRelayConfig not found: {relay_config_id}",
        )
    return _cast_response(result, LiveRelayConfigResponse)


@staff_router.post(
    "/relay-configs/{relay_config_id}/health",
    response_model=LiveRelayConfigResponse,
    summary="Update relay health from a station probe",
    dependencies=[Depends(require_any_role("setup_admin"))],
    responses={
        404: {"description": "LiveRelayConfig not found"},
        422: {"description": "Invalid payload"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def update_relay_health(
    relay_config_id: str,
    payload: LiveRelayHealthUpdate,
    live_relay_config_store: Any = Depends(get_live_relay_config_store),
) -> LiveRelayConfigResponse:
    """Update operator-visible remote ingest health state."""
    store = _require_store(live_relay_config_store, surface="live relay health")

    from civiccast.live.store import LiveRelayConfigNotFoundError

    try:
        return _cast_response(
            store.update_health(relay_config_id, payload),
            LiveRelayConfigResponse,
        )
    except LiveRelayConfigNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"LiveRelayConfig not found: {exc.relay_config_id}",
        ) from exc


# ===========================================================================
# RecordingTarget CRUD
# ===========================================================================


@staff_router.post(
    "/recording-targets",
    response_model=RecordingTargetResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a recording target",
    dependencies=[Depends(require_any_role("setup_admin"))],
    responses={
        409: {"description": "recording_target_id already exists"},
        422: {"description": "Invalid payload"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def create_recording_target(
    payload: RecordingTargetCreate,
    recording_target_store: Any = Depends(get_recording_target_store),
) -> RecordingTargetResponse:
    """Persist a new ``RecordingTarget`` row."""
    store = _require_store(recording_target_store, surface="recording target CRUD")

    from civiccast.live.store import RecordingTargetAlreadyExistsError

    try:
        return _cast_response(store.create(payload), RecordingTargetResponse)
    except RecordingTargetAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"RecordingTarget already exists: {exc.recording_target_id}",
        ) from exc


@staff_router.get(
    "/recording-targets",
    response_model=list[RecordingTargetResponse],
    summary="List recording targets",
    responses={
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def list_recording_targets(
    recording_target_store: Any = Depends(get_recording_target_store),
) -> list[RecordingTargetResponse]:
    """Return every ``RecordingTarget``."""
    store = _require_store(recording_target_store, surface="recording target CRUD")
    rows: list[RecordingTargetResponse] = store.list()
    return rows


@staff_router.get(
    "/recording-targets/{recording_target_id}",
    response_model=RecordingTargetResponse,
    summary="Get one recording target",
    responses={
        404: {"description": "RecordingTarget not found"},
        503: {"description": _DB_NOT_READY_DESCRIPTION},
    },
)
def get_recording_target(
    recording_target_id: str,
    recording_target_store: Any = Depends(get_recording_target_store),
) -> RecordingTargetResponse:
    """Return one ``RecordingTarget`` by id or 404."""
    store = _require_store(recording_target_store, surface="recording target CRUD")
    result = store.get(recording_target_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"RecordingTarget not found: {recording_target_id}",
        )
    return _cast_response(result, RecordingTargetResponse)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _cast_response[T: BaseModel](value: Any, model: type[T]) -> T:
    """Cast the store's return -- typed ``Any`` because the DI seams are
    ``Any`` to dodge a router/store circular import -- to the declared
    response model.

    The store-side type is already the response model class (or in the
    case of ``preflight_evaluator.evaluate`` a ``PreflightEvaluation``);
    this helper exists purely to satisfy mypy without re-importing the
    store types at module top.
    """
    if isinstance(value, model):
        return value
    return model.model_validate(value)
