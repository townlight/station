# ADR 0066: Scheduling-automation authority

## Decision

`station-schedule-automation` owns a pure, fail-closed v2 contract for revisioned
saved searches, typed candidate projections, daypart blocks, recurring rules, and
draft materialization intent. It does not modify the existing schedule authority.
A saved search is channel- and owner-bound and carries bounded typed metadata
filters, ordering, and a result limit. The resolver operates only on the typed
candidate projections supplied to it; it does not claim to query or resolve the
media catalog. Candidate selection supports `Top`, `Newest`, deterministic
`Random`, and `LeastRecentlyAired` strategies. Repeat avoidance requires an
explicit `PriorAiring` input set from an authentic as-run/airing source; this pure
contract neither fabricates nor authenticates that source.

A daypart permanently binds to one channel and a bounded same-day local-minute
interval. It can constrain weekday, season, and an optional rule allow-list. A
recurring rule binds one saved-search revision, one block revision, and one channel;
media is selected by the saved-search query at resolution time rather than by a
stored exact asset or mutable asset revision. It carries bounded priority,
weekday/interval cadence, anchor date, local time and duration, explicit timezone
and DST-fold policy, and a 14-to-60-day rolling horizon. UTC is explicit. A named
timezone is limited to the supported station-zone catalog; normal local times
resolve uniquely, folds use the configured earlier/later policy, and nonexistent
local times fail closed. Resolved slots and materialization intents are not Serde
inputs, so callers cannot inject offsets or instants.

Resolution produces a deterministic per-slot plan with `Proposed`, `NoMatch`, or
`Blocked` outcomes (and an explicit higher-priority suppression outcome where
rules arbitrate for the same slot). Proposed occurrences receive a stable
occurrence ID derived from the rule, channel, and slot instant. The resulting
materialization intent is draft-only: it is a deterministic, version-2,
count- and length-framed canonical projection of the authority, window, and
resolved proofs, never a schedule write. Golden vectors pin the v2 canonical
domains and occurrence identity.

## Boundaries and non-claims

The pure crate has no query adapter, as-run adapter, filler alert, S4 writer,
schedule write, materialization job, dispatch, worker, API, UI, or installed-machine
evidence. ADR 0068 adds durable current/history storage for saved searches, blocks,
and rules, but no catalog/as-run or materialization persistence. This contract does
not establish that a saved search finds media,
that candidate projections came from the catalog, that `PriorAiring` inputs are
authentic, that an intent was stored, that a schedule item was created, that a
worker received a dispatch, or that any item aired. Future work must derive actors
server-side, connect trusted catalog and as-run adapters, persist later
materialization authority through the single SQLite writer, materialize/commit
through schedule authority, add filler-alert behavior and the S4 writer, and acquire
installed-machine and field evidence.
