# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Real (production) service-control seams for the D3 upgrade engine (WP-4).

WP-3 shipped the pure orchestrator + journal and left THREE seams unwired,
raising ``NotImplementedError`` from
:func:`civiccast.native.upgrade.__main__._resolve_service_control_seams`
(they cross into the Windows Service Control Manager + the running supervisor).
This module wires all three to real production callables:

* **drain_and_verify_quiescence** (D3 step 2). The D7a maintenance interlock is
  ALREADY held (step 1): every native start path honors it and the RUNNING
  supervisor, observing the held interlock, transitions to ``maintenance`` --
  it controlled-stops the writer-capable control plane and starts NO media
  workers, while Postgres stays up (the read path the backup/migrate
  need). We therefore do NOT issue a service/graceful stop here (that would
  also stop Postgres, which the pre-upgrade backup and ``alembic upgrade`` both
  connect to). Instead we (a) CONFIRM the writers actually drained by polling
  the supervisor's own status over the D7 control pipe until
  ``workers_permitted`` is False (or budget), and (b) PROVE quiescence exactly
  as D3 names it -- WS2 ``snapshot_tables`` equality across a settle interval:
  if any writer were still landing rows the two snapshots differ and we refuse.
  Both gates are fail-closed.
* **health_gate** (D3 step 6). SCM-start the service (idempotent) -- with the
  interlock held it comes up in maintenance/read-only mode -- then poll the
  supervisor's OWN maintenance-readiness gate
  (:func:`civiccast.native.supervisor.children.check_control_plane_maintenance_ready`,
  reused verbatim, not re-implemented) until it attests green (or budget).
* **stop_service** (D3 halt path). SCM-stop the service so no binary runs
  against a schema it does not match; tolerate "already stopped", raise on a
  genuine SCM failure (the halt must know its stop actually happened).

House pattern (mirrors :mod:`civiccast.native.win_probes` and
:mod:`civiccast.native.upgrade.seams`): the seam LOGIC is pure over injected
OS/DB primitives so it runs under fakes on Linux/CI; the real Win32 SCM, the
control-pipe round trip, the ``/health`` GET, and the live-Postgres snapshot
are lazily bound and only fire on the elevated install host (the WP-5 live
matrix). ``import civiccast.native.upgrade.service_control`` must succeed on
Linux -- every pywin32/registry import is lazy, inside the function that needs
it.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from civiccast.db import connect_options
from civiccast.db.url import normalize_database_url
from civiccast.native.supervisor.children import (
    ControlPlaneHealthProbe,
    check_control_plane_maintenance_ready,
)
from civiccast.native.supervisor.config import CONTROL_PIPE_NAME, SERVICE_NAME
from civiccast.native.upgrade.models import UpgradeContext
from civiccast.native.win_probes import SC_EXE

# Seam primitive shapes (documented callable contracts).
WritersActiveProbe = Callable[[], bool | None]
"""``() -> True | False | None``. True: writer-capable workers are running
(NOT drained). False: writers are drained (``workers_permitted`` is False).
None: the supervisor status could not be read (fail-closed -> not drained)."""

SnapshotDigest = Callable[[], str]
"""``() -> str``. A single deterministic digest over every app table's WS2
``snapshot_tables`` row-count + content checksum -- two equal reads a settle
interval apart prove no writer is landing rows (quiescence)."""

MaintenanceReadyProbe = Callable[[], bool]
"""``() -> bool``. True iff the running control plane attests maintenance/
read-only health (the ``check_control_plane_maintenance_ready`` gate)."""

# Bounded-poll defaults. Deliberately generous: an upgrade window trades a
# little wall time for certainty. All are overridable at build time.
DEFAULT_DRAIN_BUDGET_SECONDS = 60.0
DEFAULT_DRAIN_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_QUIESCENCE_SETTLE_SECONDS = 1.0
DEFAULT_QUIESCENCE_ATTEMPTS = 5
DEFAULT_HEALTH_BUDGET_SECONDS = 120.0
DEFAULT_HEALTH_POLL_INTERVAL_SECONDS = 2.0

# The control-plane base URL the maintenance-readiness probe GETs ``/health``
# from. Mirrors the service layer's default + env key (service.py) so the two
# read the same endpoint.
_ENV_CONTROL_PLANE_URL = "CIVICCAST_CONTROL_PLANE_URL"
_DEFAULT_CONTROL_PLANE_URL = "http://127.0.0.1:8000"
_HEALTH_HTTP_TIMEOUT_SECONDS = 2.0
_HEALTH_BODY_FIELDS = ("mode", "workers_started", "mutating_disabled", "mode_contract")

# SCM error codes we treat as "already in the target state" (idempotent).
_ERROR_SERVICE_ALREADY_RUNNING = 1056
_ERROR_SERVICE_NOT_ACTIVE = 1062

# `sc query <name>` exits 0 when the service EXISTS (any run state) and 1060
# (ERROR_SERVICE_DOES_NOT_EXIST) when no such service is registered -- the
# same documented Windows SCM contract
# civiccast.native.win_probes._default_wsl_service_present already keys its
# own "is this Windows service registered at all" classification on (see that
# function's docstring: `sc.exe`, not winreg, because some Store-packaged /
# manifested services resolve through the SCM but not through the raw
# Services registry key).
_ERROR_SERVICE_DOES_NOT_EXIST = 1060
_SERVICE_QUERY_TIMEOUT_SECONDS = 5.0

ClockFn = Callable[[], float]
SleepFn = Callable[[float], None]

#: <installer-path-audit MA-12> The digest ``_real_snapshot_digest`` produces
#: when the table enumeration returns NOTHING -- sha256 over an empty byte
#: stream. Named here so the drain seam can refuse it explicitly instead of
#: comparing two copies of it and calling the equality proof of quiescence.
EMPTY_SNAPSHOT_DIGEST = hashlib.sha256().hexdigest()


# ---------------------------------------------------------------------------
# Pure classifiers / probe builders (fully unit-testable, no Win32)
# ---------------------------------------------------------------------------


def classify_writers_active(status_reply: dict[str, Any]) -> bool | None:
    """Classify a D7 control-pipe ``status`` reply into a writers-active tri-state.

    The reply envelope is ``build_response``'s shape:
    ``{"v":1,"cmd":"status","result":"ok","data": <StatusSnapshot>}``. Only an
    ``ok`` result carrying a boolean ``workers_permitted`` is trustworthy:
    ``workers_permitted`` is True in exactly the writer-capable serving states
    (``ready``/``degraded``) and False in ``maintenance``/``stopping``/
    ``blocked_*`` -- so it is the exact "are writers running" signal. A denied/
    error result, or a missing/non-boolean field, is UNKNOWN (``None``,
    fail-closed)."""

    if status_reply.get("result") != "ok":
        return None
    data = status_reply.get("data")
    if not isinstance(data, dict):
        return None
    workers_permitted = data.get("workers_permitted")
    if not isinstance(workers_permitted, bool):
        return None
    return workers_permitted


def build_maintenance_ready_probe(
    health_check: Callable[[], ControlPlaneHealthProbe],
) -> MaintenanceReadyProbe:
    """A maintenance-readiness probe over ``health_check``, delegating the verdict
    to the supervisor's OWN fail-closed gate
    (:func:`check_control_plane_maintenance_ready`) -- reused verbatim so the
    attestation contract (200 + ``mode=="maintenance"`` + ``workers_started is
    False`` + ``mutating_disabled is True`` + ``mode_contract==1``) is defined in
    exactly one place. Returns True iff that gate reports ``ready``."""

    def _probe() -> bool:
        return check_control_plane_maintenance_ready(health_check).outcome == "ready"

    return _probe


# ---------------------------------------------------------------------------
# Seam builders (pure poll/quiescence logic over injected primitives)
# ---------------------------------------------------------------------------


def build_drain_seam(
    *,
    writers_active_probe: WritersActiveProbe,
    snapshot_digest: SnapshotDigest,
    clock: ClockFn = time.monotonic,
    sleep: SleepFn = time.sleep,
    drain_budget_seconds: float = DEFAULT_DRAIN_BUDGET_SECONDS,
    poll_interval_seconds: float = DEFAULT_DRAIN_POLL_INTERVAL_SECONDS,
    settle_seconds: float = DEFAULT_QUIESCENCE_SETTLE_SECONDS,
    quiescence_attempts: int = DEFAULT_QUIESCENCE_ATTEMPTS,
) -> Callable[[], bool]:
    """D3 step 2: confirm writers drained, then prove DB quiescence (WS2).

    Returns a callable that (1) polls ``writers_active_probe`` until it reports
    False (writers drained by the held interlock) or the drain budget expires,
    then (2) takes two ``snapshot_digest`` reads a settle interval apart and
    passes only if a pair is equal (no writes landing), retrying up to
    ``quiescence_attempts`` times. Returns True iff BOTH gates pass; False on
    either failure (fail-closed -> the orchestrator rolls back, which is safe).
    Quiescence is never even sampled unless the drain is first confirmed."""

    def _drain_and_verify_quiescence() -> bool:
        deadline = clock() + drain_budget_seconds
        drained = False
        while True:
            if writers_active_probe() is False:
                drained = True
                break
            if clock() >= deadline:
                break
            sleep(poll_interval_seconds)
        if not drained:
            return False

        for _ in range(quiescence_attempts):
            first = snapshot_digest()
            # <installer-path-audit MA-12> The digest is sha256 over
            # `snapshot_tables(engine)`, whose table names come from
            # `inspect(engine).get_table_names(schema=...)`. If that returns []
            # -- wrong schema resolved, schema absent, a dialect/schema-
            # translate regression -- the digest is sha256-over-nothing, a
            # CONSTANT, so `first == second` passed on the first attempt and
            # this gate (the one proving no writers are landing before a backup
            # is taken) was vacuously true. Both sides came from the same
            # source and could not disagree. EMPTY_SNAPSHOT_DIGEST is that
            # constant; matching it means the enumeration found nothing, which
            # is a failure to observe, not an observation of quiet.
            if first == EMPTY_SNAPSHOT_DIGEST:
                return False
            sleep(settle_seconds)
            second = snapshot_digest()
            if first == second:
                return True
        return False

    return _drain_and_verify_quiescence


@dataclass(frozen=True)
class ControlPlaneIdentity:
    """Who answered ``/health``, and what schema they are running against.

    <installer-path-audit BL-04> The maintenance attestation
    (``200 + mode=="maintenance" + workers_started is False +
    mutating_disabled is True + mode_contract==1``) names NO product version,
    NO build identity and NO schema revision -- and it is the last gate before
    COMPLETE / exit 0. So a supervisor left running from BEFORE the upgrade,
    which is in maintenance mode precisely because the interlock is held,
    answers green on the first poll: the upgrade is certified by the OLD code
    attesting to itself. Any other process listening on 8000 that returns that
    JSON shape passes too.

    These are the fields PR #143 made unconditional in ``civiccast/app.py``
    for exactly this consumer.
    """

    version: str | None = None
    schema_db_revision: str | None = None
    schema_expected_head: str | None = None


IdentityProbe = Callable[[], ControlPlaneIdentity]
"""``() -> ControlPlaneIdentity``. Reads /health and reports who answered."""


def evaluate_control_plane_identity(
    identity: ControlPlaneIdentity,
    *,
    expected_version: str,
    expected_schema_head: str,
) -> str | None:
    """Return a refusal reason, or None when the attester is the right process.

    Pure, so the gate's judgement is testable without a live control plane.
    """

    if identity.version is None:
        return "the /health attestation carries no product version"
    if identity.version != expected_version:
        return (
            f"the process attesting maintenance health reports version {identity.version!r}, "
            f"but this upgrade is installing {expected_version!r} -- the OLD supervisor is "
            "still the one answering"
        )
    if identity.schema_db_revision is None or identity.schema_expected_head is None:
        return "the /health attestation carries no schema revision pair"
    if identity.schema_db_revision != expected_schema_head:
        return (
            f"the attesting process reports the database at revision "
            f"{identity.schema_db_revision!r}, but this payload expects {expected_schema_head!r}"
        )
    if identity.schema_expected_head != expected_schema_head:
        return (
            f"the attesting process expects migration head "
            f"{identity.schema_expected_head!r}, but this payload expects "
            f"{expected_schema_head!r} -- it is not running this upgrade's code"
        )
    return None


def build_health_gate_seam(
    *,
    ensure_started: Callable[[], None],
    maintenance_ready_probe: MaintenanceReadyProbe,
    identity_probe: IdentityProbe | None = None,
    expected_version: str | None = None,
    expected_schema_head: str | None = None,
    clock: ClockFn = time.monotonic,
    sleep: SleepFn = time.sleep,
    health_budget_seconds: float = DEFAULT_HEALTH_BUDGET_SECONDS,
    poll_interval_seconds: float = DEFAULT_HEALTH_POLL_INTERVAL_SECONDS,
) -> Callable[[], bool]:
    """D3 step 6: start the service in maintenance mode and poll it green.

    Returns a callable that idempotently SCM-starts the service (with the D7a
    interlock held it boots into maintenance/read-only mode) and then polls
    ``maintenance_ready_probe`` until it attests green or the budget expires.
    Returns True iff green within budget; False otherwise (the orchestrator then
    rolls back). The start is issued BEFORE the first probe so a not-yet-running
    service is brought up rather than instantly failed.

    <installer-path-audit BL-04> When ``identity_probe`` and both expectations
    are supplied, a green attestation is not enough: the attester must also be
    the NEW code reporting the NEW schema (see
    :func:`evaluate_control_plane_identity`). The three parameters are optional
    together so a fake-seam test that only exercises the poll loop is
    unaffected; production supplies all three.
    """

    identity_gate_wired = (
        identity_probe is not None
        and expected_version is not None
        and expected_schema_head is not None
    )

    def _health_gate() -> bool:
        ensure_started()
        deadline = clock() + health_budget_seconds
        while True:
            if maintenance_ready_probe():
                if not identity_gate_wired:
                    return True
                assert identity_probe is not None
                assert expected_version is not None
                assert expected_schema_head is not None
                refusal = evaluate_control_plane_identity(
                    identity_probe(),
                    expected_version=expected_version,
                    expected_schema_head=expected_schema_head,
                )
                if refusal is None:
                    return True
                # Keep polling: the OLD supervisor answering first is exactly
                # the case this gate exists for, and the NEW one may still be
                # coming up. The budget is what bounds it.
                if clock() >= deadline:
                    return False
                sleep(poll_interval_seconds)
                continue
            if clock() >= deadline:
                return False
            sleep(poll_interval_seconds)

    return _health_gate


def build_stop_service_seam(*, scm_stop: Callable[[], None]) -> Callable[[], None]:
    """D3 halt path: ensure the service is stopped.

    A thin, honest wrapper: the halt path calls this to guarantee no binary runs
    against a schema it does not match. Any failure of the real SCM stop
    PROPAGATES (the halt must not falsely believe it stopped the service); the
    real primitive is responsible for treating an already-stopped service as
    success."""

    def _stop_service() -> None:
        scm_stop()

    return _stop_service


# ---------------------------------------------------------------------------
# Real production primitives (lazy Win32 / pg; only fire on the install host)
# ---------------------------------------------------------------------------


def _control_plane_url() -> str:
    return os.environ.get(_ENV_CONTROL_PLANE_URL, _DEFAULT_CONTROL_PLANE_URL)


def _real_health_check() -> ControlPlaneHealthProbe:
    """GET ``<control-plane>/health`` via stdlib ``urllib`` and parse it into a
    :class:`ControlPlaneHealthProbe` (mirrors the service layer's probe). Any
    error yields a probe carrying the HTTP code (or 0) so the readiness gate
    fails CLOSED -- never raises."""

    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    health_url = _control_plane_url().rstrip("/") + "/health"
    try:
        # _control_plane_url() reads an environment variable with a loopback
        # default, so the scheme is not fixed at build time. urlopen would
        # happily follow file:/ and hand the bytes to the JSON parse below as
        # if they were a health body. Reject anything but HTTP(S) first;
        # raising here is deliberate, since the except clauses fail the probe
        # CLOSED. Mirrors the identical guard in
        # civiccast/native/supervisor/service.py's health_probe.
        if urllib.parse.urlparse(health_url).scheme not in ("http", "https"):
            raise ValueError(f"refusing non-HTTP control-plane URL: {health_url!r}")
        with urllib.request.urlopen(health_url, timeout=_HEALTH_HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310  # nosec B310 - scheme checked immediately above
            status_code = int(getattr(resp, "status", 0) or 0)
            raw_body = resp.read()
    except urllib.error.HTTPError as exc:
        return ControlPlaneHealthProbe(status_code=int(exc.code))
    except Exception:
        return ControlPlaneHealthProbe(status_code=0)

    body_fields: dict[str, Any] = {}
    try:
        parsed = json.loads(raw_body)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        for key in _HEALTH_BODY_FIELDS:
            if key in parsed:
                body_fields[key] = parsed[key]
    try:
        return ControlPlaneHealthProbe(status_code=status_code, **body_fields)
    except Exception:
        return ControlPlaneHealthProbe(status_code=status_code)


def _real_service_registered_probe(
    service_name: str = SERVICE_NAME,
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> bool | None:
    """Is ``service_name`` registered in the Service Control Manager at all --
    a question distinct from whether it is REACHABLE over the D7 control pipe.

    CRITICAL fix (upgrade-routing WP, 2026-07-30): after a real uninstall
    (NSIS_HOOK_PREUNINSTALL's ``--civiccast-teardown-native-state`` call
    removes the service registration), the reinstall's D3 drain used to see
    every pipe-connect attempt fail with a transport error and land in the
    SAME ``None`` (fail-closed) bucket as a service that IS running but
    momentarily unreachable over the pipe -- so the drain always burned its
    whole budget waiting for a ``False`` that could never arrive, and every
    reinstall rolled back (audit finding #35). This probe lets the caller ask
    the more fundamental question FIRST, before ever touching the pipe.

    Uses ``sc query`` (never winreg) via an injectable ``runner`` -- the exact
    precedent :func:`civiccast.native.win_probes._default_wsl_service_present`
    already set for the identical "is this Windows service registered at
    all" question (some Store-packaged/manifested services resolve through
    the SCM but not through the raw Services registry key).

    Returns:
      * ``False`` -- DEFINITE: the query exits 1060
        (``ERROR_SERVICE_DOES_NOT_EXIST``). Windows itself confirms no such
        service is registered, so no writer-capable process can possibly be
        running under it.
      * ``None`` -- any other exit code (including 0 -- the service DOES
        exist, which says nothing about whether its control pipe is
        reachable), a timeout, or an ``OSError`` launching ``sc.exe``.
        Ambiguous; stays fail-closed exactly like an unreadable pipe status.
    """

    try:
        completed = runner(
            [str(SC_EXE), "query", service_name],
            capture_output=True,
            timeout=_SERVICE_QUERY_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode == _ERROR_SERVICE_DOES_NOT_EXIST:
        return False
    return None


def _real_service_stopped_probe(
    service_name: str = SERVICE_NAME,
    *,
    query_status: Callable[[str], Any] | None = None,
    stopped_state: int | None = None,
) -> bool | None:
    """Return whether SCM definitively reports ``service_name`` STOPPED.

    Install-over-existing quiesces the old supervisor before any tree work but
    deliberately keeps the service registered: that registration is D3's
    product-existence signal. Once stopped, its authenticated control pipe is
    correctly offline, so a pipe-only drain probe would otherwise wait for a
    status reply that cannot arrive and roll the upgrade back.

    The SCM status is the authoritative process-lifecycle answer here. ``True``
    means STOPPED (or definitively absent), ``False`` means a concrete other
    service state, and ``None`` means the query could not be trusted. Imports
    stay lazy so this module remains importable on non-Windows CI hosts.
    """

    if query_status is None:
        try:
            import win32service  # type: ignore[import-untyped]
            import win32serviceutil  # type: ignore[import-untyped]
        except ImportError:
            return None
        query_status = win32serviceutil.QueryServiceStatus
        stopped_state = win32service.SERVICE_STOPPED
    if stopped_state is None:
        return None

    try:
        status = query_status(service_name)
    except Exception as exc:  # pywintypes.error is available only on Windows
        if getattr(exc, "winerror", None) == _ERROR_SERVICE_DOES_NOT_EXIST:
            return True
        return None

    try:
        current_state = int(status[1])
    except (IndexError, TypeError, ValueError):
        return None
    return current_state == stopped_state


def _real_writers_active_probe(
    pipe_name: str = CONTROL_PIPE_NAME,
    *,
    service_name: str = SERVICE_NAME,
    service_registered_probe: Callable[[str], bool | None] | None = None,
    service_stopped_probe: Callable[[str], bool | None] | None = None,
) -> bool | None:
    """Read the running supervisor's status over the D7 control pipe and classify
    whether writers are active. The control CLIENT verifies the server's pipe
    owner is SYSTEM/Administrators before transacting (anti-squat); a refusal or
    any transport error is an unreadable status -> ``None`` (fail-closed).
    Lazily imported so this module loads on Linux.

    CRITICAL fix (upgrade-routing WP, 2026-07-30): before ever touching
    the pipe, ask whether the service is registered in the SCM at all
    (:func:`_real_service_registered_probe`). A DEFINITE "not registered"
    (``False``) means writers cannot possibly be running -- return ``False``
    (drained) immediately, without a single pipe round trip. Any other SCM
    answer next receives an authoritative SCM state query. A DEFINITE STOPPED
    answer also returns ``False``: install-over-existing intentionally keeps
    registration and upgrade identity while quiescing the old process tree.
    Every running, transitional, or ambiguous state still falls through to
    the authenticated control-pipe check and remains fail-closed on an
    unreadable pipe."""

    probe = (
        service_registered_probe
        if service_registered_probe is not None
        else _real_service_registered_probe
    )
    if probe(service_name) is False:
        return False

    stopped_probe = (
        service_stopped_probe if service_stopped_probe is not None else _real_service_stopped_probe
    )
    if stopped_probe(service_name) is True:
        return False

    from civiccast.native.supervisor.control_client import (
        ControlClientTransportError,
        ControlServerUntrustedError,
        build_control_client,
    )

    client = build_control_client(name=pipe_name)
    try:
        reply = client.status()
    except (ControlServerUntrustedError, ControlClientTransportError, OSError):
        return None
    return classify_writers_active(reply)


def _real_snapshot_digest(database_url: str) -> str:
    """A single deterministic digest over the live DB's WS2 ``snapshot_tables``
    (every table's row count + content checksum). Two equal reads a settle
    interval apart prove quiescence. Uses a short-lived engine per read so each
    call observes the CURRENT committed state (not a stale cached snapshot)."""

    import hashlib

    from sqlalchemy import create_engine

    from civiccast.dr.backup import snapshot_tables

    # normalize_database_url: a bare `postgresql://` scheme maps to the
    # uninstalled psycopg2 dialect (ADR 0008 ships psycopg v3 only) --
    # beta BLOCKER #51.
    engine = create_engine(
        normalize_database_url(database_url),
        future=True,
        pool_pre_ping=True,
        # psycopg v3 without connect_timeout can hang minutes on Windows (task #51).
        # Pinned: the quiescence phase makes up to six snapshot connections
        # after the drain deadline; a CIVICCAST_DB_CONNECT_TIMEOUT of 60
        # would turn the designed 60s worst case into 360s (sol audit
        # 2026-08-09). Site timing contract wins over global tuning.
        **connect_options(database_url, timeout_seconds=10),
    )
    try:
        snapshots = snapshot_tables(engine)
    finally:
        engine.dispose()
    hasher = hashlib.sha256()
    for snap in sorted(snapshots, key=lambda s: s.name):
        hasher.update(snap.name.encode("utf-8"))
        hasher.update(b"\x1f")
        hasher.update(str(snap.row_count).encode("utf-8"))
        hasher.update(b"\x1f")
        hasher.update(snap.checksum_sha256.encode("utf-8"))
        hasher.update(b"\x1e")
    return hasher.hexdigest()


def _real_scm_start(service_name: str = SERVICE_NAME) -> None:
    """SCM-start ``service_name`` via pywin32, treating "already running"
    (ERROR_SERVICE_ALREADY_RUNNING, 1056) as success (idempotent). Lazily
    imports pywin32 (Windows-only)."""

    import pywintypes
    import win32serviceutil

    try:
        win32serviceutil.StartService(service_name)
    except pywintypes.error as exc:
        if getattr(exc, "winerror", None) == _ERROR_SERVICE_ALREADY_RUNNING:
            return
        raise


def _real_scm_stop(service_name: str = SERVICE_NAME) -> None:
    """SCM-stop ``service_name`` via pywin32, treating "not active"
    (ERROR_SERVICE_NOT_ACTIVE, 1062) as success (already stopped). Any other
    SCM error PROPAGATES so the halt path knows its stop did not take. Lazily
    imports pywin32 (Windows-only)."""

    import pywintypes
    import win32serviceutil

    try:
        win32serviceutil.StopService(service_name)
    except pywintypes.error as exc:
        if getattr(exc, "winerror", None) == _ERROR_SERVICE_NOT_ACTIVE:
            return
        raise


# ---------------------------------------------------------------------------
# Production resolution — bound to the real primitives (what __main__ calls)
# ---------------------------------------------------------------------------


def _real_identity_probe() -> ControlPlaneIdentity:
    """GET ``<control-plane>/health`` and report who answered (BL-04).

    Separate from :func:`_real_health_check` because that one parses only the
    four maintenance-contract fields (``_HEALTH_BODY_FIELDS``) into the
    supervisor's own typed probe; widening that model would change a shape the
    supervisor also uses. Never raises -- an unreadable body yields an empty
    identity, which :func:`evaluate_control_plane_identity` refuses.
    """

    import json
    import urllib.parse
    import urllib.request

    health_url = _control_plane_url().rstrip("/") + "/health"
    try:
        if urllib.parse.urlparse(health_url).scheme not in ("http", "https"):
            raise ValueError(f"refusing non-HTTP control-plane URL: {health_url!r}")
        with urllib.request.urlopen(health_url, timeout=_HEALTH_HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310  # nosec B310 - scheme checked immediately above
            parsed = json.loads(resp.read())
    except Exception:
        return ControlPlaneIdentity()
    if not isinstance(parsed, dict):
        return ControlPlaneIdentity()

    def _string(key: str) -> str | None:
        value = parsed.get(key)
        return value if isinstance(value, str) else None

    return ControlPlaneIdentity(
        version=_string("version"),
        schema_db_revision=_string("schema_db_revision"),
        schema_expected_head=_string("schema_expected_head"),
    )


def resolve_service_control_seams(
    context: UpgradeContext,
    *,
    expected_version: str | None = None,
    expected_schema_head: str | None = None,
) -> tuple[Callable[[], bool], Callable[[], bool], Callable[[], None]]:
    """Return ``(drain_and_verify_quiescence, health_gate, stop_service)`` bound
    to the real production primitives for ``context``.

    This is the WP-4 replacement for WP-3's ``NotImplementedError`` stubs. The
    seams resolve without touching Win32/Postgres (construction is inert); the
    real SCM / control-pipe / ``/health`` / live-Postgres calls only fire when a
    seam is INVOKED on the elevated install host (the WP-5 live matrix)."""

    drain = build_drain_seam(
        writers_active_probe=_real_writers_active_probe,
        snapshot_digest=lambda: _real_snapshot_digest(context.database_url),
    )
    health = build_health_gate_seam(
        ensure_started=_real_scm_start,
        maintenance_ready_probe=build_maintenance_ready_probe(_real_health_check),
        # <installer-path-audit BL-04> The attester must be the NEW code on the
        # NEW schema, not merely A process in maintenance mode.
        identity_probe=_real_identity_probe,
        expected_version=expected_version,
        expected_schema_head=expected_schema_head,
    )
    stop = build_stop_service_seam(scm_stop=_real_scm_stop)
    return drain, health, stop


__all__ = [
    "MaintenanceReadyProbe",
    "SnapshotDigest",
    "WritersActiveProbe",
    "build_drain_seam",
    "build_health_gate_seam",
    "build_maintenance_ready_probe",
    "build_stop_service_seam",
    "classify_writers_active",
    "resolve_service_control_seams",
]
