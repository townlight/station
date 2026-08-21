# Runtime health and incident proof — 2026-08-21

## Scope

This proof covers serialized first-run database initialization, channel-worker liveness, monotonic runtime heartbeats, safe-to-air derivation, durable incident deduplication/resolution/acknowledgment, bounded restart pacing, API transport, and responsive operator presentation.

## Automated evidence

- Pure-domain tests prove healthy fallback is green, a heartbeat older than fifteen seconds is red, critical incidents escalate station color, and impossible alert lifecycles are rejected.
- SQLite tests prove heartbeat time cannot move backward and runtime truth survives reopen. Repeated failure observations create one firing event whose occurrence count advances. Acknowledgment does not resolve; recovery resolves; a later failure creates a fresh incident.
- The daemon proof launches with a deliberately missing worker executable. Two conditions fire (`encoder-death` and `off-air`), each remains a single event across multiple paced attempts, and runtime state remains red/error.
- The schedule-to-air daemon proof still loads, takes, returns to fallback, completes, and shuts down after the runtime instrumentation was added.
- API tests prove safe-to-air status, alert listing, acknowledgment, and query-bearing console deep links.
- A startup regression creates and reopens a completely fresh database before subsystem concurrency begins.

## Live machine evidence

A local station process was started from fresh state, commissioned through its own loopback API, and given one enabled UDP proof channel. Its real child worker reached `healthy_fallback`; safe-to-air returned green with a zero-second heartbeat age. A 1440×1000 Chrome render showed the green runtime banner, channel card, and empty active-alert state.

An isolated copy of the same station binaries was then used for a failure proof. The child process was identified by its verified parent process, terminated, and its proof-only executable removed so automatic restart could not immediately recover. Runtime changed to red and exactly two firing incidents appeared. A 500×1000 Chrome render showed the phone layout with the red safe-to-air banner, failed channel, both critical incidents, and acknowledgment controls. All temporary station state and copied binaries were removed afterward.

## Gate result and boundary

The final implementation passed the production frontend build, a high-severity dependency audit with zero vulnerabilities, Rust formatting, warnings-denied Clippy, the nineteen-requirement installer contract, and all 70 workspace tests after documentation and operator-copy cleanup. Repository scans also found no old-product name references and no external operator-console URLs.

This is machine proof for local runtime health and durable incidents. It is not proof of email, SMS, or webhook delivery; resource sampling; daily/weekly self-tests; support-bundle export; update alerting; a long-duration soak; or the one-station/three-station field gates.
