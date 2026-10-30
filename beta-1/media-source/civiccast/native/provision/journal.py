# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable, power-loss-resilient persistence for the provisioning journal.

Same persistence contract as :mod:`civiccast.native.upgrade.journal` (that
module's docstring, verbatim rationale, applies here unchanged):

* **Atomic writes.** Every persist writes a sibling temp file, fsyncs it, and
  ``os.replace``\\s it over the real journal, so a power loss during a write
  leaves EITHER the old complete journal OR the new complete journal on disk
  -- never a half-written one.
* **Fail-loud load, tolerant of unknown keys.** The journal is parsed
  through :class:`~civiccast.native.provision.models.ProvisionJournal`; a
  truncated, type-drifted, or hand-edited journal raises
  :class:`JournalError` instead of resuming from a value we cannot trust.
  Since beta.5.1 (2026-09-09) an UNKNOWN KEY is not corruption: journals
  written by the August 2026 beta.1/beta.2 installers carry
  ``context.nats_*`` fields the model no longer declares, and rejecting them
  halted every upgrade over such a station. :func:`load_journal` drops
  unknown keys (``extra="ignore"`` on the persisted models) and names them
  on this module's logger at INFO -- a line only a caller that configured
  logging ever sees. The provisioning CLI (:mod:`~civiccast.native.
  provision.__main__`) configures no logging, so ``main()`` writes the ONE
  ``provision note:`` line per run to its own stderr, from the ignored-key
  list :func:`load_journal_with_ignored_keys` returns alongside its first
  load (computed from the same bytes that load parsed -- there is no second
  read of the file). That is deliberately once per run, not once per load:
  ``main`` -> ``probe_resumable_journal`` -> ``probe_credential_lost_journal``
  -> ``run_provision`` each load the same file. Where that line ends up: the
  installer's Rust wrapper (``run_native_provision`` in
  ``native_service_registration.rs``) runs the CLI with ``Command::output()``
  and deliberately does not forward the captured stderr, so the note is NOT
  yet in ``install-progress.log``; forwarding it is a follow-up tracked with
  the runtime-ownership diagnosability PR. Until then the note is visible to
  anyone running the CLI by hand and to the CLI tests.

The journal file lives under the provisioning state root (ProgramData), so a
resuming process can find it regardless of what happened to the data
directories it describes.

SECURITY (municipal-shared-PC fix, 2026-07-30): two things :func:`write_journal`
does that the ORIGINAL version of this module did not --

1. **Never persists the plaintext database password.** An audit,
   independently re-verified with measured ACLs on a real Windows 11 box,
   found ``ProvisionContext.database_password`` written verbatim into this
   JSON file, which -- see point 2 -- was itself sitting in a
   world-readable directory. By inspection of every consumer of a LOADED
   journal (:mod:`civiccast.native.provision.orchestrator`,
   :func:`civiccast.native.provision.__main__.main`): nothing ever reads
   ``journal.context.database_password`` back from an on-disk journal for
   real work. The orchestrator's resume path
   (:func:`~civiccast.native.provision.orchestrator.run_provision`) never
   references it; the only two live readers of ``context.database_password``
   are (a) the ``run_initdb`` seam, which closes over the ``context`` object
   built FRESH in the CURRENT process's ``main()`` invocation (a NEW
   password every run, never the journal's persisted one), and (b)
   :func:`civiccast.native.provision.models.resolve_database_url`, called
   with that SAME fresh, never-reloaded ``context``. There is therefore no
   resume path that needs the persisted value -- it is written to
   ``payload_dict`` here as :data:`_REDACTED_DATABASE_PASSWORD_MARKER`
   instead of ``journal.context.database_password``, which is otherwise
   left untouched in memory (the caller's own copy is unaffected; only the
   on-disk serialization is redacted).
2. **Hardens the state root's own DACL** to SYSTEM + Administrators,
   inheritance disabled -- see :func:`_harden_state_root_acl`.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from civiccast.native.provision.models import (
    ProvisionContext,
    ProvisionJournal,
    ProvisionPhase,
    ProvisionPlan,
)

_LOG = logging.getLogger(__name__)

JOURNAL_FILENAME = "provision-journal.json"

#: Written into the on-disk journal's ``context.database_password`` field in
#: place of the real value (see the module docstring's security note).
#: Non-empty so it still satisfies :class:`ProvisionContext`'s own
#: ``Field(min_length=1)`` validator on reload -- a loaded journal with this
#: marker is a normal, schema-valid ``ProvisionJournal``, it just does not
#: carry a usable password (nothing needs it to).
_REDACTED_DATABASE_PASSWORD_MARKER = "REDACTED-NOT-PERSISTED-TO-DISK"  # noqa: S105 -- not a password, a redaction marker

#: SYSTEM + Administrators full control, PLUS the CURRENT process's own user
#: SID (see :func:`_state_root_sddl` -- the SID is only known at runtime, so
#: this is a format template, not the final SDDL string). Inheritable to
#: files/subdirectories created inside the hardened directory (``OICI``),
#: PROTECTED (``P``) so ProgramData's inherited, world-readable default DACL
#: -- measured on a real Windows 11 box: ``BUILTIN\\Users`` has
#: ``ReadAndExecute`` on ``C:\\ProgramData`` -- cannot flow back onto this
#: directory on a future create/reboot/GPO refresh.
#:
#: WHY THE CALLER'S OWN SID, not just SYSTEM+Administrators (disclosed
#: empirical correction, found by exercising this against the real ACL on
#: this dev box, the same "measure it, don't assume it" rule
#: ``civiccast.native.win_probes``'s module docstring already follows):
#: hardening to ONLY SYSTEM+Administrators, with no ACE for the account that
#: actually creates the directory, locks that SAME PROCESS out of its own
#: state root the moment its token does not have ``BUILTIN\\Administrators``
#: ENABLED -- which is exactly the case for an ordinary (non-elevated, or a
#: UAC-split non-admin) token, even though that token is the OWNER of the
#: directory it just created. Unlike a registry key's ``WRITE_DAC``/
#: ``READ_CONTROL`` (which an owner keeps implicitly, see
#: ``native_service_registration.rs``'s own equivalent test), ORDINARY DATA
#: access (reading/writing files inside the directory on a SUBSEQUENT,
#: freshly-opened handle) is NOT an owner-implicit right -- confirmed by
#: this exact failure mode the first cut of this fix hit live: a second
#: ``write_journal`` call (a later provisioning phase, same process) failed
#: ``PermissionError: [Errno 13]`` trying to create its own temp file inside
#: the directory it had just hardened one call earlier. Including the
#: caller's own SID reproduces what Windows's OWN default DACL already does
#: for a freshly created object (creator's SID present alongside
#: inherited entries) while still REMOVING the one ACE that matters for the
#: shared-station threat model (``BUILTIN\\Users``, which admits every OTHER
#: local account too, not just this one).
_STATE_ROOT_SDDL_TEMPLATE = "D:P(A;OICI;GA;;;SY)(A;OICI;GA;;;BA)(A;OICI;GA;;;{caller_sid})"


def _current_process_sid_sddl() -> str:
    """The current process token's user SID, as an SDDL ``S-1-...`` string --
    the piece :data:`_STATE_ROOT_SDDL_TEMPLATE` needs and can only be known at
    runtime (there is no fixed "the installer's account" SID to hard-code)."""

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
    importing (and its pure logic keeps being testable) on Linux, per the
    house rule documented in
    :mod:`civiccast.native.win_probes`'s module docstring ("``import
    civiccast.native.*`` must succeed on Linux even though these functions
    are only ever callable on Windows") -- ``pywin32`` is imported LAZILY
    here for the same reason.

    Called on EVERY :func:`write_journal` (idempotent/self-healing, not just
    first creation) and deliberately FAIL-LOUD: this engine's own stated
    convention is "fail-loud on any unexpected state, never silently
    repair" (:mod:`civiccast.native.provision.orchestrator`'s module
    docstring), and a hardening failure here means the journal -- which,
    even with the password redacted, still carries the plan/context/history
    and the recovery document -- is about to be written into a directory
    this call could not actually secure. Silently continuing would leave
    that state readable by any other local user on a shared station without
    telling anyone. In production this call always runs from an ALREADY
    elevated process (the NSIS installer, or the provisioning CLI it spawns
    -- see ``native_service_registration.rs``'s module doc), so a real
    failure here is itself a signal something is wrong with the
    installation, not routine. A NON-elevated invocation (e.g. an operator
    running the repair CLI directly, without elevation, against a
    brand-new state root) is still able to harden its OWN freshly-created
    directory -- ``WRITE_DAC`` on a just-created object is an
    owner-implicit right regardless of elevation tier -- so this does not
    itself require admin; what requires elevation is the SAME token later
    needing to read/write OTHER accounts' or SYSTEM's already-hardened
    state, which is a separate, correctly-loud ``PermissionError`` rather
    than a silent no-op.
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
            f"could not harden ACLs on provisioning state root {state_root}: {exc}"
        ) from exc


class JournalError(RuntimeError):
    """Raised when a journal on disk cannot be parsed or an illegal phase
    transition is attempted. Both are fail-loud: the engine must not proceed
    on a journal it cannot trust."""


def journal_path(state_root: str | Path) -> Path:
    return Path(state_root) / JOURNAL_FILENAME


def load_journal(state_root: str | Path) -> ProvisionJournal | None:
    """Load the journal for ``state_root``; return None if none exists.

    Raises :class:`JournalError` for a present-but-unparseable journal -- a
    provisioning run must never silently start fresh over a journal it failed
    to read, because that journal may describe partially-provisioned state
    (e.g. an initdb'd data directory with no config written yet).

    Undeclared keys are dropped and named on this module's logger at INFO;
    a caller that wants the list itself uses
    :func:`load_journal_with_ignored_keys`.
    """

    journal, _ignored = load_journal_with_ignored_keys(state_root)
    return journal


def load_journal_with_ignored_keys(
    state_root: str | Path,
) -> tuple[ProvisionJournal | None, list[str]]:
    """:func:`load_journal`, also returning the dotted names of the keys the
    persisted models did not declare (see :func:`ignored_journal_keys`).

    The list is computed from the SAME text the journal was parsed from --
    one ``read_text`` -- so a file yanked or made unreadable between two
    reads cannot escape as an uncaught :class:`OSError` in a caller that
    wanted both (the CLI's ``main()`` did exactly that before beta.5.1's
    round-3 review). Every read failure is a :class:`JournalError`. No
    journal -> ``(None, [])``.
    """

    path = journal_path(state_root)
    if not path.exists():
        return None, []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JournalError(f"cannot read journal at {path}: {exc}") from exc
    try:
        journal = ProvisionJournal.model_validate_json(raw)
    except ValidationError as exc:
        raise JournalError(f"journal at {path} is corrupt/unparseable: {exc}") from exc
    ignored = ignored_journal_keys(raw)
    if ignored:
        _LOG.info(
            "provision journal at %s carries %d field(s) this version does not declare; "
            "ignored (legacy or newer-installer keys, not corruption): %s",
            path,
            len(ignored),
            ", ".join(ignored),
        )
    return journal, ignored


def ignored_journal_keys(raw: str) -> list[str]:
    """Dotted names of the keys in the raw journal JSON that
    :class:`ProvisionJournal` / :class:`ProvisionPlan` /
    :class:`ProvisionContext` do not declare (and therefore silently drop
    under ``extra="ignore"``). Pure; returns ``[]`` for anything that is not a
    JSON object (a parse failure is :func:`load_journal`'s job to report).
    Sorted so the log line is deterministic.

    Walks exactly three dicts: the top level, ``plan`` and ``context`` --
    the three persisted models. It does NOT recurse further: none of those
    models declares a nested ``BaseModel`` field (``history`` is a list of
    string triples, everything else is scalar), so there is no deeper level
    at which pydantic could drop a key. Extend the loop if one is ever
    added, or the CLI's ignored-keys line will under-report."""

    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    ignored: list[str] = [k for k in data if k not in ProvisionJournal.model_fields]
    for section, model in (("plan", ProvisionPlan), ("context", ProvisionContext)):
        nested = data.get(section)
        if isinstance(nested, dict):
            ignored.extend(f"{section}.{k}" for k in nested if k not in model.model_fields)
    return sorted(ignored)


def write_journal(journal: ProvisionJournal) -> Path:
    """Atomically persist ``journal`` under its own context's state root.

    Uses temp-file + fsync + ``os.replace`` so a kill mid-write never yields a
    torn journal (see module docstring). Also -- see the module docstring's
    security note -- hardens the state root's own ACL (every call; cheap and
    self-healing) and never writes ``context.database_password`` to disk in
    plaintext (redacted in the serialized payload only; the in-memory
    ``journal`` object the caller holds is untouched).
    """

    state_root = Path(journal.context.state_root)
    state_root.mkdir(parents=True, exist_ok=True)
    _harden_state_root_acl(state_root)
    path = journal_path(state_root)
    payload_dict = journal.model_dump(mode="json")
    payload_dict["context"]["database_password"] = _REDACTED_DATABASE_PASSWORD_MARKER
    payload = json.dumps(payload_dict, indent=2, sort_keys=True)

    # Unique temp name so two racing writers cannot corrupt each other's temp.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)
    return path


def advance(
    journal: ProvisionJournal,
    phase: ProvisionPhase,
    detail: str,
    **updates: object,
) -> ProvisionJournal:
    """Return a new journal at ``phase`` with an appended history entry.

    Enforces the same state machine as
    :func:`civiccast.native.upgrade.journal.advance`: a forward move may only
    go to the immediately higher forward rank (no skipping a boundary), and
    the terminal state (``FAILED``) is reachable from any non-terminal phase.
    Attempting to advance FROM a terminal phase, or to skip a forward
    boundary, raises :class:`JournalError`.

    This function does NOT write; the caller persists via :func:`write_journal`
    after the corresponding real action succeeds, so the on-disk phase never
    runs ahead of the world.
    """

    current = journal.phase
    if current.is_terminal:
        raise JournalError(f"cannot advance from terminal phase {current.value!r}")

    if not phase.is_terminal and phase.rank != current.rank + 1:
        raise JournalError(
            f"illegal forward transition {current.value!r} -> {phase.value!r} "
            "(forward phases advance exactly one boundary)"
        )

    stamp = datetime.now(UTC).isoformat()
    history = [*journal.history, (phase.value, stamp, detail)]
    return journal.model_copy(update={"phase": phase, "history": history, **updates})


__all__ = [
    "JOURNAL_FILENAME",
    "JournalError",
    "advance",
    "ignored_journal_keys",
    "journal_path",
    "load_journal",
    "load_journal_with_ignored_keys",
    "write_journal",
]
