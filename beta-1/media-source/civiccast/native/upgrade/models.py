# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Typed shapes and seam protocols for the D3 journaled upgrade engine.

Every persisted structure is a pydantic model with ``extra="forbid"`` (house
pattern, matching :mod:`civiccast.native.models` and :mod:`civiccast.dr.models`)
so a schema-drifted or truncated journal fails LOUDLY at parse time rather than
resuming from a value we cannot trust. The engine's whole safety argument rests
on the journal being trustworthy across a power-loss boundary; a silently
coerced field would undermine exactly that.

The seam protocols (:class:`UpgradeSeams`) are the dependency-injection surface:
the orchestrator never calls Windows, Postgres, or the supervisor directly, it
calls these callables. The default bundle wires the real ones
(:mod:`civiccast.native.upgrade.seams`); tests pass fakes that record calls and
exercise the REAL orchestration + journal logic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class UpgradePhase(StrEnum):
    """The journaled boundaries of a D3 upgrade.

    Order matters: :meth:`rank` gives the forward position so resume can tell
    which boundary a killed run reached. The terminal states (COMPLETE,
    ROLLED_BACK, HALTED_RESTORE_FAILED, REFUSED_NON_RESTORABLE) never advance.

    The mutation frontier is ``MIGRATED``: per spec D3, a failure at OR AFTER
    step 5 (the migration) must flip the junction back AND restore the backup,
    while a failure BEFORE it only needs the junction/tree unwound (the
    database was never touched). :meth:`is_after_mutation_frontier` encodes
    that split so the orchestrator does not re-derive it.
    """

    # Forward progression (D3 steps 1-7).
    INIT = "init"  # journal written, versions bound; nothing acquired yet
    INTERLOCK_ACQUIRED = "interlock_acquired"  # step 1
    WRITERS_DRAINED = "writers_drained"  # step 2 (quiescence verified)
    BACKUP_VERIFIED = "backup_verified"  # step 3 (hash + restore-drill spot check)
    TREE_LAID = "tree_laid"  # step 4 (app\<new> laid, junction flipped)
    MIGRATED = "migrated"  # step 5 (alembic upgrade head)
    HEALTH_GREEN = "health_green"  # step 6 (maintenance/read-only health passed)
    COMPLETE = "complete"  # step 7 (interlock released; committed)

    # Terminal failure / refusal states.
    ROLLED_BACK = "rolled_back"  # junction + DB unwound cleanly, old version healthy
    HALTED_RESTORE_FAILED = "halted_restore_failed"  # restore failed; service STOPPED, doc emitted
    REFUSED_NON_RESTORABLE = "refused_non_restorable"  # declared non-restorable, no operator ack
    # <installer-path-audit BL-06> "a PREVIOUS run ended terminally; this run
    # did nothing". Its own phase because the old behaviour was to return the
    # STALE journal's phase and error verbatim -- so `__main__` logged
    # "upgrade outcome: rolled_back" with the OLD run's reason and returned
    # 10, which nsis-hooks-bootstrap.nsh (post-#143) turns into a fatal 124
    # telling the operator to "re-run setup after resolving the cause". Every
    # re-run returned 10 again, forever, whatever was fixed -- and nothing in
    # the product, the NSIS chain, or native_uninstall.rs deletes the journal
    # (%ProgramData%\CivicCast is preserved by uninstall BY DESIGN), so
    # uninstall/reinstall did not clear it either. That is the PR #143 shape
    # exactly: a terminal exit code asserting an outcome this run never
    # produced.
    REFUSED_STALE_JOURNAL = "refused_stale_journal"

    @property
    def rank(self) -> int:
        return _PHASE_RANK[self]

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_PHASES

    @property
    def is_after_mutation_frontier(self) -> bool:
        """True once the migration has (or may have) mutated the schema.

        ``MIGRATED`` and beyond are unambiguously past it. Note the engine
        NEVER persists a phase between BACKUP_VERIFIED and MIGRATED where the
        DB has changed: the only mutations before MIGRATED are filesystem
        (tree lay + junction flip), which the pre-migration unwind reverses
        without a DB restore.
        """

        return self.rank >= UpgradePhase.MIGRATED.rank and not self.is_terminal


_PHASE_RANK: dict[UpgradePhase, int] = {
    UpgradePhase.INIT: 0,
    UpgradePhase.INTERLOCK_ACQUIRED: 1,
    UpgradePhase.WRITERS_DRAINED: 2,
    UpgradePhase.BACKUP_VERIFIED: 3,
    UpgradePhase.TREE_LAID: 4,
    UpgradePhase.MIGRATED: 5,
    UpgradePhase.HEALTH_GREEN: 6,
    UpgradePhase.COMPLETE: 7,
    # Terminal states share no forward rank; rank is only meaningful for the
    # forward progression above. Give them a sentinel high rank so an
    # accidental "advance past terminal" comparison is caught by validation.
    UpgradePhase.ROLLED_BACK: 100,
    UpgradePhase.HALTED_RESTORE_FAILED: 101,
    UpgradePhase.REFUSED_NON_RESTORABLE: 102,
    UpgradePhase.REFUSED_STALE_JOURNAL: 103,
}

_TERMINAL_PHASES: frozenset[UpgradePhase] = frozenset(
    {
        UpgradePhase.COMPLETE,
        UpgradePhase.ROLLED_BACK,
        UpgradePhase.HALTED_RESTORE_FAILED,
        UpgradePhase.REFUSED_NON_RESTORABLE,
        UpgradePhase.REFUSED_STALE_JOURNAL,
    }
)


class UpgradePlan(BaseModel):
    """The immutable inputs an upgrade run is bound to.

    Recorded verbatim into the journal at phase-0 so a resuming process
    reconstructs the exact same intent. ``migration_restorable=False`` is the
    D3 "a release whose migration cannot restore-roll-back must declare it"
    flag: the engine refuses auto-upgrade unless ``operator_ack`` is supplied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    old_version: str
    new_version: str
    #: Whether this release's migration can be rolled back by restoring the
    #: pre-upgrade backup. A release that ships an irreversible/destructive
    #: migration sets this False and requires an explicit operator ack.
    migration_restorable: bool = True
    #: Operator acknowledgement token for a declared-non-restorable upgrade.
    #: Ignored when ``migration_restorable`` is True.
    operator_ack: str | None = None


class UpgradeContext(BaseModel):
    """Filesystem + database locations an upgrade operates against.

    Deliberately plain paths/URLs so the whole context is trivially faked in a
    temp directory. The junction lives at ``install_root/current`` and points
    at ``install_root/app/<version>`` (see
    :mod:`civiccast.native.upgrade.junction` for the convention rationale).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    install_root: str  # e.g. C:\Program Files\CivicCast (Native)
    #: Where the journal + backups + recovery docs live. MUST be outside
    #: install_root (ProgramData) so it survives the junction/tree swap and is
    #: readable by a resuming process regardless of which app\<v> is current.
    state_root: str  # e.g. C:\ProgramData\CivicCast\upgrade
    database_url: str
    owner_run_id: str


class BackupRef(BaseModel):
    """The recovery point the journal binds the upgrade to (D3)."""

    model_config = ConfigDict(extra="forbid")

    backup_id: str
    backup_dir: str
    #: sha256 over the backup manifest's own integrity block, i.e. the blob
    #: identity of the whole backup set. Re-derivable to detect tampering.
    manifest_hash: str
    db_artifact: str
    verified: bool
    #: True once the restore-drill spot check passed against a throwaway DB.
    restore_drill_ok: bool
    #: Detail behind a False restore_drill_ok (RestoreDrillReport.errors plus
    #: a schema_ok mismatch summary, if any) -- empty when the drill passed
    #: or was never wired to report detail. Surfaced in the orchestrator's
    #: raise and the journal's error field so a failed pre-upgrade backup is
    #: diagnosable from the journal alone, not just "verification failed".
    restore_drill_errors: list[str] = Field(default_factory=list)


class UpgradeJournal(BaseModel):
    """The durable, power-loss-resilient record of an in-flight upgrade.

    This is the single source of truth a resuming process reads. It binds
    (per D3): old/new product versions, pre/post schema revisions, the backup
    manifest hash + blob identity, the verification result, and the rollback
    outcome. ``history`` is an append-only phase log so an auditor can read
    the exact boundary sequence a run walked (including a resumed one).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    plan: UpgradePlan
    context: UpgradeContext
    phase: UpgradePhase = UpgradePhase.INIT
    pre_schema_revision: str | None = None
    post_schema_revision: str | None = None
    backup: BackupRef | None = None
    #: The junction target recorded BEFORE the flip, so a rollback restores it
    #: exactly (D3 "flip junction back"). None until TREE_LAID.
    previous_junction_target: str | None = None
    new_junction_target: str | None = None
    #: <installer-path-audit MA-01> Whether this run's seam bundle can revert
    #: the ON-DISK payload at all.
    #:
    #: FALSE under ``adapt_flat_installer_layout`` -- the only layout
    #: production ever runs (``nsis-hooks-bootstrap.nsh`` passes
    #: ``--flat-installer-layout`` on EVERY invocation). There, ``read_junction``,
    #: ``lay_tree`` and ``flip_junction`` all resolve to the same
    #: ``<install_root>\runtime`` string that the bootstrap already extracted
    #: the NEW payload into before the engine ran, so
    #: ``previous_junction_target == new_junction_target`` and the rollback's
    #: flip-back is a tautology. The journal used to record
    #: "junction/tree reverted" on that path -- a false claim in the durable
    #: record, and the one the operator recovery document then reasoned from.
    #: With this flag the rollback says what actually happened and the recovery
    #: document branches on it.
    filesystem_rollback: bool = True
    #: Append-only (phase, iso-timestamp, detail) log.
    history: list[tuple[str, str, str]] = Field(default_factory=list)
    #: Populated only on the HALTED_RESTORE_FAILED terminal state.
    recovery_document_path: str | None = None
    error: str | None = None


class UpgradeOutcome(BaseModel):
    """What ``run_upgrade`` returns: the terminal phase + the final journal."""

    model_config = ConfigDict(extra="forbid")

    phase: UpgradePhase
    journal: UpgradeJournal

    @property
    def ok(self) -> bool:
        return self.phase is UpgradePhase.COMPLETE

    @property
    def rolled_back(self) -> bool:
        return self.phase is UpgradePhase.ROLLED_BACK

    @property
    def halted(self) -> bool:
        return self.phase is UpgradePhase.HALTED_RESTORE_FAILED


class OperatorRecovery(BaseModel):
    """The operator recovery document emitted on a rollback-restore failure.

    D3 requires the halt to "emit an operator recovery document naming exact
    next steps". This is that document's structured form; the engine also
    writes a human-readable markdown rendering next to the journal.
    """

    model_config = ConfigDict(extra="forbid")

    written_utc: str
    old_version: str
    new_version: str
    backup_dir: str
    backup_manifest_hash: str
    journal_path: str
    reason: str
    next_steps: list[str]


# ---------------------------------------------------------------------------
# Seam protocols -- the dependency-injection surface (see module docstring).
# ---------------------------------------------------------------------------
#
# Each callable is a single real action the orchestrator needs. Keeping them
# as flat callables (not one fat object) means a test overrides exactly the
# seams it wants to fail-inject and inherits real defaults for the rest.


class QuiescenceProbe(Protocol):
    def __call__(self) -> bool:
        """Return True iff writers are drained and the DB is quiescent (WS2
        pre/post snapshot equality)."""


class Backup(Protocol):
    def __call__(self, backup_dir: str) -> BackupRef:
        """Take the verified pre-upgrade backup into ``backup_dir`` and return
        its bound reference (manifest hash + restore-drill result). MUST raise
        if the backup or its verification fails -- a backup we cannot verify is
        not a recovery point."""


class RestoreBackup(Protocol):
    def __call__(self, backup: BackupRef) -> None:
        """Restore ``backup`` into the live database. Raise on failure (the
        raise is what triggers the HALTED_RESTORE_FAILED path)."""


class LayTree(Protocol):
    def __call__(self, new_version: str) -> str:
        """Lay ``app\\<new_version>\\`` under the install root and return its
        absolute path (the intended junction target)."""


class FlipJunction(Protocol):
    def __call__(self, target: str) -> None:
        """Point ``install_root/current`` at ``target`` (atomically replace)."""


class ReadJunction(Protocol):
    def __call__(self) -> str | None:
        """Return the current junction target, or None if unset."""


class Migrate(Protocol):
    def __call__(self) -> None:
        """Run ``alembic upgrade head`` against the live database. Idempotent
        (already-applied migrations are skipped). Raise on failure."""


class HealthGate(Protocol):
    def __call__(self) -> bool:
        """Start the service in maintenance/read-only health mode and return
        True iff it reports green."""


class SchemaRevision(Protocol):
    def __call__(self) -> str | None:
        """Read the live database's current alembic revision (None if none)."""


class StopService(Protocol):
    def __call__(self) -> None:
        """Ensure the service is stopped (used on the halt path so no binary
        runs against a wrong schema)."""


@dataclass(frozen=True)
class UpgradeSeams:
    """The bundle of real actions the orchestrator drives.

    A frozen dataclass, not a pydantic model: it only ever holds callables
    (never persisted, never validated), and pydantic cannot build an
    isinstance validator over callable seams. The Protocol classes above stay
    as the documented shape of each seam (their named parameters are the
    contract); the fields here are plain ``Callable`` so a factory that returns
    an anonymous closure still satisfies the type without protocol-parameter-
    name matching friction.
    """

    acquire_interlock: Callable[[], None]
    release_interlock: Callable[[], None]
    drain_and_verify_quiescence: Callable[[], bool]
    backup: Callable[[str], BackupRef]
    restore_backup: Callable[[BackupRef], None]
    lay_tree: Callable[[str], str]
    flip_junction: Callable[[str], None]
    read_junction: Callable[[], str | None]
    migrate: Callable[[], None]
    health_gate: Callable[[], bool]
    schema_revision: Callable[[], str | None]
    stop_service: Callable[[], None]
    #: <installer-path-audit BL-03> The migration head the SHIPPED payload
    #: expects, read from the new code's own alembic script directory.
    #:
    #: Before this seam existed, ``post_schema_revision`` was written to the
    #: journal and never read: nothing compared it to the expected head and
    #: nothing compared it to ``pre_schema_revision``. So an
    #: ``alembic upgrade head`` that silently NO-OPS -- the version-location
    #: discovery regression ``alembic/env.py``'s own docstring warns about,
    #: across 110 migration files in 32 per-module ``versions/`` directories --
    #: left the control plane maintenance-attested, D3 committing COMPLETE,
    #: exit 0, and the station running new code on the old schema. The only
    #: thing that would have caught it was an external CI check, not the
    #: product.
    #:
    #: Optional so a fake-seam test that does not care about the migration
    #: contract keeps working; when it is ``None`` the orchestrator records
    #: that the assertion was UNAVAILABLE rather than silently skipping it.
    expected_schema_head: Callable[[], str | None] | None = None
    #: <installer-path-audit MA-01> Whether this bundle can revert the on-disk
    #: payload. See ``UpgradeJournal.filesystem_rollback``.
    filesystem_rollback: bool = True
