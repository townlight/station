# ADR 0009: Acknowledged schedule dispatch

## Status

Accepted.

## Decision

`stationd` owns one orchestration controller for every enabled channel. The controller reads only committed work from authoritative SQLite, commands the isolated channel worker, verifies the exact durable response identity, and then performs a compare-and-set dispatch transition.

- `pending` becomes `queued` only after `AssetLoaded` identifies the expected content hash.
- `queued` becomes `acknowledged` only after `TakeScheduledAsset` is accepted and the worker emits a
  `ScheduledAssetOnAir` event that echoes the report, schedule-item revision, asset, and media
  offset, together with the worker boot and sequence identities and the worker's actual on-air
  timestamp.
- `acknowledged` becomes `completed` only after the scheduled end and an `OnAirChanged` event identifies fallback.
- An explicit rejection becomes `error`; a transport or process loss leaves the last acknowledged state intact for retry.

The scheduled-take acknowledgement is an atomic storage operation, not a controller-memory
claim. Within one SQLite write transaction, storage re-reads the immutable commit report, the
exact committed schedule-history revision, and the current schedule binding; appends the
immutable actual-airing fact; and advances `queued` to `acknowledged` with compare-and-set. It
derives station receive time rather than accepting it from the worker. A valid acknowledgement
received after its committed window is retained as historical as-run evidence but does not replace
the current channel runtime projection. Deterministic report/worker acknowledgement identities
make journal delivery and process restart idempotent.

Controller memory is not authority. Before launching a replacement worker, retained journal
`ScheduledAssetOnAir` events are replayed through that same storage operation. Queued work is then
reloaded before take. Acknowledged work is reloaded and retaken only while its committed time
window remains active; after the end it returns to fallback and completes. The worker journal
remains the evidence of what physically happened, while SQLite records the authoritative
control-plane and actual-airing facts.

## Consequences

Database state can no longer imply an on-air transition merely because a command was sent. A worker crash may create repeated load or take events during reconciliation, but it cannot create an unacknowledged success claim. Per-channel failure does not stop the loopback control plane or unrelated channels; the daemon discards the failed controller and attempts a clean supervised launch on the next cycle.
