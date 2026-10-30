# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Typed shapes, pure decision functions, and seam protocols for the native
PostgreSQL provisioning engine (spec-installer-lifecycle.md D4 inventory;
spec-native-beta-recovery.md WP2: "real PostgreSQL ... provisioning").

NATS JetStream was removed from the product entirely (owner decision
2026-08-20, ADR 0023 "NATS removed -- in-process event bus", which supersedes
ADR 0001); this module no longer provisions a NATS store directory or config
file.

Every in-process decision/outcome structure is a pydantic model with
``extra="forbid"`` (house pattern -- see :mod:`civiccast.native.upgrade.models`).
The three structures that are PERSISTED and re-loaded across product
versions (:class:`ProvisionPlan`, :class:`ProvisionContext`,
:class:`ProvisionJournal`) are ``extra="ignore"`` instead, since beta.5
(2026-09-09): a journal written by an OLDER installer may carry fields a newer
model no longer declares, and rejecting those halted a real upgrade -- see
:class:`ProvisionJournal`'s docstring for the measured incident. Type errors,
missing required fields, and unknown ``phase`` values are still fail-loud; only
*unknown key names* are tolerated (and logged by
:func:`civiccast.native.provision.journal.load_journal`).

Two things live here that are deliberately kept OUT of the journaled
orchestrator so they stay unit-testable without any Windows/Postgres
process:

* **Idempotency decisions** (:func:`evaluate_postgres_cluster`) -- given only
  what an I/O seam OBSERVED (a ``PG_VERSION`` file's contents), decide
  whether to initialize, reuse, or fail closed. The orchestrator never
  re-derives this logic; it calls these functions and acts on the outcome.
* **The DatabaseUrl value** (:func:`build_database_url`,
  :func:`resolve_database_url`) -- the exact string the installer writes to
  ``HKLM\\SOFTWARE\\CivicCast\\Native\\DatabaseUrl`` (spec-installer-lifecycle.md
  D4). This module produces the value; the registry write itself is the
  installer's (NSIS/service slice), per the WS5 task boundary.

The seam protocols (:class:`ProvisionSeams`) are the dependency-injection
surface, mirroring :mod:`civiccast.native.upgrade.models`'s
``UpgradeSeams``: the orchestrator never calls Windows, ``initdb``, or
``pg_ctl`` directly, it calls these callables. The default (real) bundle is
built in :mod:`civiccast.native.provision.seams`; tests pass fakes that
record calls and exercise the REAL orchestration + journal logic.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Provisioning journal phases
# ---------------------------------------------------------------------------


class ProvisionPhase(StrEnum):
    """The journaled boundaries of a first-provisioning run.

    Order matters: :meth:`rank` gives the forward position so resume can tell
    which boundary a killed run reached. Unlike the D3 upgrade engine, a fresh
    provisioning run has no prior installed state to roll back TO on failure
    -- there is nothing to unwind, only something to stop touching. The
    terminal states are therefore just ``COMPLETE`` (succeeded) and
    ``FAILED`` (halted; the journal + a recovery document are preserved for
    the operator, and a rerun over the SAME state root refuses to silently
    retry -- see :func:`civiccast.native.provision.orchestrator.run_provision`).
    """

    INIT = "init"  # journal written; nothing acted on yet
    PACK_VERIFIED = "pack_verified"  # server-binaries pack signature + bytes verified
    POSTGRES_CLUSTER_READY = "postgres_cluster_ready"  # initdb'd OR detected-existing-same-version
    POSTGRES_CONFIG_WRITTEN = (
        "postgres_config_written"  # postgresql.conf + pg_hba.conf deltas written
    )
    DATABASE_READY = "database_ready"  # BLOCKER #52: CREATE DATABASE'd OR detected-existing
    COMPLETE = "complete"

    FAILED = "failed"  # halted; never auto-retried, see orchestrator

    @property
    def rank(self) -> int:
        return _PHASE_RANK[self]

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_PHASES


_PHASE_RANK: dict[ProvisionPhase, int] = {
    ProvisionPhase.INIT: 0,
    ProvisionPhase.PACK_VERIFIED: 1,
    ProvisionPhase.POSTGRES_CLUSTER_READY: 2,
    ProvisionPhase.POSTGRES_CONFIG_WRITTEN: 3,
    ProvisionPhase.DATABASE_READY: 4,
    ProvisionPhase.COMPLETE: 5,
    # Terminal failure shares no forward rank; sentinel high rank so an
    # accidental "advance past terminal" comparison is caught by validation.
    ProvisionPhase.FAILED: 100,
}

_TERMINAL_PHASES: frozenset[ProvisionPhase] = frozenset(
    {ProvisionPhase.COMPLETE, ProvisionPhase.FAILED}
)


# ---------------------------------------------------------------------------
# Pure idempotency decisions
# ---------------------------------------------------------------------------

PostgresClusterOutcome = Literal["needs_initdb", "already_initialized", "version_mismatch"]


class PostgresClusterDecision(BaseModel):
    """The outcome of evaluating an observed data directory against the
    expected PostgreSQL major version. Never performs I/O itself -- the
    caller observes (or does not observe) a ``PG_VERSION`` file and hands the
    result in."""

    model_config = ConfigDict(extra="forbid")

    outcome: PostgresClusterOutcome
    detail: str


def evaluate_postgres_cluster(
    *, observed_version: str | None, expected_major_version: str
) -> PostgresClusterDecision:
    """D4 idempotency rule: "detect existing cluster and DO NOT re-init;
    version-check existing cluster."

    ``observed_version`` is the exact stripped contents of the data
    directory's ``PG_VERSION`` file, or ``None`` when no such file was found
    (an uninitialized or absent data directory). A present-but-different
    version is a FAIL-CLOSED refusal, never a silent re-init or upgrade --
    the orchestrator must never touch an existing cluster it cannot prove is
    the right version.
    """

    expected = expected_major_version.strip()
    if not expected:
        raise ValueError("expected_major_version must not be empty")
    if observed_version is None:
        return PostgresClusterDecision(
            outcome="needs_initdb",
            detail="no PG_VERSION file found; the data directory is uninitialized",
        )
    observed = observed_version.strip()
    if observed == expected:
        return PostgresClusterDecision(
            outcome="already_initialized",
            detail=f"existing cluster is PostgreSQL {observed}; reused without re-running initdb",
        )
    return PostgresClusterDecision(
        outcome="version_mismatch",
        detail=(
            f"existing cluster is PostgreSQL {observed!r}, expected {expected!r}; "
            "refusing to touch it (fail-closed)"
        ),
    )


DatabaseCreationOutcome = Literal["created", "already_exists"]


class DatabaseDecision(BaseModel):
    """The outcome of BLOCKER #52's database-creation step.

    Unlike :class:`PostgresClusterDecision`, this
    is not split into a separate pure ``evaluate_*`` function over an
    offline-observable probe: whether ``plan.database_name`` exists can only
    be answered by a LIVE connection to the just-configured PostgreSQL
    cluster, so the check-then-act sequence (start postgres temporarily if
    needed, query ``pg_database``, ``CREATE DATABASE`` if absent, stop
    postgres if this step started it) is bundled into ONE seam
    (:data:`ProvisionSeams.ensure_database`) that returns this struct --
    mirroring how :data:`ProvisionSeams.run_initdb` bundles its own
    idempotency-adjacent I/O rather than being split further. The
    orchestrator still journals ``detail`` verbatim, exactly like every
    sibling step.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: DatabaseCreationOutcome
    detail: str


# ---------------------------------------------------------------------------
# DatabaseUrl construction (pure)
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def build_database_url(*, host: str, port: int, database: str, username: str, password: str) -> str:
    """Pure construction of the ``postgresql://`` DatabaseUrl value.

    This is the exact string content spec-installer-lifecycle.md D4 requires
    the installer write to
    ``HKLM\\SOFTWARE\\CivicCast\\Native\\DatabaseUrl`` -- this function
    produces the value; the registry write is the installer's (NSIS/service
    slice), not this module's.

    ``username``/``password`` are percent-encoded (RFC 3986, ``quote(...,
    safe="")``) so a generated password containing ``:``, ``@``, ``/``, or any
    other URL-significant character round-trips correctly -- the same
    encoding :mod:`civiccast.dr.backup`'s ``_parse_postgres_url`` decodes with
    ``unquote`` on the read side, so the two stay symmetric.
    """

    if not host or not host.strip():
        raise ValueError("host must not be empty")
    if isinstance(port, bool) or not (1 <= port <= 65535):
        raise ValueError(f"port must be an integer between 1 and 65535, got {port!r}")
    if not _IDENTIFIER_RE.match(database):
        raise ValueError(f"database name is not a safe SQL identifier: {database!r}")
    if not username:
        raise ValueError("username must not be empty")
    if not password:
        raise ValueError("password must not be empty")

    quoted_user = quote(username, safe="")
    quoted_password = quote(password, safe="")
    return f"postgresql://{quoted_user}:{quoted_password}@{host}:{port}/{database}"


# ---------------------------------------------------------------------------
# Plan / Context / Journal
# ---------------------------------------------------------------------------


class ProvisionPlan(BaseModel):
    """The immutable identity inputs a provisioning run is bound to.

    Recorded verbatim into the journal at phase-0 so a resuming process
    reconstructs the exact same intent (same pattern as
    :class:`civiccast.native.upgrade.models.UpgradePlan`).

    ``extra="ignore"`` (not ``forbid``): this is persisted and re-read across
    installer versions; every decision reads a NAMED field, so an unknown key
    left behind by an older or newer installer cannot change behaviour and
    must not halt an upgrade (see :class:`ProvisionJournal`).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    postgres_major_version: str = Field(min_length=1)
    database_name: str = Field(min_length=1)
    database_username: str = Field(min_length=1)
    server_pack_product_version: str = Field(min_length=1)
    server_pack_compatible_core: str = Field(min_length=1)
    server_pack_signing_key_id: str = Field(min_length=1)

    @field_validator("database_name")
    @classmethod
    def _validate_database_name(cls, value: str) -> str:
        if not _IDENTIFIER_RE.match(value):
            raise ValueError(f"database_name is not a safe SQL identifier: {value!r}")
        return value

    @field_validator("database_username")
    @classmethod
    def _validate_database_username(cls, value: str) -> str:
        # BLOCKER #52: this value is now embedded directly (double-quoted,
        # never parameterized -- CREATE DATABASE ... OWNER cannot take a bind
        # parameter) into the CREATE DATABASE OWNER clause
        # (:func:`civiccast.native.provision.seams._create_database`), so it
        # needs the exact same safe-identifier guarantee
        # ``database_name`` already carries -- an unvalidated username was a
        # latent SQL-injection surface the moment that call site was added.
        if not _IDENTIFIER_RE.match(value):
            raise ValueError(f"database_username is not a safe SQL identifier: {value!r}")
        return value


class ProvisionContext(BaseModel):
    """Filesystem + network locations one provisioning run operates against.

    Deliberately plain paths/strings (mirroring
    :class:`civiccast.native.upgrade.models.UpgradeContext`) so the whole
    context is trivially faked in a temp directory. ``database_password`` is
    carried here (not in :class:`ProvisionPlan`) because it is the one field
    :func:`resolve_database_url` needs alongside the plan's ``database_name``/
    ``database_username``.

    SECURITY (corrected 2026-07-30; the previous revision of this docstring
    claimed the journal directory was "ProgramData-ACL'd, not
    world-readable" -- that claim was FALSE: nothing in this codebase
    implemented it, and an independently re-verified audit measured
    ``BUILTIN\\Users: ReadAndExecute`` on the real ``C:\\ProgramData``,
    which flowed down uncontested). Two things are now true instead of that
    false claim:

    1. This in-memory field is NEVER written to the on-disk journal in
       plaintext -- :func:`civiccast.native.provision.journal.write_journal`
       redacts ``context.database_password`` to a fixed marker in the
       serialized payload before it ever touches disk (see that module's
       docstring for the by-inspection proof that no resume path needs the
       persisted value).
    2. The journal directory's own DACL is now actually hardened to
       SYSTEM + Administrators by
       :func:`civiccast.native.provision.journal._harden_state_root_acl`,
       called on every journal write -- so even the (already non-secret)
       plan/context/history this journal still carries is not
       world-readable either.

    LEGACY FIELDS (beta.5.1 fix, 2026-09-09): journals written by the August
    2026 beta.1/beta.2 installers carried ``nats_host`` / ``nats_port`` /
    ``nats_store_dir`` / ``nats_config_path`` / ``nats_tls`` here (removed by
    85ffe6c0 "stop provisioning a NATS store or config" with no migration).
    With ``extra="forbid"`` those five keys made a valid, ``phase: complete``
    journal unparseable and halted every upgrade over such a station. The
    context is now ``extra="ignore"``: unknown keys are dropped at load and
    named once per run on the provisioning CLI's own stderr (``main()`` in
    :mod:`civiccast.native.provision.__main__`, from the list
    :func:`civiccast.native.provision.journal.load_journal_with_ignored_keys`
    returns; the INFO line
    :func:`~civiccast.native.provision.journal.load_journal` also emits
    reaches nothing in production because the CLI configures no logging).
    The installer's Rust wrapper currently captures and does not forward
    that stderr, so the note is not yet in ``install-progress.log``;
    forwarding it is a follow-up tracked with the runtime-ownership
    diagnosability PR. Nothing here ever reads a field by anything but its
    declared name, so a stray key has no path to influence provisioning.
    """

    model_config = ConfigDict(extra="ignore")

    postgres_host: str = "127.0.0.1"
    postgres_port: int = Field(default=5432, gt=0, le=65535)
    postgres_data_dir: str
    postgres_config_path: str
    postgres_hba_path: str
    database_password: str = Field(min_length=1)

    server_pack_path: str

    #: Where the journal + recovery document live. MUST be outside any tree a
    #: repair/reinstall might replace wholesale (ProgramData), mirroring the
    #: upgrade engine's ``state_root`` convention.
    state_root: str
    owner_run_id: str = Field(min_length=1)


def resolve_database_url(*, plan: ProvisionPlan, context: ProvisionContext) -> str:
    """The pure function that produces the DatabaseUrl HKLM value content
    (D4) from a bound plan + context. Never persisted as its own journal
    field -- always re-derived from ``plan.database_name`` /
    ``plan.database_username`` and ``context.postgres_host`` /
    ``context.postgres_port`` / ``context.database_password`` so there is
    exactly one place the string is assembled.
    """

    return build_database_url(
        host=context.postgres_host,
        port=context.postgres_port,
        database=plan.database_name,
        username=plan.database_username,
        password=context.database_password,
    )


class ProvisionJournal(BaseModel):
    """The durable, power-loss-resilient record of an in-flight provisioning
    run. Same shape/persistence contract as
    :class:`civiccast.native.upgrade.models.UpgradeJournal` -- an append-only
    ``history`` log and a ``recovery_document_path`` populated only on the
    ``FAILED`` terminal state -- EXCEPT for unknown keys.

    ``extra="ignore"`` (was ``forbid`` until beta.5.1, 2026-09-09). Measured
    on an upgrade over an August-15 beta.1-era station: the installer halted
    with *"the provisioning journal at C:\\ProgramData\\CivicCast\\provision is
    corrupt/unparseable: 5 validation errors for ProvisionJournal --
    context.nats_config_path / nats_host / nats_port / nats_store_dir /
    nats_tls: Extra inputs are not permitted"* over a journal that was valid
    JSON, ``schema_version: 1``, ``phase: complete``. The journal is an
    upgrade-surviving file (ProgramData) read by every later installer, so a
    field the model stops declaring must be tolerated, not treated as
    corruption. What stays fail-loud: truncated/invalid JSON, wrong types,
    missing required fields, and an unknown ``phase`` -- the cases where the
    value genuinely cannot be trusted. ``history`` entries are plain string
    triples, so legacy phase names recorded there (``nats_store_ready``,
    ``nats_config_written``) load unchanged. What becomes of the legacy keys
    on disk depends on the path the CLI takes over such a station: the
    ADOPT_EXISTING path clears the journal outright (pre-existing
    behaviour; the engine then writes a fresh one, history discarded) and
    the NOOP_REUSE_EXISTING path never writes it, so the file is left
    exactly as-is, legacy keys included. Either way no legacy key can
    influence a later run: any ``write_journal`` serialises only declared
    fields, and any load drops the undeclared ones again.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = 1
    plan: ProvisionPlan
    context: ProvisionContext
    phase: ProvisionPhase = ProvisionPhase.INIT
    #: Append-only (phase, iso-timestamp, detail) log.
    history: list[tuple[str, str, str]] = Field(default_factory=list)
    recovery_document_path: str | None = None
    error: str | None = None


class ProvisionOutcome(BaseModel):
    """What ``run_provision`` returns: the terminal phase + the final journal."""

    model_config = ConfigDict(extra="forbid")

    phase: ProvisionPhase
    journal: ProvisionJournal

    @property
    def ok(self) -> bool:
        return self.phase is ProvisionPhase.COMPLETE

    @property
    def failed(self) -> bool:
        return self.phase is ProvisionPhase.FAILED


class ProvisionRecovery(BaseModel):
    """The operator recovery document emitted on a ``FAILED`` halt. Mirrors
    :class:`civiccast.native.upgrade.models.OperatorRecovery`'s shape."""

    model_config = ConfigDict(extra="forbid")

    written_utc: str
    attempting_phase: str
    reason: str
    journal_path: str
    next_steps: list[str]


# ---------------------------------------------------------------------------
# Seam protocols -- the dependency-injection surface.
# ---------------------------------------------------------------------------


class VerifyPack(Protocol):
    def __call__(self) -> None:
        """Verify the server-binaries pack's signature + byte inventory
        (:mod:`civiccast.native.provision.pack`). MUST raise
        :class:`civiccast.installer.native_packs.NativePackVerificationError`
        (or propagate it) on any mismatch -- this is the FIRST forward phase,
        so a verification failure halts before any provisioning action
        touches disk."""


class DetectPostgresCluster(Protocol):
    def __call__(self) -> str | None:
        """Return the observed data directory's ``PG_VERSION`` contents
        (stripped), or ``None`` if no such file exists."""


class RunInitdb(Protocol):
    def __call__(self) -> None:
        """Run ``initdb`` against the (confirmed-empty) data directory. Raise
        on failure."""


class WritePostgresConf(Protocol):
    def __call__(self, content: str) -> None:
        """Atomically write ``postgresql.conf``'s rendered content."""


class WritePgHbaConf(Protocol):
    def __call__(self, content: str) -> None:
        """Atomically write ``pg_hba.conf``'s rendered content."""


@dataclass(frozen=True)
class ProvisionSeams:
    """The bundle of real actions the orchestrator drives.

    A frozen dataclass, not a pydantic model, for the same reason
    :class:`civiccast.native.upgrade.models.UpgradeSeams` is one: it only ever
    holds callables (never persisted, never validated).
    """

    verify_pack: Callable[[], None]
    detect_postgres_cluster: Callable[[], str | None]
    run_initdb: Callable[[], None]
    write_postgres_conf: Callable[[str], None]
    write_pg_hba_conf: Callable[[str], None]
    ensure_database: Callable[[], DatabaseDecision]


__all__ = [
    "DatabaseCreationOutcome",
    "DatabaseDecision",
    "PostgresClusterDecision",
    "PostgresClusterOutcome",
    "ProvisionContext",
    "ProvisionJournal",
    "ProvisionOutcome",
    "ProvisionPhase",
    "ProvisionPlan",
    "ProvisionRecovery",
    "ProvisionSeams",
    "build_database_url",
    "evaluate_postgres_cluster",
    "resolve_database_url",
]
