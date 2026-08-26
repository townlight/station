# ADR 0072: Transactional schedule materialization

## Decision

`StationStore::materialize_scheduled_channel` is the production S19 boundary
which may turn recurring scheduling authority into durable schedule Drafts.
One call opens one `BEGIN IMMEDIATE` transaction and either commits the
complete materialization run, its plan and outcome provenance, its Draft
schedule rows, their normal schedule-item history, and their immutable origin
records, or rolls all of them back. The explicit-window
`materialize_schedule_batch_with_explicit_window` variant remains a trusted
in-process storage-test seam; it must never be bound to HTTP or another remotely
caller-controlled boundary.

The production caller supplies one canonical channel identity, authenticated
operator attribution, and the server-only source-evidence authority. It cannot
supply a window, rule subset, catalog candidates, prior airings, receipts,
compiler plans, outcomes, occurrence identities, schedule-item identities,
observation time, or occurrence time. Inside the transaction the writer reads
the complete current set of enabled recurring rules for the channel. The batch
must contain exactly one compiled plan for every one of those rules; a partial,
duplicate, missing, stale, or rebound rule set fails the whole write.

The writer derives one common start at the second upcoming UTC minute reported
by the authority. Each enabled rule gets an exact plan window ending after that
rule's own `rolling_window_days`. The durable run window is the union from the
common start through the maximum enabled rule horizon; disabled rules cannot
shorten it. Checked UTC arithmetic fails closed on negative clocks or overflow.

The transaction takes one complete current Published/Ready catalog snapshot,
including its exact S22 metadata provenance, and reuses that snapshot for every
rule. For each rule it reads the exact pinned saved-search and daypart history
revisions and the immutable actual-airing facts covering that rule's complete
avoid-repeat lookback. Prior-airing evidence is independently projected and
authenticated per rule against that rule's exact plan window because its
coverage and source scope can differ. Both candidate and prior-airing receipts
are issued inside the transaction. The
source-evidence authority supplies the observed UTC time; callers cannot submit
or override that clock. The same authority-owned observation is persisted as
the run occurrence time after the writer has rejected an already-past window.

The authenticated batch compiler derives each rule against its exact horizon,
then applies cross-rule precedence once across the complete union of enabled
channel authority. Thus a shorter, higher-priority rule suppresses overlapping
slots inside its own horizon without preventing a longer rule from continuing
beyond it. Every compiled plan retains its rule, saved-search, block, channel,
exact window, source-authority, and authenticated input binding even when the
plan has no slot outcomes. A legitimate zero-outcome run is durable successful
coverage: it stores the run and plan records, creates no Draft, and is not
treated as a capacity failure.

## Deterministic identities and retry

Each proposed occurrence keeps the pure S19 occurrence identity derived from
its rule, channel, and UTC start. Its schedule-item identity is a deterministic
UUIDv8 mapping. The writer hashes the domain
`townlight.schedule-item-id.v1`, the little-endian schema value `1`, the
little-endian byte length of the ASCII occurrence identity, and the occurrence
bytes. It takes the first 16 digest bytes, sets the UUID version nibble to 8,
and sets the RFC variant bits. The writer must be able to recompute this UUID
from the stored occurrence; a mismatch is an integrity collision.

Materialization stores two separate domain-separated SHA-256 identities:

- `request_sha256` covers the exact transaction-reread enabled-rule authority,
  union run window and exact per-rule plan windows, deterministic candidate and
  prior-airing payload digests,
  prior-airing coverage and scope, and the compiler's semantic
  pre-reconciliation plans and outcomes.
- `result_sha256` covers `request_sha256` plus the exact semantic outcomes after
  reconciliation with current schedule rows, lifecycle, ownership, payload,
  and actual-airing authority.

Random receipt identities, issue times, and attestations are retained in the
per-plan audit provenance but are excluded from both semantic retry hashes.
They therefore cannot make an otherwise identical retry appear new. The run
identity is a domain-separated UUIDv8 derived from `result_sha256`.

An exact retry which reproduces both hashes returns `AlreadyApplied` with the
existing immutable run and creates no duplicate history or Draft. The same
source request observed after an existing occurrence is edited, committed,
cancelled, or aired produces a different `result_sha256` and a new run which
records that changed fact. It must not return a stale earlier `Proposed`
result. A run UUID which resolves to different result content, or a stored
semantic hash which disagrees with the reconstructed record, is a hard
collision and rolls back.

## Durable outcomes, collisions, and capacity

Pure compiler outcomes are persisted as `Proposed`, `NoMatch`, `Blocked`, or
`SuppressedByPrecedence`. Before inserting a proposed Draft, the writer
reconciles its deterministic identity and half-open time range with current
schedule, origin, and actual-airing authority. Ordinary conflicts are durable
explicit dispositions rather than destructive updates:

- an exact existing automation Draft is `ExistingAutomationDraftUnchanged`;
- an edited automation Draft is `ExistingAutomationDraftChanged`, retaining
  the proposed and existing payload differences required to explain the
  decision;
- committed, cancelled, and aired occurrences become `ExistingCommitted`,
  `CancelledOccurrence`, and `AlreadyAired` respectively;
- overlap with an existing committed item, manual Draft, or automation Draft
  becomes `SuppressedByExistingSchedule` and records the exact winning item,
  state, and owner class.

Existing schedule precedence is deterministic: committed items outrank manual
Drafts, which outrank automation Drafts; stable schedule ordering breaks ties.
Materialization never overwrites an edited Draft, reopens a committed or
cancelled item, or creates a second row for an already-aired occurrence.

Integrity contradictions are not ordinary dispositions. The complete
transaction rolls back when a deterministic UUID is occupied by a manual row
without automation origin, an origin maps a different occurrence or schedule
identity, provenance exists without its schedule row, the UUID cannot be
recomputed from the occurrence, indexed origin fields disagree with immutable
origin JSON, an origin's creating run is missing or contradictory, or any
current/history/provenance row is malformed. Authority, evidence coverage,
capacity, compiler completeness, and partial-write failures also roll back the
whole batch.

The core is explicitly bounded to at most 64 enabled rules for one channel and
4,096 outcomes for one run. Draft capacity is at most 4,096 automation-owned
schedule rows whose current state is still `Draft` per channel, including the
new proposals. Historical provenance for committed or cancelled items does not
consume active Draft capacity. Collision scans are bounded over actual relevant
overlaps and active automation Drafts rather than over all historical schedule
or provenance rows.

Every run, plan, outcome, and Draft-origin record is append-only. Plan
provenance includes the exact plan window, authority revisions, source-authority digest,
candidate and prior-airing projection digests, receipt digests, coverage,
scope, and combined input binding. Outcome provenance carries stable ordinals,
the owning plan authority, materialization identity where present, and the
final collision disposition. Draft origin permanently links the schedule item
and occurrence to the creating run, exact plan window, rule and block revisions,
materialization, and source bindings.

## Draft-only S4 handoff

The only newly airable-domain objects created here are revision-one
`ScheduleState::Draft` rows. They receive ordinary schedule-item history in the
same transaction, attributed to the authenticated operator. Materialization
does not create a commit report, change a Draft to `Committed`, queue dispatch,
or send a worker command.

Each generated Draft must later pass through the existing S4 prepare and commit
boundary. S4 shows the current conflict, missing-media, duration, and gap plan;
operator approval then reacquires the station write lock, reruns the full gate,
atomically records server-attributed approval, and only then changes that exact
Draft revision to `Committed`. A successful materialization run is therefore
evidence of schedule generation, never evidence of approval, dispatch, or
airing.

## Authenticated operator entrypoint

The implemented HTTP boundary is
`POST /api/v1/scheduling/channels/{channel_id}/materializations`. It requires an
enabled Setup Admin or Meeting Operator account and the normal authenticated,
same-origin, CSRF-protected unsafe-request boundary. The request is bodyless:
the canonical channel identity comes only from the path, and the browser cannot
submit a window, rule subset, candidates, prior airings, evidence, receipt,
actor, or clock. Stationd constructs one source-evidence authority per
console/service boot and the API reuses that same authority across per-request
`Api` reconstruction before calling `StationStore::materialize_scheduled_channel`.
The explicit-window storage seam is not reachable from HTTP.

A newly committed run returns `201` with `applied`; an exact semantic retry
returns the existing immutable run with `200` and `already_applied`. The response
contains the union window, exact per-rule plans, source-provenance digests, and
compiled and reconciled outcomes. It exposes receipt digests for audit but not
raw receipts, receipt identifiers, attestations, source projections, or the
boot authority secret. Stale authority returns a redacted `409`, invalid or
capacity-bounded server derivation a redacted `422`, and storage or integrity
failure a redacted `500`.

The authenticated Schedule screen includes a channel-scoped **Generate
recurring drafts** card. It sends the bodyless request, is enabled only for the
same two writer roles, reports `applied` versus `already_applied`, the union
window and disposition counts, and refreshes the channel schedule after a new
run. Its copy explicitly keeps generated items at Draft and directs operators
through the existing S4 review and commit boundary.

## Boundaries and non-claims

This decision provides the transactional storage writer, deterministic retry
and occurrence mapping, immutable provenance, collision policy, and Draft-only
handoff contract, authenticated bodyless materialization API, and the bounded
operator generation card. ADR 0073 adds a supervised stationd control-plane
worker which uses a successful durable run as a once-per-UTC-day checkpoint and
invokes this same production boundary for each enabled channel with an enabled
recurring rule that lacks the current day's checkpoint. It also adds the atomic,
channel-scoped `ScheduleNoMatch` Warning derived only from final durable
`NoMatch` outcomes.

This does not provide same-day reactivity after a rule, search, block, or
catalog edit; preview or rule simulation; a materialization-run read/list/history
API or screen; filler insertion; bulk approval; automatic commit or dispatch;
installed-machine evidence; or one- and three-station field proof. ADR 0073
defines the distinct typed automatic-run failure Warning. The remaining
capabilities must be implemented and proved separately before S19 or the
product can be called complete.
