# ADR 0069: Catalog editorial authority

## Decision

`station-catalog` owns the pure editorial authority for one content-addressed
station asset. The asset identity is an immutable lowercase SHA-256 digest;
every successor revision must preserve that digest. A new record is always
revision one and starts as `Draft`.

Editorial state is explicit and fail-closed:

- `Draft` has no publication timestamp.
- `Published` has a nonnegative first-publication Unix timestamp.
- `Withdrawn` retains that same first-publication timestamp and is not an
  eligible published candidate.

The first `Draft` to `Published` successor binds its publication timestamp to
the trusted server occurrence time supplied by the authority boundary. Later
withdrawal, publication, and republication successors preserve the original
first-publication timestamp; republication does not silently create a new
publication time. A future requirement for distinct republication times must
add a separate event/history field.

Editorial titles and meeting-body text are bounded visible UTF-8 text of at
most 240 bytes. Limits are byte limits, matching the scheduling candidate's
fail-closed UTF-8 byte-bound behavior. Values must be nonempty, trimmed, and
free of control characters.

The pure successor operation requires compare-and-set against the current
revision and accepts only the next revision. It validates the complete
record, rejects content-identity rebinding, rejects invalid state transitions,
and rejects publication-time rebinding. Unknown serialized fields are rejected
at the record boundary.

## Persistence and trust boundary

The pure crate does not own clocks, actors, authentication, or SQLite.
`station-storage` provides the single-writer boundary: it accepts separately
derived event time and actor, performs the compare-and-set under `BEGIN
IMMEDIATE`, and atomically persists the current record with an append-only
historical snapshot. Reads validate JSON immediately, cross-check indexed fields,
and require current/latest-history parity. Current listings use bounded exclusive
SHA-256 keyset pages, and bounded history reads return newest revisions first.
Storage also rejects an event time that precedes the asset's latest historical event; the pure record stores
first-publication time, not a complete transition journal.

## Non-claims

This crate does not provide or infer:

- media readiness, duration, file paths, ingest completion, or playback
  capability;
- S22 metadata schema identity/revision, asset-record revision, projected
  facets, or their provenance digest;
- the trusted S19 candidate adapter that joins catalog, media-lifecycle, and
  S22 authority into an authenticated candidate projection;
- as-run history, prior-airing evidence, receipt issuance or verification;
- saved-search resolution, schedule materialization, or schedule writes;
- an API, UI, or operator authentication; or
- installer, installed-machine, live-station, or field evidence.

Catalog publication state must not be inferred from a filename, directory,
ingest completion time, or an untrusted client payload. A later adapter may
consume this authority, but it must bind the exact catalog revision and all
other source revisions into the candidate authority required by S19/ADR 0066.
