# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S24 underwriting / sponsorship-spot management API surface.

Three surface families over the ``UnderwritingStore`` + ``AffidavitService`` DI seam:

* **Staff spot CRUD** (``/api/staff/underwriting/spots[/{spot_id}]``) —
  ``publish_operator`` / ``setup_admin`` manage underwriting spots. Each spot
  carries the operator's editorial 47 CFR 73.503 attestation
  (``fcc_compliant_ack``); the route does NOT police content, that's human
  review. Delete cascades to flights and placements (loose-ref convention; the
  store does the transactional cleanup).

* **Staff flight CRUD + placement view**
  (``/api/staff/underwriting/flights[/{flight_id}]``,
  ``/api/staff/underwriting/placements``) — ``publish_operator`` /
  ``setup_admin``. The placements GET is the operator's
  upcoming-and-aired-insertions-per-channel view (DC-1) — read-only here; the
  trafficking compiler in :mod:`civiccast.underwriting.service` writes them.

* **Affidavits** (``/api/staff/underwriting/affidavits``) — ``support_admin``
  read (franchise-compliance billing surface). Joins S23's ``as_run_log``
  filtered to ``source_kind="spot"`` through the underwriting → placement
  chain to attribute aired seconds back to one underwriter over a period
  (DC-3). Companion ``.../export`` returns CSV / XML / PDF for billing.

The DI seams (``get_underwriting_store`` / ``get_affidavit_service``) return
``None`` at import so the module opens no database; the app factory overrides
them in ``_wire_durable_stores``. An unwired surface is a 503 — never a
silent 200 against storage that is not there. ``x-required-roles`` is
surfaced into the generated OpenAPI so the published contract cannot drift
from the runtime role gate.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from pydantic import ValidationError

from civiccast.auth.roles import require_any_role
from civiccast.underwriting.models import (
    SpotFlight,
    SpotFlightInput,
    SpotFlightUpdate,
    SpotPlacement,
    UnderwritingSpot,
    UnderwritingSpotInput,
    UnderwritingSpotUpdate,
)
from civiccast.underwriting.service import (
    AffidavitService,
    CandidateBreakSlot,
    CompileResult,
    TraffickingCompiler,
    UnderwriterAffidavit,
    export_affidavit_csv,
    export_affidavit_pdf,
    export_affidavit_xml,
)
from civiccast.underwriting.store import (
    FlightNotFoundError,
    SpotNotFoundError,
    UnderwritingStore,
)

_DB_NOT_READY = "Durable storage is not ready yet."

# Spec §4 roles. publish_operator + setup_admin manage spots / flights;
# support_admin reads franchise-compliance affidavits.
_MANAGE = ("publish_operator", "setup_admin")
_AFFIDAVIT_READ = ("support_admin",)

_MANAGE_EXTRA = {"x-required-roles": list(_MANAGE)}
_AFFIDAVIT_READ_EXTRA = {"x-required-roles": list(_AFFIDAVIT_READ)}

_DEFAULT_STATION_ID = "civiccast-station"

# Station-policy gate (DC-5). When ``CIVICCAST_REQUIRE_FCC_ACK=1`` the create /
# patch routes refuse to persist a spot whose ``fcc_compliant_ack`` is False —
# the operator must explicitly attest 47 CFR 73.503 (sponsor-ID only; no CTA /
# price / qualitative claims) before the spot is schedulable. Stations that
# leave the env unset (or set to "0") get the legacy behavior: the ack is
# stored as posted but not enforced at the API gate. Code does NOT inspect the
# asset for content — content review is human.
_REQUIRE_FCC_ACK_ENV = "CIVICCAST_REQUIRE_FCC_ACK"
_FCC_ACK_REQUIRED_DETAIL = (
    "Station policy requires fcc_compliant_ack=true (CIVICCAST_REQUIRE_FCC_ACK=1): "
    "the operator must attest the spot meets 47 CFR 73.503 (sponsor-ID only; "
    "no calls-to-action, price, or qualitative claims) before it can be saved."
)


def _station_id() -> str:
    """The active station id (single-station deployment; env-overridable)."""
    import os

    return os.environ.get("CIVICCAST_STATION_ID") or _DEFAULT_STATION_ID


def _fcc_ack_required() -> bool:
    """Read the station-policy env knob fresh each call (per-request semantics)."""
    import os

    return os.environ.get(_REQUIRE_FCC_ACK_ENV) == "1"


def _enforce_fcc_ack_policy(ack: bool) -> None:
    """Raise 422 if the station requires the FCC ack and this spot lacks it."""
    if _fcc_ack_required() and not ack:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_FCC_ACK_REQUIRED_DETAIL)


# --- DI seams (overridden by the app factory) --------------------------------


def get_underwriting_store() -> UnderwritingStore | None:
    return None


def get_affidavit_service() -> AffidavitService | None:
    return None


def get_trafficking_compiler() -> TraffickingCompiler | None:
    """DI seam for the trafficking compiler — overridden by the app factory.

    The default ``None`` matches the rest of the underwriting surface's
    fail-closed posture: an unwired compile endpoint returns 503 (never a
    silent 200 against missing storage)."""
    return None


def _require_store(store: UnderwritingStore | None) -> UnderwritingStore:
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return store


def _require_affidavit_service(svc: AffidavitService | None) -> AffidavitService:
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return svc


def _require_trafficking_compiler(
    compiler: TraffickingCompiler | None,
) -> TraffickingCompiler:
    if compiler is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return compiler


# --- routers -----------------------------------------------------------------

staff_router = APIRouter(prefix="/api/staff", tags=["staff", "underwriting"])


# --- spot CRUD --------------------------------------------------------------


@staff_router.get(
    "/underwriting/spots",
    response_model=list[UnderwritingSpot],
    summary="List underwriting spots for the station",
    dependencies=[Depends(require_any_role(*_MANAGE))],
    openapi_extra=_MANAGE_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def list_underwriting_spots(
    underwriter: str | None = Query(None, description="Filter to one underwriter (exact match)"),
    store: UnderwritingStore | None = Depends(get_underwriting_store),
) -> list[UnderwritingSpot]:
    """All spots for the active station, ordered by underwriter then spot_id.

    Optional ``underwriter`` filter narrows to one sponsoring entity — useful
    when an operator is preparing a per-underwriter compliance review.
    """
    return _require_store(store).list_spots(_station_id(), underwriter=underwriter)


@staff_router.post(
    "/underwriting/spots",
    response_model=UnderwritingSpot,
    status_code=status.HTTP_201_CREATED,
    summary="Create an underwriting spot",
    dependencies=[Depends(require_any_role(*_MANAGE))],
    openapi_extra=_MANAGE_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def create_underwriting_spot(
    payload: UnderwritingSpotInput,
    store: UnderwritingStore | None = Depends(get_underwriting_store),
) -> UnderwritingSpot:
    """Create one spot. ``fcc_compliant_ack`` defaults to False; the operator
    must explicitly attest the spot meets 47 CFR 73.503 (sponsor-ID only — no
    CTAs / price / qualitative claims). Content is not inspected — that's a
    human editorial gate (DC-5).

    Station policy: when ``CIVICCAST_REQUIRE_FCC_ACK=1`` is set, a spot whose
    ``fcc_compliant_ack`` is False is rejected with 422 instead of being
    persisted (the spec §6 / DC-5 "configurable" knob).

    Q-4: a POST that targets an existing ``spot_id`` returns 409 Conflict —
    the store would otherwise upsert silently, retroactively overwriting
    another underwriter's spot when an operator typos the id. Use PATCH to
    update an existing spot."""
    _enforce_fcc_ack_policy(payload.fcc_compliant_ack)
    resolved = _require_store(store)
    if resolved.get_spot(payload.spot_id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(f"Underwriting spot {payload.spot_id!r} already exists. Use PATCH to update."),
        )
    return resolved.upsert_spot(UnderwritingSpot(**payload.model_dump()))


@staff_router.get(
    "/underwriting/spots/{spot_id}",
    response_model=UnderwritingSpot,
    summary="Get one underwriting spot",
    dependencies=[Depends(require_any_role(*_MANAGE))],
    openapi_extra=_MANAGE_EXTRA,
    responses={
        404: {"description": "Underwriting spot not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def get_underwriting_spot(
    spot_id: str,
    store: UnderwritingStore | None = Depends(get_underwriting_store),
) -> UnderwritingSpot:
    spot = _require_store(store).get_spot(spot_id)
    if spot is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Underwriting spot {spot_id!r} not found."
        )
    return spot


@staff_router.patch(
    "/underwriting/spots/{spot_id}",
    response_model=UnderwritingSpot,
    summary="Patch an underwriting spot (absent keys unchanged)",
    dependencies=[Depends(require_any_role(*_MANAGE))],
    openapi_extra=_MANAGE_EXTRA,
    responses={
        404: {"description": "Underwriting spot not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def patch_underwriting_spot(
    spot_id: str,
    payload: UnderwritingSpotUpdate,
    store: UnderwritingStore | None = Depends(get_underwriting_store),
) -> UnderwritingSpot:
    """Patch a spot.

    Patch semantics: an absent key leaves the stored field unchanged; an
    explicit ``null`` clears the field (e.g. ``{"review_notes": null}`` sets
    ``review_notes`` to ``None``). ``spot_id`` / ``station_id`` are set at
    creation and not editable here.
    """
    resolved = _require_store(store)
    current = resolved.get_spot(spot_id)
    if current is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Underwriting spot {spot_id!r} not found."
        )
    updates = payload.model_dump(exclude_unset=True)
    merged = current.model_copy(update=updates)
    # Station policy: a patch that lands the spot at fcc_compliant_ack=False
    # while CIVICCAST_REQUIRE_FCC_ACK=1 is refused with 422.
    _enforce_fcc_ack_policy(merged.fcc_compliant_ack)
    return resolved.upsert_spot(merged)


@staff_router.delete(
    "/underwriting/spots/{spot_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an underwriting spot (cascades flights + placements)",
    dependencies=[Depends(require_any_role(*_MANAGE))],
    openapi_extra=_MANAGE_EXTRA,
    responses={
        404: {"description": "Underwriting spot not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def delete_underwriting_spot(
    spot_id: str,
    store: UnderwritingStore | None = Depends(get_underwriting_store),
) -> Response:
    """Delete a spot AND every flight + placement that referenced it.

    The cascade is performed in a single transaction by the store: the loose-
    ref convention has no DB foreign key, so orphan flights or placements
    would silently break the affidavit join. A 404 is returned when the spot
    does not exist (never a silent no-op).
    """
    try:
        _require_store(store).delete_spot(spot_id)
    except SpotNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- flight CRUD ------------------------------------------------------------


@staff_router.get(
    "/underwriting/flights",
    response_model=list[SpotFlight],
    summary="List flights, optionally narrowed by spot or active date",
    dependencies=[Depends(require_any_role(*_MANAGE))],
    openapi_extra=_MANAGE_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def list_spot_flights(
    spot_id: str | None = Query(None, description="Filter to flights for one spot"),
    active_on: str | None = Query(
        None,
        description="ISO date (YYYY-MM-DD) — narrow to flights whose [start, end] window covers this date.",
    ),
    store: UnderwritingStore | None = Depends(get_underwriting_store),
) -> list[SpotFlight]:
    """Flights, ordered by start_date then flight_id.

    ``active_on`` is the trafficking-compiler hot path: returns flights whose
    inclusive ``[start_date, end_date]`` window covers the given date.
    """
    from datetime import date as _date

    parsed: _date | None = None
    if active_on is not None:
        try:
            parsed = _date.fromisoformat(active_on)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"active_on must be ISO date (YYYY-MM-DD); got {active_on!r}.",
            ) from exc
    return _require_store(store).list_flights(spot_id=spot_id, active_on=parsed)


@staff_router.post(
    "/underwriting/flights",
    response_model=SpotFlight,
    status_code=status.HTTP_201_CREATED,
    summary="Create a flight",
    dependencies=[Depends(require_any_role(*_MANAGE))],
    openapi_extra=_MANAGE_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def create_spot_flight(
    payload: SpotFlightInput,
    store: UnderwritingStore | None = Depends(get_underwriting_store),
) -> SpotFlight:
    """Q-4 parity: a POST that targets an existing ``flight_id`` returns 409
    Conflict; use PATCH to update."""
    resolved = _require_store(store)
    if resolved.get_flight(payload.flight_id) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(f"Spot flight {payload.flight_id!r} already exists. Use PATCH to update."),
        )
    return resolved.upsert_flight(SpotFlight(**payload.model_dump()))


@staff_router.get(
    "/underwriting/flights/{flight_id}",
    response_model=SpotFlight,
    summary="Get one flight",
    dependencies=[Depends(require_any_role(*_MANAGE))],
    openapi_extra=_MANAGE_EXTRA,
    responses={
        404: {"description": "Spot flight not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def get_spot_flight(
    flight_id: str,
    store: UnderwritingStore | None = Depends(get_underwriting_store),
) -> SpotFlight:
    flight = _require_store(store).get_flight(flight_id)
    if flight is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Spot flight {flight_id!r} not found."
        )
    return flight


@staff_router.patch(
    "/underwriting/flights/{flight_id}",
    response_model=SpotFlight,
    summary="Patch a flight (absent keys unchanged)",
    dependencies=[Depends(require_any_role(*_MANAGE))],
    openapi_extra=_MANAGE_EXTRA,
    responses={
        404: {"description": "Spot flight not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def patch_spot_flight(
    flight_id: str,
    payload: SpotFlightUpdate,
    store: UnderwritingStore | None = Depends(get_underwriting_store),
) -> SpotFlight:
    """Patch semantics: absent key unchanged, explicit ``null`` clears.
    ``flight_id`` / ``spot_id`` are set at creation."""
    resolved = _require_store(store)
    current = resolved.get_flight(flight_id)
    if current is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Spot flight {flight_id!r} not found."
        )
    updates = payload.model_dump(exclude_unset=True)
    merged = current.model_copy(update=updates)
    # Q-2: ``model_copy`` skips validators, so a partial patch that pushes
    # ``end_date`` before ``start_date`` (or vice versa) would otherwise hit
    # the DB CHECK constraint as an IntegrityError → 500. Re-validate the
    # merged shape so the operator gets a clean 422 with the per-field
    # message before any SQL fires.
    try:
        merged = SpotFlight.model_validate(merged.model_dump())
    except ValidationError as exc:
        # Strip the ``ctx`` (carries the raw ValueError) and ``input`` (raw
        # date objects) so the 422 body is plain JSON.
        errors = [
            {"loc": err.get("loc"), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors) from exc
    return resolved.upsert_flight(merged)


@staff_router.delete(
    "/underwriting/flights/{flight_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a flight (cascades placements)",
    dependencies=[Depends(require_any_role(*_MANAGE))],
    openapi_extra=_MANAGE_EXTRA,
    responses={
        404: {"description": "Spot flight not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def delete_spot_flight(
    flight_id: str,
    store: UnderwritingStore | None = Depends(get_underwriting_store),
) -> Response:
    try:
        _require_store(store).delete_flight(flight_id)
    except FlightNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- placement view ---------------------------------------------------------


@staff_router.get(
    "/underwriting/placements",
    response_model=list[SpotPlacement],
    summary="What will / did air — placements over a window",
    dependencies=[Depends(require_any_role(*_MANAGE))],
    openapi_extra=_MANAGE_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def list_spot_placements(
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    channel_id: str | None = Query(None, alias="channel"),
    flight_id: str | None = Query(None, alias="flight"),
    store: UnderwritingStore | None = Depends(get_underwriting_store),
) -> list[SpotPlacement]:
    """Placements over the half-open ``[from, to)`` window on ``scheduled_at``.

    Optional ``channel`` + ``flight`` filters. Read-only here — placements are
    materialized by the trafficking compiler (slice 2).
    """
    return _require_store(store).list_placements(
        channel_id=channel_id, flight_id=flight_id, from_ts=from_ts, to_ts=to_ts
    )


# --- affidavits -------------------------------------------------------------


@staff_router.get(
    "/underwriting/affidavits",
    response_model=UnderwriterAffidavit,
    summary="Per-underwriter proof-of-airing for a period (billing-ready, DC-3)",
    dependencies=[Depends(require_any_role(*_AFFIDAVIT_READ))],
    openapi_extra=_AFFIDAVIT_READ_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def get_underwriter_affidavit(
    underwriter: str = Query(..., description="Exact sponsoring-entity name"),
    period_start: str = Query(..., alias="from", description="ISO date (YYYY-MM-DD); inclusive"),
    period_end: str = Query(..., alias="to", description="ISO date (YYYY-MM-DD); inclusive"),
    svc: AffidavitService | None = Depends(get_affidavit_service),
) -> UnderwriterAffidavit:
    """Join S23's ``as_run_log`` filtered to ``source_kind="spot"`` through
    placements / flights / spots → list of airings + totals for one
    underwriter over an inclusive day range."""
    from datetime import date as _date

    try:
        from_date = _date.fromisoformat(period_start)
        to_date = _date.fromisoformat(period_end)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"from/to must be ISO date (YYYY-MM-DD); got {period_start!r} / {period_end!r}.",
        ) from exc
    # Q-5: an inverted period silently returns an empty affidavit, which is
    # indistinguishable from "this underwriter had no airings" — reject it.
    if from_date > to_date:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="from must be on or before to",
        )
    return _require_affidavit_service(svc).for_underwriter(
        station_id=_station_id(),
        underwriter=underwriter,
        period_start=from_date,
        period_end=to_date,
    )


@staff_router.get(
    "/underwriting/affidavits/export",
    summary="Download an affidavit as CSV / XML / PDF for billing",
    dependencies=[Depends(require_any_role(*_AFFIDAVIT_READ))],
    openapi_extra=_AFFIDAVIT_READ_EXTRA,
    responses={
        200: {
            "description": "Affidavit document (CSV / XML / PDF)",
            "content": {"text/csv": {}, "application/xml": {}, "application/pdf": {}},
        },
        503: {"description": _DB_NOT_READY},
    },
)
def export_underwriter_affidavit(
    underwriter: str = Query(...),
    period_start: str = Query(..., alias="from"),
    period_end: str = Query(..., alias="to"),
    fmt: Literal["csv", "xml", "pdf"] = Query(..., alias="format"),
    svc: AffidavitService | None = Depends(get_affidavit_service),
) -> Response:
    """``format=csv|xml|pdf`` → file download. The CSV / XML are deterministic
    over the affidavit; PDF is a single-page billing-ready document.
    """
    from datetime import date as _date

    try:
        from_date = _date.fromisoformat(period_start)
        to_date = _date.fromisoformat(period_end)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"from/to must be ISO date (YYYY-MM-DD); got {period_start!r} / {period_end!r}.",
        ) from exc
    if from_date > to_date:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="from must be on or before to",
        )
    affidavit = _require_affidavit_service(svc).for_underwriter(
        station_id=_station_id(),
        underwriter=underwriter,
        period_start=from_date,
        period_end=to_date,
    )
    if fmt == "csv":
        body: str | bytes = export_affidavit_csv(affidavit)
        media_type = "text/csv"
    elif fmt == "xml":
        body = export_affidavit_xml(affidavit)
        media_type = "application/xml"
    else:
        body = export_affidavit_pdf(affidavit)
        media_type = "application/pdf"
    # E-2 / Q-3: build the Content-Disposition with an ASCII-only ``filename``
    # token (no underwriter interpolation — quotes / CR-LF / semicolons in the
    # name would otherwise inject extra parameters or split the header) plus
    # a UTF-8 percent-encoded ``filename*`` (RFC 5987 / RFC 6266) carrying
    # the underwriter for clients that honor the extension.
    safe_underwriter = quote(underwriter, safe="")
    ascii_filename = f"affidavit-{period_start}-{period_end}.{fmt}"
    encoded_filename = f"affidavit-{safe_underwriter}-{period_start}-{period_end}.{fmt}"
    disposition = f"attachment; filename=\"{ascii_filename}\"; filename*=UTF-8''{encoded_filename}"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )


# --- operator-triggered trafficking compile --------------------------------


@staff_router.post(
    "/underwriting/compile",
    response_model=CompileResult,
    summary="Run the trafficking compiler for one date over operator-supplied candidates",
    dependencies=[Depends(require_any_role(*_MANAGE))],
    openapi_extra=_MANAGE_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def compile_trafficking_for_date(
    for_date: date = Body(..., embed=True, description="ISO date the candidate slots cover"),
    candidates: list[CandidateBreakSlot] = Body(
        ..., embed=True, description="Program-log break slots the compiler should fill"
    ),
    local_tz_offset_minutes: int = Body(
        0,
        embed=True,
        description="Operator's offset from UTC in minutes (daypart filter input)",
    ),
    compiler: TraffickingCompiler | None = Depends(get_trafficking_compiler),
) -> CompileResult:
    """Operator-triggered compile (T-1).

    The compiler is otherwise driven by the schedule worker (slice 4 follow-up);
    this endpoint exposes the same code path for ad-hoc operator "Recompute"
    actions and integration tests. The DI seam (``get_trafficking_compiler``)
    returns ``None`` at import; the app factory's ``_wire_durable_stores``
    overrides it with a compiler bound to the same underwriting store + an
    ``AutoScheduleStore``-backed daypart resolver, so DC-1 daypart enforcement
    is honored in production (E-3).
    """
    resolved = _require_trafficking_compiler(compiler)
    return resolved.compile_for_date(
        for_date=for_date,
        candidates=candidates,
        local_tz_offset_minutes=local_tz_offset_minutes,
    )


__all__ = [
    "get_affidavit_service",
    "get_trafficking_compiler",
    "get_underwriting_store",
    "staff_router",
]
