# ADR 0033: Wall-clock schedule catch-up after live return

Date: 2026-08-22

Status: Accepted

## Context

Live takeover can span one or more scheduled boundaries. Returning to the item that was
previously on air would replay stale programming when that item has ended, while allowing a
preloaded successor to become visible before its window would put future programming on air early.
The schedule therefore needs one wall-clock decision at each dispatch and at live handback, with
durable outcomes for work that can no longer air.

## Decision

`ScheduleItem::window_at` defines every committed item as a half-open wall-clock window:

```text
now < start       -> future
start <= now < end -> active, with media offset now - start
now >= end        -> ended
```

An item beginning at an exact boundary is eligible at offset zero; an item ending at that same
instant is no longer eligible. Offset calculation uses checked integer arithmetic and fails on
overflow rather than inventing a position.

Schedule dispatch and live handback use the same classification. A pending or queued item whose
window has ended is advanced to the terminal `missed` dispatch state and is never taken to air.
An acknowledged item is allowed to complete only after the worker has acknowledged its return to
fallback or to the next valid program. Dispatch status remains separate from schedule authority:

```text
pending -> queued -> acknowledged -> completed
       \-> missed
queued  \-> missed
```

Loading an asset prepares and parks its validated decoder while the current source remains on air.
The parked decoder cannot consume future programming. A scheduled take starts the prepared decoder
at the calculated `media_offset_ms` and selects it only after the worker acknowledges the expected
asset identity. Restart reconciliation uses the same offset-aware take while the item remains
active.

While a live session is `on_air`, schedule dispatch is blocked. On return, stationd evaluates the
current wall clock, retires ended pending/queued work, and selects the first committed item whose
window is active at the handback decision. The worker receives either:

- `Program { asset_id, media_offset_ms }` for that current item; or
- `Fallback` when no committed window is active.

Handback preparation accounts for time spent preparing the target. If the candidate expires while
it is being prepared, the controller re-evaluates the schedule rather than taking an expired item.
If the schedule crosses more than the bounded preparation limit, or the station clock moves
backward, the operation fails closed.

The durable takeover audit records `return_requested`, `returning`, and `completed`. The completed
detail identifies the returned asset and media offset, or records the fallback result. A scheduled
item that was acknowledged before live and expires during live is completed when the valid return
target is acknowledged; a successor selected at handback becomes acknowledged after its worker take
is confirmed. Return is not complete until `LiveReturned` and `LiveDisarmed` are both acknowledged.

## Fail-closed boundaries

- A stale authority, source revision, or session state cannot change the selected return target.
- An ended pending/queued item becomes `missed`; it is never replayed as catch-up programming.
- A return target that expires during preparation is discarded and selection is retried at the
  current wall clock.
- A backward station clock or excessive boundary crossing fails the live handback rather than
  selecting an ambiguous source.
- An unexpected worker event, worker restart, failed return, or failed disarm leaves the takeover
  failed rather than reporting completion.
- If no schedule window is active at handback, the deterministic target is healthy fallback.

## Consequences

Returning from live is deterministic at the actual handback time: it does not replay an expired
item and does not take a future item early. Operators and audit consumers can distinguish missed
scheduled work, completed prior work, the selected successor, and the exact successor media offset.
The policy is bounded and channel-scoped; it does not claim additional live transports or installed
machine capture evidence. The behavior is covered by schedule, media-engine, storage, and
orchestration tests; an installed proof remains a separate gate.

