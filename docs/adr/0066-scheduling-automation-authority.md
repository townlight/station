# ADR 0066: Scheduling-automation authority

## Decision

`station-schedule-automation` owns a pure, fail-closed contract for revisioned saved
searches, daypart blocks, recurring rules, and idempotent materialization intent. It
does not modify the existing schedule authority. A saved search permanently binds to
one channel and owner; it contains a bounded ordered set of safe search terms. A
daypart permanently binds to one channel and a bounded local-minute interval.

A recurring rule binds exactly one search revision, one block revision, one channel,
and one exact asset revision. It carries a nonzero weekday mask, one-to-four-week
interval, bounded anchor date, local time and duration, and an explicit recurrence
timezone. UTC is explicit. A named timezone is limited to the supported station-zone
catalog and must choose an earlier- or later-fold DST policy; absent, unknown, or
`Standard` named-zone policy fails closed. The pinned Jiff authority derives bounded
UTC slots against the local calendar cadence, daypart, duration, explicit DST
application, and materialization window. It derives IANA-zone instants
internally: normal times resolve uniquely, folds require and apply the configured
earlier/later policy, and nonexistent local times fail closed. Resolved slots and
materialization intents are not Serde inputs, so callers cannot inject offsets or
instants.
Dayparts are deliberately same-day-only for beta and reject overnight intervals.

Materialization intent uses explicitly versioned, collection-count- and length-framed canonical hashing of
the exact source authority, window, and ordered resolution proofs.
It is deterministic and contains the exact rule, channel, asset, and block revisions.
An append boundary validates each existing bounded ledger identity. Re-submitting the
same exact intent is `AlreadyPresent`, an idempotent no-op; a duplicate ledger entry,
forged identity, or content-changing identity reuse fails closed. Rule, search, and
block successor validation uses exact CAS revision progression and rejects identity,
channel, or asset rebinding.

## Boundaries and non-claims

This slice has no persistence/history, authentication, search execution, media
selection, materialization job, schedule write, dispatch,
worker, API, UI, or installed-machine evidence. It does not establish that a saved
search finds media, an intent was stored, a schedule
item was created, a worker received a dispatch, or any item aired. Future work must
derive actors server-side, persist authority through the single SQLite writer,
materialize/commit through schedule authority,
and acquire installed-machine and field evidence.
