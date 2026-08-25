# ADR 0064: Optional-subscription authority

## Decision

`station-subscriptions` owns a pure, fail-closed optional-subscription contract for
default-off plans, entitlement lifecycle intent, hosted-checkout intent, digest-only
magic links, signed-webhook verification intent, and immutable receipts. A plan must
be created at revision one with checkout disabled. Only a later explicit CAS update
can enable hosted-checkout intent. An entitlement is created disabled, binds one
account and plan permanently, and uses exact plan revisions. Its lifecycle permits
only bounded Disabled, PendingCheckout, Active, Suspended, Cancelled, and Expired
transitions; Cancelled and Expired are terminal and cannot be reanimated.

Hosted-checkout intent names only exact plan/entitlement revisions and a request time.
It has no payment-provider URL/session/customer token/card field. A magic link holds
only a SHA-256 token digest with a bounded expiry. A redemption request supplies only
the matching digest and may append one receipt before expiry; a second receipt for the
same link fails closed. A signed-webhook verification intent stores delivery and
signature digests only. Delivery-digest and verification identities are append-only,
so webhook replay fails before any future verifier runs.

All writes use creation-at-revision-one or exact compare-and-set replacement. Plan,
entitlement, and account identities cannot be rebound. Receipt identity and receipt
subject are each append-only and unique. Receipt outcomes express only acceptance or
rejection for future processing; they are not checkout, email, signature-verification,
payment, refund, or entitlement-delivery evidence.

## Boundaries and non-claims

This crate has no persistence, authentication, API, UI, provider adapter, hosted
checkout call, raw card handling, token generation, email sender, webhook receiver,
signature verifier, payment processor, entitlement enforcement runtime, or audit
delivery. It does not establish that a customer paid, a checkout page opened, an email
arrived, a webhook came from a provider, a signature is valid, or a subscriber received
access. Later slices must persist revisions and immutable receipts through the single
SQLite writer, obtain actors/time server-side, protect provider credentials and raw
tokens outside this authority, execute and verify provider exchanges, enforce access,
and gather installed-machine and field evidence.
