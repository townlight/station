# ADR 0017: Rule-owned alert policy and bounded re-alerting

## Status

Accepted — 2026-08-21

## Context

An operational condition producer knows what failed, but it must not independently decide who gets paged, how severe the page is, or how often it repeats. Embedding those decisions in channel supervision would make policy changes risky and would recreate alert storms from five-second health samples. The existing delivery uniqueness constraint also allowed only one firing delivery per event/destination, so it could not preserve an auditable series of bounded re-alerts.

## Decision

SQLite seeds one validated rule for every registered alert condition. A rule owns enabled state, severity, selected destination IDs, output-channel scope, dedupe window, re-alert interval, resolution notification, and operator/time audit fields. An empty destination selection deliberately means every enabled destination, giving a newly commissioned station a safe routing default; selecting IDs narrows the route. Rules are listed and edited through the loopback API and Health console. Condition and rule identity are immutable, destination references must exist, update time cannot move backward, and all rate windows are bounded.

The rule is matched inside the same immediate transaction that observes an alert. A disabled or out-of-scope rule creates no firing incident. A matched rule replaces caller-suggested severity before persistence and owns first-fire routing. Repeated observations update the one firing event. They create no delivery until each selected destination's latest firing/re-alert record reaches the configured interval. Pending or leased work blocks another page; sent work starts the next interval at provider acceptance, while terminal dead-letter starts it at terminalization so a persistent condition can try a new bounded cycle later.

Re-alerts are a distinct immutable notification kind and receive time-derived SHA-256 delivery identities. A partial unique index continues to enforce exactly one initial firing, resolution, or test per event/destination while permitting multiple paced re-alert rows. The installed-database migration rebuilds only the delivery table, preserves all historical rows, and seeds rules idempotently.

Resolution always closes the firing event, even if policy changed while it was active. A resolution page is created only when the current matching rule is enabled and `notify_on_resolve` is true.

## Consequences

- Five-second failure sampling cannot page faster than policy allows.
- Original sends, retries, dead letters, and every later re-alert remain independently auditable.
- Operators can change routing and rate policy without restarting the service or editing code.
- The persisted `dedupe_window_seconds` remains part of the product contract; firing-event identity currently provides stronger dedupe until explicit resolution.
- Resource sampling, scheduled self-tests, update alerting, and credentialed external-provider receipt remain separate proof boundaries.
