# ADR 0009: Acknowledged schedule dispatch

## Status

Accepted.

## Decision

`stationd` owns one orchestration controller for every enabled channel. The controller reads only committed work from authoritative SQLite, commands the isolated channel worker, verifies the exact durable response identity, and then performs a compare-and-set dispatch transition.

- `pending` becomes `queued` only after `AssetLoaded` identifies the expected content hash.
- `queued` becomes `acknowledged` only after the scheduled start and an `OnAirChanged` event identifies that asset.
- `acknowledged` becomes `completed` only after the scheduled end and an `OnAirChanged` event identifies fallback.
- An explicit rejection becomes `error`; a transport or process loss leaves the last acknowledged state intact for retry.

Controller memory is not authority. After a daemon or worker restart, queued work is reloaded before take. Acknowledged work is reloaded and retaken only while its committed time window remains active; after the end it returns to fallback and completes. The worker journal remains the evidence of what physically happened, while SQLite records control-plane progress.

## Consequences

Database state can no longer imply an on-air transition merely because a command was sent. A worker crash may create repeated load or take events during reconciliation, but it cannot create an unacknowledged success claim. Per-channel failure does not stop the loopback control plane or unrelated channels; the daemon discards the failed controller and attempts a clean supervised launch on the next cycle.
