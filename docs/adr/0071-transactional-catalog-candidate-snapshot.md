# ADR 0071: Transactional catalog candidate snapshot

## Decision

S19 candidate discovery begins at `StationStore::snapshot_catalog_candidates`,
not in the browser and not from filenames or ingest observations. The adapter
opens one SQLite read transaction and reads the complete bounded current catalog.
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

The snapshot is complete for the single SQLite read transaction and the bounded
authorities described above. It does not issue or verify source-evidence receipts,
choose a rule-specific subset, read saved-search/daypart/rule authority in the
same transaction, read as-run history, materialize occurrences, or write the
schedule. It deliberately excludes media paths and ingest state from both
eligibility and provenance.

A stationd-owned adapter must compose the relevant S19 authority snapshot with
this candidate snapshot, derive the exact materialization scope, and use the one
boot-scoped authority from ADR 0070 to issue an
`AuthenticatedCandidateProjection`. That composition must avoid nested SQLite
transactions and must not let a browser provide candidates, digests, receipt
fields, issuer identity, or time authority.
