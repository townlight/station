# ADR 0014: Runtime health and incident authority

## Status

Accepted — 2026-08-21

## Context

A channel can be healthy while showing fallback between programs, and it can be dead while no schedule command is pending. Treating all non-program output as failure creates false alarms; checking a worker only when sending a command leaves an unattended failure invisible. Logs alone cannot support operator acknowledgment, deduplication, recovery proof, or restart-safe status.

A brand-new station also has three subsystems that open the database immediately: HTTP, media ingest, and channel orchestration. Allowing all three to initialize schema concurrently can make first startup fail with a locked database.

## Decision

`stationd` initializes the complete SQLite schema once before starting any subsystem threads. The orchestration loop is the sole producer of channel-runtime observations. Each enabled channel records a monotonic observation after the worker's durable `Ready` handshake, after every on-air/fallback transition, and at least every five seconds. The state is one of `starting`, `healthy_fallback`, `on_air`, `error`, or `stopped`; only `on_air` can carry an asset identity.

Worker process liveness is polled every orchestration iteration even when no dispatch work exists. A heartbeat older than fifteen seconds fails red. Verified fallback is green. Process/IPC failure records red runtime truth and two durable critical incidents: `encoder-death` and `off-air`. A partial unique index permits only one firing event for a condition/resource pair. Repeated observations advance its time and occurrence count; they do not create an alert storm. A successful worker restart resolves both incidents. Acknowledgment records operator/time independently from resolution.

Restart attempts use bounded 1, 5, 15, then 60-second delays. The loopback API derives overall safe-to-air color as the worst enabled channel, escalated by firing critical or warning alerts. Query-bearing console URLs continue to serve the embedded shell so phone/desktop health deep links work without a separate web server.

## Consequences

- Unattended worker death is visible without waiting for the next scheduled item.
- Healthy fallback is not mislabeled as off-air.
- Restart storms are paced and incident storms are deduplicated.
- Runtime truth and acknowledgment survive service and PC restarts.
- Encrypted/redacted external alert destinations attach to the durable event boundary without changing channel supervision; ADR 0015 owns that storage contract.
- Email/SMS/webhook dispatch and delivery receipts, system-resource samples, scheduled self-tests, support bundles, and update alerts are explicitly not claimed by this ADR.
