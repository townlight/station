# ADR 0029: Safe CG feed-ingestion boundary

Date: 2026-08-21

Status: Accepted

## Context

CG feeds are operator-configured network inputs, so a malformed document, private-network target,
credential mistake, slow provider, or transient outage must not compromise the station or make the
playout path dependent on a live request. Feed parsing and network transport also need independent
offline tests. The current authority and snapshot layers must remain deterministic when a feed is
unavailable.

## Decision

Feed adapters are pure parsing/normalization components behind an injected network transport. The
transport accepts HTTPS URLs only, rejects userinfo and malformed targets, refuses redirects, and
enforces bounded connect/total/read-idle timeouts, response bytes, item count, and field lengths.
Hostname resolution must classify every resolved address as publicly routable and pin the approved
resolved address for the connection; this closes the DNS-rebinding gap between an SSRF preflight and
the actual request. Private, loopback, link-local, multicast, reserved, and unspecified addresses
fail closed.

Credentials are supplied through an injected provider using an opaque persisted credential reference.
They never appear in feed URLs, authority records, snapshots, logs, errors, or API responses. CalDAV
and provider-authenticated weather/social sources therefore require an explicit provider profile and
operator configuration; arbitrary endpoint-specific behavior is not inferred from a URL.

The durable cache stores normalized items, body digest, fetch time/expiry, `ETag`, `Last-Modified`,
last-success, and bounded error state per feed. A fresh cache avoids a request; validators permit a
`304 Not Modified` refresh; a transport or parse failure preserves the last successful items and
marks the result stale. A feed with no successful cache resolves to an explicit unavailable-data
placeholder. Retry/backoff and scheduling belong to the station scheduler, not the parser or media
worker.

RSS/Atom parsing uses a reviewed, exact-pinned XML dependency when introduced; no ad-hoc XML parser
is accepted for production. iCalendar parsing is shared by iCal and CalDAV. Normalized item identity
is source-namespaced and content-addressed so feed-item approvals remain stable and collision-safe.

## Consequences

Feed failures cannot block snapshot generation or silently replace known-good content. The same
adapter/parser tests run against fixtures without network access, while the transport can be tested
separately for caps, redirect refusal, address policy, validators, and credential redaction. Stale
status is visible to the operator and remains distinct from an empty, never-successful feed.

The boundary requires a small network client capable of connecting to the approved resolved address
while preserving TLS host identity. Provider profiles and machine-protected credential storage are
additional configuration work, not parser behavior.

## Explicit non-claims

This ADR does not claim live connectivity or provider compatibility for any RSS, iCal, CalDAV,
weather, or social service. It does not claim provider credentials, rate-limit agreements, live DNS/
TLS behavior, authenticated CalDAV REPORT behavior, or a field-station soak. Those require recorded
provider-specific configuration and separate machine/field proof. It also does not claim that a
feed-enabled zone is on-air until the snapshot-to-renderer integration is proven.

