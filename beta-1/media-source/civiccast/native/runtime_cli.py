# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""``civiccast runtime`` CLI verbs -- thin; logic lives in runtime_guard.py
(the decision table) and win_probes.py (the real probes). This module's own
job is orchestrating D7/D8's journal-backed phases and printing reports.

Platform note: this module's own top-level imports are Linux-safe (it
imports ``civiccast.native.win_probes``, which is itself Linux-import-safe
per that module's docstring). The CLI COMMANDS carry no explicit platform
gate -- ``tests/native/test_runtime_cli.py`` deliberately has no "win" in
its name and runs everywhere, because every registry/wsl/mutex interaction
is reached through the module-level probe bindings below
(``read_selector``, ``probe_keeper``, ...), which tests monkeypatch. A real,
unmocked invocation on non-Windows will surface a plain ``ModuleNotFoundError``
from the lazy ``import winreg`` inside win_probes.py -- acceptable, since
this whole package is Windows-only functionality; the CI/test
platform-independence contract is what matters here, not non-Windows UX
polish.

Dual CLI registration (disclosed in evidence/DESIGN-NOTES.md): this module
is registered BOTH as a `civiccast runtime <verb>` sub-app (via
``civiccast/cli.py``'s ``app.add_typer(runtime_app)``) AND as its own
``civiccast-runtime`` console script (`[project.scripts]` in
pyproject.toml) -- satisfying spec D7/D8's literal `civiccast-runtime
cutover-to-native` invocation AND ADR-0005's umbrella-CLI convention.
``--civiccast-runtime-host`` (the Rust installer's keepalive flag) is an
unrelated legacy surface -- name-distinct from `civiccast-runtime` on
purpose, not a typo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import ValidationError

from civiccast.native.models import (
    CutoverJournal,
    CutoverPhaseRecord,
    GuardInputs,
    InterlockRead,
    MaintenanceRecord,
)
from civiccast.native.runtime_guard import RUN_KEY_PATH, RUNTIME_HOST_FLAG, WSL_DISTRO_NAME, decide
from civiccast.native.win_probes import (
    WSL_EXE,
    RuntimeOwnerMutex,
    detect_wsl_install,
    probe_indistro_services,
    probe_keeper,
    read_interlock,
    read_selector,
    release_interlock,
    scan_run_entries,
    take_interlock,
    write_selector,
)

VerifyFn = Callable[[], tuple[bool, str]]
InterlockReaderFn = Callable[[], InterlockRead]
InterlockTakerFn = Callable[[str], MaintenanceRecord]
InterlockReleaserFn = Callable[[], MaintenanceRecord]

_INTERLOCK_OPTION_HELP = (
    "Migration-owned interlock: verify (never take) a held record bound to this owner run id."
)
_INTERLOCK_GENERATION_OPTION_HELP = (
    "Migration-owned interlock: the generation the record must still match at every phase boundary."
)
_FORCE_NEW_OPTION_HELP = (
    "Explicitly discard an existing INCOMPLETE journal of the opposite direction and start fresh "
    "(CC-WS4-007) -- only after confirming the in-flight run is genuinely abandoned."
)

ROLLBACK_ACK = (
    "native-era rows and media do not flow back; recovery point is the pre-cutover migration backup"
)

runtime_app = typer.Typer(
    name="runtime", no_args_is_help=True, help="Dual-runtime exclusion guard (WS4)."
)

_JSON_OPTION = typer.Option("--json", help="Emit machine-readable JSON.")
_STATE_DIR_OPTION = typer.Option(
    "--state-dir",
    help="Override the journal/evidence directory (default: %ProgramData%\\CivicCast\\logs).",
)


def _default_state_dir() -> Path:
    program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    return Path(program_data) / "CivicCast" / "logs"


def _decode(data: bytes) -> str:
    if not data:
        return ""
    if b"\x00" in data[:64]:
        try:
            return data.decode("utf-16-le", errors="replace")
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# GuardInputs composition (used by status/probe)
# ---------------------------------------------------------------------------


def _compose_guard_inputs() -> GuardInputs:
    mutex = RuntimeOwnerMutex()
    a3 = mutex.acquire()
    mutex.release()
    return GuardInputs(
        selector=read_selector(),
        wsl_install_detected=detect_wsl_install(),
        a1=probe_keeper(),
        a2=probe_indistro_services(),
        a3=a3,
        interlock=read_interlock(),
    )


@runtime_app.command("status")
def runtime_status(json_output: Annotated[bool, _JSON_OPTION] = False) -> None:
    """Read selector + interlock + run all probes + decide; report."""

    inputs = _compose_guard_inputs()
    decision = decide(inputs)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "inputs": json.loads(inputs.model_dump_json()),
                    "decision": json.loads(decision.model_dump_json()),
                },
                indent=2,
            )
        )
    else:
        typer.echo(
            f"Selector: {inputs.selector.value} (ok={inputs.selector.ok}) -- {inputs.selector.detail}"
        )
        typer.echo(f"Interlock: {inputs.interlock.status} -- {inputs.interlock.detail}")
        typer.echo(
            f"A1 (keeper): live_process={inputs.a1.live_process} run_entry={inputs.a1.run_entry}"
        )
        typer.echo(f"A2 (in-distro service): {inputs.a2.status} -- {inputs.a2.detail}")
        typer.echo(f"A3 (mutex): {inputs.a3.status} -- {inputs.a3.detail}")
        typer.echo(f"WSL install detected: {inputs.wsl_install_detected}")
        typer.echo("")
        probe_note = f" (probe={decision.named_probe})" if decision.named_probe else ""
        typer.echo(f"Decision: {decision.action}{probe_note}")
        typer.echo(decision.message)
    if decision.action not in ("start", "start_degraded"):
        raise typer.Exit(code=1)


@runtime_app.command("probe")
def runtime_probe(json_output: Annotated[bool, _JSON_OPTION] = False) -> None:
    """Dump raw GuardInputs (diagnostics)."""

    inputs = _compose_guard_inputs()
    typer.echo(inputs.model_dump_json(indent=2))
    if not json_output:
        pass  # the structured dump IS the diagnostic report either way


# ---------------------------------------------------------------------------
# Journal helpers (shared by cutover and rollback)
# ---------------------------------------------------------------------------


def _journal_path(state_dir: Path) -> Path:
    return state_dir / "runtime-cutover-journal.json"


class _JournalCorruptError(Exception):
    """CC-WS4-007 fix (round 2, Major -- auditor panel): raised by
    ``_load_journal`` when the journal file EXISTS but cannot be parsed --
    distinct from "genuinely absent" (a missing file returns ``None``
    cleanly, the normal fresh-run case). The round-1 bug conflated these:
    ``_load_journal`` returned ``None`` for BOTH a missing file and an
    unreadable/invalid one, so both ``run_cutover``/``run_rollback`` would
    silently start a brand-new run and overwrite the only journal on top of
    a malformed one (the auditor's literal repro: placing ``{not-json`` at
    the canonical path)."""

    def __init__(self, path: Path, detail: str) -> None:
        self.path = path
        self.detail = detail
        super().__init__(detail)


def _load_journal(path: Path) -> CutoverJournal | None:
    if not path.exists():
        return None
    try:
        return CutoverJournal.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError) as exc:
        raise _JournalCorruptError(path, f"journal at {path} is unreadable/invalid: {exc}") from exc


def _preserve_corrupt_journal(path: Path) -> str:
    """CC-WS4-007: rename an unreadable/invalid journal file OUT OF THE WAY
    (never silently discarded/overwritten) so the operator can diagnose it
    later. Returns the archived filename (not the full path)."""

    n = 1
    while True:
        candidate = path.with_name(f"{path.name}.corrupt-{n}")
        if not candidate.exists():
            break
        n += 1
    path.rename(candidate)
    return candidate.name


def _save_journal(path: Path, journal: CutoverJournal) -> None:
    """CC-WS4-007 fix (round 2, Major): atomic durable write. Writes to a
    temp file IN THE SAME DIRECTORY (``os.replace`` is only atomic within
    one filesystem/volume), flushes + ``fsync``s it, then atomically
    renames it onto the canonical path -- a power-loss mid-write can never
    leave a truncated/torn journal at ``path``; a reader always observes
    either the complete prior content or the complete new content."""

    path.parent.mkdir(parents=True, exist_ok=True)
    data = journal.model_dump_json(indent=2)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        tmp_path.replace(path)
    except BaseException:
        with suppress(OSError):
            tmp_path.unlink()
        raise


def _load_or_start_journal(
    *,
    journal_path: Path,
    direction: Literal["cutover", "rollback"],
    run_id: str | None,
    force_new: bool,
) -> tuple[CutoverJournal | None, CutoverJournal | None]:
    """CC-WS4-007 fix (round 2, Major): the shared journal-acquisition
    contract for both ``run_cutover`` and ``run_rollback``. Returns
    ``(journal_to_use, refusal_journal)`` -- exactly one is non-``None``;
    the caller must return the refusal journal as-is (never write it back
    to ``journal_path``) when it is present.

    Two fail-closed guarantees:
    1. An unreadable/invalid journal is preserved (renamed to
       ``<path>.corrupt-N``) and this run REFUSES -- it never silently
       starts a fresh run on top of it.
    2. An existing journal of the OPPOSITE direction that is still
       INCOMPLETE (``ok`` is False -- a genuinely in-flight opposite-
       direction transaction) is never silently replaced; the operator
       must pass ``force_new=True`` to explicitly override. A COMPLETE
       opposite-direction journal (the NORMAL transition -- e.g. rollback
       after a successful cutover) is not blocked: that journal's own
       bracket is already closed, and CC-WS4-006 relies on carrying its
       ``removed_run_entry_exe_path`` forward into the fresh journal for
       THIS direction.
    """

    try:
        loaded = _load_journal(journal_path)
    except _JournalCorruptError as exc:
        archived_name = _preserve_corrupt_journal(journal_path)
        return None, CutoverJournal(
            v=1,
            run_id=run_id or uuid.uuid4().hex,
            direction=direction,
            phases=[],
            unloaded_profiles=[],
            errors=[
                f"existing journal was unreadable/invalid and has been preserved as {archived_name} for "
                f"diagnosis (fail-closed, never silently discarded/overwritten): {exc.detail}"
            ],
        )

    if loaded is not None and loaded.direction != direction:
        if not loaded.ok and not force_new:
            return None, CutoverJournal(
                v=1,
                run_id=run_id or uuid.uuid4().hex,
                direction=direction,
                phases=[],
                unloaded_profiles=[],
                errors=[
                    f"an existing INCOMPLETE {loaded.direction} journal (run_id={loaded.run_id}) is present; "
                    f"refusing to silently replace it with a new {direction} journal -- pass --force-new to "
                    "override (only after confirming the in-flight run is genuinely abandoned)."
                ],
            )
        return (
            CutoverJournal(
                v=1,
                run_id=run_id or uuid.uuid4().hex,
                direction=direction,
                phases=[],
                unloaded_profiles=[],
                errors=[],
                removed_run_entry_exe_path=loaded.removed_run_entry_exe_path,
            ),
            None,
        )

    if loaded is None:
        return (
            CutoverJournal(
                v=1,
                run_id=run_id or uuid.uuid4().hex,
                direction=direction,
                phases=[],
                unloaded_profiles=[],
                errors=[],
            ),
            None,
        )

    return loaded, None


def _find_phase(journal: CutoverJournal, phase: int) -> CutoverPhaseRecord | None:
    for record in journal.phases:
        if record.phase == phase:
            return record
    return None


def _upsert_phase(journal: CutoverJournal, record: CutoverPhaseRecord) -> None:
    journal.phases = sorted(
        [p for p in journal.phases if p.phase != record.phase] + [record], key=lambda p: p.phase
    )


def _clear_stale_errors(journal: CutoverJournal, journal_path: Path, prefix: str) -> None:
    """CC-WS4-009 fix (round 2b, verification defect): strip ``journal.errors``
    entries starting with ``prefix`` and persist -- the round-2 success-side
    counterpart ``_run_phase`` already has for its own `phase {N} failed:`
    prefix (see there), but which three round-2 error classes lacked:
    interlock bracket setup failures, per-label interlock bracket boundary
    failures, and the CC-WS4-006 rollback exe-path preflight failure.
    ``CutoverJournal.ok`` requires ``not self.errors`` -- a stale entry left
    by a prior failed attempt made ``journal.ok`` permanently False even
    after a fully successful resume, which never fires the OWNED-mode
    ``release()`` call (gated on ``journal.ok``) that frees the D7a
    maintenance freeze -- a self-sustaining deadlock. Called at each class's
    matching success point (never on failure -- over-clearing would let a
    genuinely still-broken bracket/preflight report success)."""

    filtered = [e for e in journal.errors if not e.startswith(prefix)]
    if len(filtered) != len(journal.errors):
        journal.errors = filtered
        _save_journal(journal_path, journal)


def _run_phase(
    journal: CutoverJournal,
    journal_path: Path,
    phase: int,
    name: str,
    postcondition: str,
    action: Callable[[], str],
    clock_fn: Callable[[], str],
    verify: VerifyFn | None = None,
    verify_after_fresh_action: bool = False,
) -> bool:
    """Run one journal phase, honoring F2's resume-time re-verify contract.

    A phase already recorded "done" is NOT trusted on faith: if a ``verify``
    callable was supplied, it is called to re-check the phase's
    machine-checkable postcondition for real.
    - verify PASSES -> skip (the original round-1 behavior), and
      ``verified_on_resume`` is recorded True on the existing record.
    - verify FAILS -> the phase falls through to being RE-EXECUTED below,
      exactly like a phase that was never "done" -- a stale "done" record
      must never survive a postcondition that no longer holds (e.g. the
      reviewer's exact scenario: cutover writes selector=native, journal
      marks phase 3 done, something external flips the selector back to
      "wsl" before the next resume -- that resume must re-run phase 3, not
      silently skip it).
    - No ``verify`` supplied at all -> unchanged pre-F2 behavior: skip
      without any re-check (only used where the brief did not ask for a
      postcondition check, or by tests exercising phases in isolation).

    CC-WS4-005 fix (round 2, Major): ``verify_after_fresh_action`` (opt-in,
    default False so every OTHER phase's existing behavior/tests are
    untouched) confirms the postcondition for real immediately after a
    FRESH action succeeds, before the phase is recorded done -- an action
    that raised no exception is not proof its postcondition actually holds
    (phase 2's exact hazard: a partial Run-entry removal scan could return
    without raising while still leaving a marker under a hive it missed).
    Wired for phase 2 in ``run_cutover`` below; left off elsewhere to avoid
    silently pulling every phase's REAL default verify (registry/wsl reads)
    into tests that intentionally do not override it.
    """

    existing = _find_phase(journal, phase)
    resume_verify_failure_detail: str | None = None
    if existing is not None and existing.status == "done":
        if verify is None:
            return True
        verify_ok, verify_detail = verify()
        if verify_ok:
            existing.verified_on_resume = True
            _save_journal(journal_path, journal)
            return True
        resume_verify_failure_detail = verify_detail

    started = clock_fn()
    try:
        detail = action()
        if resume_verify_failure_detail is not None:
            detail = (
                f"{detail} (RE-EXECUTED: resume-time verify failed: {resume_verify_failure_detail})"
            )
        if verify_after_fresh_action and verify is not None:
            fresh_verify_ok, fresh_verify_detail = verify()
            if not fresh_verify_ok:
                raise RuntimeError(
                    f"postcondition not confirmed after action: {fresh_verify_detail}"
                )
        _upsert_phase(
            journal,
            CutoverPhaseRecord(
                phase=phase,
                name=name,
                status="done",
                started_utc=started,
                finished_utc=clock_fn(),
                detail=detail,
                postcondition=postcondition,
                verified_on_resume=(False if resume_verify_failure_detail is not None else None),
            ),
        )
        # A retry that now succeeds must clear any STALE error this same
        # phase recorded on a prior failed attempt -- otherwise journal.ok
        # stays permanently False even after every phase reaches "done"
        # (found by test_run_cutover_phase2_failure_then_resume_does_not_redo_phase1).
        journal.errors = [e for e in journal.errors if not e.startswith(f"phase {phase} failed")]
        ok = True
    except Exception as exc:
        _upsert_phase(
            journal,
            CutoverPhaseRecord(
                phase=phase,
                name=name,
                status="failed",
                started_utc=started,
                finished_utc=clock_fn(),
                detail=str(exc),
                postcondition=postcondition,
                verified_on_resume=(False if resume_verify_failure_detail is not None else None),
            ),
        )
        journal.errors.append(f"phase {phase} failed: {exc}")
        ok = False
    _save_journal(journal_path, journal)
    return ok


def _write_evidence(
    state_dir: Path, journal: CutoverJournal, timestamp: str, probe_snapshot: GuardInputs | None
) -> None:
    """CC-WS4-007 fix (round 2, Major): the evidence JSON now includes the
    probe-result snapshot (``probe_snapshot`` -- ``None`` only when the
    caller passed no snapshot callable at all, never silently omitted) IN
    ADDITION to the journal's own run_id/direction/ALL committed phase
    outcomes -- ``_default_phase5_verify_for`` below validates this exact
    shape rather than trusting "the lexically latest parseable JSON" in the
    directory."""

    stamp = timestamp.replace(":", "").replace("-", "").replace(".", "").replace("+", "Z")
    state_dir.mkdir(parents=True, exist_ok=True)
    json_path = state_dir / f"runtime-cutover-evidence-{stamp}.json"
    md_path = state_dir / f"runtime-cutover-evidence-{stamp}.md"
    payload = json.loads(journal.model_dump_json())
    payload["probe_snapshot"] = (
        json.loads(probe_snapshot.model_dump_json()) if probe_snapshot is not None else None
    )
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# CivicCast runtime cutover/rollback evidence",
        "",
        f"run_id: {journal.run_id}",
        f"direction: {journal.direction}",
        f"generated: {timestamp}",
        f"probe_snapshot bound: {probe_snapshot is not None}",
        "",
        "## Phases",
        "",
    ]
    for phase in journal.phases:
        lines.append(f"- Phase {phase.phase} ({phase.name}): {phase.status} -- {phase.detail}")
    lines += [
        "",
        "## Unloaded profiles",
        "",
        "Resurrection of the Run entry on these profiles is closed by the WSL keeper patch "
        "(see wsl-keeper-patch/), not by hive surgery here -- these are recorded for evidence only.",
        "",
    ]
    lines += [f"- {sid}" for sid in journal.unloaded_profiles] or ["(none)"]
    if journal.errors:
        lines += ["", "## Errors", ""]
        lines += [f"- {err}" for err in journal.errors]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CC-WS4-003 (round 2, Critical): the D7a maintenance/freeze interlock
# bracket. `cutover-to-native` and `rollback-to-wsl` both mutate the
# selector -- both now run INSIDE the interlock rather than outside it.
#
# Two modes, chosen by whether `--interlock-owner`/`--interlock-generation`
# are supplied:
#   OWNED (default, no --interlock-owner): the command TAKES and HOLDS the
#     interlock itself for the whole transaction (fresh owner_run_id = the
#     journal's own run_id, a fresh generation via take_interlock's own
#     increment). Released on full success; on ANY failure it is LEFT HELD
#     and journaled, so a resume continues inside the SAME bracket -- a
#     half-done cutover stays frozen, which is the safe direction.
#   EXTERNAL (--interlock-owner + --interlock-generation both supplied,
#     the migration case -- migration takes the freeze FIRST per migration
#     D1): the command does NOT take its own interlock. It CONTINUOUSLY
#     re-verifies, before every phase and again immediately after the
#     selector-mutating phase, that a held record still exists bound to
#     that exact owner_run_id + generation. Free, released, unreadable,
#     wrong-owner, or generation-drift aborts BEFORE any further selector
#     mutation -- this command never releases an EXTERNAL-mode interlock
#     (the migration tooling that took it owns that decision).
#
# The bound owner_run_id/generation are recorded on the journal itself
# (CutoverJournal.interlock_owner_run_id/interlock_generation) so a resume
# knows which record it must keep re-verifying against, in either mode.
# ---------------------------------------------------------------------------


def _verify_interlock_bracket(
    *, journal: CutoverJournal, interlock_reader: InterlockReaderFn
) -> tuple[bool, str]:
    """Re-read the D7a interlock and confirm it is STILL held, bound to the
    exact owner_run_id + generation recorded on ``journal``. Used
    identically for both OWNED and EXTERNAL mode -- once bound, "is this
    still our bracket" is the same question either way. Never trusts a
    prior "held" observation; always re-reads."""

    if journal.interlock_owner_run_id is None or journal.interlock_generation is None:
        return False, "interlock bracket was never bound on this journal (internal error)"
    current = interlock_reader()
    if current.status == "unreadable":
        return False, f"interlock unreadable: {current.detail}"
    if current.status == "free":
        return False, f"interlock is free (released or never held): {current.detail}"
    record = current.record
    if record is None:
        return False, "interlock reports held but carries no record (internal error)"
    if record.owner_run_id != journal.interlock_owner_run_id:
        return (
            False,
            f"interlock owner mismatch: bound to {journal.interlock_owner_run_id!r}, "
            f"now held by {record.owner_run_id!r}",
        )
    if record.generation != journal.interlock_generation:
        return (
            False,
            f"interlock generation drift: bound to generation {journal.interlock_generation}, "
            f"now generation {record.generation}",
        )
    return True, f"interlock held by {record.owner_run_id} generation {record.generation}"


def _bind_interlock_bracket(
    *,
    journal: CutoverJournal,
    journal_path: Path,
    interlock_owner: str | None,
    interlock_generation: int | None,
    interlock_taker: InterlockTakerFn,
) -> str | None:
    """Bind ``journal``'s interlock fields for either mode. Returns an error
    string on failure (nothing further should run), ``None`` on success.
    Does NOT itself verify held-ness afterward -- the per-phase-boundary
    ``_verify_interlock_bracket`` call does that uniformly for every mode,
    including immediately after this binds a fresh OWNED take."""

    if interlock_owner is not None or interlock_generation is not None:
        if interlock_owner is None or interlock_generation is None:
            return "--interlock-owner and --interlock-generation must both be provided together"
        if journal.interlock_owner_run_id is None:
            journal.interlock_owner_run_id = interlock_owner
            journal.interlock_generation = interlock_generation
            _save_journal(journal_path, journal)
        elif (
            journal.interlock_owner_run_id != interlock_owner
            or journal.interlock_generation != interlock_generation
        ):
            return (
                "--interlock-owner/--interlock-generation do not match this journal's "
                f"already-bound values ({journal.interlock_owner_run_id!r}, {journal.interlock_generation!r})"
            )
        # CC-WS4-009 fix (round 2b): a bound-and-matching (or freshly bound)
        # EXTERNAL record is a successful bind -- clear any stale setup
        # error a prior failed bind attempt left on this journal.
        _clear_stale_errors(journal, journal_path, "interlock bracket setup failed:")
        return None

    # OWNED mode. A journal that is already bound AND not yet complete
    # (some phase failed or is still pending) is a prior OWNED attempt that
    # LEFT the interlock held on purpose (the safe direction) -- do NOT
    # re-take it; the per-phase-boundary verify below confirms it is still
    # ours. A journal that is already bound but ALREADY COMPLETE (ok=True
    # -- the prior transaction finished and released it) is a NEW
    # invocation (idempotent re-run, or a drift-repair re-verify) and must
    # open its OWN fresh bracket, same as a never-bound journal.
    if journal.interlock_owner_run_id is not None and not journal.ok:
        # CC-WS4-009 fix (round 2b): an already-bound bracket is usable as
        # is -- clear any stale setup error (defensive; a bind that failed
        # never sets interlock_owner_run_id in the first place, so this is
        # normally a no-op, but keeps the invariant "bound => no stale
        # setup error" true unconditionally).
        _clear_stale_errors(journal, journal_path, "interlock bracket setup failed:")
        return None
    try:
        record = interlock_taker(journal.run_id)
    except RuntimeError as exc:
        return f"could not take interlock: {exc}"
    journal.interlock_owner_run_id = record.owner_run_id
    journal.interlock_generation = record.generation
    _save_journal(journal_path, journal)
    _clear_stale_errors(journal, journal_path, "interlock bracket setup failed:")
    return None


# ---------------------------------------------------------------------------
# D7 cutover-to-native
# ---------------------------------------------------------------------------


def _default_phase1_stop_service() -> str:
    """F1 fix: ``detect_wsl_install()`` is a tri-state (bool | None) -- a
    bare ``if not detect_wsl_install():`` would treat an UNKNOWN install
    state (None, falsy in Python) exactly like a CONFIRMED absence, and
    record phase 1 as "done" with nothing actually verified stopped. An
    unknown state must FAIL the phase (journal shows "failed", a re-run
    retries) -- never a false "done"."""

    installed = detect_wsl_install()
    if installed is None:
        raise RuntimeError(
            "WSL install state unknown (detect_wsl_install could not determine an answer); "
            "cannot safely conclude there is nothing to stop"
        )
    if not installed:
        return "no distro registered"
    argv = [
        str(WSL_EXE),
        "-d",
        WSL_DISTRO_NAME,
        "--user",
        "root",
        "--exec",
        "systemctl",
        "disable",
        "--now",
        "civiccast*",
    ]
    result = subprocess.run(argv, capture_output=True, timeout=30)  # noqa: S603 - fixed argv (built above), no shell
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl disable --now civiccast* failed (exit {result.returncode}): {_decode(result.stderr).strip()}"
        )
    return "civiccast* disabled and stopped in-distro"


_ERROR_NO_MORE_ITEMS = 259


def _enumerate_unloaded_profiles() -> list[str]:
    """CC-WS4-005 fix (round 2, Major -- auditor panel): the SAME
    EnumKey-OSError-as-end-of-list conflation F3 already fixed in
    ``win_probes.scan_run_entries`` -- only ``winerror == 259``
    (``ERROR_NO_MORE_ITEMS``) is the real end-of-enumeration sentinel;
    every other ``EnumKey``/open failure now RAISES (fails phase 2)
    instead of silently returning an empty or partial list. A failed
    enumeration here must never let the journal/evidence claim a clean,
    trustworthy profile inventory that was not actually established."""

    import winreg

    profile_list_key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
    all_sids: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, profile_list_key) as key:
            index = 0
            while True:
                try:
                    all_sids.append(winreg.EnumKey(key, index))
                except OSError as exc:
                    winerror = getattr(exc, "winerror", None)
                    if winerror == _ERROR_NO_MORE_ITEMS:
                        break
                    raise RuntimeError(
                        f"ProfileList enumeration failed at index {index} (winerror={winerror}): {exc}"
                    ) from exc
                index += 1
    except OSError as exc:
        raise RuntimeError(f"could not open ProfileList key: {exc}") from exc

    loaded_sids: set[str] = set()
    try:
        with winreg.OpenKey(winreg.HKEY_USERS, "") as users_key:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(users_key, index)
                except OSError as exc:
                    winerror = getattr(exc, "winerror", None)
                    if winerror == _ERROR_NO_MORE_ITEMS:
                        break
                    raise RuntimeError(
                        f"HKEY_USERS enumeration failed at index {index} (winerror={winerror}): {exc}"
                    ) from exc
                index += 1
                if not name.endswith("_Classes"):
                    loaded_sids.add(name)
    except OSError as exc:
        raise RuntimeError(f"could not open HKEY_USERS root: {exc}") from exc

    return [sid for sid in all_sids if sid not in loaded_sids]


def _parse_exe_path_from_run_value(value: str) -> str | None:
    """CC-WS4-006: extract the executable path portion of a Run value like
    ``'"C:\\CivicCast\\civiccast.exe" --civiccast-runtime-host'`` -- strips
    the ``RUNTIME_HOST_FLAG`` suffix and any surrounding quotes."""

    without_flag = value.replace(RUNTIME_HOST_FLAG, "").strip()
    if without_flag.startswith('"') and without_flag.endswith('"') and len(without_flag) >= 2:
        return without_flag[1:-1] or None
    return without_flag or None


def _default_phase2_remove_run_entries() -> tuple[list[str], list[str], str | None]:
    """CC-WS4-005 fix (round 2, Major -- auditor panel): the SAME
    conflation as ``_enumerate_unloaded_profiles`` above -- only
    ``winerror == 259`` ends the HKEY_USERS hive scan; every other
    EnumKey/open/query/delete OSError now FAILS the phase (raises) with
    hive/index context, instead of a bare ``continue`` that let a
    genuinely partial scan report ``removed=[]`` as if it were a
    confirmed, trustworthy success. ``FileNotFoundError`` for a
    per-hive Run key/value that legitimately does not exist is still NOT
    an error (most hives never had this autostart entry) -- only distinct
    from that is failed.

    CC-WS4-006 fix (round 2, Major): ALSO captures and returns the exe path
    parsed out of the FIRST removed value's data (``None`` if nothing was
    removed) -- ``run_cutover`` binds this onto the journal
    (``CutoverJournal.removed_run_entry_exe_path``) so a LATER rollback can
    derive its required ``--exe-path`` from a prior cutover instead of
    requiring the operator to re-supply it.
    """

    import winreg

    removed: list[str] = []
    exe_path: str | None = None
    with winreg.OpenKey(winreg.HKEY_USERS, "") as root_key:
        index = 0
        while True:
            try:
                subkey_name = winreg.EnumKey(root_key, index)
            except OSError as exc:
                winerror = getattr(exc, "winerror", None)
                if winerror == _ERROR_NO_MORE_ITEMS:
                    break
                raise RuntimeError(
                    f"HKEY_USERS enumeration failed at index {index} (winerror={winerror}): {exc}"
                ) from exc
            index += 1
            if subkey_name.endswith("_Classes"):
                continue
            run_path = f"{subkey_name}\\{RUN_KEY_PATH}"
            try:
                with winreg.OpenKey(
                    winreg.HKEY_USERS, run_path, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
                ) as run_key:
                    try:
                        value, value_type = winreg.QueryValueEx(run_key, "CivicCast Autostart")
                    except FileNotFoundError:
                        # This hive's Run key exists but has no CivicCast
                        # Autostart value -- legitimate, not an error.
                        continue
                    if value_type == winreg.REG_SZ and RUNTIME_HOST_FLAG in value:
                        winreg.DeleteValue(run_key, "CivicCast Autostart")
                        removed.append(subkey_name)
                        if exe_path is None:
                            exe_path = _parse_exe_path_from_run_value(value)
            except FileNotFoundError:
                # This hive has no Run key at all -- legitimate, not an error.
                continue
            except OSError as exc:
                raise RuntimeError(
                    f"hive {subkey_name!r} Run-entry access/delete failed "
                    f"(winerror={getattr(exc, 'winerror', None)}): {exc}"
                ) from exc
    return removed, _enumerate_unloaded_profiles(), exe_path


def _default_phase3_write_native() -> None:
    write_selector("native")


def _default_phase4_record_retained() -> str:
    installed = detect_wsl_install()
    return (
        f"CivicCast WSL distro {WSL_DISTRO_NAME} retained as rollback media (never unregistered); "
        f"currently registered={installed}"
    )


# ---------------------------------------------------------------------------
# F2: default `verify` callables -- the machine-checkable re-check that
# guards a "done" cutover phase from being skipped on faith at resume time.
# Each mirrors the phase's own postcondition string. An UNKNOWN probe state
# is always treated as verify-FAILS (never verify-passes): a phase whose
# postcondition cannot be confirmed is not safe to skip.
# ---------------------------------------------------------------------------


def _default_phase1_verify() -> tuple[bool, str]:
    """p1 postcondition: distro absent OR in-distro civiccast* inactive."""

    installed = detect_wsl_install()
    if installed is None:
        return False, "WSL install state unknown; cannot confirm nothing is running in-distro"
    if installed is False:
        return True, "no CivicCast distro registered"
    a2 = probe_indistro_services()
    if a2.status == "negative":
        return True, f"civiccast* confirmed inactive: {a2.detail}"
    return False, f"civiccast* not confirmed inactive: {a2.detail}"


def _default_phase2_verify() -> tuple[bool, str]:
    """p2 postcondition: no loaded-hive Run entry carries the runtime-host
    flag."""

    status, detail = scan_run_entries()
    return status == "negative", detail


def _default_phase3_verify() -> tuple[bool, str]:
    """p3 postcondition: selector reads exactly "native"."""

    result = read_selector()
    if result.ok and result.value == "native":
        return True, f"ActiveRuntime confirmed native: {result.detail}"
    return False, f"ActiveRuntime is not confirmed native: {result.detail}"


def _default_phase4_verify() -> tuple[bool, str]:
    """p4 postcondition: the distro-retained note is a RECORDED assertion
    (this phase takes no destructive action, so there is nothing further to
    independently re-check) -- trivially true once recorded, per the
    brief."""

    return True, "distro-retained note is a recorded assertion, not independently re-checked"


_EVIDENCE_REQUIRED_KEYS = frozenset(
    {"v", "run_id", "direction", "phases", "unloaded_profiles", "errors", "probe_snapshot"}
)


def _default_phase5_verify_for(state_dir: Path, journal: CutoverJournal) -> VerifyFn:
    """p5 postcondition (CC-WS4-007 fix, round 2, Major): the evidence bytes
    are VALIDATED, not just "the lexically latest parseable JSON" in the
    directory (the round-1 bug: a stale, unrelated, or wrong-run file there
    would satisfy resume). A candidate file only counts when it: parses as
    a JSON object; carries every required top-level key
    (``_EVIDENCE_REQUIRED_KEYS``, including ``probe_snapshot``); has
    ``run_id``/``direction`` matching THIS journal exactly; and whose
    ``phases`` array's phase numbers are a superset of every phase number
    already committed on ``journal`` (every committed outcome is present).
    Bound to ``state_dir``/``journal`` via closure."""

    def _verify() -> tuple[bool, str]:
        evidence_files = sorted(state_dir.glob("runtime-cutover-evidence-*.json"))
        journal_phase_numbers = {p.phase for p in journal.phases}
        matching: list[str] = []
        for candidate in evidence_files:
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if not _EVIDENCE_REQUIRED_KEYS.issubset(payload.keys()):
                continue
            if payload.get("run_id") != journal.run_id:
                continue
            if payload.get("direction") != journal.direction:
                continue
            evidence_phases = payload.get("phases")
            if not isinstance(evidence_phases, list):
                continue
            evidence_phase_numbers = {
                p.get("phase") for p in evidence_phases if isinstance(p, dict)
            }
            if not journal_phase_numbers.issubset(evidence_phase_numbers):
                continue
            matching.append(candidate.name)
        if not matching:
            return False, (
                f"no evidence file under {state_dir} matches run_id={journal.run_id!r} "
                f"direction={journal.direction!r} with the full committed phase set and required schema "
                f"({sorted(_EVIDENCE_REQUIRED_KEYS)})"
            )
        return (
            True,
            f"evidence file {matching[-1]} matches run_id/direction/phase-set and required schema",
        )

    return _verify


def run_cutover(
    *,
    state_dir: Path,
    run_id: str | None = None,
    force_new: bool = False,
    interlock_owner: str | None = None,
    interlock_generation: int | None = None,
    interlock_reader: InterlockReaderFn | None = None,
    interlock_taker: InterlockTakerFn | None = None,
    interlock_releaser: InterlockReleaserFn | None = None,
    probe_snapshot: Callable[[], GuardInputs] | None = None,
    phase1_stop_service: Callable[[], str] | None = None,
    phase1_verify: VerifyFn | None = None,
    phase2_remove_run_entries: Callable[[], tuple[list[str], list[str], str | None]] | None = None,
    phase2_verify: VerifyFn | None = None,
    phase3_write_native: Callable[[], None] | None = None,
    phase3_verify: VerifyFn | None = None,
    phase4_record_retained: Callable[[], str] | None = None,
    phase4_verify: VerifyFn | None = None,
    phase5_verify: VerifyFn | None = None,
    clock: Callable[[], str] | None = None,
) -> CutoverJournal:
    """D7: journal-backed, idempotent, resumable cutover phases.

    CC-WS4-003 fix (round 2, Critical): cutover now runs INSIDE the D7a
    transfer interlock rather than outside it. Default (OWNED mode, no
    ``interlock_owner``): this command takes and holds the interlock itself
    for the whole transaction, releasing it only on full success; any
    failure leaves it held so a resume continues inside the same bracket.
    Migration case (EXTERNAL mode, both ``interlock_owner`` and
    ``interlock_generation`` supplied): this command never takes its own --
    it continuously re-verifies the caller-owned held record before every
    phase and again immediately after phase 3's selector write. See the
    module-level comment above ``_verify_interlock_bracket`` for the full
    contract, and ``evidence/DESIGN-NOTES.md`` for the disclosed decision.

    F2 fix: every phase now carries a ``verify`` callable (defaulting to the
    real ``_default_phaseN_verify`` functions above) -- a "done" phase is
    re-verified for real on resume, not skipped on faith. See
    ``_run_phase``'s docstring for the skip/re-execute contract.
    """

    clock_fn = clock or _utc_now_iso
    journal_path = _journal_path(state_dir)
    journal, refusal = _load_or_start_journal(
        journal_path=journal_path, direction="cutover", run_id=run_id, force_new=force_new
    )
    if refusal is not None:
        return refusal
    assert (
        journal is not None
    )  # narrows for type-checking; _load_or_start_journal's contract guarantees this

    interlock_reader_fn = interlock_reader or read_interlock
    interlock_taker_fn = interlock_taker or take_interlock
    interlock_releaser_fn = interlock_releaser or release_interlock

    bind_error = _bind_interlock_bracket(
        journal=journal,
        journal_path=journal_path,
        interlock_owner=interlock_owner,
        interlock_generation=interlock_generation,
        interlock_taker=interlock_taker_fn,
    )
    if bind_error is not None:
        journal.errors.append(f"interlock bracket setup failed: {bind_error}")
        _save_journal(journal_path, journal)
        return journal

    def _bracket_ok(before: str) -> bool:
        ok, detail = _verify_interlock_bracket(
            journal=journal, interlock_reader=interlock_reader_fn
        )
        if not ok:
            journal.errors.append(f"interlock bracket failed before {before}: {detail}")
            _save_journal(journal_path, journal)
        else:
            # CC-WS4-009 fix (round 2b): a genuine re-verify success AT THIS
            # EXACT boundary label clears only that label's stale failure --
            # per-label, so a success at one boundary never clears a
            # failure recorded at a different one.
            _clear_stale_errors(journal, journal_path, f"interlock bracket failed before {before}:")
        return ok

    if not _bracket_ok("phase 1"):
        return journal
    if not _run_phase(
        journal,
        journal_path,
        1,
        "in-distro disable+stop",
        "civiccast* disabled+stopped, or no distro registered",
        phase1_stop_service or _default_phase1_stop_service,
        clock_fn,
        verify=phase1_verify or _default_phase1_verify,
    ):
        return journal

    def _phase2_action() -> str:
        removed_hives, unloaded, exe_path = (
            phase2_remove_run_entries or _default_phase2_remove_run_entries
        )()
        journal.unloaded_profiles = unloaded
        if exe_path:
            # CC-WS4-006: bind the removed value's exe path onto the
            # journal -- a later rollback derives --exe-path from this.
            journal.removed_run_entry_exe_path = exe_path
        return (
            f"removed keeper Run entry from hive(s): {', '.join(removed_hives) or '(none found)'}; "
            f"unloaded profiles enumerated: {len(unloaded)}"
        )

    if not _bracket_ok("phase 2"):
        return journal
    if not _run_phase(
        journal,
        journal_path,
        2,
        "remove keeper Run entries",
        "no loaded-hive Run entry carries the runtime-host flag",
        _phase2_action,
        clock_fn,
        verify=phase2_verify or _default_phase2_verify,
        verify_after_fresh_action=True,
    ):
        return journal

    def _phase3_action() -> str:
        (phase3_write_native or _default_phase3_write_native)()
        return "ActiveRuntime written as native"

    if not _bracket_ok("phase 3 (selector write)"):
        return journal
    if not _run_phase(
        journal,
        journal_path,
        3,
        "selector := native",
        "ActiveRuntime == native",
        _phase3_action,
        clock_fn,
        verify=phase3_verify or _default_phase3_verify,
    ):
        return journal

    # Re-verify immediately AFTER the selector mutation -- this doubles as
    # the "before phase 4" boundary check since phase 4 immediately follows
    # with no intervening step.
    if not _bracket_ok("phase 4 (post-selector-mutation re-verify)"):
        return journal
    if not _run_phase(
        journal,
        journal_path,
        4,
        "record distro retained as rollback media",
        "distro not unregistered",
        phase4_record_retained or _default_phase4_record_retained,
        clock_fn,
        verify=phase4_verify or _default_phase4_verify,
    ):
        return journal

    def _phase5_action() -> str:
        snapshot = (probe_snapshot or _compose_guard_inputs)()
        _write_evidence(state_dir, journal, clock_fn(), snapshot)
        return "evidence written (probe snapshot bound)"

    if not _bracket_ok("phase 5"):
        return journal
    _run_phase(
        journal,
        journal_path,
        5,
        "write evidence file",
        "evidence file present",
        _phase5_action,
        clock_fn,
        verify=phase5_verify or _default_phase5_verify_for(state_dir, journal),
    )

    # OWNED mode only: release on full success. EXTERNAL mode never
    # releases -- the migration tooling that took it owns that decision.
    if journal.ok and interlock_owner is None:
        try:
            interlock_releaser_fn()
        except RuntimeError as exc:
            journal.errors.append(f"cutover succeeded but interlock release failed: {exc}")
            _save_journal(journal_path, journal)
    return journal


@runtime_app.command("cutover-to-native")
def cutover_to_native_command(
    state_dir: Annotated[Path | None, _STATE_DIR_OPTION] = None,
    interlock_owner: Annotated[
        str | None, typer.Option("--interlock-owner", help=_INTERLOCK_OPTION_HELP)
    ] = None,
    interlock_generation: Annotated[
        int | None, typer.Option("--interlock-generation", help=_INTERLOCK_GENERATION_OPTION_HELP)
    ] = None,
    force_new: Annotated[bool, typer.Option("--force-new", help=_FORCE_NEW_OPTION_HELP)] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    """D7: cut over from WSL to native. Journal-backed; safe to re-run.

    Runs INSIDE the D7a transfer interlock (CC-WS4-003): by default this
    command takes and holds the interlock itself for the whole transaction.
    Pass ``--interlock-owner``/``--interlock-generation`` when a migration
    has already taken the freeze -- this command then only verifies (never
    takes) that held record, continuously, before every phase.

    CC-WS4-007: an unreadable/invalid existing journal is preserved
    (renamed ``<journal>.corrupt-N``) and refuses rather than being
    silently overwritten. An existing INCOMPLETE rollback journal also
    refuses (pass ``--force-new`` to override) -- a COMPLETE one does not
    (the normal cutover-after-rollback transition).
    """

    resolved_state_dir = state_dir or _default_state_dir()
    journal = run_cutover(
        state_dir=resolved_state_dir,
        interlock_owner=interlock_owner,
        interlock_generation=interlock_generation,
        force_new=force_new,
    )
    _print_journal(journal, json_output)
    if not journal.ok:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# D8 rollback-to-wsl
# ---------------------------------------------------------------------------


def _default_phase2_write_wsl() -> None:
    write_selector("wsl")


def _default_phase3_reenable_service() -> str:
    """F1 fix: same tri-state handling as
    ``_default_phase1_stop_service`` -- an UNKNOWN install state (None) must
    raise with its own distinct message, not be silently folded into the
    "no distro registered" (confirmed False) error path."""

    installed = detect_wsl_install()
    if installed is None:
        raise RuntimeError(
            "WSL install state unknown (detect_wsl_install could not determine an answer); "
            f"cannot confirm the CivicCast distro ({WSL_DISTRO_NAME}) exists to roll back to"
        )
    if not installed:
        raise RuntimeError(
            f"no CivicCast distro ({WSL_DISTRO_NAME}) registered -- nothing to roll back to"
        )
    argv = [
        str(WSL_EXE),
        "-d",
        WSL_DISTRO_NAME,
        "--user",
        "root",
        "--exec",
        "systemctl",
        "enable",
        "--now",
        "civiccast*",
    ]
    result = subprocess.run(argv, capture_output=True, timeout=30)  # noqa: S603 - fixed argv (built above), no shell
    if result.returncode != 0:
        raise RuntimeError(
            f"systemctl enable --now civiccast* failed (exit {result.returncode}): {_decode(result.stderr).strip()}"
        )
    return "civiccast* re-enabled in-distro"


def _default_phase4_restore_run_entry(
    exe_path: str | None, *, root: int | None = None, key_path: str = RUN_KEY_PATH
) -> str:
    """CC-WS4-006 fix (round 2, Major): ``exe_path`` absent is no longer a
    normal "NOT restored" detail that lets the phase record done -- it now
    RAISES (run_rollback's preflight below is the real gate that should
    have already caught this before ANY phase ran; this is a defensive
    backstop, never a silent success). After writing the Run value, the
    EXACT value data is read back immediately and compared byte-for-byte
    before this phase can be considered done -- a write that silently
    failed or was redirected must not be trusted on faith.

    ``root``/``key_path`` are injectable (house pattern, mirrors
    ``win_probes.read_selector`` et al.) so tests can target a scoped HKCU
    test subkey instead of the real production
    ``Software\\Microsoft\\Windows\\CurrentVersion\\Run`` key -- production
    callers never pass them (default HKCU/RUN_KEY_PATH)."""

    import winreg

    if not exe_path:
        raise RuntimeError(
            "no exe path available for Run entry restoration "
            "(no --exe-path provided and no recorded exe path in the journal; "
            "run_rollback's preflight should have refused before this phase ever ran)"
        )
    resolved_root = winreg.HKEY_CURRENT_USER if root is None else root
    value_data = f'"{exe_path}" {RUNTIME_HOST_FLAG}'
    with winreg.CreateKeyEx(
        resolved_root, key_path, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
    ) as key:
        winreg.SetValueEx(key, "CivicCast Autostart", 0, winreg.REG_SZ, value_data)
        read_back_value, read_back_type = winreg.QueryValueEx(key, "CivicCast Autostart")
        if read_back_type != winreg.REG_SZ or read_back_value != value_data:
            raise RuntimeError(
                f"Run entry write did not read back correctly: wrote {value_data!r}, "
                f"read back {read_back_value!r} (type={read_back_type})"
            )
    return f"Run entry restored for the invoking user, pointing at {exe_path} (read-back verified)"


# ---------------------------------------------------------------------------
# F2: default `verify` callables for rollback -- D8 mirror of the cutover
# verify functions above.
# ---------------------------------------------------------------------------


def _default_rollback_phase1_verify() -> tuple[bool, str]:
    """p1' postcondition: no native transmission children running -- ws5's
    supervisor has not landed, so this is trivially true, same as the
    action itself."""

    return True, "no native supervisor children to stop (ws5 not landed)"


def _default_rollback_phase2_verify() -> tuple[bool, str]:
    """p2' postcondition: selector reads exactly "wsl"."""

    result = read_selector()
    if result.ok and result.value == "wsl":
        return True, f"ActiveRuntime confirmed wsl: {result.detail}"
    return False, f"ActiveRuntime is not confirmed wsl: {result.detail}"


def _default_rollback_phase3_verify() -> tuple[bool, str]:
    """p3' postcondition: civiccast* active in-distro."""

    a2 = probe_indistro_services()
    if a2.status == "positive":
        return True, f"civiccast* confirmed active: {a2.detail}"
    return False, f"civiccast* not confirmed active: {a2.detail}"


def _default_rollback_phase4_verify() -> tuple[bool, str]:
    """p4' postcondition: HKCU Run entry present for the invoking user,
    carrying the runtime-host flag."""

    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            value, value_type = winreg.QueryValueEx(key, "CivicCast Autostart")
    except OSError as exc:
        return False, f"HKCU Run entry not present: {exc}"
    if value_type == winreg.REG_SZ and RUNTIME_HOST_FLAG in value:
        return True, f"HKCU Run entry present and carries {RUNTIME_HOST_FLAG}: {value!r}"
    return False, f"HKCU Run entry present but does not carry {RUNTIME_HOST_FLAG}: {value!r}"


def _preflight_rollback_exe_path(
    *, exe_path: str | None, journal: CutoverJournal, journal_path: Path
) -> tuple[str | None, str]:
    """CC-WS4-006 fix (round 2, Major): preflight a valid, EXISTING
    executable path BEFORE any rollback mutation. Prefers the path already
    bound on ``journal`` (carried forward from a prior cutover's phase 2
    removal, or bound by an earlier preflight on THIS same journal) over
    the caller's explicit ``exe_path``; either way the path must exist as
    a real file on disk. Returns ``(resolved_path, detail)`` on success or
    ``(None, error_detail)`` on failure -- the caller must not proceed to
    any phase when the first element is ``None``.

    CC-WS4-009 fix (round 2b): a successful preflight clears any stale
    ``rollback preflight failed:`` error a prior failed attempt left on
    ``journal`` -- see ``_clear_stale_errors``."""

    candidate = journal.removed_run_entry_exe_path or exe_path
    if not candidate:
        return None, (
            "no --exe-path provided and no exe path recorded by a prior cutover's journal "
            "or a previous rollback preflight on this journal"
        )
    if not Path(candidate).is_file():
        return None, f"exe path {candidate!r} does not exist on disk"
    _clear_stale_errors(journal, journal_path, "rollback preflight failed:")
    return candidate, f"resolved exe path: {candidate}"


def run_rollback(
    *,
    state_dir: Path,
    exe_path: str | None = None,
    run_id: str | None = None,
    force_new: bool = False,
    interlock_owner: str | None = None,
    interlock_generation: int | None = None,
    interlock_reader: InterlockReaderFn | None = None,
    interlock_taker: InterlockTakerFn | None = None,
    interlock_releaser: InterlockReleaserFn | None = None,
    phase1_verify: VerifyFn | None = None,
    phase2_write_wsl: Callable[[], None] | None = None,
    phase2_verify: VerifyFn | None = None,
    phase3_reenable_service: Callable[[], str] | None = None,
    phase3_verify: VerifyFn | None = None,
    phase4_restore_run_entry: Callable[[str | None], str] | None = None,
    phase4_verify: VerifyFn | None = None,
    clock: Callable[[], str] | None = None,
) -> CutoverJournal:
    """D8: mirror of D7. Phase 1 is a placeholder (ws5's supervisor has not
    landed yet, so there are no native children to stop). Unlike cutover's
    phase 1, an absent distro during phase 3's re-enable IS an error --
    there is nothing to roll back to.

    CC-WS4-003 fix (round 2, Critical): the same D7a interlock-bracket
    treatment as ``run_cutover`` -- rollback also mutates the selector
    (phase 2). See ``run_cutover``'s docstring and the module-level comment
    above ``_verify_interlock_bracket``.

    F2 fix: same resume-time re-verify contract as ``run_cutover`` -- see
    ``_run_phase``'s docstring.
    """

    clock_fn = clock or _utc_now_iso
    journal_path = _journal_path(state_dir)
    # CC-WS4-006 note: _load_or_start_journal already carries
    # removed_run_entry_exe_path forward from an opposite-direction
    # journal (a prior CUTOVER's phase 2 removal) into the fresh journal
    # for THIS direction -- see its docstring.
    journal, refusal = _load_or_start_journal(
        journal_path=journal_path, direction="rollback", run_id=run_id, force_new=force_new
    )
    if refusal is not None:
        return refusal
    assert (
        journal is not None
    )  # narrows for type-checking; _load_or_start_journal's contract guarantees this

    interlock_reader_fn = interlock_reader or read_interlock
    interlock_taker_fn = interlock_taker or take_interlock
    interlock_releaser_fn = interlock_releaser or release_interlock

    bind_error = _bind_interlock_bracket(
        journal=journal,
        journal_path=journal_path,
        interlock_owner=interlock_owner,
        interlock_generation=interlock_generation,
        interlock_taker=interlock_taker_fn,
    )
    if bind_error is not None:
        journal.errors.append(f"interlock bracket setup failed: {bind_error}")
        _save_journal(journal_path, journal)
        return journal

    def _bracket_ok(before: str) -> bool:
        ok, detail = _verify_interlock_bracket(
            journal=journal, interlock_reader=interlock_reader_fn
        )
        if not ok:
            journal.errors.append(f"interlock bracket failed before {before}: {detail}")
            _save_journal(journal_path, journal)
        else:
            # CC-WS4-009 fix (round 2b): a genuine re-verify success AT THIS
            # EXACT boundary label clears only that label's stale failure --
            # per-label, so a success at one boundary never clears a
            # failure recorded at a different one.
            _clear_stale_errors(journal, journal_path, f"interlock bracket failed before {before}:")
        return ok

    # D7a's interlock is checked FIRST, same precedence as the rest of the
    # guard (decide()'s own step 1) -- ahead of the CC-WS4-006 exe-path
    # preflight below.
    if not _bracket_ok("phase 1"):
        return journal
    if not _run_phase(
        journal,
        journal_path,
        1,
        "stop native supervisor children",
        "no native transmission children running",
        lambda: "no native supervisor children to stop (ws5 not landed)",
        clock_fn,
        verify=phase1_verify or _default_rollback_phase1_verify,
    ):
        return journal

    # CC-WS4-006: preflight the exe path BEFORE any rollback MUTATION --
    # phase 1 above is a harmless placeholder (ws5's supervisor has not
    # landed), so this still runs before phase 2, the first real mutation
    # (the selector write). A resolved path is bound onto the journal so a
    # later resume does not need --exe-path re-supplied.
    resolved_exe_path, exe_path_detail = _preflight_rollback_exe_path(
        exe_path=exe_path, journal=journal, journal_path=journal_path
    )
    if resolved_exe_path is None:
        journal.errors.append(f"rollback preflight failed: {exe_path_detail}")
        _save_journal(journal_path, journal)
        return journal
    if journal.removed_run_entry_exe_path != resolved_exe_path:
        journal.removed_run_entry_exe_path = resolved_exe_path
        _save_journal(journal_path, journal)

    def _phase2_action() -> str:
        (phase2_write_wsl or _default_phase2_write_wsl)()
        return "ActiveRuntime written as wsl"

    if not _bracket_ok("phase 2 (selector write)"):
        return journal
    if not _run_phase(
        journal,
        journal_path,
        2,
        "selector := wsl",
        "ActiveRuntime == wsl",
        _phase2_action,
        clock_fn,
        verify=phase2_verify or _default_rollback_phase2_verify,
    ):
        return journal

    # Re-verify immediately AFTER the selector mutation -- doubles as the
    # "before phase 3" boundary check.
    if not _bracket_ok("phase 3 (post-selector-mutation re-verify)"):
        return journal
    if not _run_phase(
        journal,
        journal_path,
        3,
        "re-enable in-distro services",
        "civiccast* active in-distro",
        phase3_reenable_service or _default_phase3_reenable_service,
        clock_fn,
        verify=phase3_verify or _default_rollback_phase3_verify,
    ):
        return journal

    def _phase4_action() -> str:
        return (phase4_restore_run_entry or _default_phase4_restore_run_entry)(resolved_exe_path)

    if not _bracket_ok("phase 4"):
        return journal
    _run_phase(
        journal,
        journal_path,
        4,
        "restore keeper Run entry for invoking user",
        "HKCU Run entry present for the invoking user",
        _phase4_action,
        clock_fn,
        verify=phase4_verify or _default_rollback_phase4_verify,
    )

    if journal.ok and interlock_owner is None:
        try:
            interlock_releaser_fn()
        except RuntimeError as exc:
            journal.errors.append(f"rollback succeeded but interlock release failed: {exc}")
            _save_journal(journal_path, journal)
    return journal


@runtime_app.command("rollback-to-wsl")
def rollback_to_wsl_command(
    ack: Annotated[
        str | None, typer.Option("--ack", help="Must exactly equal the printed boundary statement.")
    ] = None,
    exe_path: Annotated[
        str | None,
        typer.Option("--exe-path", help="Installed exe path for the restored Run entry."),
    ] = None,
    state_dir: Annotated[Path | None, _STATE_DIR_OPTION] = None,
    interlock_owner: Annotated[
        str | None, typer.Option("--interlock-owner", help=_INTERLOCK_OPTION_HELP)
    ] = None,
    interlock_generation: Annotated[
        int | None, typer.Option("--interlock-generation", help=_INTERLOCK_GENERATION_OPTION_HELP)
    ] = None,
    force_new: Annotated[bool, typer.Option("--force-new", help=_FORCE_NEW_OPTION_HELP)] = False,
    json_output: Annotated[bool, _JSON_OPTION] = False,
) -> None:
    """D8: roll back from native to WSL. Refuses without --ack. Runs INSIDE
    the D7a transfer interlock (CC-WS4-003), same treatment as
    cutover-to-native -- it also mutates the selector.

    CC-WS4-007: an unreadable/invalid existing journal is preserved
    (renamed ``<journal>.corrupt-N``) and refuses rather than being
    silently overwritten. An existing INCOMPLETE cutover journal also
    refuses (pass ``--force-new`` to override).
    """

    typer.echo(ROLLBACK_ACK)
    if ack != ROLLBACK_ACK:
        typer.echo("Refusing: --ack must exactly equal the boundary statement printed above.")
        raise typer.Exit(code=1)

    resolved_state_dir = state_dir or _default_state_dir()
    journal = run_rollback(
        state_dir=resolved_state_dir,
        exe_path=exe_path,
        interlock_owner=interlock_owner,
        interlock_generation=interlock_generation,
        force_new=force_new,
    )
    _print_journal(journal, json_output)
    if not journal.ok:
        raise typer.Exit(code=1)


def _print_journal(journal: CutoverJournal, json_output: bool) -> None:
    if json_output:
        typer.echo(journal.model_dump_json(indent=2))
        return
    typer.echo(f"{journal.direction} run_id={journal.run_id}: {'OK' if journal.ok else 'FAILED'}")
    for phase in journal.phases:
        typer.echo(f"  Phase {phase.phase} ({phase.name}): {phase.status} -- {phase.detail}")
    if journal.unloaded_profiles:
        typer.echo(f"  Unloaded profiles enumerated: {len(journal.unloaded_profiles)}")
    for error in journal.errors:
        typer.echo(f"  ERROR: {error}")


def main_entrypoint() -> None:  # pragma: no cover - thin shim
    """Console script entry point used by pyproject.toml's [project.scripts]
    `civiccast-runtime` -- satisfies the spec's literal binary name alongside
    the `civiccast runtime` sub-app registration in civiccast/cli.py."""

    try:
        runtime_app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main_entrypoint()


__all__ = [
    "ROLLBACK_ACK",
    "cutover_to_native_command",
    "rollback_to_wsl_command",
    "run_cutover",
    "run_rollback",
    "runtime_app",
    "runtime_probe",
    "runtime_status",
]
