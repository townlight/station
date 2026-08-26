# Installed automatic recurring Draft proof boundary — 2026-08-25

## Purpose

`scripts/prove-installed-automatic-materialization.ps1` is the installed-machine
gate for automatic recurring Draft generation. It authenticates through the
installed loopback session, verifies the exact clean candidate against the
installed receipts and binaries, checks both LocalSystem services and ready
health, and reads the current channel, recurring-rule, schedule, and alert
surfaces.

The gate never calls the operator materialization POST. That endpoint proves an
authenticated manual generation, not the supervised automatic worker, so using
it here would create a false automatic-execution claim.

## Invocation

Run only against a current clean installed candidate and an existing channel
that has an enabled recurring rule:

```powershell
$credential = Get-Credential
.\scripts\prove-installed-automatic-materialization.ps1 `
  -Credential $credential `
  -ChannelId '00000000-0000-4000-8000-000000000001' `
  -ExpectedSourceCommit '0000000000000000000000000000000000000000' `
  -ResultPath '.\automatic-materialization-proof.json'
```

The example identity and commit are placeholders and must be replaced with
real candidate-bound values. The credential remains in memory and is never
placed in arguments, environment variables, receipts, or logs.

## Current result

The authenticated `/alerts` surface now exposes durable channel-scoped typed
`schedule-materialization-failure` Warning episodes, as well as
`schedule-no-match` evidence. Those alerts are operator-visible, but the
current public interfaces do not expose a bounded read/history contract for
automatic materialization runs, so the alerts and schedule Drafts cannot be
bound to a specific automatic run, checkpoint day, or retry attempt. This gate
also has no supported service restart boundary. After successful authenticated
preflight, the harness emits `status: blocked` with these non-claims:

- once-per-UTC-day idempotence is not observable;
- restart durability is not observable;
- generated Drafts, failure alerts, and `ScheduleNoMatch` alerts cannot be
  bound to an automatic run identity or checkpoint day; and
- failure alert presence is observable, but automatic attempt/retry attribution
  is not a release claim.

The missing interface requirement is a bounded authenticated history endpoint
such as
`GET /api/v1/scheduling/channels/{channel_id}/materializations/history?limit={1..128}`
returning automatic-vs-operator origin, run identity, checkpoint UTC day,
disposition, outcomes, generated Draft identities, source commit, and the
associated failure/retry state so the existing alert episodes can be bound to
that run. The installed authority also needs a bounded restart-proof boundary.

Until those interfaces exist, a source test or a successful manual materialize
POST must not be recorded as installed automatic-worker proof.
