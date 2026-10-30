# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API routes for broadcast facility integration previews."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from civiccast.auth.roles import require_any_role
from civiccast.facility.models import (
    RouterInventory,
    RouterScheduledTakePlan,
    RouterScheduledTakePreviewRequest,
    RouterScheduledTakeRequest,
    RouterTakePlan,
    RouterTakePreviewRequest,
    RouterTakeRequest,
    VirtualRouterPanel,
)
from civiccast.facility.router_control import (
    build_router_scheduled_take_plan,
    build_router_take_plan,
    build_virtual_router_panel,
)
from civiccast.facility.store import FacilityRouteNotFoundError, InMemoryFacilityRouterStore

staff_router = APIRouter(prefix="/api/staff/facility", tags=["staff", "facility"])

_FACILITY_ROUTER_STORE = InMemoryFacilityRouterStore.default()


def get_facility_router_store() -> InMemoryFacilityRouterStore:
    return _FACILITY_ROUTER_STORE


@staff_router.get(
    "/router-inventory",
    response_model=RouterInventory,
    summary="Read operator-safe facility router inventory",
)
def router_inventory(
    store: InMemoryFacilityRouterStore = Depends(get_facility_router_store),
) -> RouterInventory:
    return store.inventory()


@staff_router.post(
    "/router-take-plan",
    response_model=RouterTakePlan,
    summary="Preview a facility router take command",
    dependencies=[Depends(require_any_role("meeting_operator", "support_admin"))],
    responses={404: {"description": "Router endpoint, source, or destination not found"}},
)
def router_take_plan(
    payload: RouterTakePreviewRequest,
    store: InMemoryFacilityRouterStore = Depends(get_facility_router_store),
) -> RouterTakePlan:
    try:
        request = RouterTakeRequest(
            request_id=payload.request_id,
            endpoint=store.get_endpoint(payload.endpoint_id),
            source=store.get_source(payload.source_id),
            destination=store.get_destination(payload.destination_id),
            requested_by=payload.requested_by,
            scheduled_for=payload.scheduled_for,
            reason=payload.reason,
        )
    except FacilityRouteNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return build_router_take_plan(request)


@staff_router.post(
    "/router-schedule-plan",
    response_model=RouterScheduledTakePlan,
    summary="Preview a schedule-triggered facility router take",
    dependencies=[Depends(require_any_role("meeting_operator", "support_admin"))],
    responses={404: {"description": "Router endpoint, source, or destination not found"}},
)
def router_schedule_plan(
    payload: RouterScheduledTakePreviewRequest,
    store: InMemoryFacilityRouterStore = Depends(get_facility_router_store),
) -> RouterScheduledTakePlan:
    try:
        request = RouterScheduledTakeRequest(
            request_id=payload.request_id,
            schedule_item_id=payload.schedule_item_id,
            channel_id=payload.channel_id,
            starts_at=payload.starts_at,
            endpoint=store.get_endpoint(payload.endpoint_id),
            source=store.get_source(payload.source_id),
            destination=store.get_destination(payload.destination_id),
            requested_by=payload.requested_by,
            preroll_seconds=payload.preroll_seconds,
            reason=payload.reason,
        )
    except FacilityRouteNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return build_router_scheduled_take_plan(request)


@staff_router.get(
    "/router-panel",
    response_model=VirtualRouterPanel,
    summary="Read the mobile-friendly facility router panel",
    responses={404: {"description": "Router endpoint not found"}},
)
def router_panel(
    endpoint_id: str = "control-room-router",
    store: InMemoryFacilityRouterStore = Depends(get_facility_router_store),
) -> VirtualRouterPanel:
    try:
        endpoint = store.get_endpoint(endpoint_id)
    except FacilityRouteNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return build_virtual_router_panel(
        panel_id=f"{endpoint.endpoint_id}-panel",
        label=f"{endpoint.label} panel",
        endpoint=endpoint,
        sources=store.list_sources(),
        destinations=store.list_destinations(),
    )
