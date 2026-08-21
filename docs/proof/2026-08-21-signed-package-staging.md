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

## Installed candidate evidence

- Frozen installed source: `b070db1c51072bb7e28493f2d19d8c9f9f8dca7d`
- Installer SHA-256: `C92D827CB55F856DF1A2685ABC4E79381F1DF4D38CA7F90E4585A78CF7DE1A71`
- Installed `stationd.exe` SHA-256: `517C5E282FB3295FD53B2B333F0CADDE5A11190CE606A64A5FBD812E14C2F984`
- Installed `channel-worker.exe` SHA-256: `1B11B408ED256BC1010BEC232635A506B6160EB6169B3F6287439FE4CF6C7595`
- Installed GStreamer runtime SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`
- The installed automatic check accepted the signed `0.2.0` offer and exact release notes while running as LocalSystem.
- Discovery created zero package files. The proof server observed exactly `GET /stable.json` before the explicit operator stage, then exactly `GET /TownLight-Station-0.2.0.tlu` during staging.
- The explicit staging action published 56 bytes at SHA-256 `B24C2696D20E534537D89EA783CA5347F71F8247C840491BF5ADDF93D52ACFEE` as a read-only content-addressed file and persisted the exact record.
- Quarantine contained zero `.part` fragments after success.
- Removing installer-owned trust and restarting the service cleared the latest offer and staged record, resolved the update alert, and returned status to unconfigured.
- Proof cleanup removed the inert staged package and trust fixture. Final inspection found zero staged packages, zero quarantine fragments, and zero firing update alerts.

## Boundary

This proves installed explicit verified package staging. The `.tlu` package remains inert proof bytes; package structure validation, immutable application-slot extraction, maintenance activation, post-update proof, and rollback are not claimed here.
