# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S22 custom-metadata-fields staff + public API.

Three surfaces, all over the single :class:`CustomFieldService` DI seam:

* **Staff field definitions** (``/api/staff/custom-fields[/{field_id}]``) — list/get are READ
  roles (setup_admin / meeting_operator / records_clerk); create/patch/delete are
  ``setup_admin`` only (the field-definition act, spec §4). ``key`` is immutable: a PATCH that
  changes it is a 409, never a silent no-op. DELETE is blocked with a 409 when values exist
  unless ``?confirm=true`` cascades them — never a silent data loss (spec §6).

* **Staff asset values** (``/api/staff/assets/{asset_id}/custom-fields``) — GET/PUT for the
  value-write roles (setup_admin / meeting_operator / records_clerk). PUT is a full-replace,
  typed-validated by the service+store (list-must-be-an-option, number→value_num, date→
  value_date, required-present, asset_ref/producer_ref must resolve); any validation failure
  is a 422.

* **Public search** (``GET /api/public/search``) — UNAUTHENTICATED, capped to the public
  exposure set. ``cf.<key>=<value>`` exact-match facets and ``cf.<key>_gte`` / ``cf.<key>_lte``
  numeric/date ranges filter packaged assets; only ``searchable AND api_exposed`` fields are
  eligible (a hidden field is silently ignored, never confirmed or leaked — DC-5). The
  response model carries only the exposed values, so a non-exposed value physically cannot
  serialize out.

The single DI seam (``get_custom_field_service``) returns ``None`` at import so the module
opens no database; the app factory overrides it in ``_wire_durable_stores``. An unwired
service is a 503 — never a silent 200 against storage that is not there.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from civiccast.auth.roles import require_any_role
from civiccast.metadata.models import (
    AssetCustomFieldsUpdate,
    CustomFieldDef,
    CustomFieldDefInput,
    CustomFieldDefUpdate,
    CustomFieldValue,
    PublicSearchAsset,
    PublicTranscript,
)
from civiccast.metadata.service import CustomFieldService, FieldReferenceError
from civiccast.metadata.store import (
    FieldAlreadyExistsError,
    FieldImmutableKeyError,
    FieldNotFoundError,
    FieldValidationError,
    FieldValuesExistError,
)
from civiccast.paywall.router import get_paywall_service
from civiccast.paywall.service import PaywallService

_DB_NOT_READY = "Durable storage is not ready yet."

# Spec §4 role table: setup_admin defines fields; meeting_operator / records_clerk set
# values; field-definition reads are scoped to those same three roles (spec §4 — no broader
# read role; OpenAPI ``x-required-roles`` is sourced from this same tuple so it cannot drift).
_READ = ("setup_admin", "meeting_operator", "records_clerk")
_DEF_WRITE = ("setup_admin",)
_VALUE_WRITE = ("setup_admin", "meeting_operator", "records_clerk")

# Surfaced into the generated OpenAPI (``x-required-roles``) beside the enforced gate so the
# published contract cannot drift from the runtime check.
_READ_ROLES_EXTRA = {"x-required-roles": list(_READ)}
_DEF_WRITE_ROLES_EXTRA = {"x-required-roles": list(_DEF_WRITE)}
_VALUE_WRITE_ROLES_EXTRA = {"x-required-roles": list(_VALUE_WRITE)}

# The public search route reads filters from the raw ``request.query_params`` (the
# ``cf.<key>`` keys are dynamic, so they cannot be declared as typed FastAPI query params).
# FastAPI therefore emits NO parameters for this op — without this the published OpenAPI /
# API-REFERENCE would say "Parameters: none" and a public/integration consumer could not
# discover the facet/range contract. ``openapi_extra={"parameters": [...]}`` documents the
# dynamic ``cf.<key>`` family (the same seam the staff routes use for ``x-required-roles``).
_PUBLIC_SEARCH_PARAMS_EXTRA = {
    "parameters": [
        {
            "name": "q",
            "in": "query",
            "required": False,
            "schema": {"type": "string", "maxLength": 200},
            "description": "Search titles, descriptions and accessible published reviewed transcript passages.",
        },
        {
            "name": "cf.<key>",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": (
                "Exact-match facet: filter to assets whose custom field <key> equals this "
                "value (canonical string compare). <key> is a field's immutable machine key. "
                "Only fields that are BOTH searchable and api_exposed are eligible; a key "
                "resolving to a hidden/unknown field is silently ignored (never a 400) so a "
                "hidden field can be neither confirmed nor used to enumerate assets (DC-5). "
                "Multiple cf.* filters compose as AND."
            ),
        },
        {
            "name": "cf.<key>_gte",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": (
                "Inclusive lower bound for a number or date field (matched against the "
                "denormalized value_num / value_date). Combine with cf.<key>_lte for a closed "
                "range; either bound alone is valid. A malformed/non-finite bound is silently "
                "ignored. Same searchable+api_exposed-only / silently-ignored (DC-5) rule as "
                "the exact-match facet."
            ),
        },
        {
            "name": "cf.<key>_lte",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": (
                "Inclusive upper bound for a number or date field (matched against the "
                "denormalized value_num / value_date). See cf.<key>_gte; same exposed-only / "
                "silently-ignored (DC-5) semantics."
            ),
        },
    ]
}

# The single-station default (matches the app-platform default station_id). The public
# search + value writes scope to this station's field definitions.
_DEFAULT_STATION_ID = "civiccast-station"

staff_router = APIRouter(prefix="/api/staff", tags=["staff", "custom-fields"])
public_router = APIRouter(prefix="/api/public", tags=["public", "custom-fields"])


# --- DI seam (overridden by the app factory) --------------------------------


def get_custom_field_service() -> CustomFieldService | None:
    return None


def _require_service(svc: CustomFieldService | None) -> CustomFieldService:
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return svc


def _station_id() -> str:
    """The active station id (single-station deployment; env-overridable)."""
    import os

    return os.environ.get("CIVICCAST_STATION_ID") or _DEFAULT_STATION_ID


# --- field definitions -------------------------------------------------------


@staff_router.get(
    "/custom-fields",
    response_model=list[CustomFieldDef],
    summary="List the station's custom-field definitions",
    dependencies=[Depends(require_any_role(*_READ))],
    openapi_extra=_READ_ROLES_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def list_custom_fields(
    svc: CustomFieldService | None = Depends(get_custom_field_service),
) -> list[CustomFieldDef]:
    """Every custom-field definition for the station, ordered by ``order`` then ``label``."""
    return _require_service(svc).list_fields(_station_id())


@staff_router.post(
    "/custom-fields",
    response_model=CustomFieldDef,
    status_code=status.HTTP_201_CREATED,
    summary="Define a new custom field (setup_admin)",
    dependencies=[Depends(require_any_role(*_DEF_WRITE))],
    openapi_extra=_DEF_WRITE_ROLES_EXTRA,
    responses={
        409: {"description": "A field with this ID or station key already exists; use Edit"},
        503: {"description": _DB_NOT_READY},
    },
)
def create_custom_field(
    payload: CustomFieldDefInput,
    svc: CustomFieldService | None = Depends(get_custom_field_service),
) -> CustomFieldDef:
    """Create a field definition (key/label/type/options/flags). ``key`` is immutable hereafter."""
    service = _require_service(svc)
    try:
        return service.create_field(CustomFieldDef(**payload.model_dump()))
    except FieldAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.get(
    "/custom-fields/{field_id}",
    response_model=CustomFieldDef,
    summary="Get one custom-field definition",
    dependencies=[Depends(require_any_role(*_READ))],
    openapi_extra=_READ_ROLES_EXTRA,
    responses={404: {"description": "Field not found"}, 503: {"description": _DB_NOT_READY}},
)
def get_custom_field(
    field_id: str,
    svc: CustomFieldService | None = Depends(get_custom_field_service),
) -> CustomFieldDef:
    definition = _require_service(svc).get_field(field_id)
    if definition is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Custom field {field_id!r} not found."
        )
    return definition


@staff_router.patch(
    "/custom-fields/{field_id}",
    response_model=CustomFieldDef,
    summary="Edit a custom-field definition (label/options/flags; key is immutable)",
    dependencies=[Depends(require_any_role(*_DEF_WRITE))],
    openapi_extra=_DEF_WRITE_ROLES_EXTRA,
    responses={
        404: {"description": "Field not found"},
        409: {"description": "key is immutable"},
        503: {"description": _DB_NOT_READY},
    },
)
def update_custom_field(
    field_id: str,
    payload: CustomFieldDefUpdate,
    svc: CustomFieldService | None = Depends(get_custom_field_service),
) -> CustomFieldDef:
    """Patch a field definition. Absent keys are unchanged; a changed ``key`` is a 409."""
    service = _require_service(svc)
    existing = service.get_field(field_id)
    if existing is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Custom field {field_id!r} not found."
        )
    patch = payload.model_dump(exclude_unset=True)
    merged = existing.model_copy(update=patch)
    try:
        return service.update_field(merged)
    except FieldImmutableKeyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@staff_router.delete(
    "/custom-fields/{field_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a custom-field definition (blocked if values exist unless confirm)",
    dependencies=[Depends(require_any_role(*_DEF_WRITE))],
    openapi_extra=_DEF_WRITE_ROLES_EXTRA,
    responses={
        404: {"description": "Field not found"},
        409: {"description": "Values exist; pass ?confirm=true to cascade"},
        503: {"description": _DB_NOT_READY},
    },
)
def delete_custom_field(
    field_id: str,
    confirm: bool = False,
    svc: CustomFieldService | None = Depends(get_custom_field_service),
) -> None:
    """Delete a field. A field with existing values is a 409 unless ``?confirm=true`` cascades."""
    service = _require_service(svc)
    try:
        service.delete_field(field_id, confirm=confirm)
    except FieldValuesExistError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except FieldNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


# --- asset values ------------------------------------------------------------


@staff_router.get(
    "/assets/{asset_id}/custom-fields",
    response_model=list[CustomFieldValue],
    summary="Get one asset's custom-field values",
    dependencies=[Depends(require_any_role(*_VALUE_WRITE))],
    openapi_extra=_VALUE_WRITE_ROLES_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def get_asset_custom_fields(
    asset_id: str,
    svc: CustomFieldService | None = Depends(get_custom_field_service),
) -> list[CustomFieldValue]:
    """This asset's values (empty list = the valid zero-state)."""
    return _require_service(svc).get_asset_values(asset_id)


@staff_router.put(
    "/assets/{asset_id}/custom-fields",
    response_model=list[CustomFieldValue],
    summary="Replace one asset's custom-field values (typed-validated)",
    dependencies=[Depends(require_any_role(*_VALUE_WRITE))],
    openapi_extra=_VALUE_WRITE_ROLES_EXTRA,
    responses={
        422: {"description": "A value failed typed validation"},
        503: {"description": _DB_NOT_READY},
    },
)
def put_asset_custom_fields(
    asset_id: str,
    payload: AssetCustomFieldsUpdate,
    svc: CustomFieldService | None = Depends(get_custom_field_service),
) -> list[CustomFieldValue]:
    """Full-replace this asset's values. Typed validation failure (incl. an unresolved
    ``asset_ref`` / ``producer_ref``) is a 422; nothing persists on failure."""
    service = _require_service(svc)
    values = [
        CustomFieldValue(asset_id=asset_id, field_id=item.field_id, value=item.value)
        for item in payload.values
    ]
    try:
        return service.set_asset_values(asset_id, values, station_id=_station_id())
    except (FieldReferenceError, FieldValidationError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


# --- public search -----------------------------------------------------------


@public_router.get(
    "/search",
    response_model=list[PublicSearchAsset],
    summary="Public custom-field search (exposed fields only)",
    openapi_extra=_PUBLIC_SEARCH_PARAMS_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def public_search(
    request: Request,
    response: Response,
    svc: CustomFieldService | None = Depends(get_custom_field_service),
    paywall: PaywallService | None = Depends(get_paywall_service),
) -> list[PublicSearchAsset]:
    """Filter packaged public assets by ``cf.<key>`` facets / ``cf.<key>_gte|_lte`` ranges.

    Unauthenticated; only ``searchable AND api_exposed`` fields are eligible, so a non-exposed
    field is silently ignored (never confirmed, never leaked). Each returned asset carries
    only its exposed custom-field values.
    """
    service = _require_service(svc)
    response.headers["Cache-Control"] = "no-store"
    return service.search_public_assets(
        station_id=_station_id(),
        query_params=dict(request.query_params),
        transcript_access=lambda asset_id: _transcript_access(request, paywall, asset_id),
    )


def _transcript_access(request: Request, paywall: PaywallService | None, asset_id: str) -> bool:
    if paywall is None:
        return False
    sid = _station_id()
    email = paywall.viewer_email(sid, request.cookies.get("civiccast_viewer"))
    return paywall.has_access(sid, email, "asset", asset_id)


@public_router.get(
    "/assets/{asset_id}/transcript",
    response_model=PublicTranscript,
    summary="Read the current published reviewed transcript",
    responses={
        403: {"description": "Viewer access required"},
        404: {"description": "Recording not published"},
        503: {"description": _DB_NOT_READY},
    },
)
def public_transcript(
    asset_id: str,
    request: Request,
    response: Response,
    svc: CustomFieldService | None = Depends(get_custom_field_service),
    paywall: PaywallService | None = Depends(get_paywall_service),
) -> PublicTranscript:
    service = _require_service(svc)
    if paywall is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    try:
        transcript = service.public_transcript(
            asset_id,
            transcript_access=lambda current: _transcript_access(request, paywall, current),
        )
    except PermissionError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if transcript is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Recording not published.")
    response.headers["Cache-Control"] = "no-store"
    return transcript


__all__ = [
    "get_custom_field_service",
    "public_router",
    "staff_router",
]
