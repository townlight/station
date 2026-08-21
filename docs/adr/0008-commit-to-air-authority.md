# ADR 0008: Commit-to-air authority

Date: 2026-08-20

Status: Accepted

## Context

Materializing a schedule item must not make it air-eligible automatically. An operator needs a preview, but preview alone cannot prevent a conflict or media-state change from appearing before approval. The durable record of approval must also exist before any process command is sent.

## Decision

`station-schedule` defines the transport-independent gate and `station-storage` executes the authoritative commit.

- A playable asset has an immutable SHA-256 identity, visible path, positive duration, and `ready` state.
- A proposed item fails the gate if its asset is absent, invalid, not ready, identity-mismatched, or shorter than the scheduled duration.
- Every non-cancelled item on the same channel is actively checked for half-open interval overlap.
- The nearest prior gap greater than one second is reported but does not fail approval.
- Preview reads current state and returns the complete plan.
- Approval begins `BEGIN IMMEDIATE`, reruns the complete gate, inserts a `pending` approval report, and changes the item to `committed` in one transaction.
- A failed rerun rolls back completely: no approval report and no item-state change.
- Dispatch status remains `pending` until a supervised worker acknowledges the command; persistence is never presented as proof that content aired.

## Consequences

The operator can review a useful plan without relying on an in-memory plan cache. The approval transaction is the race authority. SQLite serializes writers at the station boundary, while the pure gate remains easy to test. Authentication/roles, rollback, worker dispatch, recurring materialization, dayparts, and the operator schedule interface remain subsequent slices.
