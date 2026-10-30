# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The journaled PostgreSQL provisioning sequence.

This module is PURE ORCHESTRATION over the injected
:class:`~civiccast.native.provision.models.ProvisionSeams`, mirroring
:mod:`civiccast.native.upgrade.orchestrator`'s design: it never touches
Windows, ``initdb``, or ``pg_ctl`` itself -- every real action is a seam.
That is what lets a fake-seam test exercise the REAL state machine (the same
code that runs in production), rather than a re-implementation of it.

NATS JetStream was removed from the product entirely (owner decision
2026-08-20, ADR 0023 "NATS removed -- in-process event bus", which supersedes
ADR 0001); this engine no longer provisions a NATS store directory or config
file.

Forward sequence:

  1. verify the server-binaries pack               -> PACK_VERIFIED
  2. detect/initdb the PostgreSQL data directory    -> POSTGRES_CLUSTER_READY
  3. write postgresql.conf + pg_hba.conf            -> POSTGRES_CONFIG_WRITTEN
  4. detect/create the "civiccast" database         -> DATABASE_READY (BLOCKER #52)
  5. done                                            -> COMPLETE

Failure handling (deliberately simpler than the D3 upgrade engine's: a fresh
provisioning run has no prior installed state to roll back TO -- see
:class:`~civiccast.native.provision.models.ProvisionPhase`'s docstring):

* ANY step failure -- including a pack-verification refusal or a
  version-mismatch fail-closed refusal -- halts the run at ``FAILED``. The
  journal and an operator recovery document are preserved; nothing is
  auto-repaired.
* A rerun of :func:`run_provision` over a journal already at ``FAILED``
  returns that terminal outcome UNCHANGED -- it never silently retries.
  This is deliberate fail-closed behavior (WS5 task instruction: "fail-loud
  on any unexpected state, never silently repair"); an operator must
  investigate before a fresh run is attempted (typically over a fresh
  ``state_root``, or after clearing the specific blocking condition named in
  the recovery document).
* A rerun over a journal already at ``COMPLETE`` returns that outcome
  unchanged without touching anything -- provisioning is idempotent at the
  whole-run level, not just per-step.

Resume (idempotent): :func:`run_provision` reads any existing non-terminal
journal for the context and continues from its recorded boundary. Because the
on-disk phase only advances AFTER its real action succeeds and is persisted,
a kill at any boundary leaves a journal the resume can drive to a clean
COMPLETE or a clean FAILED halt. Each forward step is individually idempotent
(the POSTGRES_CLUSTER_READY and DATABASE_READY steps explicitly detect and
reuse existing state rather than re-running initdb / re-creating an existing
database), so re-running a step whose effect already landed is safe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from civiccast.native.provision import journal as journal_io
from civiccast.native.provision.conf import (
    render_pg_hba_conf,
    render_postgresql_conf,
)
from civiccast.native.provision.models import (
    ProvisionContext,
    ProvisionJournal,
    ProvisionOutcome,
    ProvisionPhase,
    ProvisionPlan,
    ProvisionRecovery,
    ProvisionSeams,
    evaluate_postgres_cluster,
)

RECOVERY_DOC_NAME = "PROVISION-RECOVERY.md"


def _persist(
    journal: ProvisionJournal, phase: ProvisionPhase, detail: str, **updates: object
) -> ProvisionJournal:
    """Advance the journal one boundary and write it atomically."""

    journal = journal_io.advance(journal, phase, detail, **updates)
    journal_io.write_journal(journal)
    return journal


def _terminal(
    journal: ProvisionJournal, phase: ProvisionPhase, detail: str, **updates: object
) -> ProvisionJournal:
    """Move to a terminal phase (not rank-gated) and write it atomically."""

    journal = journal_io.advance(journal, phase, detail, **updates)
    journal_io.write_journal(journal)
    return journal


def run_provision(
    plan: ProvisionPlan,
    context: ProvisionContext,
    seams: ProvisionSeams,
) -> ProvisionOutcome:
    """Execute (or resume) provisioning for ``context``; return the outcome.

    Never raises for an expected provisioning failure -- every failure lands
    as the terminal ``FAILED`` journal phase so the caller (NSIS hook / CLI)
    reads an exit code, not a stack trace. A programming error in a seam
    still propagates.
    """

    existing = journal_io.load_journal(context.state_root)
    if existing is not None and existing.phase.is_terminal:
        # COMPLETE and FAILED are both left alone on rerun: COMPLETE because
        # provisioning is idempotent at the whole-run level (nothing to redo),
        # FAILED because a halt is never silently retried (see module
        # docstring) -- the operator must act first.
        return ProvisionOutcome(phase=existing.phase, journal=existing)
    if existing is not None:
        return _drive_forward(existing, seams)

    journal = ProvisionJournal(plan=plan, context=context, phase=ProvisionPhase.INIT)
    journal_io.write_journal(journal)  # phase-0 recorded before anything is acted on
    return _drive_forward(journal, seams)


def _drive_forward(journal: ProvisionJournal, seams: ProvisionSeams) -> ProvisionOutcome:
    """Walk the forward provisioning steps from the journal's current phase
    to COMPLETE, halting to FAILED on the first step that fails."""

    attempting = journal.phase
    try:
        if journal.phase.rank < ProvisionPhase.PACK_VERIFIED.rank:
            attempting = ProvisionPhase.PACK_VERIFIED
            seams.verify_pack()
            journal = _persist(
                journal,
                ProvisionPhase.PACK_VERIFIED,
                "server-binaries pack signature and byte inventory verified",
            )

        if journal.phase.rank < ProvisionPhase.POSTGRES_CLUSTER_READY.rank:
            attempting = ProvisionPhase.POSTGRES_CLUSTER_READY
            observed = seams.detect_postgres_cluster()
            cluster_decision = evaluate_postgres_cluster(
                observed_version=observed,
                expected_major_version=journal.plan.postgres_major_version,
            )
            if cluster_decision.outcome == "version_mismatch":
                raise RuntimeError(f"PostgreSQL cluster refused: {cluster_decision.detail}")
            if cluster_decision.outcome == "needs_initdb":
                seams.run_initdb()
            journal = _persist(
                journal, ProvisionPhase.POSTGRES_CLUSTER_READY, cluster_decision.detail
            )

        if journal.phase.rank < ProvisionPhase.POSTGRES_CONFIG_WRITTEN.rank:
            attempting = ProvisionPhase.POSTGRES_CONFIG_WRITTEN
            conf = render_postgresql_conf(
                host=journal.context.postgres_host, port=journal.context.postgres_port
            )
            hba = render_pg_hba_conf(host=journal.context.postgres_host)
            seams.write_postgres_conf(conf)
            seams.write_pg_hba_conf(hba)
            journal = _persist(
                journal,
                ProvisionPhase.POSTGRES_CONFIG_WRITTEN,
                "postgresql.conf and pg_hba.conf written",
            )

        if journal.phase.rank < ProvisionPhase.DATABASE_READY.rank:
            # BLOCKER #52: D4 provisioning ran initdb, wrote config, and
            # persisted a DatabaseUrl naming database "civiccast" -- but
            # nothing ever executed CREATE DATABASE, so the installed
            # service faulted at startup with "FATAL: database civiccast
            # does not exist" (live-proven, Sandbox run 14). ensure_database
            # bundles its own start-postgres-temporarily/detect/create/
            # stop-postgres sequence into one seam (see DatabaseDecision's
            # docstring for why this one is not split into a separate pure
            # evaluate_* function like its siblings) and is idempotent: a
            # rerun against an already-provisioned cluster finds the
            # database present and takes no action.
            attempting = ProvisionPhase.DATABASE_READY
            database_decision = seams.ensure_database()
            journal = _persist(journal, ProvisionPhase.DATABASE_READY, database_decision.detail)

        if journal.phase.rank < ProvisionPhase.COMPLETE.rank:
            attempting = ProvisionPhase.COMPLETE
            journal = _persist(journal, ProvisionPhase.COMPLETE, "provisioning complete")

    except Exception as exc:  # any step failure funnels to the fail-closed halt
        return _halt(journal, reason=str(exc), attempting=attempting)

    return ProvisionOutcome(phase=journal.phase, journal=journal)


def _halt(
    journal: ProvisionJournal,
    *,
    reason: str,
    attempting: ProvisionPhase,
    next_steps: list[str] | None = None,
) -> ProvisionOutcome:
    """The FAILED terminal state: preserve the journal, emit an operator
    recovery document naming exact next steps, never auto-repair."""

    doc_path = _write_recovery_document(
        journal, reason=reason, attempting=attempting, next_steps=next_steps
    )
    detail = (
        f"HALTED while attempting {attempting.value}: {reason}; recovery document at {doc_path}"
    )
    journal = _terminal(
        journal,
        ProvisionPhase.FAILED,
        detail,
        error=reason,
        recovery_document_path=str(doc_path),
    )
    return ProvisionOutcome(phase=journal.phase, journal=journal)


def halt_resume_credential_lost(journal: ProvisionJournal) -> ProvisionOutcome:
    """WS5 task #57 (disclosed in commit abdba55b): called by the CLI BEFORE
    any seam is built, for a non-terminal journal at or past
    ``POSTGRES_CLUSTER_READY``.

    ``__main__.py``'s resume path (task #55 / audit-lite FINDING-003) always
    generates a FRESH password for the RUN branch it drives through
    :func:`run_provision`. That is safe when the loaded journal is still
    BEFORE ``POSTGRES_CLUSTER_READY`` (nothing has touched ``initdb`` yet, so
    there is no existing credential to contradict). It is NOT safe once the
    journal is AT OR PAST that boundary: ``initdb --pwfile`` already baked a
    real credential into the cluster on disk (see
    ``civiccast.native.provision.__main__``'s password-handling docstring),
    and that credential is never persisted anywhere recoverable (the journal
    itself redacts it -- see :func:`~civiccast.native.provision.journal.
    write_journal`). Resuming with a new, different password would drive the
    engine to a later step (e.g. ``DATABASE_READY``'s ``psql``/``PGPASSWORD``
    connect) that authenticates with the WRONG password and fails with a
    generic auth error, not a diagnosis of the real cause.

    This halts the run the same way any other provisioning failure halts
    (terminal ``FAILED`` phase, journal written, recovery document emitted)
    but with next_steps HONEST about what this specific state actually
    allows -- unlike the CLI's generic "needs a repair install" message
    (:data:`~civiccast.native.provision.__main__.ProvisionCliAction.
    FAIL_LOUD_MISSING_REGISTRY`), reinstalling/repairing over this same data
    directory does not restore a credential that was never written down
    anywhere; only supplying the ORIGINAL known-good connection string, or
    purging the partial (never-completed, so no real civiccast data lost)
    cluster and starting over, actually recovers.
    """

    boundary = ProvisionPhase.POSTGRES_CLUSTER_READY
    reason = (
        f"a previous provisioning attempt reached phase {journal.phase.value!r} "
        f"(at or past {boundary.value!r}) but never completed; its PostgreSQL cluster "
        "is therefore already initialized with a real credential (set once, by "
        "initdb, when it first reached that phase) that was never persisted to the "
        "DatabaseUrl registry value and cannot be reconstructed -- the provisioning "
        "journal never stores it in recoverable form"
    )
    next_steps = [
        "This is NOT a generic 'needs a repair install' situation: reinstalling or "
        "repairing over this same data directory will not recover the missing "
        "credential -- the already-initialized PostgreSQL cluster (not the "
        "installed files) is what is missing it, and resuming with a freshly "
        "generated password would only fail authentication against it.",
        "If the original database password was recorded elsewhere when this "
        "cluster was first initialized, set the DatabaseUrl registry value (or "
        "pass --existing-database-url) to that known-good connection string and "
        "rerun -- this restores the existing-cluster no-op reuse path.",
        f"Otherwise, delete {journal.context.postgres_data_dir} entirely -- this "
        "provisioning run never reached completion, so it holds no completed "
        "civiccast data -- and start a fresh provisioning run.",
        f"Preserve this journal for support: {journal_io.journal_path(journal.context.state_root)}",
    ]
    return _halt(journal, reason=reason, attempting=journal.phase, next_steps=next_steps)


def halt_adopt_foreign_cluster(
    plan: ProvisionPlan, context: ProvisionContext, *, reason: str
) -> ProvisionOutcome:
    """N-15: the CLI attempted to ADOPT a surviving cluster at the product
    data directory (credential deleted by uninstall, data preserved) but the
    adoption seam
    (:func:`~civiccast.native.provision.seams.reset_cluster_credential`)
    refused it as NOT product-owned
    (:class:`~civiccast.native.provision.seams.AdoptionForeignClusterError`):
    its bootstrap superuser role or its ``civiccast`` database was absent.

    Halts the run the same fail-closed way every other provisioning refusal
    does (a fresh terminal ``FAILED`` journal + an operator recovery document)
    but with next_steps HONEST about this specific state: the installer will
    never take over a PostgreSQL cluster it cannot prove the product itself
    created, and the operator must resolve the foreign data directory (or point
    the product at a clean one) before a reinstall can proceed. Never claims a
    plain repair install would help -- it would hit the same refusal.
    """

    journal = ProvisionJournal(plan=plan, context=context, phase=ProvisionPhase.INIT)
    next_steps = [
        "The PostgreSQL data directory at "
        f"{context.postgres_data_dir} holds an initialized cluster that does NOT "
        "present CivicCast's own bootstrap superuser role and database, so this "
        "installer refused to adopt it (it never takes over a cluster it cannot "
        "prove the product created).",
        "If this data directory is left over from a DIFFERENT PostgreSQL product "
        "or a corrupted/foreign cluster, move or remove it (with a verified "
        "backup if its contents matter) so a fresh CivicCast cluster can be "
        "provisioned in its place, then run the installer again.",
        "If you believe this IS a CivicCast cluster, do NOT delete it -- preserve "
        "it and escalate to support with this recovery document and the "
        "provisioning journal; a genuine CivicCast cluster missing its own role "
        "or database indicates a partially-torn-down or damaged data directory "
        "that needs investigation, not a blind re-provision.",
        f"Preserve this journal for support: {journal_io.journal_path(context.state_root)}",
    ]
    return _halt(
        journal,
        reason=reason,
        attempting=ProvisionPhase.POSTGRES_CLUSTER_READY,
        next_steps=next_steps,
    )


def write_recovery_document(
    state_root: str | Path,
    *,
    reason: str,
    attempting: str,
    journal_path_for_reference: str | Path | None = None,
    next_steps: list[str] | None = None,
) -> Path:
    """Write the operator recovery markdown under ``state_root``; return its
    path.

    The GENERAL form of what used to be a ``ProvisionJournal``-only helper
    (see :func:`_write_recovery_document` below, now a thin wrapper over
    this): every real provisioning failure this engine can produce halts
    through a journal (:func:`_halt`), but ``civiccast.native.provision.
    __main__.main`` also has several failure points that occur BEFORE a
    journal exists at all -- decoding the pack public key, reading a corrupt
    journal, or a pre-check (adoption's own ``verify_pack``/credential-reset
    calls) that fails before ``run_provision`` is ever invoked. Every one of
    those still exits with the SAME installer-level code the NSIS hook chain
    maps to a single, unconditional "see PROVISION-RECOVERY.md" message
    (``nsis-hooks-bootstrap.nsh``'s ``step d4-provision`` handling: ANY
    nonzero exit from the Python CLI collapses to installer exit 75 via
    ``native_service_registration::run_native_provision``'s generic error
    wrapping, so the SAME static "see ... PROVISION-RECOVERY.md" text is
    shown for every one of them, not just the ones that happen to route
    through this module's own ``_halt``). A failure message that references a
    document that was never written is a dead end for the operator
    (fleet-tester candidate 99db2c6, ``soak/INSTALL-FAILED.md``: "Referenced
    ... PROVISION-RECOVERY.md: absent") -- so every one of those call sites
    now calls this function directly, keyed on the state root alone rather
    than a full journal.

    ``next_steps`` defaults to the generic mid-run-failure guidance below;
    callers with a MORE SPECIFIC, already-diagnosed situation (see
    :func:`halt_resume_credential_lost`) pass their own honest list instead
    of the generic template, which does not fit every halt reason -- e.g. it
    would otherwise tell an operator whose cluster is missing its credential
    to "obtain a correctly signed pack" or "escalate to support" and never
    mention the ONE fact that actually matters here (the credential is gone,
    not the files).
    """

    state_root = Path(state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    doc_path = state_root / RECOVERY_DOC_NAME
    journal_file = (
        Path(journal_path_for_reference)
        if journal_path_for_reference is not None
        else journal_io.journal_path(state_root)
    )

    if next_steps is None:
        next_steps = [
            "Do NOT assume PostgreSQL is safely provisioned -- "
            f"provisioning halted while attempting {attempting!r}.",
            f"Read the failure reason recorded in this journal: {journal_file}",
            "If the failure was a server-binaries pack verification refusal, "
            "obtain a correctly signed pack before retrying.",
            "If the failure was a PostgreSQL cluster version mismatch, do NOT "
            "delete or re-initialize the existing data directory without a "
            "verified backup -- escalate to support.",
            "If the failure was while creating the database (attempting "
            f"{ProvisionPhase.DATABASE_READY.value!r}), PostgreSQL's own data "
            "directory and role were left untouched -- check that postgres "
            "could actually start against the just-written config (port/host "
            "conflicts, permissions) and that the pg_ctl/psql binaries in the "
            "server-binaries pack are present before retrying.",
            "This provisioning run will not be silently retried. Once the "
            "blocking condition is resolved, start a fresh provisioning run "
            "(a new state_root, or the same one after the operator has "
            "confirmed it is safe to proceed).",
            f"Preserve this journal for support: {journal_file}",
        ]
    recovery = ProvisionRecovery(
        written_utc=datetime.now(UTC).isoformat(),
        attempting_phase=attempting,
        reason=reason,
        journal_path=str(journal_file),
        next_steps=next_steps,
    )

    lines = [
        "# CivicCast (Native) -- Provisioning Halted",
        "",
        f"- Written: {recovery.written_utc}",
        f"- Attempting: {recovery.attempting_phase}",
        f"- Reason: {recovery.reason}",
        f"- Journal: {recovery.journal_path}",
        "",
        "## Provisioning has been HALTED and will not auto-retry.",
        "",
    ]
    lines.extend(f"{i}. {step}" for i, step in enumerate(recovery.next_steps, start=1))
    lines.append("")
    doc_path.write_text("\n".join(lines), encoding="utf-8")
    return doc_path


def _write_recovery_document(
    journal: ProvisionJournal,
    *,
    reason: str,
    attempting: ProvisionPhase,
    next_steps: list[str] | None = None,
) -> Path:
    """Journal-bound wrapper over :func:`write_recovery_document` -- the shape
    every in-engine halt (:func:`_halt`) already used before that function was
    generalized so ``__main__.py``'s pre-journal failure points could call it
    too. Behavior is byte-for-byte unchanged for every existing caller."""

    return write_recovery_document(
        journal.context.state_root,
        reason=reason,
        attempting=attempting.value,
        journal_path_for_reference=journal_io.journal_path(journal.context.state_root),
        next_steps=next_steps,
    )


__all__ = [
    "RECOVERY_DOC_NAME",
    "halt_adopt_foreign_cluster",
    "halt_resume_credential_lost",
    "run_provision",
    "write_recovery_document",
]
