# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The two direct-child contracts (postgres, control plane) and the D5
restart policy -- PURE and CI-testable everywhere: no subprocess is ever
launched here, no socket is ever opened, no Windows import appears anywhere
in this module. Every piece of I/O the real supervisor needs (spawn a
process, run a DB check, GET /health, sleep) is an injected callable; this
module only decides shapes and outcomes from whatever the caller hands it.

NATS JetStream was removed from the product (owner decision 2026-08-20; see
ADR 0023, which supersedes ADR 0001). It never did real production work --
the platform substrate always defaulted to the in-process broker -- so
cutting it drops a supervised child, a port, a config file, and a health
gate that could fail a station for nothing. The ``nats`` child spec and its
JetStream publish+ack readiness check (``nats_child_spec`` /
``check_nats_ready``) are gone; postgres and control plane are the only two
direct children now.

Grounding (facts cited below are from ``recon/r2-children.md``, read in
full before writing this module -- see also ``design.md`` Sec.3's
``children.py`` package-layout entry and ``design-addendum-ratification.md``
RAT-001 / the egress-workdir AC, both AUTHORITATIVE over ``design.md`` where
they differ):

* **postgres** -- r2 confirms ``pg_ctl`` appears in-repo in exactly ONE
  place, ``spec-supervisor.md`` D5's own text: ``pg_ctl stop -m fast``. No
  production launch convention exists yet (spec-packaging-closure's D6 pg
  verification is forward-looking). This module therefore matches the
  graceful-stop text VERBATIM and uses an ordinary, undisputed ``pg_ctl
  start`` convention for the launch argv -- the one spec-fixed fact is the
  stop command, and that is what the tests pin.
* **control plane** -- r2's exact production invocation (
  ``headless-bootstrap.ps1:780``): ``python -I -m uvicorn
  civiccast.app:create_app --factory --host <host> --port <port>``.
  ``new_process_group=True`` is D5's ``CREATE_NEW_PROCESS_GROUP`` (the
  graceful stop, CTRL_BREAK_EVENT, targets the GROUP, not one pid).
  RAT-001 (addendum, authoritative): a control-plane child launched while
  ``SupervisorState == maintenance`` carries
  ``CIVICCAST_SUPERVISOR_MODE=maintenance`` +
  ``CIVICCAST_SUPERVISOR_MODE_CONTRACT=1`` in its env, and its readiness gate
  is a SEPARATE, fail-closed function (``check_control_plane_maintenance_ready``)
  from the normal-mode gate (``check_control_plane_ready``, D6: GET /health
  200 only -- the addendum's "D6 vs code" note explains DB connectivity is
  checked directly by the supervisor via the postgres readiness check above,
  not through this endpoint). The egress-workdir AC
  (ratified, binding) requires ``CIVICCAST_EGRESS_WORK_DIR`` in the
  control-plane child's env, resolved into the ``ProgramData\\CivicCast\\data``
  tree, so ``default_egress_work_dir()`` in ``civiccast/egress/automation.py``
  never falls through to the SYSTEM profile's ``%LOCALAPPDATA%`` (outside
  D4's ACL) -- set unconditionally, in both normal and maintenance mode.
  Grounded fix (2026-08-01 investigation): the same on-air path also needs
  ``CIVICCAST_UPLOAD_DIR``, which a native install never set at all --
  ``civiccast/app.py``'s ``_configure_upload_dir`` yields to any already-set
  value and only then falls back to ``_managed_upload_dir_if_ready()``, a
  separate install-state-dependent path. :func:`default_upload_dir` mirrors
  :func:`default_egress_work_dir` exactly (``ProgramData\\CivicCast\\data\\
  uploads``, beside ``\\data\\egress``, both plain-``mkdir``/inherited-DACL --
  the PROTECTED SDDL treatment is reserved for credential-bearing state
  roots, not operator media) and is set unconditionally in
  ``control_plane_child_spec``, the same shape as the egress work dir.
  ``CIVICCAST_SUPERVISED_ENV_VAR`` (``CIVICCAST_SUPERVISED=1``) is likewise
  set unconditionally -- see its own docstring -- so the child's
  ``civiccast.app.create_app`` entrypoint knows to attach the control-plane
  INFO file logger (Gate A T4 diagnostic-visibility fix, 2026-09).

D5 restart policy: :func:`backoff_with_jitter` applies the +/-20% jitter on
top of ``states.backoff_base_seconds`` (that module's docstring: "jitter
applied separately by the caller with an injected RNG" -- this is that
caller). :func:`restart_storm_check` is a thin, config-driven wrapper over
``states.is_restart_storm`` (reused, not reimplemented) so a caller holding
a :class:`~civiccast.native.supervisor.config.SupervisorConfig` does not
have to unpack its threshold/window fields by hand at every call site.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from civiccast.ai_runtime.ollama_client import DEFAULT_OLLAMA_BASE_URL
from civiccast.native.supervisor.config import SupervisorConfig
from civiccast.native.supervisor.states import backoff_base_seconds, is_restart_storm

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

ChildName = Literal["postgres", "control_plane", "ollama"]
GracefulStopKind = Literal["argv", "ctrl_break_event"]
ReadinessOutcome = Literal["ready", "not_ready", "timeout", "aborted"]
"""``aborted`` is DISTINCT from ``timeout`` on purpose (F1, 2026-07-31): a poll
that ended because a service STOP was requested did not exhaust its readiness
budget and says nothing about the child's health, so it must never be read (or
logged) as a readiness failure. See :func:`poll_until_ready`'s ``should_abort``."""
ControlPlaneMode = Literal["normal", "maintenance"]

#: Set unconditionally (both normal AND maintenance mode) in the
#: control-plane child's env, the SAME shape as ``CIVICCAST_EGRESS_WORK_DIR``
#: / ``CIVICCAST_UPLOAD_DIR`` above -- signals to ``civiccast.app.create_app``
#: (the uvicorn ``--factory`` entrypoint this argv launches) that it is
#: running as the supervisor's spawned child, not a bare dev run or a test's
#: direct ``create_app()`` call, so it should attach the control-plane INFO
#: file logger (``service.configure_control_plane_logging``) -- see that
#: function's docstring for the diagnosability gap this closes. Read by
#: ``civiccast.app``'s ``_maybe_configure_control_plane_logging``.
CIVICCAST_SUPERVISED_ENV_VAR = "CIVICCAST_SUPERVISED"

# D6 postgres readiness budget (spec value, verbatim). control-plane has no
# spec-fixed budget number -- this is a WS5-chosen, disclosed default
# (overridable by every caller).
POSTGRES_READY_BUDGET_SECONDS = 60.0
# U31 (2026-09-25): 30.0 was too small for a cold start on a loaded box, and it
# cost the station ~11 minutes of dead air. MEASURED on this box (three idle
# runs, `%TEMP%\u31\startup_measurements.json`): a cold control-plane start cost
# 15462.6 / 10883.3 / 11207.9 ms of wall time, of which the interpreter import
# alone was 12669.9 / 8304.3 / 8559.3 ms and `create_app` only 1066.8 / 1302.3 /
# 1331.8 ms -- and on the LOADED station (two other units running ffmpeg/
# GStreamer work) it exceeded 30 s outright, three times in a row. 180.0 is the
# absolute cap, and it is only safe because the same spec carries a no-progress
# window (DEFAULT_CONTROL_PLANE_STALL_SECONDS): a child that has stopped doing
# work fails at 60 s regardless of this number, so the raise cannot make a
# wedge slower to detect than it was.
DEFAULT_CONTROL_PLANE_READY_BUDGET_SECONDS = 180.0
# U31: how long the control plane may make NO CPU progress at all before it is
# read as wedged rather than slow -- sampled by `process_cpu_seconds` and
# applied by `poll_until_ready(progress=..., stall_seconds=...)`. 60.0 s is 4x
# the worst measured idle start (15.5 s) and ~13x the create_app half of it, so
# a merely slow import cannot trip it; a deadlock, a blocking socket read, or a
# thread that will never be scheduled does.
DEFAULT_CONTROL_PLANE_STALL_SECONDS = 60.0
# Task #57 D2: matches the ONE in-repo authority for bringing the staged
# ollama runtime up -- the installer's production self-test
# (apps/installer/src-tauri/src/main.rs, NativeOllamaSelfTestServer::
# wait_until_ready) polls /api/version against a 60-second deadline.
DEFAULT_OLLAMA_READY_BUDGET_SECONDS = 60.0
DEFAULT_GRACEFUL_STOP_DEADLINE_SECONDS = 15.0  # D5: 15s deadline, then TerminateProcess.

# Task #57 D2: the host:port the OPTIONAL ollama child must serve on --
# derived from the SAME constant the app-side consumers dial
# (civiccast/ai_runtime/ollama_client.py's DEFAULT_OLLAMA_BASE_URL,
# ``http://127.0.0.1:11434``; summary/ollama.py and translate/ollama.py both
# default their ``base_url`` to it and the control-plane env carries no
# override), so the supervisor-launched server and the control plane's
# client can never silently disagree on where local AI lives.
_OLLAMA_RUNTIME_NETLOC = urlparse(DEFAULT_OLLAMA_BASE_URL)
DEFAULT_OLLAMA_HOST: str = _OLLAMA_RUNTIME_NETLOC.hostname or "127.0.0.1"
DEFAULT_OLLAMA_PORT: int = _OLLAMA_RUNTIME_NETLOC.port or 11434

# ---------------------------------------------------------------------------
# ChildSpec + GracefulStopAction
# ---------------------------------------------------------------------------


class ChildSpec(BaseModel):
    """One direct child's static launch + graceful-stop shape (design.md
    Sec.3). Pure data -- never spawns anything. ``extra="forbid"`` so an
    unknown key (typo, schema drift) fails at construction, matching the
    house fail-closed posture every other native model in this package
    carries.
    """

    model_config = ConfigDict(extra="forbid")

    name: ChildName
    argv: list[str]
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    # D3/D5: CREATE_NEW_PROCESS_GROUP -- only the control-plane child sets
    # this (its graceful stop, CTRL_BREAK_EVENT, targets the group).
    new_process_group: bool = False

    graceful_stop_kind: GracefulStopKind
    # Literal argv tokens for a command-based graceful stop (postgres). A
    # token containing the substring "{pid}" is substituted with the live
    # child's pid by graceful_stop_action(); postgres's own template carries
    # no such token today, but the substitution stays generic. Empty (and
    # therefore meaningless) for graceful_stop_kind == "ctrl_break_event",
    # which carries no argv at all.
    graceful_stop_argv_template: list[str] = Field(default_factory=list)
    graceful_stop_deadline_seconds: float = Field(gt=0)

    readiness_budget_seconds: float = Field(gt=0)

    # U31 (2026-09-25): how long this child may make NO CPU progress while its
    # readiness gate is polling, before the poll stops reading it as slow and
    # starts reading it as wedged. None (the default, and every child but the
    # control plane) means the budget above is the only rule -- the exact
    # pre-U31 behaviour, correct for a child whose readiness probe is a
    # one-shot statement (postgres' `SELECT 1`, ollama's `GET /api/version`)
    # rather than a process that must import an application first. Only
    # meaningful together with `poll_until_ready`'s `progress` seam, which
    # `core.start_child` supplies from `process_cpu_seconds` exactly when this
    # is set.
    readiness_stall_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_graceful_stop_shape(self) -> ChildSpec:
        if self.graceful_stop_kind == "argv" and not self.graceful_stop_argv_template:
            raise ValueError(
                "graceful_stop_argv_template is required when graceful_stop_kind == 'argv'"
            )
        if self.graceful_stop_kind == "ctrl_break_event" and self.graceful_stop_argv_template:
            raise ValueError(
                "graceful_stop_argv_template must be empty when "
                "graceful_stop_kind == 'ctrl_break_event' (CTRL_BREAK_EVENT carries no argv)"
            )
        return self


class GracefulStopAction(BaseModel):
    """The CONCRETE graceful-stop action for one live child instance --
    computed at stop time (:func:`graceful_stop_action`) once the child's
    real pid is known, never stored statically on :class:`ChildSpec`."""

    model_config = ConfigDict(extra="forbid")

    kind: GracefulStopKind
    argv: list[str] | None = None
    target_pid: int | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> GracefulStopAction:
        if self.kind == "argv":
            if not self.argv:
                raise ValueError("argv is required when kind == 'argv'")
            if self.target_pid is not None:
                raise ValueError("target_pid must be unset when kind == 'argv'")
        else:  # kind == "ctrl_break_event"
            if self.target_pid is None:
                raise ValueError("target_pid is required when kind == 'ctrl_break_event'")
            if self.argv is not None:
                raise ValueError("argv must be unset when kind == 'ctrl_break_event'")
        return self


def graceful_stop_action(spec: ChildSpec, *, pid: int) -> GracefulStopAction:
    """Resolve ``spec``'s graceful-stop shape against the LIVE child's pid.

    For ``ctrl_break_event`` (control plane) the pid IS the action -- it
    names the process group CTRL_BREAK_EVENT targets. For ``argv``
    (postgres), any token containing ``"{pid}"`` is substituted; postgres's
    template carries no such token (its stop command needs no pid, only
    ``-D <data_dir>``), so the same spec always produces the same stop argv
    regardless of pid.
    """

    if spec.graceful_stop_kind == "ctrl_break_event":
        return GracefulStopAction(kind="ctrl_break_event", target_pid=pid)
    argv = [token.format(pid=pid) for token in spec.graceful_stop_argv_template]
    return GracefulStopAction(kind="argv", argv=argv)


# ---------------------------------------------------------------------------
# Egress work-dir (ratified, binding AC)
# ---------------------------------------------------------------------------


def default_egress_work_dir(*, program_data_root: str | None = None) -> str:
    """The ``ProgramData\\CivicCast\\data`` path the control-plane child's
    ``CIVICCAST_EGRESS_WORK_DIR`` env var must resolve into (ratification
    addendum egress-workdir AC), so ``civiccast/egress/automation.py``'s
    ``default_egress_work_dir()`` never falls through to the SYSTEM
    profile's ``%LOCALAPPDATA%`` (outside D4's ACL). String-built (not
    ``pathlib.Path``) so the Windows-style backslash path is identical
    regardless of the host OS running this pure module's tests -- mirrors
    ``civiccast/native/runtime_cli.py``'s
    ``os.environ.get("PROGRAMDATA", r"C:\\ProgramData")`` convention.
    """

    root = (program_data_root or os.environ.get("PROGRAMDATA", r"C:\ProgramData")).rstrip("\\")
    return f"{root}\\CivicCast\\data\\egress"


def default_upload_dir(*, program_data_root: str | None = None) -> str:
    """The ``ProgramData\\CivicCast\\data\\uploads`` path the control-plane
    child's ``CIVICCAST_UPLOAD_DIR`` env var must resolve into -- mirrors
    :func:`default_egress_work_dir` exactly (same ``ProgramData\\CivicCast\\
    data`` tree, same string-built-not-``pathlib.Path`` convention so the
    Windows-style backslash path is identical regardless of the host OS
    running this pure module's tests).

    Without this, ``civiccast.app``'s ``_configure_upload_dir`` never sees a
    pre-set ``CIVICCAST_UPLOAD_DIR`` and falls through to
    ``_managed_upload_dir_if_ready()`` (a SEPARATE, install-state-dependent
    resolution path) -- on a native install this is one of the two missing
    inputs that keeps the control plane's only working on-air path (the
    installer rehearsal flow) from running at all. Operator media
    (recordings/uploads) is NOT credential-bearing (unlike
    ``provision/journal.py``'s state root or ``native/pgdata_acl.py``'s
    cluster dir), so this directory gets a plain, inherited-DACL ``mkdir``
    like ``data\\egress`` beside it -- no bespoke SDDL treatment.
    """

    root = (program_data_root or os.environ.get("PROGRAMDATA", r"C:\ProgramData")).rstrip("\\")
    return f"{root}\\CivicCast\\data\\uploads"


# ---------------------------------------------------------------------------
# postgres
# ---------------------------------------------------------------------------


def postgres_child_spec(
    *,
    pg_ctl_path: str = "pg_ctl",
    data_dir: str,
    host: str = "127.0.0.1",
    port: int = 5432,
    extra_env: Mapping[str, str] | None = None,
    graceful_stop_deadline_seconds: float = DEFAULT_GRACEFUL_STOP_DEADLINE_SECONDS,
    readiness_budget_seconds: float = POSTGRES_READY_BUDGET_SECONDS,
) -> ChildSpec:
    """D6/D5 postgres child. The one spec-fixed fact is the graceful-stop
    command, matched verbatim: ``pg_ctl stop -m fast`` (r2-children's only
    in-repo citation). Readiness is a plain ``SELECT 1`` (D6, 60s budget by
    default) -- always injected by the caller via :func:`check_postgres_ready`,
    never performed by this module.

    The production runner captures the merged stdout/stderr of ``pg_ctl`` in
    ``postgres.log`` and drains the inherited pipe continuously. ``pg_ctl
    start -w`` passes those standard handles to the durable postmaster, so
    server diagnostics continue to reach the same bounded file after the
    launcher exits. Do not add ``-l`` here: PostgreSQL would reopen an
    independent, unbounded file and Windows sharing modes can conflict with
    the supervisor-owned rotating sink.
    """

    argv = [pg_ctl_path, "start", "-D", data_dir, "-w", "-o", f"-p {port} -h {host}"]
    return ChildSpec(
        name="postgres",
        argv=argv,
        env=dict(extra_env or {}),
        cwd=None,
        new_process_group=False,
        graceful_stop_kind="argv",
        graceful_stop_argv_template=[pg_ctl_path, "stop", "-D", data_dir, "-m", "fast"],
        graceful_stop_deadline_seconds=graceful_stop_deadline_seconds,
        readiness_budget_seconds=readiness_budget_seconds,
    )


def read_postmaster_pid(data_dir: str) -> int | None:
    """CC-WS5-003: read the DURABLE PostgreSQL postmaster's pid from
    ``<data_dir>/postmaster.pid``.

    PostgreSQL writes the postmaster's own pid on the FIRST line of
    ``postmaster.pid`` inside its data directory (the remaining lines carry the
    data-dir path, start epoch, port, socket dir, listen address, and shared
    memory key -- none of which this reader needs). ``postgres_child_spec``
    launches ``pg_ctl start -D <data_dir> -w``; that launcher self-exits once the
    postmaster is up, so the supervisor must resolve the postmaster's real pid
    from THIS file to contain and monitor the durable process (not the
    short-lived launcher).

    Pure (``pathlib`` only, no subprocess, no Windows import) so it is unit-tested
    on any OS. Returns the first-line pid as an int, or ``None`` if the file is
    missing, unreadable, empty, or its first line is not an integer -- every
    unresolvable case maps to ``None`` so the caller can fail CLOSED rather than
    contain the wrong (or no) process.
    """

    try:
        text = Path(data_dir, "postmaster.pid").read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines:
        return None
    try:
        return int(lines[0].strip())
    except ValueError:
        return None


DbCheckFn = Callable[[], bool]


class ReadinessResult(BaseModel):
    """The outcome of one readiness evaluation -- either a single check
    (``ready``/``not_ready``) or a bounded poll loop (``ready``/``timeout``,
    see :func:`poll_until_ready`)."""

    model_config = ConfigDict(extra="forbid")

    outcome: ReadinessOutcome
    detail: str


def check_postgres_ready(select_one: DbCheckFn) -> ReadinessResult:
    """D6 postgres readiness: the injected ``select_one`` callable performs
    the real ``SELECT 1`` (psycopg) and returns whether it succeeded. A
    raised exception (connection refused, auth failure, ...) is treated the
    same as an explicit ``False`` -- not ready, never propagated -- so a
    single flaky probe can never crash the caller's poll loop."""

    try:
        ok = select_one()
    except Exception as exc:
        return ReadinessResult(outcome="not_ready", detail=f"SELECT 1 check raised: {exc}")
    if ok:
        return ReadinessResult(outcome="ready", detail="SELECT 1 succeeded")
    return ReadinessResult(outcome="not_ready", detail="SELECT 1 did not succeed")


# ---------------------------------------------------------------------------
# control plane
# ---------------------------------------------------------------------------


def control_plane_child_spec(
    *,
    python_path: str = "python",
    host: str = "127.0.0.1",
    port: int = 8000,
    mode: ControlPlaneMode = "normal",
    egress_work_dir: str | None = None,
    upload_dir: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    graceful_stop_deadline_seconds: float = DEFAULT_GRACEFUL_STOP_DEADLINE_SECONDS,
    readiness_budget_seconds: float = DEFAULT_CONTROL_PLANE_READY_BUDGET_SECONDS,
    readiness_stall_seconds: float | None = DEFAULT_CONTROL_PLANE_STALL_SECONDS,
) -> ChildSpec:
    """D6/D5 control-plane child. Launch argv is the production uvicorn
    ``--factory`` invocation, r2-children's exact string
    (``headless-bootstrap.ps1:780``): ``python -I -m uvicorn
    civiccast.app:create_app --factory --host <host> --port <port>``, plus
    ``-u`` (see the diagnosability note below).
    ``new_process_group=True`` is ``CREATE_NEW_PROCESS_GROUP`` (D5:
    CTRL_BREAK_EVENT graceful stop targets the GROUP, not one pid).

    Adjacent diagnosability fix (2026-08-12, TESTER2 b5 evidence):
    ``control_plane.log`` was observed at 0 bytes for a 5+ hour run during
    which the control plane demonstrably served ``GET /health`` 200s the
    whole time. ``-I`` (isolated mode) implies ``-E``, which makes the
    interpreter IGNORE every ``PYTHON*`` environment variable, including
    ``PYTHONUNBUFFERED`` -- so that env var can never be used to unbuffer
    this child's stdio, and nothing here previously tried. Without it,
    Python's stdout stream is fully block-buffered (not line-buffered) once
    it is a redirected file rather than a console, so uvicorn's own INFO
    logging (and anything the app prints) can sit in the child's userspace
    buffer for an effectively unbounded time on a quiet station. ``-u``
    (forces stdin/stdout/stderr unbuffered) is an explicit interpreter flag,
    not an env var, so it survives ``-I``/``-E`` and is not optional here.

    Every launch -- normal AND maintenance -- sets
    ``CIVICCAST_EGRESS_WORK_DIR`` into the ``ProgramData\\CivicCast\\data``
    tree (ratification addendum egress-workdir AC), defaulting to
    :func:`default_egress_work_dir` unless the caller overrides it.

    Every launch -- normal AND maintenance, unconditionally, the SAME shape
    as the egress work dir just above -- also sets ``CIVICCAST_UPLOAD_DIR``,
    defaulting to :func:`default_upload_dir` unless the caller overrides it.
    Without this a native install never sets the var at all, and
    ``civiccast.app``'s ``_configure_upload_dir`` (which YIELDS to any
    already-set ``CIVICCAST_UPLOAD_DIR`` and only then falls back to its own,
    separate, install-state-dependent resolution) never gets the chance.

    ``mode="maintenance"`` additionally sets
    ``CIVICCAST_SUPERVISOR_MODE=maintenance`` +
    ``CIVICCAST_SUPERVISOR_MODE_CONTRACT=1`` (RAT-001) -- the INPUT half of
    the maintenance contract. The readiness HALF is a separate, fail-closed
    gate: :func:`check_control_plane_maintenance_ready`.

    U31 (2026-09-25): this is the ONLY child that carries a
    ``readiness_stall_seconds`` window, because it is the only one whose
    readiness depends on a cold Python start -- measured at 10.9-15.5 s of wall
    time IDLE on this box (8.3-12.7 s of it the interpreter import) and past
    30 s on the loaded station, where a 30 s budget killed it three times and
    cost ~11 minutes of dead air (see
    :data:`DEFAULT_CONTROL_PLANE_READY_BUDGET_SECONDS`). An operator who wants
    the old wall-clock-only rule back passes
    ``readiness_stall_seconds=None`` (and, presumably, the old budget), which
    restores :func:`poll_until_ready`'s pre-U31 behaviour exactly.
    """

    argv = [
        python_path,
        "-I",
        "-u",
        "-m",
        "uvicorn",
        "civiccast.app:create_app",
        "--factory",
        "--host",
        host,
        "--port",
        str(port),
    ]
    env = dict(extra_env or {})
    env["CIVICCAST_EGRESS_WORK_DIR"] = egress_work_dir or default_egress_work_dir()
    env["CIVICCAST_UPLOAD_DIR"] = upload_dir or default_upload_dir()
    env[CIVICCAST_SUPERVISED_ENV_VAR] = "1"
    if mode == "maintenance":
        env["CIVICCAST_SUPERVISOR_MODE"] = "maintenance"
        env["CIVICCAST_SUPERVISOR_MODE_CONTRACT"] = "1"
    return ChildSpec(
        name="control_plane",
        argv=argv,
        env=env,
        cwd=None,
        new_process_group=True,
        graceful_stop_kind="ctrl_break_event",
        graceful_stop_argv_template=[],
        graceful_stop_deadline_seconds=graceful_stop_deadline_seconds,
        readiness_budget_seconds=readiness_budget_seconds,
        readiness_stall_seconds=readiness_stall_seconds,
    )


class ControlPlaneHealthProbe(BaseModel):
    """The subset of the control-plane's ``GET /health`` response the
    supervisor's readiness gates read. ``status_code`` is always present;
    the RAT-001 attestation fields are ``None`` when absent from the body
    (an old or mode-ignoring control plane) -- absence must never be
    silently treated as a positive attestation (fail-closed, see
    :func:`check_control_plane_maintenance_ready`)."""

    model_config = ConfigDict(extra="forbid")

    status_code: int
    mode: Literal["normal", "maintenance", "unknown"] | None = None
    workers_started: bool | None = None
    mutating_disabled: bool | None = None
    mode_contract: int | None = None


HealthCheckFn = Callable[[], ControlPlaneHealthProbe]


def check_control_plane_ready(health_check: HealthCheckFn) -> ReadinessResult:
    """D6 normal-mode readiness gate: ``GET /health`` 200, nothing more.
    (Addendum's "Note on D6 vs code": DB readiness is checked
    DIRECTLY by the supervisor via :func:`check_postgres_ready`, not
    through this endpoint.) A raised
    exception (connection refused, ...) is not ready, never propagated."""

    try:
        probe = health_check()
    except Exception as exc:
        return ReadinessResult(outcome="not_ready", detail=f"GET /health raised: {exc}")
    if probe.status_code == 200:
        return ReadinessResult(outcome="ready", detail="GET /health returned 200")
    return ReadinessResult(outcome="not_ready", detail=f"GET /health returned {probe.status_code}")


def check_control_plane_maintenance_ready(health_check: HealthCheckFn) -> ReadinessResult:
    """RAT-001's maintenance-readiness gate. Satisfied ONLY when ``GET
    /health`` returns 200 AND ``mode == "maintenance"`` AND
    ``workers_started is False`` AND ``mutating_disabled is True`` AND
    ``mode_contract == 1``. Absent, ``"unknown"``, ``"normal"``, any missing
    attestation field, or a contract-version mismatch -> ``not_ready``
    (fail-closed): an old or mode-ignoring control plane can never look
    maintenance-ready, and the freeze holds. A raised exception is treated
    identically to an unattested response -- not ready, never propagated.
    """

    try:
        probe = health_check()
    except Exception as exc:
        return ReadinessResult(outcome="not_ready", detail=f"GET /health raised: {exc}")

    if (
        probe.status_code == 200
        and probe.mode == "maintenance"
        and probe.workers_started is False
        and probe.mutating_disabled is True
        and probe.mode_contract == 1
    ):
        return ReadinessResult(outcome="ready", detail="maintenance attestation satisfied")

    return ReadinessResult(
        outcome="not_ready",
        detail=(
            "maintenance attestation NOT satisfied (fail-closed): "
            f"status={probe.status_code} mode={probe.mode!r} "
            f"workers_started={probe.workers_started!r} "
            f"mutating_disabled={probe.mutating_disabled!r} "
            f"mode_contract={probe.mode_contract!r}"
        ),
    )


# ---------------------------------------------------------------------------
# ollama (task #57 D2: the OPTIONAL local-AI runtime child)
# ---------------------------------------------------------------------------


def ollama_child_spec(
    *,
    ollama_exe_path: str,
    models_dir: str,
    host: str = DEFAULT_OLLAMA_HOST,
    port: int = DEFAULT_OLLAMA_PORT,
    extra_env: Mapping[str, str] | None = None,
    graceful_stop_deadline_seconds: float = DEFAULT_GRACEFUL_STOP_DEADLINE_SECONDS,
    readiness_budget_seconds: float = DEFAULT_OLLAMA_READY_BUDGET_SECONDS,
) -> ChildSpec:
    """The OPTIONAL fourth child: ``ollama serve`` over the installer-staged
    model store, so summary/translation have a live runtime post-install
    (task #57 D2 -- before this child existed, NOTHING launched ollama after
    install and the app-side clients dialed a server that was never there).

    Launch shape mirrors the ONE in-repo production authority for serving
    the staged store -- the installer's own D2 self-test
    (``apps/installer/src-tauri/src/main.rs``, ``NativeOllamaSelfTestServer``):
    the staged ``ollama.exe`` run as ``serve`` from its own directory, with
    ``OLLAMA_HOST``/``OLLAMA_MODELS`` in the SERVER's environment (the whole
    point of #57's D1 finding: the server env decides the store, never a
    client's) plus the offline hardening (``OLLAMA_NO_CLOUD``, ``NO_PROXY``)
    matching the station's ``offline_only`` runtime contract
    (``station_runtime.EXPECTED_RUNTIME_CONTRACT``). The self-test's
    scheduling knobs (``OLLAMA_KEEP_ALIVE=0``, ``OLLAMA_MAX_LOADED_MODELS=1``,
    ``OLLAMA_NUM_PARALLEL=1``) are deliberately NOT copied: they exist to
    make a one-shot install-time probe cheap; at runtime they would force a
    multi-gigabyte model reload on every summary/translation request.

    ``host``/``port`` default to the exact host:port the app-side clients
    dial (:data:`DEFAULT_OLLAMA_HOST`/:data:`DEFAULT_OLLAMA_PORT`, derived
    from ``ai_runtime.ollama_client.DEFAULT_OLLAMA_BASE_URL``). Graceful stop
    is CTRL_BREAK to the child's own process group (``new_process_group=True``,
    same D5 shape as the control plane -- ollama is a Go server whose runner
    subprocesses live in its group); the shared deadline+TerminateProcess
    escalation and the Job Object kill-on-close remain the backstops.
    Readiness is the bounded ``/api/version`` poll (:func:`check_ollama_ready`
    via the provider's probe), matching the self-test's own readiness gate.
    """

    env = dict(extra_env or {})
    env["OLLAMA_HOST"] = f"{host}:{port}"
    env["OLLAMA_MODELS"] = models_dir
    env["OLLAMA_NO_CLOUD"] = "1"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return ChildSpec(
        name="ollama",
        argv=[ollama_exe_path, "serve"],
        env=env,
        cwd=str(Path(ollama_exe_path).parent),
        new_process_group=True,
        graceful_stop_kind="ctrl_break_event",
        graceful_stop_argv_template=[],
        graceful_stop_deadline_seconds=graceful_stop_deadline_seconds,
        readiness_budget_seconds=readiness_budget_seconds,
    )


class OllamaChildDecision(BaseModel):
    """What the service layer's spec provider decided about the OPTIONAL
    ollama child at (re)start time: ``spec`` to launch it, or ``spec=None``
    with ``detail`` naming exactly why it is skipped (binary absent / no
    staged store) -- degraded AI, service healthy, visible in the status
    snapshot's ``blocked_detail``. Pure data, decided at the service layer
    (the only layer allowed to touch the filesystem)."""

    model_config = ConfigDict(extra="forbid")

    spec: ChildSpec | None = None
    detail: str


OllamaVersionProbeFn = Callable[[], bool]


def check_ollama_ready(version_probe: OllamaVersionProbeFn) -> ReadinessResult:
    """Task #57 D2 readiness: the injected ``version_probe`` performs the
    real bounded HTTP ``GET /api/version`` against the runtime base URL and
    returns whether it succeeded -- the same readiness gate the installer's
    production self-test uses. A raised exception is treated as not ready,
    never propagated (same contract as :func:`check_postgres_ready`)."""

    try:
        ok = version_probe()
    except Exception as exc:
        return ReadinessResult(outcome="not_ready", detail=f"GET /api/version raised: {exc}")
    if ok:
        return ReadinessResult(outcome="ready", detail="GET /api/version succeeded")
    return ReadinessResult(outcome="not_ready", detail="GET /api/version did not succeed")


# ---------------------------------------------------------------------------
# Bounded readiness polling (shared across all three children)
# ---------------------------------------------------------------------------

ClockFn = Callable[[], float]
SleepFn = Callable[[float], None]
ReadinessCheckFn = Callable[[], ReadinessResult]
AbortFn = Callable[[], bool]
"""``() -> bool`` -- "has a service stop been requested?". Injected into
:func:`poll_until_ready` (and threaded down from ``core.Supervisor``) so a
long readiness budget cannot hold the stop chain hostage; see F1 below."""

ProgressFn = Callable[[], float | None]
"""``() -> float | None`` -- this child's cumulative CPU seconds so far (see
:func:`process_cpu_seconds`), or ``None`` when it cannot be sampled. Injected
into :func:`poll_until_ready` together with ``stall_seconds`` so a slow start
can be told from a wedged one; see U31 below."""

AliveFn = Callable[[], bool]
"""``() -> bool`` -- "is this child's process still alive?". Injected into
:func:`poll_until_ready` so a child that has already exited fails its gate at
once instead of holding the budget; see U31 below."""


def process_cpu_seconds(pid: int) -> float | None:
    """The process's cumulative CPU time in seconds (user + system), or ``None``
    when it cannot be read. U31's progress signal for :func:`poll_until_ready`.

    Why this and not something cheaper: it is monotone for a live process, and
    unlike wall time it cannot be advanced by anything except the child's own
    threads doing work -- a log file's mtime moves when a watchdog touches it,
    a heartbeat on disk moves when the parent writes it, and neither says the
    child is doing anything. It is also the exact discriminator the station
    needed on 2026-09-25: the control plane was burning CPU on three channels'
    start preparation while its readiness gate kept answering "not ready".

    ``psutil`` is imported lazily (the same posture as
    ``egress.relay_reclaim.PsutilPortOwnerApi`` -- it is a platform dependency
    of process handling, not of importing this module). EVERY failure returns
    ``None``: no such process, access denied, a platform without the call, a
    pid that is not an int. Silently -- the caller's readiness detail line is
    where the outcome is reported, and a poll ticks this once a second.

    ``cpu_times()`` rather than ``cpu_percent()``: a percent is a delta over a
    window this function does not own, and the poll already owns the sampling
    cadence. ``None`` is "cannot be sampled", NEVER 0.0 -- "we could not look"
    and "it used no CPU" are different facts, and conflating them would
    stall-fail a healthy child (see :func:`poll_until_ready`).
    """
    if pid <= 0:
        return None
    try:
        import psutil

        times = psutil.Process(pid).cpu_times()
    except Exception:
        return None
    return float(times.user + times.system)


def poll_until_ready(
    check: ReadinessCheckFn,
    *,
    budget_seconds: float,
    clock: ClockFn,
    sleep: SleepFn,
    poll_interval_seconds: float = 1.0,
    should_abort: AbortFn | None = None,
    progress: ProgressFn | None = None,
    stall_seconds: float | None = None,
    liveness: AliveFn | None = None,
) -> ReadinessResult:
    """Call ``check()`` until it reports ``ready`` or ``budget_seconds`` has
    elapsed (per ``clock``), sleeping ``poll_interval_seconds`` between
    attempts via the injected ``sleep``. Pure aside from those two injected
    callables -- no real waiting happens in this function, so tests exercise
    a 60s budget in microseconds with a fake clock/sleep pair.

    The deadline is computed once, at entry, from ``clock()``; a ``check()``
    that never reports ``ready`` is guaranteed to terminate in ``timeout``
    once the injected clock crosses that deadline -- there is no unbounded
    branch. A ``budget_seconds`` of ``0`` (or a ``check()`` that fails on the
    very first call with no budget remaining) times out without sleeping.

    F1 (BLOCKER, 2026-07-31): the ``should_abort`` seam ends the poll EARLY,
    with the distinct ``aborted`` outcome, when a service stop has been
    requested. Without it the budget is the only exit, and one supervisor
    iteration could chain THREE of them (60s + 30s + 60s when F1 was written;
    the control plane's own budget is 180s as of U31, so the same chain is now
    300s) while ``SvcStop`` waited -- long enough for the 150s stop watchdog to
    fire MID-CHAIN and hard-kill an unclean postgres cluster. It is checked (a)
    at the top of each iteration, so an
    already-requested stop costs zero probe attempts, and (b) immediately after
    ``check()`` returns, so a stop that arrives DURING a probe costs at most
    that one in-flight attempt and never the following sleep. ``None`` (the
    default) preserves the old budget-only behaviour exactly.

    U31 (2026-09-25): ``budget_seconds`` alone cannot tell a SLOW child from a
    WEDGED one, and the station paid for that -- the control plane was alive
    and running all three channels' start preparation when its 30 s budget
    expired, so the supervisor killed it (and each restart re-queued the same
    preparation: ~11 minutes of dead air, ``supervisor.log`` 07:20:32-07:31:45).
    Two further OPTIONAL seams split the question; both are inert unless
    supplied, so every existing caller is byte-for-byte unaffected:

    * ``progress`` + ``stall_seconds``: the child's cumulative CPU seconds,
      sampled once per iteration. An increase restarts a ``stall_seconds``
      window measured from that sample; a sample that does NOT advance for a
      whole window means the child has done no work in ``stall_seconds`` and
      the poll ends. ``progress()`` returning ``None`` (cannot be sampled) is
      treated as no evidence either way and restarts the window -- failing a
      child on evidence we do not have is the same mistake as the defect. The
      outcome stays ``timeout`` (the caller's handling -- backoff, WARNING,
      restart -- is identical) but the detail says which rule fired, so the log
      distinguishes "stalled" from "still working".
    * ``liveness``: ``False`` ends the poll at once with ``not_ready`` -- the
      child is gone, there is nothing left to wait for, and the restart path
      owns it. It is checked after each probe, including a ready response, so a
      different listener cannot make an already-exited child look ready. The
      abort seam is checked first, so a stop still wins.
    """

    deadline = clock() + budget_seconds
    last_progress = progress() if progress is not None else None
    last_progress_at = clock()
    while True:
        if should_abort is not None and should_abort():
            return ReadinessResult(
                outcome="aborted",
                detail="readiness poll aborted by stop request (no probe attempted)",
            )
        result = check()
        if should_abort is not None and should_abort():
            return ReadinessResult(
                outcome="aborted",
                detail=f"readiness poll aborted by stop request; last result: {result.detail}",
            )
        if liveness is not None and not liveness():
            return ReadinessResult(
                outcome="not_ready",
                detail=(
                    "child process exited while waiting for readiness; "
                    f"last result: {result.detail}"
                ),
            )
        if result.outcome == "ready":
            return result
        now = clock()
        if progress is not None and stall_seconds is not None:
            current = progress()
            if current is None or last_progress is None or current > last_progress:
                last_progress = current
                last_progress_at = now
            elif now - last_progress_at >= stall_seconds:
                return ReadinessResult(
                    outcome="timeout",
                    detail=(
                        f"readiness stalled: no CPU progress for {stall_seconds}s; "
                        f"last result: {result.detail}"
                    ),
                )
        if now >= deadline:
            return ReadinessResult(
                outcome="timeout",
                detail=f"readiness budget ({budget_seconds}s) exhausted; last result: {result.detail}",
            )
        sleep(poll_interval_seconds)


# ---------------------------------------------------------------------------
# D5 restart policy: exponential backoff + jitter, restart-storm predicate
# ---------------------------------------------------------------------------

RngFn = Callable[[], float]


def backoff_with_jitter(
    attempt: int,
    *,
    initial_seconds: float,
    max_seconds: float,
    jitter_fraction: float,
    rng: RngFn,
) -> float:
    """D5 restart backoff: the exponential BASE delay
    (``states.backoff_base_seconds``) with +/- ``jitter_fraction`` jitter
    applied via the injected ``rng`` (expected to return a value in
    ``[0.0, 1.0)``, matching ``random.random()``'s contract).
    ``rng() == 0.0`` yields the delay's minimum bound
    (``base * (1 - jitter_fraction)``); ``rng() == 1.0`` yields its maximum
    bound (``base * (1 + jitter_fraction)``); the mapping is linear in
    between. ``jitter_fraction <= 0`` returns the base delay unchanged
    without calling ``rng`` at all -- so a caller can pass a jitter-free
    config without needing a real RNG. Clamped at 0 so no pathological
    ``rng``/``jitter_fraction`` combination can go negative.
    """

    base = backoff_base_seconds(attempt, initial_seconds, max_seconds)
    if jitter_fraction <= 0:
        return base
    offset = (rng() * 2.0 - 1.0) * jitter_fraction
    return max(base * (1.0 + offset), 0.0)


def restart_storm_check(
    restart_epochs: Sequence[float], *, now: float, config: SupervisorConfig
) -> bool:
    """D5 restart-storm predicate: reuses
    ``states.is_restart_storm`` (not reimplemented) with the threshold and
    window carried on ``config`` (``restart_storm_threshold`` /
    ``restart_storm_window_seconds``), so a caller holding only a
    :class:`SupervisorConfig` and a restart-epoch history does not need to
    unpack those two fields by hand at every call site. The actual
    ``>= 5 restarts / 10 min -> degraded`` decision (and firing the alert)
    stays the CALLER's responsibility -- this is only the predicate."""

    return is_restart_storm(
        restart_epochs, now, config.restart_storm_window_seconds, config.restart_storm_threshold
    )


__all__ = [
    "CIVICCAST_SUPERVISED_ENV_VAR",
    "DEFAULT_CONTROL_PLANE_READY_BUDGET_SECONDS",
    "DEFAULT_CONTROL_PLANE_STALL_SECONDS",
    "DEFAULT_GRACEFUL_STOP_DEADLINE_SECONDS",
    "DEFAULT_OLLAMA_HOST",
    "DEFAULT_OLLAMA_PORT",
    "DEFAULT_OLLAMA_READY_BUDGET_SECONDS",
    "POSTGRES_READY_BUDGET_SECONDS",
    "AbortFn",
    "AliveFn",
    "ChildName",
    "ChildSpec",
    "ControlPlaneHealthProbe",
    "ControlPlaneMode",
    "GracefulStopAction",
    "GracefulStopKind",
    "OllamaChildDecision",
    "ProgressFn",
    "ReadinessOutcome",
    "ReadinessResult",
    "backoff_with_jitter",
    "check_control_plane_maintenance_ready",
    "check_control_plane_ready",
    "check_ollama_ready",
    "check_postgres_ready",
    "control_plane_child_spec",
    "default_egress_work_dir",
    "default_upload_dir",
    "graceful_stop_action",
    "ollama_child_spec",
    "poll_until_ready",
    "postgres_child_spec",
    "process_cpu_seconds",
    "read_postmaster_pid",
    "restart_storm_check",
]
