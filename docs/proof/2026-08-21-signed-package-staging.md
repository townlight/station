# Signed-package staging proof — 2026-08-21

## Source evidence

- Package staging is reachable only through explicit `POST /api/v1/updates/stage`; hourly discovery never invokes it.
- The coordinator accepts only the current installed version's durable `update_available` record, the active release channel, and a key identifier still present in installer-owned trust. Expired manifests and updater-too-old offers cannot stage.
- Download uses the Windows platform verifier for remote HTTPS, a ten-minute global ceiling, zero redirects, and the same bounded release client identity as discovery. Loopback HTTP remains proof-only.
- A declared `Content-Length`, when present, must equal the signed size before body transfer. Streaming stops at signed size plus one byte, requires the exact final byte count, hashes bytes while writing, and compares SHA-256 before publication.
- Bytes first enter a unique managed `quarantine/*.part` file. Any network, size, write, or digest failure removes that file and creates no staged record.
- Verified bytes are atomically renamed to `staged/<lowercase-sha256>.tlu`, synchronized before rename, and made read-only. An existing content address is fully re-hashed before idempotent reuse.
- SQLite stores one validated newest staged record only after filesystem publication. Status exposes it only while it exactly matches the current verified offer and the content-addressed file still exists at the signed size.
- Removing release trust clears both source-specific check and stage authority; no stale staged offer remains actionable.
- The loopback server admits at most sixteen concurrent requests and opens an independent full-mutex store per request. A regression test holds one request incomplete while a second `/health` request completes, proving that a staging transfer cannot monopolize health or the operator API.
- Unit tests exercise exact streaming, content-addressed reuse, a real signed-feed-to-real-package coordinator flow, restart persistence, stale-write refusal, and quarantine cleanup after digest mismatch.

## Boundary

This proves source behavior and loopback tests for explicit verified package staging. The current installed candidate does not yet contain this slice. The `.tlu` package is retained as inert bytes; package structure validation, immutable application-slot extraction, maintenance activation, post-update proof, and rollback are not claimed here.
