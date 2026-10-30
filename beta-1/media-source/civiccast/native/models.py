# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Typed shapes for the dual-runtime exclusion guard (slice:ws4-dual-runtime-guard).

These types are the seam between the pure decision table in
``civiccast.native.runtime_guard`` and the real Windows probes in
``civiccast.native.win_probes``: every probe result and every guard decision
is a pydantic model with ``extra="forbid"`` (house pattern) so a malformed or
schema-drifted value fails loudly at parse time rather than being silently
tolerated. Derived pass/fail is always a ``@property``, never a manually set
boolean (``CutoverJournal.ok``).

Honest boundary: these are shapes only. They encode no I/O and no policy --
see ``runtime_guard.decide`` for the D3 decision table and ``win_probes`` for
what each probe actually reads.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Selector = Literal["native", "wsl", "absent"]
"""D1's authoritative selector value. "absent" is a VALUE (key/value missing
is a readable absence), distinct from an unreadable read (see SelectorRead)."""

ProbeStatus = Literal["negative", "positive", "error", "unreadable"]
"""Shared vocabulary for A1/A2 sub-signals. A1 uses negative/positive/error
(no distro-registration ambiguity on the Windows side); A2 uses
negative/positive/unreadable (a WSL command timeout is classified unreadable,
not error -- see win_probes.probe_indistro_services)."""

MutexStatus = Literal["acquired", "acquired_abandoned", "denied", "error"]
"""A3's four outcomes for Global\\CivicCastRuntimeOwner acquisition."""

InterlockStatus = Literal["free", "held", "unreadable"]
"""D7a maintenance/freeze interlock read outcome."""

GuardAction = Literal[
    "start",
    "start_degraded",
    "refuse",
    "blocked_probe_unavailable",
    "never_start",
    "refuse_instruct",
]
"""Every action decide() can return -- see runtime_guard.decide's docstring
for the full D3 precedence table this vocabulary encodes."""


class SelectorRead(BaseModel):
    """A read of HKLM\\SOFTWARE\\CivicCast\\ActiveRuntime.

    ``ok=False`` is UNREADABLE: the key/value exists but could not be
    interpreted (wrong registry type, or a string that is not exactly
    "native"/"wsl"/"absent" -- case and type mismatches fail closed rather
    than being coerced). ``ok=True, value="absent"`` is the readable-absence
    case (D1): the value is legitimately missing, which is not a failure.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    value: Selector | None
    detail: str


class A1Result(BaseModel):
    """A1: keeper-activity probe -- two independent sub-signals.

    ``live_process``: a live ``wsl.exe`` keeper process or a process carrying
    ``RUNTIME_HOST_FLAG``. ``run_entry``: the autostart Run-key marker in any
    LOADED hive. The two are composed by runtime_guard.decide's D2 rule, not
    here -- this model just carries both raw readings plus a combined detail
    string naming which sub-signal(s) fired.
    """

    model_config = ConfigDict(extra="forbid")

    live_process: ProbeStatus
    run_entry: ProbeStatus
    detail: str


class A2Result(BaseModel):
    """A2: in-distro CivicCast service activity probe."""

    model_config = ConfigDict(extra="forbid")

    status: ProbeStatus
    detail: str


class A3Result(BaseModel):
    """A3: Global\\CivicCastRuntimeOwner mutex acquisition outcome."""

    model_config = ConfigDict(extra="forbid")

    status: MutexStatus
    detail: str


class MaintenanceRecord(BaseModel):
    """D7a's journaled maintenance/freeze interlock record.

    Serialized as compact JSON into the ``Maintenance`` REG_SZ value at
    ``HKLM\\SOFTWARE\\CivicCast``. ``generation`` increments on each take;
    release rewrites ``state``/``released_utc`` but leaves ``generation``
    unchanged (the migration spec re-checks generation stability across its
    freeze window, so a release must not bump it).
    """

    model_config = ConfigDict(extra="forbid")

    v: Literal[1]
    state: Literal["held", "released"]
    generation: int = Field(ge=0)
    owner_run_id: str
    taken_utc: str
    released_utc: str | None = None
    #: <installer-path-audit BL-05> The PID of the process that took the
    #: interlock.
    #:
    #: Without it there was no liveness or expiry record of any kind, so a
    #: killed engine left ``state="held"`` forever and every subsequent upgrade
    #: died at step 1 -> exit 10 -> NSIS 124: a permanent, unexplained wedge.
    #: Recording it lets :func:`civiccast.native.win_probes.take_interlock`
    #: report a held-by-a-dead-process interlock as EXPLICITLY STALE, naming
    #: the run id and pid, instead of a bare "cannot take interlock: held".
    #: It is deliberately diagnostic, not an auto-steal: a live upgrade's
    #: interlock must never be taken from underneath it.
    #:
    #: Optional with a default so a record written by an older build still
    #: parses under ``extra="forbid"``.
    owner_pid: int | None = None


class InterlockRead(BaseModel):
    """A read of the D7a maintenance interlock.

    ``status="unreadable"`` covers: malformed JSON, a value that fails
    MaintenanceRecord's schema (including unknown fields, per extra=
    "forbid"), or the wrong registry type -- all FAIL-CLOSED per D4 ("a
    transmitter that can't check permission doesn't transmit"). Absent
    key/value is the readable-free case (status="free", record=None).
    """

    model_config = ConfigDict(extra="forbid")

    status: InterlockStatus
    record: MaintenanceRecord | None
    detail: str


class GuardDecision(BaseModel):
    """The output of runtime_guard.decide -- what the caller should do.

    ``retry_seconds`` is 10 exactly when ``action="blocked_probe_unavailable"``,
    else None. ``state_name`` carries the supervisor-visible state vocabulary
    from spec D5/D3 ("blocked_wsl_active" | "blocked_probe_unavailable" |
    None): decide() itself only ever sets it for blocked_probe_unavailable;
    GuardMonitor is responsible for the mid-operation "blocked_wsl_active"
    relabeling described in its own docstring.
    """

    model_config = ConfigDict(extra="forbid")

    action: GuardAction
    named_probe: str | None
    message: str
    retry_seconds: int | None
    state_name: str | None


class GuardInputs(BaseModel):
    """Everything runtime_guard.decide needs, pre-composed by the caller.

    ``wsl_install_detected`` is a TRI-STATE (F1 fix): ``True``/``False`` are
    confirmed answers from ``win_probes.detect_wsl_install`` (the CivicCast
    WSL distro definitely is/isn't registered); ``None`` means the detector
    itself could not determine an answer (timeout/OSError/undecodable
    output) -- an UNKNOWN install state, never silently folded into
    ``False``. See ``runtime_guard.decide``'s selector="absent" row for how
    each of the three states is handled.
    """

    model_config = ConfigDict(extra="forbid")

    selector: SelectorRead
    wsl_install_detected: bool | None
    a1: A1Result
    a2: A2Result
    a3: A3Result
    interlock: InterlockRead


class CutoverPhaseRecord(BaseModel):
    """One journaled phase of a D7 cutover or D8 rollback run.

    ``postcondition`` is the human-readable description of what "done"
    means for this phase. ``verified_on_resume`` (F2 fix) is the MACHINE
    outcome of the last time that postcondition was actually RE-CHECKED on
    resume (``None`` = no resume-verify has happened yet, e.g. this phase
    was freshly executed rather than skipped; ``True``/``False`` = the last
    resume-time verify passed/failed). See ``runtime_cli._run_phase``: a
    "done" phase is never skipped on trust alone -- its ``verify`` callable
    re-checks the postcondition for real before the phase is allowed to be
    skipped.
    """

    model_config = ConfigDict(extra="forbid")

    phase: int = Field(ge=1, le=5)
    name: str
    status: Literal["pending", "done", "failed"]
    started_utc: str | None
    finished_utc: str | None
    detail: str
    postcondition: str
    verified_on_resume: bool | None = None


class CutoverJournal(BaseModel):
    """The resumable journal for `civiccast-runtime cutover-to-native` /
    `rollback-to-wsl`, persisted at
    `%ProgramData%\\CivicCast\\logs\\runtime-cutover-journal.json`.

    CC-WS4-003 fix (round 2, Critical): ``interlock_owner_run_id`` /
    ``interlock_generation`` bind the D7a maintenance/freeze interlock
    bracket this run is transacting inside of -- either the fresh record
    THIS command took (default OWNED mode) or the caller-supplied record a
    migration already holds (``--interlock-owner``/``--interlock-generation``,
    EXTERNAL mode). A resume re-verifies the interlock is still held under
    these exact bound values before continuing any phase (see
    ``runtime_cli._verify_interlock_bracket``) -- generation drift or an
    unexpectedly free/released/unreadable/wrong-owner record aborts before
    any further selector mutation. Both default to ``None`` so existing
    journals written before this fix still parse (a ``None``-bound journal
    predates the interlock bracket entirely and is never re-verified against
    a stale absence of binding -- it is simply a fact about when it was
    written, not itself a defect this field's presence introduces).
    """

    model_config = ConfigDict(extra="forbid")

    v: Literal[1]
    run_id: str
    direction: Literal["cutover", "rollback"]
    phases: list[CutoverPhaseRecord] = Field(default_factory=list)
    unloaded_profiles: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    interlock_owner_run_id: str | None = None
    interlock_generation: int | None = None
    removed_run_entry_exe_path: str | None = None
    """CC-WS4-006 fix (round 2, Major): the executable path a CUTOVER's
    phase 2 captured from the keeper Run entry it removed (parsed from the
    first matching hive's value data), OR the path a ROLLBACK preflight
    resolved (either carried forward from a prior cutover journal, or the
    caller's explicit ``--exe-path``) and bound before mutating anything.
    Carried forward across a direction switch (cutover journal -> rollback
    journal) so rollback's phase 4 does not require re-supplying
    ``--exe-path`` when a prior cutover already recorded it."""

    @property
    def ok(self) -> bool:
        return (
            bool(self.phases) and not self.errors and all(p.status == "done" for p in self.phases)
        )


__all__ = [
    "A1Result",
    "A2Result",
    "A3Result",
    "CutoverJournal",
    "CutoverPhaseRecord",
    "GuardAction",
    "GuardDecision",
    "GuardInputs",
    "InterlockRead",
    "InterlockStatus",
    "MaintenanceRecord",
    "MutexStatus",
    "ProbeStatus",
    "Selector",
    "SelectorRead",
]
