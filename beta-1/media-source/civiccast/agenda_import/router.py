# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Agenda import API surface (plan §6): import + discovery routes.

``CIVICCAST_AGENDA_SOURCE=off`` (the default) makes both routes act as if
they don't exist -- a 404 naming the env var, never a 500 or a startup
failure (plan §7). Role gating mirrors :mod:`civiccast.agenda.router`'s
``_AUTHOR`` scopes (``records_clerk`` / ``meeting_operator``): agenda import
is a staff-only write path, same trust level as the existing agenda CRUD.

Both routes are wired generically against the
:class:`~civiccast.agenda_import.base.AgendaSource` Protocol (plan §8 task 5)
-- all four vendors (``legistar``, ``primegov``, ``civicclerk``, ``js_portal``)
resolve to a working adapter as of Phase 4. A third route
(``GET /agenda-sources/js-portal/posture``) is ``js_portal``-only: it reports
whether the optional crawl4ai/Playwright runtime is installed, independent
of routing/enable state, so the operator console can show a "not installed"
posture before an operator even attempts an import.
"""

from __future__ import annotations

import functools
import logging
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from civiccast.agenda.models import AgendaItem
from civiccast.agenda.router import get_agenda_store
from civiccast.agenda.store import AgendaItemOrderConflictError, AgendaNotFoundError, AgendaStore
from civiccast.agenda_import.base import (
    AgendaSourceAuthRequiredError,
    AgendaSourceDependencyMissingError,
    AgendaSourceNotAvailableError,
    AgendaSourceUpstreamError,
)
from civiccast.agenda_import.config import (
    AgendaImportSettings,
    validate_client_code,
    validate_portal_url,
)
from civiccast.agenda_import.js_portal import describe_js_portal_runtime
from civiccast.agenda_import.mapper import import_external_agenda
from civiccast.agenda_import.models import AgendaSourceName, ExternalMeetingSummary
from civiccast.agenda_import.provenance import AgendaImportProvenanceStore
from civiccast.agenda_import.registry import build_source
from civiccast.auth.roles import require_any_role

logger = logging.getLogger(__name__)

_AUTHOR = ("records_clerk", "meeting_operator")
_AUTHOR_EXTRA = {"x-required-roles": list(_AUTHOR)}

_DISABLED_DETAIL = (
    "Agenda import is not enabled. Set CIVICCAST_AGENDA_SOURCE to 'legistar', "
    "'primegov', 'civicclerk', or 'js_portal' to turn it on."
)

_JS_PORTAL_SOURCE = "js_portal"


def _validate_js_portal_fields(payload: AgendaImportExternalRequest) -> AgendaImportExternalRequest:
    """Shared model_validator body: ``portal_url``/``portal_vendor_hint`` are
    required exactly when ``source == "js_portal"``, forbidden otherwise --
    see :class:`AgendaImportExternalRequest`'s docstring."""
    if payload.source == _JS_PORTAL_SOURCE:
        if not payload.portal_url:
            raise ValueError("portal_url is required when source is 'js_portal'.")
        payload.portal_url = validate_portal_url(payload.portal_url)
    elif payload.portal_url is not None or payload.portal_vendor_hint is not None:
        raise ValueError(
            "portal_url/portal_vendor_hint are only accepted when source is "
            "'js_portal' -- they have no meaning for the other vendors."
        )
    return payload


@functools.cache
def _settings() -> AgendaImportSettings:
    """Cached env read (matches ``civiccast.agenda.router._station_id``'s
    convention). Tests that flip ``CIVICCAST_AGENDA_SOURCE`` mid-run must
    call ``_settings.cache_clear()``."""
    return AgendaImportSettings.from_env()


def get_agenda_import_settings() -> AgendaImportSettings:
    """DI seam so tests can override without touching the environment."""
    return _settings()


def _require_enabled(settings: AgendaImportSettings) -> None:
    if not settings.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=_DISABLED_DETAIL)


def _require_store(store: AgendaStore | None) -> AgendaStore:
    if store is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="Durable storage is not ready yet."
        )
    return store


def get_agenda_import_provenance_store() -> AgendaImportProvenanceStore | None:
    """DI seam -- overridden by the app factory. ``None`` means "don't record
    provenance"; it is bookkeeping only and never blocks an import."""
    return None


router = APIRouter(prefix="/api/staff", tags=["staff", "agenda-import"])


class AgendaImportExternalRequest(BaseModel):
    """POST .../import-external body (plan §6).

    ``portal_url``/``portal_vendor_hint`` are ``js_portal``-only additions
    (Phase 4): required exactly when ``source == "js_portal"``, forbidden
    (must be omitted/null) for every other source -- enforced below, not by
    the field types alone, so a legacy client that only ever sends
    ``source``/``client_code``/``event_id`` (the first three vendors) is
    unaffected. ``client_code`` stays required for every source, including
    ``js_portal`` -- there it is purely an operator-assigned display label
    for provenance/logging (see ``js_portal.py``'s class docstring), never
    spliced into a request URL, so it does not need the SSRF-conscious
    tenant-token shape :func:`validate_client_code` enforces for the other
    three vendors... except it still gets that same validation, because a
    short safe label is a reasonable thing to require regardless of vendor.
    """

    model_config = ConfigDict(extra="forbid")

    source: AgendaSourceName
    client_code: Annotated[
        str, Field(min_length=1, max_length=64), AfterValidator(validate_client_code)
    ]
    event_id: Annotated[str, Field(min_length=1, max_length=120)]
    portal_url: Annotated[str | None, Field(default=None, max_length=500)] = None
    portal_vendor_hint: Annotated[str | None, Field(default=None, max_length=40)] = None

    @model_validator(mode="after")
    def _check_js_portal_config(self) -> AgendaImportExternalRequest:
        return _validate_js_portal_fields(self)


@router.post(
    "/agenda/{agenda_id}/import-external",
    response_model=list[AgendaItem],
    summary="Import agenda items from an external agenda system",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={
        404: {"description": "Agenda import disabled, or meeting agenda not found"},
        422: {"description": "Source not yet available in this release"},
        502: {"description": "Upstream fetch failed (includes token-gated tenants)"},
        503: {
            "description": (
                "Durable storage is not ready yet, or (js_portal only) the optional "
                "crawl4ai/Playwright runtime is not installed"
            )
        },
    },
)
def import_external(
    agenda_id: str,
    payload: AgendaImportExternalRequest,
    store: AgendaStore | None = Depends(get_agenda_store),
    settings: AgendaImportSettings = Depends(get_agenda_import_settings),
    provenance_store: AgendaImportProvenanceStore | None = Depends(
        get_agenda_import_provenance_store
    ),
) -> list[AgendaItem]:
    _require_enabled(settings)
    resolved_store = _require_store(store)
    try:
        source = build_source(
            payload.source,
            timeout_seconds=settings.timeout_seconds,
            token=settings.token,
            portal_url=payload.portal_url,
            portal_vendor_hint=payload.portal_vendor_hint,
        )
        external = source.fetch_agenda(payload.client_code, payload.event_id)
        written = import_external_agenda(resolved_store, agenda_id, external)
        if provenance_store is not None:
            try:
                provenance_store.record_import(
                    agenda_id=agenda_id,
                    source=payload.source,
                    client_code=payload.client_code,
                    external_id=external.external_id,
                )
            except Exception:
                # Bookkeeping only, never blocks an import (see
                # get_agenda_import_provenance_store's docstring) -- the
                # agenda items above are already durably committed, so a
                # provenance-write race (e.g. a double-submitted import)
                # must not turn an already-successful import into a
                # reported failure.
                logger.exception("agenda_import.record_import failed for agenda_id=%s", agenda_id)
        return written
    except AgendaNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AgendaItemOrderConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except AgendaSourceNotAvailableError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except AgendaSourceDependencyMissingError as exc:
        # A genuinely new failure mode the other three vendors never had
        # (base.py's docstring): "not installed here" is a station-config
        # fact, not an upstream vendor failure, so it gets its own status
        # rather than folding into the 502 branch below and losing the
        # distinction the UI needs to render a "not installed" state.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except (AgendaSourceAuthRequiredError, AgendaSourceUpstreamError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except ValueError as exc:
        # Hostile doc_url (bad scheme) rejected by validate_source_doc_url,
        # or any other malformed-payload ValueError from the mapper -- fail
        # loud, nothing was written.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get(
    "/agenda-sources/{source}/{client_code}/meetings",
    response_model=list[ExternalMeetingSummary],
    summary="List upcoming/recent meetings from an external agenda system",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
    responses={
        404: {"description": "Agenda import disabled"},
        422: {
            "description": ("Source not yet available, or (js_portal) portal_url missing/invalid")
        },
        502: {"description": "Upstream fetch failed (includes token-gated tenants)"},
        503: {"description": "js_portal only: crawl4ai/Playwright not installed"},
    },
)
def list_external_meetings(
    source: AgendaSourceName,
    client_code: Annotated[str, AfterValidator(validate_client_code)],
    since: Annotated[
        date | None, Query(description="Only meetings on/after this date (default: today)")
    ] = None,
    portal_url: Annotated[
        str | None,
        Query(
            max_length=500,
            description="js_portal only: the JS-hydrated portal's own URL. Required "
            "when source=js_portal, rejected otherwise.",
        ),
    ] = None,
    portal_vendor_hint: Annotated[
        str | None,
        Query(
            max_length=40,
            description="js_portal only: optional vendor tuning hint "
            "(civicplus/granicus/legistar_js/primegov_js/generic).",
        ),
    ] = None,
    settings: AgendaImportSettings = Depends(get_agenda_import_settings),
) -> list[ExternalMeetingSummary]:
    _require_enabled(settings)
    resolved_portal_url = _resolve_js_portal_query_config(
        source, portal_url=portal_url, portal_vendor_hint=portal_vendor_hint
    )
    try:
        adapter = build_source(
            source,
            timeout_seconds=settings.timeout_seconds,
            token=settings.token,
            portal_url=resolved_portal_url,
            portal_vendor_hint=portal_vendor_hint,
        )
        return adapter.fetch_meetings(client_code, since=since)
    except AgendaSourceNotAvailableError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except AgendaSourceDependencyMissingError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except (AgendaSourceAuthRequiredError, AgendaSourceUpstreamError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


def _resolve_js_portal_query_config(
    source: str, *, portal_url: str | None, portal_vendor_hint: str | None
) -> str | None:
    """GET-route counterpart to :func:`_validate_js_portal_fields` -- there is
    no pydantic body model on a GET request to hang a model_validator off
    of, so the same "required exactly when js_portal, forbidden otherwise"
    rule is applied by hand here and raised as the same 422 a bad body would
    produce."""
    if source == _JS_PORTAL_SOURCE:
        if not portal_url:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="portal_url query parameter is required when source is 'js_portal'.",
            )
        try:
            return validate_portal_url(portal_url)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    if portal_url is not None or portal_vendor_hint is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="portal_url/portal_vendor_hint are only accepted when source is "
            "'js_portal' -- they have no meaning for the other vendors.",
        )
    return None


class JsPortalPostureResponse(BaseModel):
    """GET .../agenda-sources/js-portal/posture body -- whether the optional
    crawl4ai/Playwright runtime is importable on this station, independent
    of whether ``js_portal`` is the currently CIVICCAST_AGENDA_SOURCE-
    selected vendor. Lets the operator console show an honest "not
    installed" state before the operator even attempts an import."""

    model_config = ConfigDict(extra="forbid")

    installed: bool
    detail: str


@router.get(
    "/agenda-sources/js-portal/posture",
    response_model=JsPortalPostureResponse,
    summary="Report whether the optional js_portal (crawl4ai/Playwright) runtime is installed",
    dependencies=[Depends(require_any_role(*_AUTHOR))],
    openapi_extra=_AUTHOR_EXTRA,
)
def js_portal_posture() -> JsPortalPostureResponse:
    # Deliberately NOT gated by _require_enabled: an operator deciding
    # whether to switch CIVICCAST_AGENDA_SOURCE to js_portal wants to know
    # if it will work BEFORE making that switch, not only after.
    status_report = describe_js_portal_runtime()
    return JsPortalPostureResponse(installed=status_report.installed, detail=status_report.detail)


__all__ = [
    "JsPortalPostureResponse",
    "get_agenda_import_provenance_store",
    "get_agenda_import_settings",
    "router",
]
