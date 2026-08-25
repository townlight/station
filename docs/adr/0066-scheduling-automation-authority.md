# ADR 0066: Scheduling-automation authority

## Decision

`station-schedule-automation` owns a pure, fail-closed v2 authority contract for revisioned
saved searches, typed candidate projections, daypart blocks, recurring rules, and
draft materialization intent. It does not modify the existing schedule authority.
A saved search is channel- and owner-bound and carries bounded typed metadata
filters, ordering, and a result limit. The resolver operates only on the typed
candidate projections supplied to it; it does not claim to query or resolve the
media catalog. Candidate selection supports `Top`, `Newest`, deterministic `Random`,
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
`Blocked` outcomes (and an explicit higher-priority suppression outcome where
rules arbitrate for the same slot). Proposed occurrences receive a stable
occurrence ID derived from the rule, channel, and slot instant. The resulting
materialization intent is draft-only: it is a deterministic, version-3, count- and
length-framed canonical projection of the authority, window, exact candidate/as-run
payload digests, verified receipt digests, and resolved proofs, never a schedule
write. Empty projections require the same verification and cannot silently claim
that no candidate or prior airing exists. The v2 source-authority domain and
occurrence identity remain stable; v2 intents are not reinterpreted as v3
evidence-bound intents. Admission reauthenticates the exact projections and receipts;
structural hash validation alone is not an admission path.

## Boundaries and non-claims

The pure crate has no query adapter, as-run adapter, filler alert, S4 writer,
schedule write, materialization job, dispatch, worker, API, UI, or installed-machine
evidence. ADR 0068 adds durable current/history storage for saved searches, blocks,
and rules, but no catalog/as-run or materialization persistence. No production
catalog/as-run receipt issuer or verifier exists yet. This contract therefore does
not establish that a saved search finds media, that a projection came from a real
catalog or as-run journal, that an intent was stored, that a schedule item was
created, that a worker received a dispatch, or that any item aired. Future work must derive actors
server-side, connect trusted catalog and as-run adapters, persist later
materialization authority through the single SQLite writer, materialize/commit
through schedule authority, add filler-alert behavior and the S4 writer, and acquire
installed-machine and field evidence.
