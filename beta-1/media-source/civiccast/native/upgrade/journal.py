# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable, power-loss-resilient persistence for the upgrade journal.

The journal is the ONLY thing a resuming process trusts after a kill. Two
properties make that trust sound:

* **Atomic writes.** Every persist writes a sibling temp file, fsyncs it, and
  ``os.replace``\\s it over the real journal. ``os.replace`` is atomic on both
  POSIX and Windows (``MoveFileEx`` with ``REPLACE_EXISTING``), so a power loss
  during a write leaves EITHER the old complete journal OR the new complete
  journal on disk -- never a half-written one. A reader therefore never parses
  a torn record.
* **Fail-loud load.** The journal is parsed through
  :class:`~civiccast.native.upgrade.models.UpgradeJournal` (``extra="forbid"``);
  a truncated, schema-drifted, or hand-edited journal raises
  :class:`JournalError` instead of resuming from a value we cannot trust.

The journal file lives under the upgrade state root (ProgramData), NOT the
install root, so it survives the ``current`` junction/tree swap that the very
upgrade it describes performs.

SECURITY (municipal-shared-PC fix, 2026-07-30; mirrors
:mod:`civiccast.native.provision.journal`'s fix for the same class of defect
in that sibling module -- see its module docstring for the fuller narrative):

1. **The state root's own DACL is hardened** to SYSTEM + Administrators + the
   calling process's own account, on every :func:`write_journal` call -- see
   :func:`_harden_state_root_acl`. Before this fix the directory was created
   with a bare ``mkdir`` and inherited ``C:\\ProgramData``'s default DACL,
   where ``BUILTIN\\Users`` has ``ReadAndExecute`` -- world-readable on a
   shared station. This also covers the pre-upgrade ``pg_dump`` backups the
   engine writes under ``state_root/backups/pre-<version>/``
   (:func:`civiccast.native.upgrade.orchestrator._drive_forward`'s
   ``BACKUP_VERIFIED`` step, via :mod:`civiccast.dr.backup`): that directory
   is created with a plain ``mkdir`` too, AFTER the state root has already
   been hardened by the phase-0 :func:`write_journal` call in
   :func:`civiccast.native.upgrade.orchestrator.run_upgrade`, so it inherits
   the hardened, non-``BUILTIN\\Users`` DACL via ordinary NTFS ACE
   inheritance (the SDDL's ``OICI`` flags) rather than needing its own
   hardening call.

2. **``context.database_url`` is NOT redacted here**, unlike
   :mod:`civiccast.native.provision.journal`'s ``database_password``. That
   sibling fix redacts because tracing every consumer of a LOADED
   provisioning journal found nothing reads the persisted password back. The
   same trace here finds a real, live consumer:
   :func:`civiccast.native.upgrade.orchestrator._write_recovery_document`
   embeds ``journal.context.database_url`` verbatim into the
   operator-facing ``UPGRADE-RECOVERY.md`` on the ``HALTED_RESTORE_FAILED``
   path -- and on a RESUMED run (``run_upgrade`` -> ``_resume(existing,
   seams)``), ``journal`` there IS the journal ``load_journal`` read back
   from disk, so ``journal.context.database_url`` at that point is exactly
   the on-disk value. Redacting it the way the sibling fix redacts the
   password would silently replace the real Postgres connection string with
   a fixed marker in the one document a human reads to perform a manual
   restore during the engine's own worst-case failure path -- a functional
   regression, not just a docs fix, and NOT something this change makes
   unilaterally (flagged for the owner instead; see the task report). The
   DACL hardening in point 1 is therefore this fix's entire mitigation for
   this secret: it remains plaintext on disk, but the directory holding it
   is no longer world-readable.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from civiccast.native.upgrade.models import UpgradeJournal, UpgradePhase

JOURNAL_FILENAME = "upgrade-journal.json"

#: SYSTEM + Administrators full control, PLUS the CURRENT process's own user
#: SID -- identical shape and rationale to
#: ``civiccast.native.provision.journal._STATE_ROOT_SDDL_TEMPLATE``; see that
#: constant's docstring for the full trap narrative (hardening to ONLY
#: SYSTEM+Administrators locks the writing process out of its own SECOND
#: write, because ordinary data access on a subsequent freshly-opened handle
#: is not an owner-implicit right the way ``WRITE_DAC``/``READ_CONTROL``
#: are). Inheritable (``OICI``) so the ``backups/pre-<version>/`` pg_dump
#: directory created under this state root inherits the same hardening
#: without a hardening call of its own. Protected (``P``) so ProgramData's
#: inherited, world-readable default DACL cannot flow back onto this
#: directory on a future create/reboot/GPO refresh.
_STATE_ROOT_SDDL_TEMPLATE = "D:P(A;OICI;GA;;;SY)(A;OICI;GA;;;BA)(A;OICI;GA;;;{caller_sid})"


def _current_process_sid_sddl() -> str:
    """The current process token's user SID, as an SDDL ``S-1-...`` string --
    the piece :data:`_STATE_ROOT_SDDL_TEMPLATE` needs and can only be known at
    runtime (there is no fixed "the installer's account" SID to hard-code).

    Identical to ``civiccast.native.provision.journal``'s function of the
    same name; not shared to keep each module importable standalone (both
    are Windows-only lazy imports, per the house rule that ``import
    civiccast.native.*`` must succeed on Linux)."""

    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    sid, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
    # str(): pywin32 ships no type stubs, so this call is Any and the
    # declared -> str was unenforced. Coercing makes the annotation true
    # at runtime instead of merely claimed.
    return str(win32security.ConvertSidToStringSid(sid))


def _harden_state_root_acl(state_root: Path) -> None:
    """Restrict ``state_root``'s own DACL to SYSTEM + Administrators + the
    calling process's own account (see :data:`_STATE_ROOT_SDDL_TEMPLATE`'s
    docstring for why the third grantee is required).

    Windows-only; a no-op on any other platform so this module keeps
    importing (and its pure logic keeps being testable) on Linux. ``pywin32``
    is imported LAZILY here for the same reason.

    Called on EVERY :func:`write_journal` (idempotent/self-healing, not just
    first creation) and deliberately FAIL-LOUD, mirroring
    ``civiccast.native.provision.journal._harden_state_root_acl``: a
    hardening failure here means the journal -- which still carries the
    plan/context/history and, on the halt path, the recovery document -- is
    about to be written into a directory this call could not actually
    secure. Silently continuing would leave that state (including the
    plaintext ``database_url``; see the module docstring's security note on
    why it is not redacted) readable by any other local user on a shared
    station without telling anyone.
    """

    if os.name != "nt":
        return

    import win32security

    sddl = _STATE_ROOT_SDDL_TEMPLATE.format(caller_sid=_current_process_sid_sddl())
    security_descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        sddl, win32security.SDDL_REVISION_1
    )
    try:
        win32security.SetFileSecurity(
            str(state_root),
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            security_descriptor,
        )
    except Exception as exc:  # pragma: no cover - real Win32 call; see docstring
        raise JournalError(
            f"could not harden ACLs on upgrade state root {state_root}: {exc}"
        ) from exc


class JournalError(RuntimeError):
    """Raised when a journal on disk cannot be parsed or an illegal phase
    transition is attempted. Both are fail-loud: the engine must not proceed
    on a journal it cannot trust."""


def journal_path(state_root: str | Path) -> Path:
    return Path(state_root) / JOURNAL_FILENAME


def load_journal(state_root: str | Path) -> UpgradeJournal | None:
    """Load the journal for ``state_root``; return None if none exists.

    Raises :class:`JournalError` for a present-but-unparseable journal -- an
    upgrade must never silently start fresh over a journal it failed to read,
    because that journal may describe an in-flight mutation whose recovery
    point (the backup) still matters.
    """

    path = journal_path(state_root)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - unreadable file is env-specific
        raise JournalError(f"cannot read journal at {path}: {exc}") from exc
    try:
        return UpgradeJournal.model_validate_json(raw)
    except ValidationError as exc:
        raise JournalError(f"journal at {path} is corrupt/unparseable: {exc}") from exc


def write_journal(journal: UpgradeJournal) -> Path:
    """Atomically persist ``journal`` under its own context's state root.

    Uses temp-file + fsync + ``os.replace`` so a kill mid-write never yields a
    torn journal (see module docstring). Also -- see the module docstring's
    security note -- hardens the state root's own ACL on every call (cheap
    and self-healing).
    """

    state_root = Path(journal.context.state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    _harden_state_root_acl(state_root)
    path = journal_path(state_root)
    payload = json.dumps(journal.model_dump(mode="json"), indent=2, sort_keys=True)

    # Unique temp name so two racing writers cannot corrupt each other's temp.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)
    return path


def advance(
    journal: UpgradeJournal,
    phase: UpgradePhase,
    detail: str,
    **updates: object,
) -> UpgradeJournal:
    """Return a new journal at ``phase`` with an appended history entry.

    Enforces the state machine: a forward move may only go to the immediately
    higher forward rank (no skipping a boundary), and a terminal state is
    reachable from any non-terminal phase (rollback/halt/refuse can be entered
    whenever a step fails). Attempting to advance FROM a terminal phase, or to
    skip a forward boundary, raises :class:`JournalError` -- the transition
    grammar is part of the safety proof, not a convention.

    This function does NOT write; the caller persists via :func:`write_journal`
    after the corresponding real action succeeds, so the on-disk phase never
    runs ahead of the world.
    """

    current = journal.phase
    if current.is_terminal:
        raise JournalError(f"cannot advance from terminal phase {current.value!r}")

    # A forward move (non-terminal target) may advance exactly one boundary;
    # terminal targets (rollback/halt/refuse) are reachable from any phase.
    if not phase.is_terminal and phase.rank != current.rank + 1:
        raise JournalError(
            f"illegal forward transition {current.value!r} -> {phase.value!r} "
            "(forward phases advance exactly one boundary)"
        )

    stamp = datetime.now(UTC).isoformat()
    history = [*journal.history, (phase.value, stamp, detail)]
    return journal.model_copy(update={"phase": phase, "history": history, **updates})
