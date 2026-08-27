# ADR 0059: Underwriting spot authority

## Decision

`station-underwriting` owns a pure, fail-closed contract for underwriting spots,
sponsor flights, placement intent, proof-of-airing join intent, and billing-export
intent. A spot is a bounded-duration, content-addressed asset bound permanently to
one sponsor. A flight binds one exact current spot revision to a bounded station
window, a bounded unique channel set, and explicit weekday/daypart/count/separation
constraints. Spot, flight, and placement writes use revision-one creation plus
explicit compare-and-set replacement; their stable identities cannot be rebound to
another sponsor, spot, flight, or channel.

Placement validation is intentionally individual and deterministic. It proves only
that the requested channel, instant, station-local weekday, and minute of day fit
the exact flight revision. It does not claim a scheduler reserved capacity or that
prior/future placements meet aggregate count and separation constraints.

A proof-of-airing join names exact spot, flight, and placement revisions, one
as-run-event identity, and bounded timing/duration facts. It is append-only intent:
duplicate proof or as-run-event identities fail, and there is no update/delete
operation. A billing export request contains a bounded unique proof-ID set for one
sponsor and one bounded period; validation requires every supplied immutable proof
to match that sponsor and period. It is authority to construct a later export, not
an invoice, payment, tax, or delivery receipt.

## Boundaries and non-claims

This crate is not SQLite trafficking/history, schedule enforcement, a playout or
as-run observer, affidavit/report generator, billing-system client, API,
authentication/authorization layer, or operator UI. It does not prove an asset
exists, a spot aired, an as-run fact is truthful, placement capacity was reserved,
count/separation constraints were globally reconciled, an invoice was accepted, or
payment is due. Later slices must persist immutable joins through the single SQLite
writer, reconcile aggregate placement constraints against authoritative schedule and
as-run data, derive authenticated actor/time authority, generate actual affidavits
and billing exports, and acquire installed-machine and field evidence.

## S24 durable placement increment

`station-storage` now persists underwriting spots, flights, and schedule-bound
placements through the station's single SQLite writer. Spot, flight, and
placement history is append-only; current spot reads cross-check indexed
columns, JSON, and the exact latest history revision. Writes run under
`BEGIN IMMEDIATE`, apply the pure compare-and-set and no-rebinding contracts,
and retain the authenticated caller identity and server-supplied occurrence
time.

A placement is admitted only when its exact flight and spot revisions exist,
its media authority is `Ready` with the exact spot duration, and one exact
Draft schedule item has the same revision, channel, asset, start, and duration.
The write transaction reconciles total-flight capacity and minimum separation
against every current placement for the flight. Later Draft schedule mutations
are rejected when they would invalidate that binding.

This increment does not yet persist proof joins or export requests, consume
trusted ScheduledAssetOnAir facts, render affidavit/export bytes, expose an
authenticated staff API or operator UI, or enforce the per-station-day count
across timezone transitions. It makes no claim about billing delivery,
invoicing or payment, actual external airtime, installed proof, or field proof.
