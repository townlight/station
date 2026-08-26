# Installed automatic recurring Draft proof harness and protocol — 2026-08-25

## Purpose

`scripts/prove-installed-automatic-materialization.ps1` is the exact-candidate
installed-machine proof harness and protocol for a stationd automatic
recurring-Draft checkpoint. It becomes installed-machine evidence only when it
produces and preserves a passing same-candidate result receipt; this document
does not claim that such a receipt exists. It does not manufacture a run, call
the operator materialization endpoint, or use a process kill/restart mechanism.

Before any service boundary, it fails closed unless all of these are true:

- the checked-out source candidate is clean and the candidate manifest matches
  `HEAD`;
- the installed receipt, runtime manifest digest, every bounded private-runtime
  file path/size/hash, active-slot receipt, and
  installed `stationd.exe`, `channel-worker.exe`, and `station-updater.exe`
  hashes match that candidate;
- Windows, System32, Program Files, and ProgramData roots resolve from canonical
  Windows known folders, and conflicting environment values are rejected;
- the exact installed `TownLightStation` and updater identities have the
  expected command paths, `LocalSystem` account, and automatic start mode;
- loopback health is ready; and
- the credential is an enabled Setup Admin, Meeting Operator, or Support Admin
  account: the reader role required by every scheduling surface queried by the
  harness; and
- authenticated reads of the exact channel, enabled recurring rules, current
  schedule, alerts, and bounded materializations/history surface find exactly
  one current-UTC-day `origin: automatic` run, with the reserved automatic
  actor and valid stable request/result hashes. That run must expose at least
  one unique `(occurrence_id, schedule_item_id)` generated Draft binding whose
  current schedule item still belongs to the enabled channel and is `draft`.

If that automatic Draft fixture is absent, the receipt is `status: blocked`,
`error_code: fixture_not_ready` (the fixture-not-ready outcome), and the
harness performs no Windows Service Control Manager control or mutation. It is
safe to use this outcome to tell the operator that the installed station needs
a real automatic fixture; it is not a reason to force a run.

## Invocation

Run the read-only preflight against a real installed candidate and a real
channel with an already-existing automatic Draft run. Its credential must be an
enabled Setup Admin, Meeting Operator, or Support Admin account, is acquired in
memory, and is never put in arguments, a file, or an environment variable.
Elevation is required only when `-EnableServiceRestartProof` is supplied after
the ready fixture has passed.

```powershell
$credential = Get-Credential
.\scripts\prove-installed-automatic-materialization.ps1 `
  -Credential $credential `
  -ChannelId '00000000-0000-4000-8000-000000000001' `
  -ExpectedSourceCommit '0000000000000000000000000000000000000000' `
  -EnableServiceRestartProof `
  -ResultPath '.\automatic-materialization-proof.json'
```

The example identity and commit are placeholders and must be replaced with the
candidate-bound values under proof. Omitting `-EnableServiceRestartProof`
performs the read-only preflight and returns `status: blocked` with
`restart_proof_not_enabled`; it performs no Windows Service Control Manager
control or mutation.

When the bounded materialization history needs another page, the harness sends
only the server-returned exclusive cursor. Its
`before_occurred_at_unix_ms` timestamp must be strictly greater than zero and
its `before_run_id` must be canonical; zero and negative durable timestamps are
invalid.

## Controlled service boundary

With the explicit flag, elevation, and a ready fixture, the harness acquires
`Global\TownLightStationAutomaticMaterializationInstalledProof`. It then checks
the exact `Win32_Service` identity/path/account/start-mode before and while
waiting for transitions. It controls only `TownLightStation` using the
canonically resolved Windows System directory's exact `sc.exe stop
TownLightStation` and `sc.exe start TownLightStation` commands. It does not
trust `SystemRoot` to select that executable and does not control, reconfigure,
or restart `TownLightStationUpdater`.

After start it establishes a new authenticated session for the same account,
waits a bounded window
greater than 60 seconds while checking the exact station service identity, and
rereads the candidate receipts, ready health, channel, rule, schedule, alert,
and materialization-history surfaces. The proof passes only when the same one
current-day automatic run remains: its run ID, fixed actor, request/result
hashes, generated occurrence/Draft bindings, and a canonical digest of every
exposed current ScheduleItem field must be unchanged. A newer or
duplicate current-day automatic run, a duplicate Draft ID or occurrence, a
changed current Draft, a candidate mismatch, a service identity drift, UTC-day
rollover, or any timeout fails closed.

Failure and no-match alert episodes are recorded before and after the boundary
as independent channel-scoped observations. This receipt does not claim that
an alert is bound to a particular materialization run.

If a successfully issued stop leaves the exact verified station service
definitively stopped before the normal start is issued, one recovery `sc.exe
start` may be attempted. The actual stop outcome is carried into that decision;
no recovery action is taken after access denial or any failed/unissued stop,
for a timeout with a non-stopped service, identity drift, an uncertain state,
or after any start has already been issued.

## Result and non-claims

The JSON receipt records the source commit; candidate/receipt/runtime/binary
hashes; authenticated account identity; pre- and post-boundary automatic-run
and Draft identities; alert observations; and whether the narrowly scoped
service boundary, exact stop, exact start, or recovery start was attempted and
each recorded outcome. It contains no credential,
token, request body, or raw Service Control Manager output.

A passed same-candidate receipt proves durable once-per-UTC-day automatic
materialization identity across one controlled restart boundary. Until that
receipt exists, this document records only the harness and protocol. The
harness does not claim uninterrupted output, an on-air playout result, or
alert-to-run binding. It does not claim field proof, a multi-station pilot, or
a week of operation. Those require their own installed and field evidence.
