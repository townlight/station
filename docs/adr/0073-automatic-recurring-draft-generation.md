# ADR 0073: Automatic recurring Draft generation

## Decision

Stationd owns automatic recurring Draft generation in a dedicated supervised
control-plane thread. The worker is separate from channel playout workers and
from the operator API, but it uses the exact same boot-owned
`Arc<StationSourceEvidenceAuthority>` as the API. It never constructs a second
source-evidence authority and never supplies rules, windows, candidates,
airings, receipts, actor identity, or time to the materializer. Its only write
call is the channel-scoped production boundary from ADR 0072 with the fixed
server actor.

On each bounded cycle the worker reads one durable SQLite snapshot of every
enabled channel that has at least one enabled recurring rule and that channel's
newest fully validated materialization run. The snapshot fails closed on
duplicate or excessive authority and when the newest run binding disagrees
with its plan authority; it cannot skip a corrupt newest run and fall back to
an older one. A fully validated successful run completed in the current UTC
day is the durable checkpoint, whether the run was initiated automatically or
by an operator. After that success the worker does not invoke the channel again
in the same UTC day, including after restart; failed attempts may retry under
the backoff policy below. Channels that are enabled but have no enabled
recurring rule are intentionally outside the automatic authority set.

The storage writer repeats channel enablement after `BEGIN IMMEDIATE`, in the
same transaction that derives and writes the run. Disabling a channel between
checkpoint enumeration and execution therefore produces no materialization
write. Per-channel materialization failures are isolated: the cycle continues
to other channels and applies bounded exponential backoff to the failed
channel. Invalid global clock or checkpoint authority is fatal to the worker
rather than being treated as a channel-local condition.

The worker checks the shared stop authority before snapshot work and before
each channel. Between cycles it uses interruptible park slices no longer than
250 milliseconds; it does not busy-poll. Stationd joins the named worker on
every applicable console and service cleanup path. A panic, unexpected clean
exit, or fatal controller error requests the global stop and writes only a
bounded generic failure receipt. Underlying database, authority, rule, or
channel details are not copied into that receipt.

## ScheduleNoMatch warning

ADR 0072 materialization reconciles the channel's `ScheduleNoMatch` warning in
the same SQLite transaction as the immutable run, plans, outcomes, generated
Drafts, schedule history, and alert-delivery outbox. One or more final durable
`NoMatch` outcomes fire a channel-scoped Warning which identifies the run,
affected rules, count, and earliest affected UTC slot. A successful run with
no `NoMatch` outcome resolves the firing condition. An alert-rule, routing, or
outbox failure rolls back the entire new materialization run; an exact
`AlreadyApplied` retry does not create a duplicate alert episode.

`ScheduleNoMatch` means only that recurring generation found no published,
ready catalog asset satisfying the saved-search and repeat policy. It is not a
filler insertion, schedule-gap determination, missing-media result, commit
failure, dispatch failure, or proof of what aired.

## Automatic materialization failure warning

After a channel-local automatic materialization attempt has returned an error
and its write transaction has rolled back, the controller records a separate
channel-scoped `ScheduleMaterializationFailure` Warning. The durable event and
its initial delivery outbox share their own `BEGIN IMMEDIATE` transaction. The
event exposes only a fixed typed failure class; raw database errors, paths, and
authority details never enter operator evidence or the stationd failure
receipt. Repeating the same typed failure while it is firing leaves its
occurrence count and delivery set unchanged. The next successful automatic
`Applied` or `AlreadyApplied` result resolves only that channel's condition.

Failure to persist or route this required alert is a fatal controller error.
Stationd requests global stop and emits only its bounded generic supervisor
receipt rather than silently continuing without durable operator evidence.

## Materialization history

Enabled Setup Admin, Meeting Operator, and Support Admin accounts can inspect a
bounded, newest-first history for one channel. Storage performs the read in one
deferred transaction with an exclusive `(occurred_at_unix_ms, run_id)` keyset
cursor. The cursor timestamp is strictly greater than zero and its run ID is
canonical; zero and negative timestamps are invalid. Storage validates every
selected run, plan, outcome, Draft origin, and current schedule binding before
returning any page. A corrupt newest row, missing or duplicate run-to-channel
binding, cross-channel nested record, oversized aggregate, or storage failure
fails the page closed; the reader never skips bad authority and falls back to
older records.

The API exposes only the immutable run identity, server-attributed origin and
actor, request/result digests, UTC window and observation times, redacted plans
and outcomes, and generated Draft/current-schedule facts. It never exposes the
source-evidence receipts, HMAC material, alert-delivery state, or internal retry
state. Every persisted materialization actor is a canonical UUID. Automatic
origin is derived only at this trusted storage boundary from the reserved,
non-login automatic actor identity; operator-account creation, database open,
and migration auditing prevent that identity from being assigned to a person.
The operator console preserves this distinction, uses the server cursor, and
rejects late or cross-channel responses.

## Boundaries and non-claims

This decision provides once-per-UTC-day automatic invocation for eligible
channels, restart-stable durable checkpointing, per-channel retry isolation,
supervised shutdown, the transactional `ScheduleNoMatch` Warning, and a
durable typed `ScheduleMaterializationFailure` Warning for channel-local
automatic attempts. It also provides the bounded authenticated
materialization-run history API and operator surface described above. It does
not rerun a channel later in the same UTC day when an operator edits a rule,
search, block, or catalog record. It does not provide preview or simulation,
automatic commit or dispatch, filler insertion, durable failed-attempt/retry
history, installed-candidate proof, or one- and three-station field proof.
