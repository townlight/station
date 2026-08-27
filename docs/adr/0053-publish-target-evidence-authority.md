# ADR 0053: Publish-target evidence authority

## Decision

`station-publishing` owns a pure, server-authoritative domain model for publish-target
accounts, profiles, and bounded connection-test evidence. An account records only the
presence of a credential configuration; it contains no raw secret material. Credential storage
and provider access are separate future boundaries, so returned status records cannot expose a
credential by construction.

Current connection evidence binds one target identity to the account and target revisions,
the target configuration hash, and the exact candidate version and source commit. The model
rejects malformed, duplicate, expired, future-unbounded, or stale evidence. A passing result
is therefore limited to a current connection-test result for that exact configuration and
candidate; it is not a claim that media was published, retained, publicly visible, or
playable.

The required publication model has three status tiers:

- canonical publication: an enabled Portal target with current passing evidence;
- permanence: both enabled Internet Archive and Local NAS targets with current passing evidence;
- reach/capacity: an enabled YouTube Live target with current passing evidence.

Syndication and Podcast targets are independently visible but optional. Their passing status
cannot supply, replace, or upgrade any required tier. Missing, disabled, credential-unavailable,
failed, stale, expired, or invalid required targets fail closed as `blocked`; a valid target that
has not been tested is `unknown`.

## Boundaries and non-claims

This ADR defines the original connection-evidence slice. That slice, by itself, has no provider
adapter, network access, SQLite persistence, API, UI, job queue, media transfer, retention verifier,
or live-stream capacity probe. The later execution authority in ADR 0076 adds protected credentials,
durable jobs, provider adapters, immutable execution receipts, API, and operator workflow without
changing the narrower meaning of the connection evidence defined here. A connection-test result
still cannot substitute for an exact current execution receipt, and configured credentials alone
still prove nothing about provider access.

The model does not establish that the Portal is a public canonical record, that Internet Archive
and NAS copies are bit-for-bit peers, or that YouTube can currently ingest or reach viewers.
Those claims require provider-specific execution and independently persisted acceptance or transfer
evidence. ADR 0076 defines that later authority and its remaining installed/provider/field proof
boundaries. This crate also does not decide publication policy for an individual program, provide
audit overrides, or let callers manufacture a pass by selecting an optional target.
