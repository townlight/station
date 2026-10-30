# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S8-5 staff API for runtime safe-to-air, system resources, and self-tests (§4).

DI seams (``get_*``) return ``None``/defaults at import so the module never opens
a database; the app factory overrides them once durable storage is ready (same
pattern as ``egress/router.py``). The runtime safe-to-air read is cached ~4s
server-side (OD-3) so a 1 Hz dashboard poll never stampedes the egress store.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from civiccast.alerting.models import (
    AlertChannel,
    AlertChannelKind,
    AlertEvent,
    AlertRule,
    AlertSeverity,
    RuntimeSafeToAirStatus,
    SelfTestKind,
    SystemResourceSample,
    SystemSelfTest,
)
from civiccast.alerting.runtime_status import compute_runtime_safe_to_air
from civiccast.alerting.self_test import (
    SelfTestDeps,
    assemble_available_self_test_checks,
    default_self_test_availability,
    run_self_test,
)
from civiccast.alerting.store import (
    acknowledge_alert_event,
    delete_alert_channel,
    get_alert_channel,
    get_alert_channels,
    get_alert_events,
    get_alert_rule,
    get_alert_rules,
    get_self_tests,
    recent_resource_samples,
    upsert_alert_channel,
    upsert_alert_rule,
)
from civiccast.auth.models import OperatorIdentity
from civiccast.auth.roles import ALL_OPERATOR_ROLES, require_any_role
from civiccast.egress.router import get_egress_store

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from civiccast.egress.store import EgressStore

SessionFactory = Callable[[], AbstractContextManager["Session"]]

_DB_NOT_READY = "Durable storage is not ready yet."


class RuntimeStatusCache:
    """Tiny TTL cache for the runtime safe-to-air read (OD-3, ~4s)."""

    def __init__(self, ttl_seconds: float = 4.0) -> None:
        self._ttl = ttl_seconds
        self._until = 0.0
        self._value: RuntimeSafeToAirStatus | None = None

    def get_or_compute(
        self, compute: Callable[[], RuntimeSafeToAirStatus]
    ) -> RuntimeSafeToAirStatus:
        monotonic = time.monotonic()
        if self._value is not None and monotonic < self._until:
            return self._value
        value = compute()
        self._value = value
        self._until = monotonic + self._ttl
        return value


_runtime_cache = RuntimeStatusCache()


def get_alerting_session_factory() -> SessionFactory | None:
    """DI seam for a context-manager session factory; app factory overrides it."""


def get_runtime_status_cache() -> RuntimeStatusCache:
    """DI seam for the shared runtime-status cache (overridable per app in tests)."""

    return _runtime_cache


def get_self_test_deps() -> SelfTestDeps | None:
    """DI seam for the self-test subcheck probes; the daemon wires the real ones."""


CredentialWriter = Callable[[str, "dict[str, str]"], None]


def get_credential_writer() -> CredentialWriter | None:
    """DI seam to persist a channel secret to the credential store; app overrides."""


staff_router = APIRouter(prefix="/api/staff", tags=["staff", "alerting"])


def _require_store(store: EgressStore | None) -> EgressStore:
    if store is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return store


def _require_factory(factory: SessionFactory | None) -> SessionFactory:
    if factory is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DB_NOT_READY)
    return factory


@staff_router.get(
    "/runtime-safe-to-air",
    response_model=RuntimeSafeToAirStatus,
    summary="Continuous runtime safe-to-air signal (cached ~4s)",
    responses={503: {"description": _DB_NOT_READY}},
)
def get_runtime_safe_to_air(
    egress_store: EgressStore | None = Depends(get_egress_store),
    factory: SessionFactory | None = Depends(get_alerting_session_factory),
    cache: RuntimeStatusCache = Depends(get_runtime_status_cache),
    _: None = Depends(require_any_role(*ALL_OPERATOR_ROLES)),
) -> RuntimeSafeToAirStatus:
    store = _require_store(egress_store)
    session_factory = _require_factory(factory)

    def compute() -> RuntimeSafeToAirStatus:
        with session_factory() as session:
            firing = get_alert_events(session, state="firing")
        return compute_runtime_safe_to_air(store, firing)

    return cache.get_or_compute(compute)


@staff_router.get(
    "/system-resources",
    response_model=list[SystemResourceSample],
    summary="Recent system-resource samples",
    responses={503: {"description": _DB_NOT_READY}},
)
def list_system_resources(
    window_minutes: int = Query(60, ge=1, le=1440),
    factory: SessionFactory | None = Depends(get_alerting_session_factory),
    _: None = Depends(require_any_role("support_admin", "setup_admin")),
) -> list[SystemResourceSample]:
    session_factory = _require_factory(factory)
    with session_factory() as session:
        return recent_resource_samples(session, window_minutes=window_minutes)


@staff_router.get(
    "/self-tests",
    response_model=list[SystemSelfTest],
    summary="Self-test history (daily/weekly)",
    responses={503: {"description": _DB_NOT_READY}},
)
def list_self_tests(
    kind: str | None = Query(None, pattern="^(daily|weekly)$"),
    since: datetime | None = Query(None),
    factory: SessionFactory | None = Depends(get_alerting_session_factory),
    _: None = Depends(require_any_role("support_admin", "setup_admin")),
) -> list[SystemSelfTest]:
    session_factory = _require_factory(factory)
    with session_factory() as session:
        return get_self_tests(session, kind=kind, since=since)


@staff_router.post(
    "/self-tests/run",
    response_model=SystemSelfTest,
    summary="Run a self-test on demand (daily or weekly check set)",
    responses={503: {"description": _DB_NOT_READY}},
)
def run_self_test_now(
    kind: SelfTestKind = Query(..., pattern="^(daily|weekly)$"),
    factory: SessionFactory | None = Depends(get_alerting_session_factory),
    deps: SelfTestDeps | None = Depends(get_self_test_deps),
    _: None = Depends(require_any_role("support_admin", "setup_admin")),
) -> SystemSelfTest:
    session_factory = _require_factory(factory)
    if deps is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Self-test probes are not wired yet (the egress daemon supplies them).",
        )
    checks = assemble_available_self_test_checks(
        kind,
        deps,
        default_self_test_availability(session_factory=session_factory),
    )
    with session_factory() as session:
        result = run_self_test(session, kind, checks, now=datetime.now(tz=UTC))
        session.commit()
        return result


# ---------------------------------------------------------------------------
# Alert rule + channel configuration (sensitive) and alert events (§4.3-4.5)
# ---------------------------------------------------------------------------


def _operator_name(request: Request) -> str:
    identity = getattr(request.state, "operator_identity", None)
    if isinstance(identity, OperatorIdentity):
        return identity.operator_display_name
    return "operator"


class AlertRuleUpdate(BaseModel):
    """Operator-tunable rule fields. Unset fields are left unchanged; the rule's
    ``condition`` and ``rule_id`` are immutable (a rule maps to one condition)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    severity: AlertSeverity | None = None
    channel_ids: list[str] | None = None
    dedupe_window_seconds: Annotated[int | None, Field(default=None, ge=0, le=86_400)] = None
    re_alert_after_seconds: Annotated[int | None, Field(default=None, ge=0, le=604_800)] = None
    scope_channel_id: Annotated[str | None, Field(default=None, max_length=80)] = None
    notify_on_resolve: bool | None = None


class AlertChannelInput(BaseModel):
    """Create/update payload for an alert channel. No secret values — the secret
    lives in the credential store keyed by ``credential_handle`` (never returned)."""

    model_config = ConfigDict(extra="forbid")

    kind: AlertChannelKind
    label: Annotated[str, Field(min_length=1, max_length=120)]
    enabled: bool = True
    target_redacted: Annotated[str, Field(min_length=1, max_length=200)]
    credential_handle: Annotated[str | None, Field(default=None, max_length=200)] = None
    quiet_hours_start_utc: Annotated[str | None, Field(default=None, max_length=5)] = None
    quiet_hours_end_utc: Annotated[str | None, Field(default=None, max_length=5)] = None
    # Write-only secret material (SMTP creds, webhook signing secret). Stored in
    # the credential store under credential_handle and NEVER returned by a read
    # (``exclude=True`` keeps it out of every serialization, incl. model_dump).
    secret: dict[str, str] | None = Field(default=None, exclude=True)


@staff_router.get(
    "/alert-rules",
    response_model=list[AlertRule],
    summary="List alert rules",
    responses={503: {"description": _DB_NOT_READY}},
)
def list_alert_rules(
    factory: SessionFactory | None = Depends(get_alerting_session_factory),
    _: None = Depends(require_any_role("setup_admin", "support_admin")),
) -> list[AlertRule]:
    session_factory = _require_factory(factory)
    with session_factory() as session:
        return get_alert_rules(session)


@staff_router.put(
    "/alert-rules/{rule_id}",
    response_model=AlertRule,
    summary="Update a tunable alert rule",
    responses={404: {"description": "Rule not found"}, 503: {"description": _DB_NOT_READY}},
)
def update_alert_rule(
    rule_id: str,
    update: AlertRuleUpdate,
    request: Request,
    factory: SessionFactory | None = Depends(get_alerting_session_factory),
    _: None = Depends(require_any_role("setup_admin")),
) -> AlertRule:
    session_factory = _require_factory(factory)
    with session_factory() as session:
        existing = get_alert_rule(session, rule_id)
        if existing is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=f"Alert rule not found: {rule_id}"
            )
        data = existing.model_dump()
        data.update(update.model_dump(exclude_unset=True))
        data["updated_at"] = datetime.now(tz=UTC)
        data["updated_by"] = _operator_name(request)
        merged = AlertRule(**data)
        result = upsert_alert_rule(session, merged)
        session.commit()
        return result


@staff_router.get(
    "/alert-channels",
    response_model=list[AlertChannel],
    summary="List alert channels (redacted; no secrets)",
    responses={503: {"description": _DB_NOT_READY}},
)
def list_alert_channels(
    factory: SessionFactory | None = Depends(get_alerting_session_factory),
    _: None = Depends(require_any_role("setup_admin", "support_admin")),
) -> list[AlertChannel]:
    session_factory = _require_factory(factory)
    with session_factory() as session:
        return get_alert_channels(session)


def _channel_from_input(
    channel_id: str,
    payload: AlertChannelInput,
    created_at: datetime,
    *,
    credential_handle: str | None,
) -> AlertChannel:
    # ``secret`` is exclude=True so model_dump drops it; credential_handle is the
    # resolved value (may be the channel_id when a secret was supplied without one).
    data = payload.model_dump(exclude={"credential_handle"})
    try:
        return AlertChannel(
            channel_id=channel_id,
            created_at=created_at,
            credential_handle=credential_handle,
            **data,
        )
    except ValidationError as exc:
        # 422 literal: starlette deprecates the HTTP_422_UNPROCESSABLE_ENTITY constant.
        # Build a JSON-safe detail (pydantic's raw errors() embeds the non-serialisable
        # ValueError in ctx, which breaks the response encoder).
        detail = [
            {"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]}
            for err in exc.errors()
        ]
        raise HTTPException(422, detail=detail) from exc


def _persist_secret(
    writer: CredentialWriter | None, channel_id: str, payload: AlertChannelInput
) -> str | None:
    """Store ``payload.secret`` (if any) and return the effective credential_handle.

    A secret with no explicit handle is stored under the channel_id. If a secret
    is supplied but no credential store is wired, fail (503) rather than silently
    dropping it.
    """
    if payload.secret is None:
        return payload.credential_handle
    if writer is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The credential store is not available to persist the secret.",
        )
    handle = payload.credential_handle or channel_id
    writer(handle, payload.secret)
    return handle


@staff_router.post(
    "/alert-channels",
    response_model=AlertChannel,
    status_code=status.HTTP_201_CREATED,
    summary="Create an alert channel (secret stays in the credential store)",
    responses={503: {"description": _DB_NOT_READY}},
)
def create_alert_channel(
    payload: AlertChannelInput,
    factory: SessionFactory | None = Depends(get_alerting_session_factory),
    writer: CredentialWriter | None = Depends(get_credential_writer),
    _: None = Depends(require_any_role("setup_admin")),
) -> AlertChannel:
    session_factory = _require_factory(factory)
    channel_id = f"ch-{uuid.uuid4().hex[:12]}"
    handle = _persist_secret(writer, channel_id, payload)
    channel = _channel_from_input(
        channel_id, payload, datetime.now(tz=UTC), credential_handle=handle
    )
    with session_factory() as session:
        result = upsert_alert_channel(session, channel)
        session.commit()
        return result


@staff_router.put(
    "/alert-channels/{channel_id}",
    response_model=AlertChannel,
    summary="Update an alert channel",
    responses={404: {"description": "Channel not found"}, 503: {"description": _DB_NOT_READY}},
)
def update_alert_channel(
    channel_id: str,
    payload: AlertChannelInput,
    factory: SessionFactory | None = Depends(get_alerting_session_factory),
    writer: CredentialWriter | None = Depends(get_credential_writer),
    _: None = Depends(require_any_role("setup_admin")),
) -> AlertChannel:
    session_factory = _require_factory(factory)
    with session_factory() as session:
        existing = get_alert_channel(session, channel_id)
        if existing is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=f"Alert channel not found: {channel_id}"
            )
        handle = _persist_secret(writer, channel_id, payload)
        channel = _channel_from_input(
            channel_id, payload, existing.created_at, credential_handle=handle
        )
        result = upsert_alert_channel(session, channel)
        session.commit()
        return result


@staff_router.delete(
    "/alert-channels/{channel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an alert channel",
    responses={404: {"description": "Channel not found"}, 503: {"description": _DB_NOT_READY}},
)
def remove_alert_channel(
    channel_id: str,
    factory: SessionFactory | None = Depends(get_alerting_session_factory),
    _: None = Depends(require_any_role("setup_admin")),
) -> None:
    session_factory = _require_factory(factory)
    with session_factory() as session:
        if not delete_alert_channel(session, channel_id):
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=f"Alert channel not found: {channel_id}"
            )
        session.commit()


@staff_router.get(
    "/alert-events",
    response_model=list[AlertEvent],
    summary="List alert events",
    responses={503: {"description": _DB_NOT_READY}},
)
def list_alert_events(
    state: str | None = Query(None, pattern="^(firing|resolved)$"),
    severity: str | None = Query(None, pattern="^(critical|warning|info)$"),
    since: datetime | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    factory: SessionFactory | None = Depends(get_alerting_session_factory),
    _: None = Depends(require_any_role("support_admin", "setup_admin", "meeting_operator")),
) -> list[AlertEvent]:
    session_factory = _require_factory(factory)
    with session_factory() as session:
        return get_alert_events(session, state=state, severity=severity, since=since, limit=limit)


@staff_router.post(
    "/alert-events/{event_id}/ack",
    response_model=AlertEvent,
    summary="Acknowledge an alert event",
    responses={404: {"description": "Event not found"}, 503: {"description": _DB_NOT_READY}},
)
def acknowledge_event(
    event_id: str,
    request: Request,
    factory: SessionFactory | None = Depends(get_alerting_session_factory),
    _: None = Depends(require_any_role("support_admin", "setup_admin", "meeting_operator")),
) -> AlertEvent:
    session_factory = _require_factory(factory)
    with session_factory() as session:
        event = acknowledge_alert_event(
            session, event_id, by=_operator_name(request), at=datetime.now(tz=UTC)
        )
        if event is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, detail=f"Alert event not found: {event_id}"
            )
        session.commit()
        return event
