# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S23 reporting + EPG-export API surface.

Three surface families, all over the single ``ReportingService`` / ``ReportingStore``
/ ``EpgExporter`` DI seam:

* **Staff reports** (``/api/staff/reports/...``) — ``support_admin`` read.
  ``as-run`` / ``shows`` / ``hours-by-category`` plus a ``export`` download for
  the as-run + shows reports in CSV or XML. Hours-by-category requires a S22
  custom-field key (e.g. ``category``); the response carries ``field_not_found``
  rather than 404 so a misnamed key surfaces visibly in the UI without breaking
  the round-trip.

* **Public as-run** (``/api/public/reports/as-run``) — UNAUTHENTICATED, narrow
  per the PEG automation "schedule report" public view. Returns a
  :class:`PublicAsRunReport` — the entry projection drops engine-internal
  metadata (``verified`` / ``created_at`` / ``updated_at``) so a public
  client cannot read internal engine-write timings off the ledger.

* **EPG export configs** (``/api/staff/epg/configs[/{id}]``) — list/get for
  ``setup_admin`` / ``publish_operator``; create/patch/delete are
  ``setup_admin`` only (the export-channel act). ``POST /{id}/generate`` runs
  the configured exporter and either returns the document (download) or POSTs
  it to the configured endpoint — the response carries the result either way,
  including a push error message rather than a 500 (a flaky aggregator must
  not crash the staff API).

The DI seams (``get_reporting_service`` / ``get_reporting_store`` /
``get_epg_exporter``) return ``None`` at import so the module opens no
database; the app factory overrides them in ``_wire_durable_stores``. An
unwired service is a 503 — never a silent 200 against storage that is not
there. ``x-required-roles`` is surfaced into the generated OpenAPI so the
published contract cannot drift from the runtime role gate.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from civiccast.auth.roles import require_any_role
from civiccast.reporting.epg import EpgExporter, EpgGenerateResult
from civiccast.reporting.models import (
    EpgExportConfig,
    EpgExportConfigInput,
    EpgExportConfigUpdate,
)
from civiccast.reporting.service import (
    AsRunReport,
    HoursByCategoryReport,
    PublicAsRunReport,
    ReportingService,
    ShowsReport,
    export_as_run_csv,
    export_as_run_xml,
    export_shows_csv,
    export_shows_xml,
)
from civiccast.reporting.store import EpgConfigNotFoundError, ReportingStore

_DB_NOT_READY = "Durable storage is not ready yet."
_EPG_NOT_READY = "EPG exporter is not ready yet."

# Spec §4 roles. ``support_admin`` reads franchise-compliance reports;
# ``setup_admin`` + ``publish_operator`` manage EPG export channels.
_REPORT_READ = ("support_admin",)
_EPG_READ = ("setup_admin", "publish_operator")
_EPG_WRITE = ("setup_admin", "publish_operator")
_EPG_GENERATE = ("setup_admin", "publish_operator")

_REPORT_READ_EXTRA = {"x-required-roles": list(_REPORT_READ)}
_EPG_READ_EXTRA = {"x-required-roles": list(_EPG_READ)}
_EPG_WRITE_EXTRA = {"x-required-roles": list(_EPG_WRITE)}
_EPG_GENERATE_EXTRA = {"x-required-roles": list(_EPG_GENERATE)}

_DEFAULT_STATION_ID = "civiccast-station"


def _station_id() -> str:
    """The active station id (single-station deployment; env-overridable)."""
    import os

    return os.environ.get("CIVICCAST_STATION_ID") or _DEFAULT_STATION_ID


# --- DI seams (overridden by the app factory) --------------------------------


def get_reporting_service() -> ReportingService | None:
    return None


def get_reporting_store() -> ReportingStore | None:
    return None


def get_epg_exporter() -> EpgExporter | None:
    return None


def _require_service(svc: ReportingService | None) -> ReportingService:
    if svc is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return svc


def _require_store(store: ReportingStore | None) -> ReportingStore:
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return store


def _require_exporter(exporter: EpgExporter | None) -> EpgExporter:
    if exporter is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_EPG_NOT_READY)
    return exporter


# --- routers -----------------------------------------------------------------

staff_router = APIRouter(prefix="/api/staff", tags=["staff", "reports"])
public_router = APIRouter(prefix="/api/public", tags=["public", "reports"])

# EPG-config routes carry ``staff`` + ``epg`` (NOT ``reports``); they are export-channel
# management, not reports — keeping them out of the ``reports`` tag prevents tag-grouped
# docs from mixing EPG management with as-run/shows/hours-by-category report endpoints.
_EPG_TAGS: list[str | Enum] = ["staff", "epg"]


# --- staff reports -----------------------------------------------------------


@staff_router.get(
    "/reports/as-run",
    response_model=AsRunReport,
    summary="As-aired log (engine-verified actual air times)",
    dependencies=[Depends(require_any_role(*_REPORT_READ))],
    openapi_extra=_REPORT_READ_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def get_as_run_report(
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    channel_id: str | None = Query(None, alias="channel"),
    field_key: str | None = Query(None, alias="field"),
    svc: ReportingService | None = Depends(get_reporting_service),
) -> AsRunReport:
    """Half-open ``[from, to)`` on ``actual_start``. Optional channel + S22 cf-key for category."""
    return _require_service(svc).as_run_report(
        station_id=_station_id(),
        from_ts=from_ts,
        to_ts=to_ts,
        channel_id=channel_id,
        field_key=field_key,
    )


@staff_router.get(
    "/reports/shows",
    response_model=ShowsReport,
    summary="Per-show play counts + airtime",
    dependencies=[Depends(require_any_role(*_REPORT_READ))],
    openapi_extra=_REPORT_READ_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def get_shows_report(
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    channel_id: str | None = Query(None, alias="channel"),
    svc: ReportingService | None = Depends(get_reporting_service),
) -> ShowsReport:
    """Aggregate as-run by asset_id over ``[from, to)``. Filler/slate/live (no asset) excluded."""
    return _require_service(svc).shows_report(
        station_id=_station_id(),
        from_ts=from_ts,
        to_ts=to_ts,
        channel_id=channel_id,
    )


@staff_router.get(
    "/reports/hours-by-category",
    response_model=HoursByCategoryReport,
    summary="Franchise hours grouped by an S22 custom-field value (DC-3)",
    description=(
        "Franchise hours-by-category report. ``field`` is a S22 custom-field key "
        "(e.g. ``category``). **Returns 200 with ``field_not_found=true`` (not 404) "
        "when no custom field with this key exists for the station** — keeps the "
        "surrounding UI chrome rendered and the round-trip honest, so a misnamed "
        "key surfaces visibly in the operator UI without breaking the page."
    ),
    dependencies=[Depends(require_any_role(*_REPORT_READ))],
    openapi_extra=_REPORT_READ_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def get_hours_by_category_report(
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    field_key: str = Query(..., alias="field"),
    channel_id: str | None = Query(None, alias="channel"),
    svc: ReportingService | None = Depends(get_reporting_service),
) -> HoursByCategoryReport:
    """``field`` is a S22 custom-field key (e.g. ``category``). Unknown key → ``field_not_found=True`` (200, not 404)."""
    return _require_service(svc).hours_by_category(
        station_id=_station_id(),
        field_key=field_key,
        from_ts=from_ts,
        to_ts=to_ts,
        channel_id=channel_id,
    )


@staff_router.get(
    "/reports/export",
    summary="Download as-run or shows as CSV or XML",
    dependencies=[Depends(require_any_role(*_REPORT_READ))],
    openapi_extra=_REPORT_READ_EXTRA,
    responses={
        200: {
            "description": "Report document (CSV or XML)",
            "content": {"text/csv": {}, "application/xml": {}},
        },
        503: {"description": _DB_NOT_READY},
    },
)
def export_report(
    report_type: Literal["as-run", "shows"] = Query(..., alias="type"),
    fmt: Literal["csv", "xml"] = Query(..., alias="format"),
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    channel_id: str | None = Query(None, alias="channel"),
    field_key: str | None = Query(None, alias="field"),
    svc: ReportingService | None = Depends(get_reporting_service),
) -> Response:
    """``type=as-run|shows`` x ``format=csv|xml`` -> file download."""
    service = _require_service(svc)
    station = _station_id()
    if report_type == "as-run":
        report = service.as_run_report(
            station_id=station,
            from_ts=from_ts,
            to_ts=to_ts,
            channel_id=channel_id,
            field_key=field_key,
        )
        body = export_as_run_csv(report.rows) if fmt == "csv" else export_as_run_xml(report.rows)
        filename = f"as-run-{from_ts.date()}-{to_ts.date()}.{fmt}"
    else:
        shows = service.shows_report(
            station_id=station,
            from_ts=from_ts,
            to_ts=to_ts,
            channel_id=channel_id,
        )
        body = export_shows_csv(shows.rows) if fmt == "csv" else export_shows_xml(shows.rows)
        filename = f"shows-{from_ts.date()}-{to_ts.date()}.{fmt}"
    media_type = "text/csv" if fmt == "csv" else "application/xml"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- public as-run -----------------------------------------------------------


@public_router.get(
    "/reports/as-run",
    response_model=PublicAsRunReport,
    summary="Public as-aired log",
    responses={503: {"description": _DB_NOT_READY}},
)
def get_public_as_run_report(
    from_ts: datetime = Query(..., alias="from"),
    to_ts: datetime = Query(..., alias="to"),
    channel_id: str | None = Query(None, alias="channel"),
    svc: ReportingService | None = Depends(get_reporting_service),
) -> PublicAsRunReport:
    """Public projection. Drops engine-internal metadata (``verified`` /
    ``created_at`` / ``updated_at``) and emits no ``category`` (categories
    are an internal S22 concern). Matches the published privacy contract in
    the module docstring (Q-1 fix)."""
    return _require_service(svc).public_as_run_report(
        station_id=_station_id(),
        from_ts=from_ts,
        to_ts=to_ts,
        channel_id=channel_id,
    )


# --- EPG export configs ------------------------------------------------------


@staff_router.get(
    "/epg/configs",
    response_model=list[EpgExportConfig],
    summary="List EPG export configs for the station",
    tags=_EPG_TAGS,
    dependencies=[Depends(require_any_role(*_EPG_READ))],
    openapi_extra=_EPG_READ_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def list_epg_configs(
    store: ReportingStore | None = Depends(get_reporting_store),
) -> list[EpgExportConfig]:
    return _require_store(store).list_configs(_station_id())


@staff_router.get(
    "/epg/configs/{config_id}",
    response_model=EpgExportConfig,
    summary="Get one EPG export config",
    tags=_EPG_TAGS,
    dependencies=[Depends(require_any_role(*_EPG_READ))],
    openapi_extra=_EPG_READ_EXTRA,
    responses={
        404: {"description": "EPG export config not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def get_epg_config(
    config_id: str,
    store: ReportingStore | None = Depends(get_reporting_store),
) -> EpgExportConfig:
    cfg = _require_store(store).get_config(config_id)
    if cfg is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"EPG config {config_id!r} not found."
        )
    return cfg


@staff_router.post(
    "/epg/configs",
    response_model=EpgExportConfig,
    status_code=status.HTTP_201_CREATED,
    summary="Create an EPG export config",
    tags=_EPG_TAGS,
    dependencies=[Depends(require_any_role(*_EPG_WRITE))],
    openapi_extra=_EPG_WRITE_EXTRA,
    responses={503: {"description": _DB_NOT_READY}},
)
def create_epg_config(
    payload: EpgExportConfigInput,
    store: ReportingStore | None = Depends(get_reporting_store),
) -> EpgExportConfig:
    cfg = EpgExportConfig(**payload.model_dump())
    return _require_store(store).upsert_config(cfg)


@staff_router.patch(
    "/epg/configs/{config_id}",
    response_model=EpgExportConfig,
    summary="Patch an EPG export config (absent keys unchanged)",
    tags=_EPG_TAGS,
    dependencies=[Depends(require_any_role(*_EPG_WRITE))],
    openapi_extra=_EPG_WRITE_EXTRA,
    responses={
        404: {"description": "EPG export config not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def patch_epg_config(
    config_id: str,
    payload: EpgExportConfigUpdate,
    store: ReportingStore | None = Depends(get_reporting_store),
) -> EpgExportConfig:
    """Patch an EPG export config.

    Patch semantics (E-6 clarification):

    * An absent key leaves the stored field unchanged.
    * An explicit ``null`` clears the field (e.g. ``{"endpoint": null}`` sets
      the row's endpoint to ``None``, switching it from push to download-only).

    ``config_id`` / ``station_id`` are set at creation and not editable here.
    """
    resolved = _require_store(store)
    current = resolved.get_config(config_id)
    if current is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"EPG config {config_id!r} not found."
        )
    updates = payload.model_dump(exclude_unset=True)
    merged = current.model_copy(update=updates)
    return resolved.upsert_config(merged)


@staff_router.delete(
    "/epg/configs/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an EPG export config",
    tags=_EPG_TAGS,
    dependencies=[Depends(require_any_role(*_EPG_WRITE))],
    openapi_extra=_EPG_WRITE_EXTRA,
    responses={
        404: {"description": "EPG export config not found"},
        503: {"description": _DB_NOT_READY},
    },
)
def delete_epg_config(
    config_id: str,
    store: ReportingStore | None = Depends(get_reporting_store),
) -> Response:
    try:
        _require_store(store).delete_config(config_id)
    except EpgConfigNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@staff_router.post(
    "/epg/configs/{config_id}/generate",
    response_model=EpgGenerateResult,
    summary="Run the EPG export — download or push to endpoint",
    tags=_EPG_TAGS,
    dependencies=[Depends(require_any_role(*_EPG_GENERATE))],
    openapi_extra=_EPG_GENERATE_EXTRA,
    responses={
        404: {"description": "EPG export config not found"},
        503: {"description": _EPG_NOT_READY},
    },
)
def generate_epg_export(
    config_id: str,
    store: ReportingStore | None = Depends(get_reporting_store),
    exporter: EpgExporter | None = Depends(get_epg_exporter),
) -> EpgGenerateResult:
    """Generate the document for ``config_id``. ``endpoint=None`` → returns document inline.

    A push failure is surfaced as ``error`` on the result, not a 500: the
    staff API stays available even when the aggregator endpoint is flaky.
    """
    resolved_store = _require_store(store)
    resolved_exporter = _require_exporter(exporter)
    cfg = resolved_store.get_config(config_id)
    if cfg is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"EPG config {config_id!r} not found."
        )
    from datetime import UTC

    return resolved_exporter.generate(cfg, now=datetime.now(UTC))


__all__ = [
    "get_epg_exporter",
    "get_reporting_service",
    "get_reporting_store",
    "public_router",
    "staff_router",
]
