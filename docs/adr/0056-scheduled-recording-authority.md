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

Recovery deliberately cannot resume an interrupted capture. A recording worker can
mark an in-progress capture for recovery only after its scheduled end and only into
`Finalizing`, retaining the fact that the completion followed an interruption. A
successful finalization must name a content hash and a positive captured duration
within a bounded drift of the scheduled interval. This stops a restart from silently
claiming a continuous recovered capture that has not been proven.

## Boundaries and non-claims

This slice has a database and append-only audit boundary, but is not a capture engine,
scheduler, media asset store, recovery probe, API, authorization provider, or operator
interface. It makes no device call, starts no source, writes no media bytes, observes
no actual duration, hashes no recording, and cannot establish that the supplied
finalization facts are true. The actor role is an authority contract supplied by a
future authenticated server boundary, not authentication itself.

Later slices must materialize recordings from authoritative schedule changes, supervise native
capture per channel, create and verify managed media assets, reconcile worker crash
evidence, expose authenticated operator workflow, and collect exact-candidate
installed-machine and field evidence. No lifecycle state or passing unit test is a
claim that scheduled recording works on a station.
