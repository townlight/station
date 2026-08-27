# ADR 0079: Immutable media recovery, replacement, and retention intent

## Decision

`MediaAsset.asset_id` remains the SHA-256 identity of immutable managed bytes.
Recovery is not a general path update. The authenticated Setup Admin endpoint
accepts a candidate only for an existing `missing` asset, requires the server to
discover, copy, and hash that candidate into the managed library, and restores
readiness only if the resulting digest is exactly the original asset identity.
The persisted path is the server-produced managed path; the browser's input path
is neither retained as authority nor allowed to declare readiness. The same
transaction appends an immutable recovery record containing the exact admitted
digest, managed path, byte count and duration together with the authenticated
account and server clock. Asset status returns only this server-attributed,
revisioned recovery history.

Replacement creates a different ready immutable asset, then appends a one-time
predecessor/successor link. The link is revision-one only and protected against
updates and deletes in SQLite. It does not alter a schedule item, committed
approval, runtime/on-air selection, as-run record, catalog record, publication
authority, or caption binding. Any later Draft-only rebinding needs its own
transactional authority and is deliberately outside this decision.

Each asset can have one revision-CAS retention policy. A policy is either
permanent or has a review deadline; a legal hold has an explicit held
disposition. Every change is recorded in append-only history. Stationd runs a
named supervised scanner that advances a due retained record to
`review_required`. It cannot delete, move, modify, or un-link media and it
never touches scheduling authority. Each scan selects a bounded due-first page;
permanent and held assets cannot consume that page and starve an overdue review.
Clock, database-open, and scan failures receive bounded exponential retry. A
third consecutive failure terminates the supervised worker, requests global
service shutdown, and therefore relies on the Windows service restart policy;
the API cannot silently remain green after the scanner dies.

The ingest supervisor also runs a bounded review-evidence worker. It opens the
managed source, copies exact verified bytes through that handle to a
worker-owned staging file, derives at most one 320×180 PNG frame within 30
seconds, bounds the PNG at 8 MiB, hashes it, and atomically stores it under its
digest in the station-owned review root. Durable thumbnail evidence binds the
source and PNG digests; authenticated readers can fetch only that verified PNG
through an asset-scoped endpoint. The operator surface can display status and
the evidence, but cannot submit a thumbnail or an output path.

## Boundaries and non-claims

This is source-level recovery, review evidence, and retention authority. It is
not proof of an installed-machine run, automated deletion, retention-law
compliance, or field operation. Managed-root ancestry is rejected when it
contains a reparse point, and source bytes are pinned by an open handle during
thumbnail staging; platform-native directory-handle race resistance remains a
hardening/installed-proof obligation.
