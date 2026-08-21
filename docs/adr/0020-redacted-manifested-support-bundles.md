# ADR 0020 — Redacted manifested support bundles

## Status

Accepted on 2026-08-21.

## Decision

TownLight Station creates support bundles through a dedicated `station-support` service. The exporter reads typed public records from SQLite and never copies the database, its WAL sidecars, directories, environment variables, or unrestricted log files. It cannot call the alert-secret read path.

Each ZIP contains a whitelist of JSON documents: installation identity and installed-artifact hashes; station profile; channel configuration; current safe-to-air state and runtime observations; alert rules and already-redacted alert destinations; reduced recent alert and delivery records; the newest 120 system-resource samples; the newest 100 self-tests; and self-test schedules. Recent alert events omit detail and operator identity. Recent deliveries omit payload, signature, and raw error text. Self-tests retain only the evidence filename, never its full path.

The ZIP uses stored entries so support recipients do not need a specialized decompressor. `manifest.json` identifies the product, bundle, source commit, creation time, explicit exclusion contract, and the byte length plus SHA-256 of every other entry. The completed archive is written to a unique temporary file, finalized, flushed, atomically renamed under the managed data directory, limited to 16 MiB, and hashed again as a whole. At most ten completed bundles are retained; cleanup trouble is returned as a warning without invalidating the newly created artifact.

The loopback API creates an archive and returns its path, hash, contents, exclusions, and a same-origin download route. Download accepts only the generated ASCII bundle-ID grammar and returns `application/zip` with a safe attachment filename. The operator console makes creation and download explicit; it does not automatically upload or transmit the archive.

## Consequences

- Redaction is structural: sensitive fields are absent from export types rather than removed from arbitrary serialized data afterward.
- The archive is independently inspectable and each entry can be verified against its manifest.
- Bounded queries and archive limits prevent diagnostics from becoming an unbounded memory or disk workload.
- Support still lacks unrestricted logs by design. Future structured logging must define a typed redacted event contract before any log window can enter the bundle.
- Authentication and role enforcement remain a separate cross-product security slice; this loopback surface does not claim that work is complete.
