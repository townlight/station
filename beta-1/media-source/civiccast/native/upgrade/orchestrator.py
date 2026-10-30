# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The D3 journaled upgrade sequence, its rollback, and its halt path.

This module is PURE ORCHESTRATION over the injected
:class:`~civiccast.native.upgrade.models.UpgradeSeams`. It never touches
Windows, Postgres, or the supervisor itself -- every real action is a seam.
That is what lets a fake-seam test exercise the REAL state machine (the same
code that runs in production), rather than a re-implementation of it.

The forward sequence (spec D3):

  1 acquire interlock              -> INTERLOCK_ACQUIRED
  2 drain writers + verify quiesce -> WRITERS_DRAINED
  3 verified pre-upgrade backup    -> BACKUP_VERIFIED   (recovery point exists)
  4 lay app\\<new>\\ + flip junction -> TREE_LAID          (records prev target)
  5 alembic upgrade head           -> MIGRATED           (records post revision)
  6 maintenance/read-only health   -> HEALTH_GREEN
  7 release interlock (commit)     -> COMPLETE

Failure handling (spec D3, exact):

* A failure BEFORE the mutation frontier (before MIGRATED) unwinds the
  filesystem only: flip the junction back to the recorded previous target (if
  a flip happened) and release the interlock -> ROLLED_BACK. The database was
  never mutated, so no restore is needed.
* A failure AT or AFTER MIGRATED flips the junction back AND restores the
  step-3 backup, so the old binary never runs against a newer schema. Success
  -> ROLLED_BACK.
* If that restore itself fails, HALT: ensure the service is stopped (never
  running on a wrong schema), preserve the verified backup + journal, and emit
  the operator recovery document -> HALTED_RESTORE_FAILED.
* A release whose migration is declared non-restorable
  (``plan.migration_restorable=False``) with no ``operator_ack`` is refused at
  phase 0 -> REFUSED_NON_RESTORABLE.

Resume (idempotent): :func:`run_upgrade` reads any existing journal for the
context and continues from its recorded boundary. Because the on-disk phase is
only advanced AFTER its real action succeeds and is persisted, a kill at any
boundary leaves a journal the resume can drive to a clean COMPLETE or a clean
rollback. Each forward step is individually idempotent (see the per-step
docstrings), so re-running a step whose effect already landed is safe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from civiccast.native.upgrade import journal as journal_io
from civiccast.native.upgrade.models import (
    OperatorRecovery,
    UpgradeContext,
    UpgradeJournal,
    UpgradeOutcome,
    UpgradePhase,
    UpgradePlan,
    UpgradeSeams,
)

RECOVERY_DOC_NAME = "UPGRADE-RECOVERY.md"


def _persist(
    journal: UpgradeJournal, phase: UpgradePhase, detail: str, **updates: object
) -> UpgradeJournal:
    """Advance the journal one boundary and write it atomically."""

    journal = journal_io.advance(journal, phase, detail, **updates)
    journal_io.write_journal(journal)
    return journal


def _terminal(
    journal: UpgradeJournal, phase: UpgradePhase, detail: str, **updates: object
) -> UpgradeJournal:
    """Move to a terminal phase (not rank-gated) and write it atomically."""

    journal = journal_io.advance(journal, phase, detail, **updates)
    journal_io.write_journal(journal)
    return journal


def run_upgrade(
    plan: UpgradePlan,
    context: UpgradeContext,
    seams: UpgradeSeams,
) -> UpgradeOutcome:
    """Execute (or resume) the D3 upgrade for ``context``; return the outcome.

    Never raises for an expected upgrade failure -- every failure lands as a
    terminal journal phase (ROLLED_BACK / HALTED_RESTORE_FAILED /
    REFUSED_NON_RESTORABLE) so the caller (NSIS hook / CLI) reads an exit code,
    not a stack trace. A programming error in a seam still propagates.
    """

    existing = journal_io.load_journal(context.state_root)
    if existing is not None and not existing.phase.is_terminal:
        return _resume(existing, seams)
    # A terminal journal for this state root: a fresh upgrade would overwrite a
    # recovery point that may still matter. Start fresh ONLY from COMPLETE (the
    # previous upgrade committed); a terminal FAILURE journal is left for the
    # operator, and we refuse to bulldoze it silently.
    #
    # <installer-path-audit BL-06> The refusal used to return the STALE
    # journal's own phase and error verbatim, so the CLI logged
    # "upgrade outcome: rolled_back" with the PREVIOUS run's reason and
    # returned 10 -- which nsis-hooks-bootstrap.nsh (post-#143) converts into a
    # fatal 124 telling the operator to "re-run setup after resolving the
    # cause". Re-running returned 10 again. Forever. Whatever was fixed. And
    # nothing deletes this journal: %ProgramData%\CivicCast is preserved by
    # uninstall BY DESIGN, so uninstall/reinstall did not clear it either.
    #
    # It now has its own terminal phase, its own exit code, and its own
    # operator text naming the file to move. The preserved journal on disk is
    # NOT rewritten -- the outcome below is an in-memory copy, so the
    # operator's recovery point survives exactly as the previous run left it.
    if existing is not None and existing.phase is not UpgradePhase.COMPLETE:
        refusal = existing.model_copy(
            update={
                "phase": UpgradePhase.REFUSED_STALE_JOURNAL,
                "error": (
                    f"a previous upgrade attempt ended in {existing.phase.value!r} and its "
                    f"journal is preserved at {journal_io.journal_path(Path(context.state_root))}; "
                    "this run did NOTHING. Archive or move that journal to retry. Its own "
                    f"recorded reason was: {existing.error or '(none recorded)'}"
                ),
            }
        )
        return UpgradeOutcome(phase=refusal.phase, journal=refusal)

    journal = UpgradeJournal(
        plan=plan,
        context=context,
        phase=UpgradePhase.INIT,
        # <installer-path-audit MA-01> Bind the run to what its seam bundle can
        # actually undo on disk, at phase 0, so every later claim in this
        # journal is checkable against it.
        filesystem_rollback=seams.filesystem_rollback,
    )

    # D3 gate: a declared-non-restorable migration refuses auto-upgrade.
    if not plan.migration_restorable and not plan.operator_ack:
        journal = _terminal(
            journal,
            UpgradePhase.REFUSED_NON_RESTORABLE,
            "migration declared non-restorable and no operator_ack supplied; "
            "auto-upgrade refused (manual path required)",
            error="migration_restorable=False without operator_ack",
        )
        return UpgradeOutcome(phase=journal.phase, journal=journal)

    journal_io.write_journal(journal)  # phase-0 recorded before anything is acquired
    journal = journal.model_copy(
        update={
            "pre_schema_revision": seams.schema_revision(),
        }
    )
    journal_io.write_journal(journal)
    return _drive_forward(journal, seams)


def _resume(journal: UpgradeJournal, seams: UpgradeSeams) -> UpgradeOutcome:
    """Continue an interrupted upgrade from its recorded boundary.

    The recorded phase names the LAST boundary whose real action both
    succeeded and was persisted. Resume re-drives forward from there; every
    forward step is idempotent, so re-attempting the next step is safe even if
    the kill happened just after its effect but just before its journal write.
    """

    return _drive_forward(journal, seams)


def _drive_forward(journal: UpgradeJournal, seams: UpgradeSeams) -> UpgradeOutcome:
    """Walk the forward D3 steps from the journal's current phase to COMPLETE.

    On any step failure, hand off to :func:`_rollback`, which chooses the
    pre- vs post-mutation unwind based on the step being ATTEMPTED when the
    failure fired -- not merely the last persisted phase. This distinction is
    load-bearing: a ``seams.migrate()`` that RAISES leaves the journal at
    TREE_LAID (MIGRATED is only persisted on success), yet the migration was
    attempted and may have partially mutated the schema, so the unwind MUST
    restore the backup. ``attempting`` carries that intent into the handler.
    """

    attempting = journal.phase
    try:
        if journal.phase.rank < UpgradePhase.INTERLOCK_ACQUIRED.rank:
            attempting = UpgradePhase.INTERLOCK_ACQUIRED
            seams.acquire_interlock()
            journal = _persist(
                journal, UpgradePhase.INTERLOCK_ACQUIRED, "D7a maintenance interlock acquired"
            )

        if journal.phase.rank < UpgradePhase.WRITERS_DRAINED.rank:
            attempting = UpgradePhase.WRITERS_DRAINED
            if not seams.drain_and_verify_quiescence():
                raise RuntimeError("writers did not drain / quiescence not verified (WS2 snapshot)")
            journal = _persist(
                journal, UpgradePhase.WRITERS_DRAINED, "writers drained; quiescence verified"
            )

        if journal.phase.rank < UpgradePhase.BACKUP_VERIFIED.rank:
            attempting = UpgradePhase.BACKUP_VERIFIED
            backup_dir = str(
                Path(journal.context.state_root) / "backups" / f"pre-{journal.plan.new_version}"
            )
            backup_ref = seams.backup(backup_dir)
            if not (backup_ref.verified and backup_ref.restore_drill_ok):
                detail = (
                    "; ".join(backup_ref.restore_drill_errors)
                    if backup_ref.restore_drill_errors
                    else "no detail reported (verified=False before a restore-drill ran)"
                )
                raise RuntimeError(
                    "pre-upgrade backup failed verification (hash or restore-drill spot check): "
                    f"{detail}"
                )
            journal = _persist(
                journal,
                UpgradePhase.BACKUP_VERIFIED,
                "verified pre-upgrade backup taken (hash + restore-drill spot check)",
                backup=backup_ref,
            )

        if journal.phase.rank < UpgradePhase.TREE_LAID.rank:
            attempting = UpgradePhase.TREE_LAID
            previous_target = seams.read_junction()
            # <installer-path-audit MA-02> Persist the previous target BEFORE
            # the flip, not after. junction.py:33-35 already CLAIMS the journal
            # records it "BEFORE the flip"; the code did not. If the post-flip
            # journal write raised (_harden_state_root_acl is deliberately
            # fail-loud; disk full; ACL denied), the journal stayed at
            # BACKUP_VERIFIED with previous_junction_target=None, so
            # _rollback's guard skipped the flip-back entirely and the run
            # reported ROLLED_BACK / exit 10 with the detail
            # "junction/tree reverted" while `current` pointed at the NEW tree
            # -- PR #143's own defect, reproduced one level up.
            journal = journal.model_copy(update={"previous_junction_target": previous_target})
            journal_io.write_journal(journal)
            new_target = seams.lay_tree(journal.plan.new_version)
            seams.flip_junction(new_target)
            journal = _persist(
                journal,
                UpgradePhase.TREE_LAID,
                f"payload target prepared and selected: {new_target}",
                previous_junction_target=previous_target,
                new_junction_target=new_target,
            )

        if journal.phase.rank < UpgradePhase.MIGRATED.rank:
            attempting = UpgradePhase.MIGRATED
            seams.migrate()
            post_revision = seams.schema_revision()
            # <installer-path-audit BL-03> VERIFY THE MIGRATION LANDED.
            #
            # post_schema_revision used to be written and never read -- nothing
            # compared it to the expected head, and nothing compared it to
            # pre_schema_revision. So an `alembic upgrade head` that silently
            # NO-OPS (the version-location discovery regression alembic/env.py's
            # own docstring warns about, across 110 migration files in 32
            # per-module versions/ directories) left the control plane
            # maintenance-attested, D3 committing COMPLETE, exit 0, and the
            # station running new code on the old schema. The next gate does not
            # close it either: the maintenance attestation names no version,
            # build identity, or schema revision (BL-04).
            if seams.expected_schema_head is None:
                # No seam at all: a fake-seam bundle that does not model this
                # contract. Recorded as UNAVAILABLE rather than silently
                # treated as a pass. `build_default_seams` always supplies it,
                # so production never takes this branch.
                expected_head = None
                detail = (
                    "alembic upgrade head applied; expected-head assertion UNAVAILABLE "
                    "(no expected_schema_head seam wired in this bundle)"
                )
            elif (expected_head := seams.expected_schema_head()) is None:
                # The seam IS wired and could not answer. Review of PR #145:
                # this used to fall into the UNAVAILABLE journal-string branch
                # above, which is fail-open -- the run committed COMPLETE with
                # the migration unverified, and (because `__main__` passes the
                # same value to the health gate) BL-04's identity gate had
                # already been silently un-wired for that run too. Refuse.
                raise RuntimeError(
                    "expected schema head unavailable: this payload's own migration head "
                    "could not be resolved, so 'the migration landed' cannot be checked and "
                    "the maintenance health gate cannot be bound to this build's identity. "
                    "Refusing to commit an unverified upgrade."
                )
            elif post_revision != expected_head:
                raise RuntimeError(
                    "migration did not land: the database is at revision "
                    f"{post_revision!r} after 'alembic upgrade head', but the shipped payload "
                    f"expects {expected_head!r}. Committing here would leave new code running "
                    "against an old schema."
                )
            else:
                detail = f"alembic upgrade head applied and verified at {expected_head}"
            journal = _persist(
                journal,
                UpgradePhase.MIGRATED,
                detail,
                post_schema_revision=post_revision,
            )

        if journal.phase.rank < UpgradePhase.HEALTH_GREEN.rank:
            attempting = UpgradePhase.HEALTH_GREEN
            if not seams.health_gate():
                raise RuntimeError(
                    "service did not reach green in maintenance/read-only health mode"
                )
            journal = _persist(
                journal, UpgradePhase.HEALTH_GREEN, "maintenance/read-only health green"
            )

        if journal.phase.rank < UpgradePhase.COMPLETE.rank:
            attempting = UpgradePhase.COMPLETE
            seams.release_interlock()
            journal = _persist(
                journal, UpgradePhase.COMPLETE, "interlock released; upgrade committed"
            )

    except NotImplementedError:
        # A genuinely unwired seam is a programming/wiring fault, NOT an
        # operational upgrade failure -- it must propagate loud, never be
        # laundered into a normal ROLLED_BACK outcome. (WP-4 wired the three
        # service-control seams; this guard remains for any future unwired seam.)
        raise
    except Exception as exc:  # any OPERATIONAL step failure funnels to the D3 unwind
        return _rollback(journal, seams, reason=str(exc), attempting=attempting)

    return UpgradeOutcome(phase=journal.phase, journal=journal)


def _rollback(
    journal: UpgradeJournal,
    seams: UpgradeSeams,
    *,
    reason: str,
    attempting: UpgradePhase,
) -> UpgradeOutcome:
    """Unwind a failed upgrade per D3, choosing pre- vs post-mutation semantics.

    * Before the mutation frontier (the failing step was BEFORE the migration):
      flip the junction back (if it moved) and release the interlock. No DB
      restore -- the schema was never touched.
    * At/after the mutation frontier (the failing step WAS the migration, or a
      step after it): flip the junction back AND restore the step-3 backup so
      the old binary never runs against a newer schema. If the restore raises,
      HALT (service stopped, backup + journal preserved, operator recovery
      document emitted).

    ``attempting`` is the phase whose action was running when the failure fired.
    Keying on it (not the last persisted phase) is what makes a RAISING migrate
    -- which never advances the journal to MIGRATED -- still restore the DB.
    """

    after_mutation = attempting.rank >= UpgradePhase.MIGRATED.rank
    sub_failures: list[str] = []

    # Restore the DB FIRST on the post-mutation path, so that if the restore
    # fails we never flip the old binary live against the new schema (the
    # service is left stopped by the halt). The junction flip-back follows a
    # successful restore.
    if after_mutation:
        try:
            if journal.backup is None:  # pragma: no cover - MIGRATED implies a backup exists
                raise RuntimeError("no backup recorded at/after MIGRATED — cannot restore")
            seams.restore_backup(journal.backup)
        except Exception as restore_exc:  # restore failure IS the halt trigger
            return _halt(journal, seams, upgrade_reason=reason, restore_error=str(restore_exc))

    # Flip the junction back to the recorded previous target (both paths, if a
    # flip happened). previous_junction_target is None on a fresh install with
    # no prior live tree; nothing to flip back to then.
    #
    # <installer-path-audit BL-07> Contained. A raise from flip_junction here
    # escaped _rollback, escaped run_upgrade (the try in _drive_forward has
    # already been exited) and reached __main__ as exit 40, "unexpected
    # fault" -- leaving a NON-TERMINAL journal, a LEAKED interlock, and no
    # statement anywhere about what the machine is now in. A sub-failure is
    # recorded and the terminal phase still lands.
    if (
        journal.previous_junction_target is not None
        and journal.phase.rank >= UpgradePhase.TREE_LAID.rank
    ):
        try:
            seams.flip_junction(journal.previous_junction_target)
        except Exception as flip_exc:
            sub_failures.append(f"junction flip-back failed: {flip_exc}")

    # Release the interlock so the (rolled-back, old-version) runtime resumes.
    #
    # <installer-path-audit BL-05> ONLY when this run actually took it. The
    # unconditional release meant a rollback caused by acquire_interlock ITSELF
    # failing (attempting=INTERLOCK_ACQUIRED, journal.phase=INIT) released the
    # interlock the OTHER run was holding: run A is inside migrate(), run B
    # (a second setup.exe, a retry, an operator double-click) cannot take the
    # interlock, funnels here, and releases A's. The supervisor then re-permits
    # writers against a schema mid-migration; B reports ROLLED_BACK/exit 10 and
    # A reports COMPLETE/exit 0, both journals internally consistent and both
    # wrong about the machine. release_interlock's own owner check (win_probes)
    # is the second half of this fix.
    if journal.phase.rank >= UpgradePhase.INTERLOCK_ACQUIRED.rank:
        try:
            seams.release_interlock()
        except Exception as release_exc:
            sub_failures.append(f"interlock release failed: {release_exc}")
    else:
        sub_failures.append(
            "interlock NOT released: this run never recorded acquiring it, so releasing it "
            "would have released another run's"
        )

    # <installer-path-audit MA-01> Say what actually happened on disk.
    #
    # Under the flat installer layout -- the ONLY layout production runs, since
    # nsis-hooks-bootstrap.nsh passes --flat-installer-layout on every
    # invocation -- read_junction, lay_tree and flip_junction all resolve to
    # the same <install_root>\runtime string the bootstrap already extracted
    # the NEW payload into before the engine started. So there is no old tree
    # anywhere, previous_junction_target == new_junction_target, and the
    # flip-back is a tautology. Writing "junction/tree reverted" there was a
    # false claim in the durable record -- and after PR #143 the installer and
    # the journal told different stories about the same event, with only the
    # installer right.
    if journal.filesystem_rollback:
        payload_detail = (
            "DB restored from backup + junction reverted"
            if after_mutation
            else "junction/tree reverted (no DB mutation)"
        )
    else:
        payload_detail = (
            "DB restored from backup; the on-disk payload is the NEW version and was NOT "
            "reverted (flat installer layout: there is no previous tree to revert to)"
            if after_mutation
            else "no DB mutation; the on-disk payload is the NEW version and was NOT reverted "
            "(flat installer layout: there is no previous tree to revert to)"
        )
    detail = f"rolled back after failure ({reason}); {payload_detail}"
    if sub_failures:
        detail += "; ROLLBACK SUB-FAILURES: " + "; ".join(sub_failures)
    error = reason
    if sub_failures:
        error = f"{reason} [rollback sub-failures: {'; '.join(sub_failures)}]"
    journal = _terminal(journal, UpgradePhase.ROLLED_BACK, detail, error=error)
    return UpgradeOutcome(phase=journal.phase, journal=journal)


def _halt(
    journal: UpgradeJournal,
    seams: UpgradeSeams,
    *,
    upgrade_reason: str,
    restore_error: str,
) -> UpgradeOutcome:
    """The rollback-restore-failure terminal state (spec D3).

    Ensures the service is stopped (so no binary runs against a wrong schema),
    preserves the verified backup + journal, and emits an operator recovery
    document naming exact next steps.
    """

    # <installer-path-audit BL-07> ORDER AND CONTAINMENT.
    #
    # The recovery document is written FIRST, and every step here is
    # individually contained. Previously `seams.stop_service()` ran first and
    # PROPAGATES BY DESIGN ("Any failure of the real SCM stop PROPAGATES"), so
    # a service stuck in STOP_PENDING -- common when a child hangs, and the
    # exact companion of a migration + restore that both just failed -- escaped
    # _halt, escaped run_upgrade, and reached __main__ as exit 40 "unexpected
    # fault". Result: no HALTED_RESTORE_FAILED journal, no UPGRADE-RECOVERY.md,
    # a journal frozen at TREE_LAID/MIGRATED, a possibly-RUNNING service on a
    # half-migrated schema, and an operator told to "see the installer log".
    # The ONE artifact designed for exactly this case was the one that did not
    # get written. `doc_path.write_text` could raise for the same reasons
    # (full disk, denied ACL), so it is contained too -- a terminal phase and
    # its documented exit code always land.
    sub_failures: list[str] = []
    doc_path: Path | None = None
    try:
        doc_path = _write_recovery_document(
            journal, upgrade_reason=upgrade_reason, restore_error=restore_error
        )
    except Exception as doc_exc:
        sub_failures.append(f"recovery document could not be written: {doc_exc}")

    # Never leave anything running on a schema we could not roll back.
    try:
        seams.stop_service()
    except Exception as stop_exc:
        sub_failures.append(
            f"the service could not be confirmed STOPPED: {stop_exc}. It may still be running "
            "against a schema it does not match -- stop it manually (sc.exe stop "
            "CivicCastSupervisor) before doing anything else"
        )

    detail = (
        f"HALTED: rollback restore failed ({restore_error}) after upgrade failure ({upgrade_reason}); "
        f"backup + journal preserved, recovery document at {doc_path or '(NOT WRITTEN)'}"
    )
    detail += (
        "; service stopped"
        if not sub_failures
        else "; HALT SUB-FAILURES: " + "; ".join(sub_failures)
    )
    journal = _terminal(
        journal,
        UpgradePhase.HALTED_RESTORE_FAILED,
        detail,
        error=f"restore failed: {restore_error}"
        + (f" [halt sub-failures: {'; '.join(sub_failures)}]" if sub_failures else ""),
        recovery_document_path=str(doc_path) if doc_path is not None else None,
    )
    return UpgradeOutcome(phase=journal.phase, journal=journal)


def _write_recovery_document(
    journal: UpgradeJournal,
    *,
    upgrade_reason: str,
    restore_error: str,
) -> Path:
    """Write the operator recovery markdown next to the journal; return its path."""

    state_root = Path(journal.context.state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    doc_path = state_root / RECOVERY_DOC_NAME
    journal_file = journal_io.journal_path(state_root)
    backup = journal.backup

    # <installer-path-audit MA-01> The next-steps branch on what this run's
    # seam bundle could actually undo on disk.
    #
    # The old text was UNCONDITIONAL and told the operator to "re-point the
    # 'current' junction under <install_root> back to <previous target>". Under
    # the flat installer layout -- the only layout production runs -- there IS
    # no 'current' junction, and <previous target> is <install_root>\runtime,
    # the directory that already holds the NEW code. So the document sent the
    # operator to re-point a junction that does not exist, at the new payload,
    # and omitted the step that actually matters. That was the worst-case
    # recovery document for the only layout that ships.
    if journal.filesystem_rollback:
        payload_steps = [
            f"After a verified restore, re-point the 'current' junction under "
            f"{journal.context.install_root} back to "
            f"{journal.previous_junction_target or '(the previous app tree)'}.",
        ]
    else:
        payload_steps = [
            f"IMPORTANT: the files on disk at {journal.context.install_root} are the "
            f"{journal.plan.new_version} payload. This installation layout keeps ONE payload "
            f"directory, so the upgrade engine could not put {journal.plan.old_version}'s files "
            "back and did not try. Restoring the database alone is NOT enough.",
            f"After a verified restore, re-install {journal.plan.old_version} over this machine "
            "(run that version's setup.exe) BEFORE starting the service, so the code on disk "
            "matches the schema you just restored.",
        ]

    next_steps = [
        "Do NOT start the CivicCast (Native) service — its schema may be the "
        "partially-migrated new schema while the binary on disk is the old "
        "version. Starting it risks corrupting data.",
        f"The verified pre-upgrade backup is preserved at: "
        f"{backup.backup_dir if backup else '(none recorded)'}",
        f"Manually restore that backup into the database at "
        f"{journal.context.database_url} using the WS2 restore tooling "
        f"(civiccast.dr.backup.run_postgres_restore against "
        f"{backup.db_artifact if backup else '(artifact)'}).",
        *payload_steps,
        "Then release the D7a maintenance interlock "
        "(HKLM\\SOFTWARE\\CivicCast\\Maintenance) and start the old version.",
        f"Preserve this journal for support: {journal_file}",
    ]
    recovery = OperatorRecovery(
        written_utc=datetime.now(UTC).isoformat(),
        old_version=journal.plan.old_version,
        new_version=journal.plan.new_version,
        backup_dir=backup.backup_dir if backup else "(none recorded)",
        backup_manifest_hash=backup.manifest_hash if backup else "(none)",
        journal_path=str(journal_file),
        reason=f"upgrade failed ({upgrade_reason}); automatic rollback restore also failed ({restore_error})",
        next_steps=next_steps,
    )

    lines = [
        "# CivicCast (Native) — Upgrade Recovery Required",
        "",
        f"- Written: {recovery.written_utc}",
        f"- Upgrade: {recovery.old_version} -> {recovery.new_version}",
        f"- Reason: {recovery.reason}",
        f"- Backup dir: {recovery.backup_dir}",
        f"- Backup manifest hash: {recovery.backup_manifest_hash}",
        f"- Journal: {recovery.journal_path}",
        "",
        "## The service has been STOPPED and left stopped on purpose.",
        "",
        "Automatic rollback could not restore the pre-upgrade database, so the "
        "engine halted rather than risk running a binary against a schema it "
        "does not match. Follow these steps IN ORDER:",
        "",
    ]
    lines.extend(f"{i}. {step}" for i, step in enumerate(recovery.next_steps, start=1))
    lines.append("")
    doc_path.write_text("\n".join(lines), encoding="utf-8")
    return doc_path
