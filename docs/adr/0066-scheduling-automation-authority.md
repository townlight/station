# ADR 0066: Scheduling-automation authority

## Decision

`station-schedule-automation` owns a pure, fail-closed v3 authority contract for revisioned
saved searches, typed candidate projections, daypart blocks, recurring rules, and
draft materialization intent. It does not modify the existing schedule authority.
A saved search is a named, station-wide authority with immutable owner and creation
time, so one search can be reused by rules on multiple channels. Its bounded query
supports title text, meeting body, duration, publication window, and typed custom
metadata equality/text/number/date/boolean predicates. Custom number/date ranges are
inclusive; fixed publication windows are half-open. Predicate order is semantically
irrelevant and canonical hashing sorts it while rejecting duplicates. The resolver operates only on the typed
candidate projections supplied to it; it does not claim to query or resolve the
media catalog. Every candidate binds a SHA-256 digest over the exact ordered S22
schema/record revisions and projected facets that produced its indexed custom
metadata, and that digest is part of the authenticated candidate-projection v3
bytes. Candidate selection supports `Top`, `Newest`, deterministic `Random`,
and `LeastRecentlyAired` strategies. Materialization accepts only typed candidate
and prior-airing projections with separate scoped receipts. A caller-supplied
verifier must authenticate each receipt against the exact canonical payload digest
and authority/window scope; this pure contract defines that boundary but implements
no key management or authenticator. Receipts reject future issue times, expiration,
and validity longer than five minutes at the caller-supplied observation time. They
are reusable immutable snapshot attestations for the same scope within that bounded
period, not one-shot bearer tokens.

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
`Blocked` outcomes. Authenticated batch derivation constructs arbitration inputs
internally from those validated plans, applies channel-scoped priority and stable
rule-identity precedence once, and records the exact winner, both priorities, and
whether suppression came from higher priority or the rule-identity tie-break.
Arbitration selects a pairwise non-overlapping winner set: a suppressed bridging
slot does not suppress a later slot that does not overlap an accepted winner.
Callers cannot inject arbitration candidates. Proposed occurrences receive a stable
occurrence ID derived from the rule, channel, and slot instant. The resulting
materialization intent is draft-only: it is a deterministic, version-3, count- and
length-framed canonical projection of the authority, window, exact candidate/as-run
payload digests, verified receipt digests, and resolved proofs, never a schedule
write. Empty projections require the same verification and cannot silently claim
that no candidate or prior airing exists. Prior-airing evidence explicitly covers
the full materialization window plus the rule's avoid-repeat lookback. The
source-authority and intent domains are v3; occurrence identity remains v2 because
it is still rule, channel, and start time. Older source/intents are not silently
reinterpreted. Admission reauthenticates the exact projections and receipts;
structural hash validation alone is not an admission path. Future storage
materialization must accept the opaque arbitrated batch rather than independently
admitting overlapping per-rule drafts.

## Boundaries and non-claims

The pure crate has no query adapter, as-run adapter, filler alert, S4 writer,
schedule write, materialization job, dispatch, worker, API, UI, or installed-machine
evidence. ADR 0068 adds durable current/history storage for saved searches, blocks,
and rules. Authenticated API routes expose bounded current/list/history reads and
server-attributed PUT create/update for those authorities, but not delete,
saved-search preview, rule simulation, or materialization. ADR 0069 adds fixed
catalog editorial authority and persistence. The storage layer now constructs a
bounded transactionally consistent catalog/media/S22 candidate snapshot and its
canonical digest. ADR 0070 adds the typed boot-scoped HMAC issuer/verifier
mechanism, but stationd singleton ownership and the adapter that issues a receipt
for that snapshot remain unwired; no trusted as-run adapter exists. This contract therefore does
not establish that a saved search finds media, that a projection came from a real
catalog or as-run journal, that an intent was stored, that a schedule item was
created, that a worker received a dispatch, or that any item aired. Future work must
connect the candidate snapshot to the boot authority, add a trusted as-run adapter, persist later
materialization authority through the single SQLite writer, materialize/commit
through schedule authority, add filler-alert behavior and the S4 writer, and acquire
installed-machine and field evidence.
