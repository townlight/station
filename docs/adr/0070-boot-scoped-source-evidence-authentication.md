# ADR 0070: Boot-scoped scheduling source-evidence authentication

## Decision

`station-source-evidence` defines the production cryptographic mechanism for S19
candidate and prior-airing projection receipts. One authority instance owns a
random 256-bit HMAC-SHA-256 key, a random version-4 issuer identity, and a UTC
origin advanced only by the process monotonic clock. Public issuance obtains its
own issue time. Production verification ignores the observation timestamp carried
by the pure S19 verifier trait and observes the authority clock independently, so
a caller cannot extend or resurrect a receipt by supplying an earlier wall time.
The key is zeroized when the authority is dropped and is never persisted to
browser state, SQLite, logs, support bundles, configuration, or a long-lived file.

The authority issues a distinct random receipt identity for one exact evidence
kind, source scope SHA-256, canonical projection SHA-256, issue time, and expiry.
The HMAC message is domain-separated and length-framed. Receipt lifetime must be
positive and no longer than five minutes. Scope and canonical-projection digests
are separate validated types at issuance. Verification repeats the exact binding,
rejects issuer/scope/payload/kind/time rebinding, and uses the MAC implementation's
constant-time verification.

Constructing a replacement authority rotates both key and issuer. A receipt from
the earlier instance therefore fails verification and the caller must build a
fresh authoritative snapshot. Reuse of an immutable receipt for the same exact
scope and payload is allowed only during its short validity interval, matching
ADR 0066.

## Boundaries and non-claims

This crate authenticates bytes and scope; it does not establish that the bytes
came from a complete catalog or as-run snapshot. The trusted adapters must first
construct canonical, bounded projections from transactionally read authority and
must use the public S19 digest domains. Browser payloads cannot provide receipts,
keys, issuer identities, issue times, expiry times, or attestation values.

Stationd now constructs exactly one authority in each console or service boot and
passes that same `Arc` into the long-running API server. Per-request `Api`
reconstruction clones the stationd-owned `Arc`; it does not rotate the key or
issuer. A server-only adapter consumes the storage-owned one-snapshot S19
authority/catalog/as-run projection, derives the two public scope/payload pairs,
and can issue distinct `AuthenticatedCandidateProjection` and
`AuthenticatedPriorAirings` receipts. No HTTP route accepts candidates, as-run
facts, authorities, digests, receipt fields, issuer identity, or time authority.

The crate and this wiring do not provide preview, simulation, materialization
persistence, schedule writes, key recovery, cross-boot receipt continuity, UI,
installed-machine evidence, or field proof. No production materializer consumes
these receipts yet. This proves boot-scoped paired source-evidence issuance, not
that production materialization is complete.
