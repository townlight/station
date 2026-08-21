# ADR 0016: Durable operator alert delivery

## Status

Accepted — 2026-08-21

## Context

Recording an incident is not enough for an unattended station. A process crash between incident persistence and provider submission must not lose the page, repeated health samples must not page every five seconds, and a provider failure must remain visible after restart. Provider credentials must be decrypted only at the narrow delivery boundary.

## Decision

First observation and resolution are routed in the same SQLite immediate transaction that commits the event change. A SHA-256-derived identity plus a unique `(event, destination, notification kind)` constraint makes each firing or resolution delivery idempotent. The delivery row holds an immutable event payload snapshot but no destination or provider credential. Repeated firing observations update the event occurrence count without creating another job.

Delivery uses a durable `pending → leased → sent | pending | dead_letter` state machine. Claiming increments the attempt count and sets a bounded lease. A process that dies during provider I/O leaves a leased row; the next worker recovers it after expiry and records that recovery before retrying. Provider failure applies a 120-second bounded exponential backoff and the fifth failed claim is terminal dead-letter. A terminal row cannot later be changed to sent.

Critical alerts ignore destination quiet hours. Warning and informational jobs created inside a quiet window receive a `next_attempt_at` equal to the UTC window end and are never dropped.

The service-owned worker decrypts target and credentials only after claiming a due job. Email uses authenticated SMTP: remote relays require implicit TLS, while plaintext SMTP is permitted only to loopback proof relays. SMS uses a Twilio-shaped form POST with Basic authentication; remote endpoints require HTTPS. Webhooks send a versioned JSON event snapshot with HMAC-SHA256 over the exact body. HTTP redirects are disabled so signed payloads or authorization cannot move to another origin. HTTPS certificate validation uses the Windows platform verifier.

The operator can explicitly queue a `test` notification for any configured destination, including one disabled for incident routing. The test request atomically records a resolved informational audit event and a normal durable delivery job; it cannot create a false firing incident or bypass the delivery ledger. The public API and console expose redacted destination metadata and delivery state/attempts/errors, never the target or credential.

## Consequences

- Database restart and process death do not silently discard queued work.
- Delivery state proves provider acceptance attempts, not human receipt or response.
- Destination changes affect a pending delivery's current transport target, while its incident payload remains the immutable event-time snapshot.
- Provider errors exposed to persistence are bounded and sanitized; target and credential values never enter delivery rows or error strings.
- Alert-rule editing, re-alert-after policy, human receipt, and installed external-provider proof remain later boundaries and are not claimed by this ADR.
