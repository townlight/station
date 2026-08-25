# ADR 0056: Scheduled-recording authority

## Decision

`station-recording` owns a pure, server-authoritative contract for one scheduled
recording. A draft definition binds its identity to exactly one channel and schedule
item, the schedule revision, a bounded wall-clock start/end interval, and the
authenticated operator or supervisor who requested it. The crate does not accept a
recording definition from a worker identity. It deliberately does not bind a future
recording to the candidate or configuration that happened to exist when an operator
scheduled it.

The lifecycle is intentionally narrow:

```text
Draft --operator/supervisor with current execution authority--> Armed --recording worker--> Capturing
Capturing --recording worker--> Finalizing --recording worker--> Completed
Capturing --recording worker, after scheduled end--> Finalizing(recovered)
Draft|Armed --operator/supervisor, before start--> Cancelled
Armed|Capturing|Finalizing --recording worker--> Failed
```

Arming records one exact server-derived execution authority: candidate version/source
commit plus channel configuration hash. Every later worker transition and its receipt
must provide that exact armed authority. This lets a legitimate software update or
configuration correction before air be armed on its current execution generation,
while preventing any post-arm generation change from being silently treated as the
same recording. Draft cancellation carries no execution authority; Armed and every
later worker-owned lifecycle state carry exactly one.

Every transition requires the expected optimistic revision. It emits a receipt with
the acting identity, prior/resulting revisions, event kind, time, and either the
exact execution authority or an explicit absence for a draft cancellation, for a
later append-only persistence boundary. Invalid UUIDs, hashes, revisions, time
windows, actors, reasons, transitions, duplicate/replayed revision attempts, armed
authority mismatch, and terminal-state reanimation fail closed.

`station-storage` persists the current recording authority through the one SQLite
writer. Draft creation is revision one; every later write must carry a receipt whose
prior/resulting revisions are exactly the persisted/current revisions. The current
row indexes the recording, channel, schedule item/revision, window, lifecycle,
execution authority, and revision alongside its full JSON record. Every read
cross-checks those indexed values against validated JSON. A bounded append-only
history records creation or the exact receipt plus the resulting recording snapshot;
its update/delete triggers reject mutation. JSON/index/receipt/lifecycle/execution
disagreement fails closed rather than being repaired or guessed.

Schedule-item mutation is separately attributed and append-only: public item writes
admit drafts only, while the private commit gate is the sole path to `Committed`.
Current schedule rows are cross-checked against their latest immutable history
snapshot. The migration seeds pre-history schedule rows with the explicit
`legacy-migration` actor and timestamp zero, which means migration time is unknown;
zero is invalid for all ordinary schedule writes.

`station-api` now supplies the authenticated server boundary for this authority.
Enabled Setup Admins map to recording supervisors and enabled Meeting Operators
map to recording operators; the browser supplies only a recording identity and a
schedule-item identity for creation, optimistic revisions, and a cancellation
reason. The server reads the current committed schedule item and derives its
channel, wall-clock window, and durable CAS revision; it never accepts browser
schedule channel/window/revision facts. The server rechecks that exact committed
schedule identity/revision/channel/window before arm or a trusted worker
transition for early operator feedback; the SQLite writer repeats that check inside
the same `BEGIN IMMEDIATE` transaction as every non-cancellation transition
persistence. A schedule-item edit, schedule-item cancellation, or deletion therefore
blocks arm and worker transitions even when it races an API precheck. Explicit
operator cancellation remains available without a current schedule binding. It
also derives the actor and event time, and derives candidate/channel configuration
authority for an operator arm from the currently verified installed candidate and
current channel configuration. Browser routes do not accept lifecycle receipts,
worker actor identity, candidate, configuration hash, timestamp, or
capture/finalization facts.

Non-operator lifecycle steps are available only through an in-process trusted
observation type. That type has no candidate, configuration, timestamp, receipt,
or actor fields; the server supplies the fixed recording-worker identity, current
time, and current verified candidate/configuration before delegating to the domain
contract. It is deliberately not a browser route. The authenticated API exposes
bounded list, current-read, and append-only-history routes. Its separate,
role-gated recording-candidate route accepts only an exact bounded query and
returns current committed items strictly after server-derived now, within a
31-day future window and SQL limit; it never enumerates schedule history. The
operator console exposes the corresponding channel workflow: select a committed
schedule item, create a recording intent, read its authoritative revision, arm
or cancel with CAS, and review the durable transition history. The UI does not
materialize schedule timing, submit binding facts, or operate a worker.

Recovery deliberately cannot resume an interrupted capture. A recording worker can
mark an in-progress capture for recovery only after its scheduled end and only into
`Finalizing`, retaining the fact that the completion followed an interruption. A
successful finalization must name a content hash and a positive captured duration
within a bounded drift of the scheduled interval. This stops a restart from silently
claiming a continuous recovered capture that has not been proven.

## Boundaries and non-claims

This slice has a database, append-only audit, authenticated API boundary, and
operator intent workflow, but is not a capture engine, scheduler, media asset store,
or recovery probe. It makes no device call, starts no source, writes no media bytes,
observes no actual duration, hashes no recording, and cannot establish that the
supplied finalization facts are true. The API derives all operator and worker
authority facts at its trust boundary; it does not prove capture, finalization, or
recovery ran.

Later slices must materialize recordings from authoritative schedule changes, supervise native
capture per channel, create and verify managed media assets, reconcile worker crash
evidence, and collect exact-candidate installed-machine and field evidence. No
lifecycle state, operator intent, or passing unit test is a claim that scheduled
recording works on a station.
