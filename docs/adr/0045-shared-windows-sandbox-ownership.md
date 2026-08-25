# ADR 0045: Shared Windows Sandbox ownership

## Status

Accepted — 2026-08-24

## Context

TownLight and an independent Claude workflow run on the same Windows host. Either may need Windows
Sandbox for clean-machine evidence. A visible absence of a sandbox window, a running Hyper-V compute
service, an old timestamp, or an unfamiliar process is not proof that TownLight owns the resource.
Process-name cleanup can destroy another workflow's active test, while modifying another workflow's
configuration or lease file can erase the only ownership evidence.

## Decision

Every TownLight Windows Sandbox launch must use `scripts/invoke-owned-windows-sandbox.ps1`.

The launcher takes the machine-global `Global\Scott.SharedWindowsSandbox.Launch.v1` mutex without
waiting, then enumerates the exact Windows Sandbox host/client process names through the ordinary
user-token `Get-Process` snapshot. It does not depend on elevated `Win32_Process` access. Failure to
inspect, inability to read the exact identity of a detected process, an abandoned or unavailable
mutex, or any matching process makes ownership unknown or occupied and blocks launch. The global
mutex is agent-neutral so the Claude harness can use the same check-and-launch serialization
boundary. A check that observes no process reports `no_active_sandbox_detected` and
`none_observed`, never "free" or "available"; that result is an instantaneous guarded observation.

When a Sandbox process is present, check-only mode returns its PID, process name, creation time, and
executable path without changing it. The process is attributed to TownLight only when every observed
identity has exactly one matching immutable TownLight launch record and all matches share one run
ID. Anything else is `external_or_unclaimed` and therefore inside the no-touch fence. Launch mode
fails closed for both classifications. It never adopts, reuses, or cleans an already-running
Sandbox, including one launched by Claude.

Launch accepts only an existing `.wsb` file beneath the committed TownLight-owned
`harness\windows-sandbox\configs` directory and only the Microsoft-signed
`%WINDIR%\System32\WindowsSandbox.exe`. Before launch it creates a new immutable request record;
after `Start-Process -PassThru` returns, it verifies and records the exact PID, creation identity,
configuration hash, executable hash, run ID, and TownLight owner namespace. Records are created with
`FileMode.CreateNew` beneath
`%ProgramData%\Scott Shared Harness\WindowsSandbox\ownership-v1`; they are never overwritten or
removed by the launcher.

The launcher has no termination path. Any later cleanup implementation must require an exact
harness-owned record and revalidate PID, creation identity, executable identity, and run ID before
acting. It must never target processes by name, age, port, VM state, or broad enumeration. Claude
processes, configurations, worktrees, logs, leases, and evidence are always outside TownLight's
ownership boundary.

## Consequences

- Concurrent cooperative launches cannot race through the preflight boundary.
- A sandbox started outside the shared mutex can still appear between inspection and launch; both
  workflows therefore must use this same boundary. Detected occupancy always fails closed.
- Stale or foreign evidence is retained. It cannot become a cleanup target merely because its
  process is absent.
- The current launcher favors leaving an uncertain process running over damaging another workflow.
  Manual adjudication remains necessary when ownership cannot be proved.
