# ADR 0071: Transactional catalog candidate snapshot

## Decision

Authenticated S19 materialization evidence begins at
`StationStore::snapshot_materialization_evidence`, not in the browser and not
from filenames or ingest observations. Its catalog portion is the complete
bounded current catalog snapshot described below. The standalone
`StationStore::snapshot_catalog_candidates` method remains available only as an
unauthenticated/testing/diagnostic projection; it must not be used as an
authenticated production materialization input. The adapter opens one SQLite
read transaction and reads the complete bounded current catalog.
It includes only records whose validated current row equals the exact latest
history snapshot, whose editorial state is Published, and whose corresponding
validated media authority exists with explicit Ready state.

For every eligible asset, the adapter reads all exact S22 asset-metadata records
and their referenced historical schema revisions inside that same snapshot. It
projects only schema-declared search facets and preserves text, signed number,
signed datetime, boolean, and controlled-choice key types. Duplicate facet keys,
missing history/schema authority, malformed rows, or any configured capacity
overflow reject the entire snapshot rather than omitting or truncating evidence.

Each candidate carries a domain-separated SHA-256 provenance digest over the
catalog identity/revision/editorial values, executable media identity/duration/
readiness, and ordered S22 record/schema revisions and projected typed facets.
Candidates are ordered by asset SHA-256. The returned whole-projection digest is
the public S19 canonical candidate-projection v3 digest, including for an empty
result.

## Boundaries and non-claims

`StationStore::snapshot_materialization_evidence` composes this catalog adapter
with S19's current recurring rule, its exact pinned saved-search and daypart
history revisions, and the exact prior-airing coverage window in one SQLite read
transaction. The rule is read first; malformed, missing, stale, rebound, or
inconsistent authority rejects the whole result. Raw immutable as-run facts are
read with a hard 128-record limit; only the resulting canonical `PriorAiring`
projection deduplicates `(asset_sha256, aired_at_utc_ms)` keys. It returns both
public canonical projection digests, including for empty catalog and as-run
evidence.

The server-only station API adapter accepts only a rule identity and a
server-owned materialization window, derives the exact public scopes, and uses
the one stationd-owned boot authority from ADR 0070 to issue distinct
`AuthenticatedCandidateProjection` and `AuthenticatedPriorAirings` receipts. No
HTTP route lets a browser provide candidates, airings, authorities, digests,
receipt fields, issuer identity, or time authority. This does not materialize
occurrences, write the schedule, or prove installed/field behavior; it only
closes the trusted one-snapshot source-evidence boundary.
