# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Item 23 staff API: series applications, volunteer roster, call sheets,
equipment roster + checkouts, training badges, and access rules.

Staff-only surface (no public routes — these are internal station-ops
records, unlike ``civiccast.contribute``'s public intake). Gated the same
way as the existing ``civiccast.contribute`` staff routes
(``publish_operator`` / ``meeting_operator`` / ``support_admin``) since no
dedicated "producer ops" role exists yet in
:data:`civiccast.auth.roles.ALL_OPERATOR_ROLES` — flagged as a product-shape
decision in the PR rather than inventing a new role silently.

DI seam (``get_producer_ops_store``) returns ``None`` at import so the
module opens no database; the app factory overrides it in
``_wire_durable_stores``. An unwired surface is a 503, never a silent 200.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from civiccast.auth.roles import require_any_role
from civiccast.producer_ops.models import (
    CallSheet,
    CallSheetAssignment,
    EquipmentAccessRule,
    EquipmentCheckout,
    EquipmentItem,
    SeriesApplication,
    SeriesApplicationState,
    TrainingBadge,
    VolunteerRole,
)
from civiccast.producer_ops.store import (
    CallSheetAlreadyExistsError,
    CallSheetAssignmentAlreadyExistsError,
    CallSheetNotFoundError,
    CheckoutNotFoundError,
    EquipmentAlreadyCheckedOutError,
    EquipmentNotFoundError,
    MissingRequiredBadgeError,
    ProducerOpsStore,
    SeriesApplicationAlreadyExistsError,
    SeriesApplicationNotFoundError,
    TrainingBadgeAlreadyExistsError,
    VolunteerNotFoundError,
)

_STAFF_ROLES = ("publish_operator", "meeting_operator", "support_admin")
_DB_NOT_READY = "Durable storage is not ready yet."

staff_router = APIRouter(
    prefix="/api/staff/producer-ops",
    tags=["staff", "producer-ops"],
    dependencies=[Depends(require_any_role(*_STAFF_ROLES))],
)


def get_producer_ops_store() -> ProducerOpsStore | None:
    """DI seam — overridden by ``_wire_durable_stores`` in the app factory."""
    return None


def get_store(store: ProducerOpsStore | None = Depends(get_producer_ops_store)) -> ProducerOpsStore:
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return store


# --- series applications ------------------------------------------------


class SeriesApplicationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: SeriesApplicationState
    review_notes: str | None = None
    series_id: str | None = None


@staff_router.post(
    "/series-applications",
    response_model=SeriesApplication,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a producer's request for a recurring series slot",
    responses={409: {"description": "application_id already exists"}},
)
def create_series_application(
    application: SeriesApplication,
    store: ProducerOpsStore = Depends(get_store),
) -> SeriesApplication:
    try:
        return store.create_series_application(application)
    except SeriesApplicationAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.get(
    "/series-applications",
    response_model=list[SeriesApplication],
    summary="List producer series applications",
)
def list_series_applications(
    store: ProducerOpsStore = Depends(get_store),
) -> list[SeriesApplication]:
    return store.list_series_applications()


@staff_router.get(
    "/series-applications/{application_id}",
    response_model=SeriesApplication,
    summary="Read one series application",
    responses={404: {"description": "Series application not found"}},
)
def read_series_application(
    application_id: str,
    store: ProducerOpsStore = Depends(get_store),
) -> SeriesApplication:
    try:
        return store.get_series_application(application_id)
    except SeriesApplicationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@staff_router.post(
    "/series-applications/{application_id}/review",
    response_model=SeriesApplication,
    summary="Apply a staff review decision to a series application",
    responses={404: {"description": "Series application not found"}},
)
def review_series_application(
    application_id: str,
    payload: SeriesApplicationReviewRequest,
    store: ProducerOpsStore = Depends(get_store),
) -> SeriesApplication:
    try:
        return store.review_series_application(
            application_id,
            state=payload.state,
            review_notes=payload.review_notes,
            series_id=payload.series_id,
        )
    except SeriesApplicationNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# --- volunteer roster -----------------------------------------------------


@staff_router.put(
    "/volunteers/{volunteer_id}",
    response_model=VolunteerRole,
    summary="Create or update a volunteer roster entry",
)
def upsert_volunteer(
    volunteer_id: str,
    volunteer: VolunteerRole,
    store: ProducerOpsStore = Depends(get_store),
) -> VolunteerRole:
    if volunteer.volunteer_id != volunteer_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Path volunteer_id must match body volunteer_id.",
        )
    return store.upsert_volunteer(volunteer)


@staff_router.get(
    "/volunteers",
    response_model=list[VolunteerRole],
    summary="List the volunteer roster",
)
def list_volunteers(
    active_only: bool = False,
    store: ProducerOpsStore = Depends(get_store),
) -> list[VolunteerRole]:
    return store.list_volunteers(active_only=active_only)


@staff_router.get(
    "/volunteers/{volunteer_id}",
    response_model=VolunteerRole,
    summary="Read one volunteer roster entry",
    responses={404: {"description": "Volunteer not found"}},
)
def read_volunteer(
    volunteer_id: str,
    store: ProducerOpsStore = Depends(get_store),
) -> VolunteerRole:
    try:
        return store.get_volunteer(volunteer_id)
    except VolunteerNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# --- call sheets ------------------------------------------------------------


@staff_router.post(
    "/call-sheets",
    response_model=CallSheet,
    status_code=status.HTTP_201_CREATED,
    summary="Create a call sheet",
    responses={409: {"description": "call_sheet_id already exists"}},
)
def create_call_sheet(
    call_sheet: CallSheet,
    store: ProducerOpsStore = Depends(get_store),
) -> CallSheet:
    try:
        return store.create_call_sheet(call_sheet)
    except CallSheetAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.get(
    "/call-sheets",
    response_model=list[CallSheet],
    summary="List call sheets",
)
def list_call_sheets(store: ProducerOpsStore = Depends(get_store)) -> list[CallSheet]:
    return store.list_call_sheets()


@staff_router.get(
    "/call-sheets/{call_sheet_id}",
    response_model=CallSheet,
    summary="Read one call sheet",
    responses={404: {"description": "Call sheet not found"}},
)
def read_call_sheet(
    call_sheet_id: str,
    store: ProducerOpsStore = Depends(get_store),
) -> CallSheet:
    try:
        return store.get_call_sheet(call_sheet_id)
    except CallSheetNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@staff_router.post(
    "/call-sheets/{call_sheet_id}/assignments",
    response_model=CallSheetAssignment,
    status_code=status.HTTP_201_CREATED,
    summary="Assign a volunteer's crew role on a call sheet",
    responses={
        404: {"description": "Call sheet or volunteer not found"},
        409: {"description": "assignment_id already exists"},
    },
)
def add_call_sheet_assignment(
    call_sheet_id: str,
    assignment: CallSheetAssignment,
    store: ProducerOpsStore = Depends(get_store),
) -> CallSheetAssignment:
    if assignment.call_sheet_id != call_sheet_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Path call_sheet_id must match body call_sheet_id.",
        )
    try:
        return store.add_call_sheet_assignment(assignment)
    except (CallSheetNotFoundError, VolunteerNotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CallSheetAssignmentAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.get(
    "/call-sheets/{call_sheet_id}/assignments",
    response_model=list[CallSheetAssignment],
    summary="List crew assignments on a call sheet",
)
def list_call_sheet_assignments(
    call_sheet_id: str,
    store: ProducerOpsStore = Depends(get_store),
) -> list[CallSheetAssignment]:
    return store.list_call_sheet_assignments(call_sheet_id)


# --- equipment roster + checkouts -------------------------------------------


@staff_router.put(
    "/equipment/{equipment_id}",
    response_model=EquipmentItem,
    summary="Create or update an equipment roster entry",
)
def upsert_equipment(
    equipment_id: str,
    item: EquipmentItem,
    store: ProducerOpsStore = Depends(get_store),
) -> EquipmentItem:
    if item.equipment_id != equipment_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Path equipment_id must match body equipment_id.",
        )
    return store.upsert_equipment(item)


@staff_router.get(
    "/equipment",
    response_model=list[EquipmentItem],
    summary="List the equipment roster",
)
def list_equipment(
    active_only: bool = False,
    store: ProducerOpsStore = Depends(get_store),
) -> list[EquipmentItem]:
    return store.list_equipment(active_only=active_only)


@staff_router.put(
    "/equipment/{equipment_id}/access-rule",
    response_model=EquipmentAccessRule,
    summary="Set the training badge required to check out an equipment item",
    responses={404: {"description": "Equipment not found"}},
)
def set_access_rule(
    equipment_id: str,
    rule: EquipmentAccessRule,
    store: ProducerOpsStore = Depends(get_store),
) -> EquipmentAccessRule:
    if rule.equipment_id != equipment_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Path equipment_id must match body equipment_id.",
        )
    try:
        return store.set_access_rule(rule)
    except EquipmentNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@staff_router.get(
    "/equipment/{equipment_id}/access-rule",
    response_model=EquipmentAccessRule | None,
    summary="Read the access rule for an equipment item, if any",
)
def read_access_rule(
    equipment_id: str,
    store: ProducerOpsStore = Depends(get_store),
) -> EquipmentAccessRule | None:
    return store.get_access_rule(equipment_id)


@staff_router.post(
    "/checkouts",
    response_model=EquipmentCheckout,
    status_code=status.HTTP_201_CREATED,
    summary="Check out an equipment item to a volunteer",
    responses={
        404: {"description": "Equipment or volunteer not found"},
        409: {"description": "Already checked out, or the volunteer lacks the required badge"},
    },
)
def check_out_equipment(
    checkout: EquipmentCheckout,
    store: ProducerOpsStore = Depends(get_store),
) -> EquipmentCheckout:
    try:
        return store.check_out(checkout)
    except (EquipmentNotFoundError, VolunteerNotFoundError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (EquipmentAlreadyCheckedOutError, MissingRequiredBadgeError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.post(
    "/checkouts/{checkout_id}/return",
    response_model=EquipmentCheckout,
    summary="Return a checked-out equipment item",
    responses={404: {"description": "Checkout not found"}},
)
def return_checkout(
    checkout_id: str,
    store: ProducerOpsStore = Depends(get_store),
) -> EquipmentCheckout:
    try:
        return store.return_checkout(checkout_id)
    except CheckoutNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@staff_router.get(
    "/volunteers/{volunteer_id}/checkouts",
    response_model=list[EquipmentCheckout],
    summary="List equipment checkouts for a volunteer",
)
def list_checkouts_for_volunteer(
    volunteer_id: str,
    store: ProducerOpsStore = Depends(get_store),
) -> list[EquipmentCheckout]:
    return store.list_checkouts_for_volunteer(volunteer_id)


# --- training badges ---------------------------------------------------------


@staff_router.post(
    "/volunteers/{volunteer_id}/badges",
    response_model=TrainingBadge,
    status_code=status.HTTP_201_CREATED,
    summary="Grant a training badge to a volunteer",
    responses={
        404: {"description": "Volunteer not found"},
        409: {"description": "badge_id already exists"},
    },
)
def grant_badge(
    volunteer_id: str,
    badge: TrainingBadge,
    store: ProducerOpsStore = Depends(get_store),
) -> TrainingBadge:
    if badge.volunteer_id != volunteer_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Path volunteer_id must match body volunteer_id.",
        )
    try:
        return store.grant_badge(badge)
    except VolunteerNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except TrainingBadgeAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.get(
    "/volunteers/{volunteer_id}/badges",
    response_model=list[TrainingBadge],
    summary="List training badges earned by a volunteer",
)
def list_badges_for_volunteer(
    volunteer_id: str,
    store: ProducerOpsStore = Depends(get_store),
) -> list[TrainingBadge]:
    return store.list_badges_for_volunteer(volunteer_id)


__all__ = ["get_producer_ops_store", "staff_router"]
