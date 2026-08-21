# ADR 0010: Durable background media ingest

## Status

Accepted.

## Decision

Media admission is a daemon-owned background operation, not work performed inside the HTTP request. `POST /api/v1/assets/ingest` validates a caller-generated canonical job UUID and an existing absolute regular-file path, persists a `pending` `MediaIngestJob`, and returns HTTP 202. The job is queryable throughout its lifecycle.

The single station ingest worker atomically claims the oldest pending job as `running`, increments its attempt count, and executes the content-addressed admission boundary. Success commits the measured `ready` asset and `completed` job in one SQLite transaction. Rejection or file failure commits `failed` plus a bounded operator-readable reason and never creates an asset record. A second request using the same job identity receives a conflict instead of duplicating work.

On daemon startup, any job left `running` is returned to `pending`. Content-addressed admission is idempotent, so a crash after the media rename but before database commit safely revalidates the stored object on retry.

## Consequences

Large validation and copy operations no longer block the loopback control plane. A restart cannot strand invisible ingest work or produce a ready asset without a completed job. The current source is an absolute path visible to the local service; multipart browser upload and settled watch-folder discovery will enqueue the same durable job type later.
