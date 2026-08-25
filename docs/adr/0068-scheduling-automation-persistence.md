# ADR 0068: Scheduling-automation persistence

## Decision

`station-storage` is the single SQLite writer for the current and historical
authority of S19 saved searches, daypart blocks, and recurring rules. Each write
runs under `BEGIN IMMEDIATE`, requires compare-and-set revision authority, accepts
only the next successor revision, writes the current row and append-only historical
snapshot atomically, and records a separately supplied actor and event time. Search
identity, owner, and creation time cannot be changed; its name and query can. Daypart
and recurring-rule channel bindings cannot be changed. A recurring rule also permanently binds its
saved-search and block identities, and every write must name their exact current
revisions.

Saved searches are listed station-wide rather than by channel. Their current and
historical rows index identity, name, owner, creation time, and revision beside
canonical JSON. Reads reject
indexed/JSON disagreement, malformed records, missing exact rule dependencies, and
any current row that is not equal to the actual latest valid historical snapshot.
Historical reads validate exact revision snapshots. History tables reject updates
and deletes with database triggers; current rows cannot be deleted. List and history
queries are bounded, creation capacity is checked inside the same write transaction,
and conflict, missing-dependency, stale-dependency, rebound, capacity, invalid-input,
and storage failures remain typed.

The storage API validates a recurring-rule contract before dereferencing its saved
search or block, so malformed caller input cannot be misreported as a storage or
dependency failure. Unix epoch zero remains a valid timestamp, consistent with the
existing storage convention; the authenticated API must derive real operator event
times server-side.

## Boundaries and non-claims

This persistence slice stores only saved-search, daypart-block, and recurring-rule
authority. It does not resolve catalog candidates, index metadata facets, authenticate
publication facts or prior airings, persist occurrence/materialization decisions,
simulate schedules, write Draft schedule items, emit no-match filler alerts, commit
through S4, dispatch workers, or provide API, UI, runtime, installed-machine, or field
proof. Indexed/JSON parity and append-only triggers detect accidental or unauthorized
SQL mutation through the normal database contract; they are not a signature-backed
defense against an attacker who can replace the database or deliberately drop its
triggers and coherently rewrite every copy of a record.

The pure v3 materialization contract now binds each plan to exact candidate and
prior-airing payload/receipt digests. A later authority must produce, authenticate,
and persist those projections and receipts before runtime materialization can be
claimed. Empty or browser-supplied candidate/as-run inputs are not evidence.
