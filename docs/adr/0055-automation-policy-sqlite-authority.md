# ADR 0055: Automation policy SQLite authority

## Decision

The single `StationStore` SQLite writer persists automation adapter descriptors, one policy per
automation feature, and append-only connection-test evidence. Adapter and policy writes are
compare-and-set revisions: creation requires revision one and no expected revision; an update
requires the current revision and advances it by exactly one. A policy write rejects a selected or
advisory adapter reference that is missing, malformed, or unsupported for the policy feature.
It also rejects rebinding an existing policy identity to another feature. Every accepted adapter
and policy revision is snapshotted in append-only history with the server-supplied actor and time.
History reads are bounded to 128 entries and reject malformed attribution; authority lists fetch one
extra row and fail rather than silently truncating more than 64 adapters or three policies.

Evidence is accepted only when its exact policy, selected adapter, revisions, configuration hash,
candidate version/source commit, and non-future time window are current at append time. The evidence
table rejects duplicate evidence identities and database triggers reject update or delete. Old
evidence remains history after a policy or adapter revision advances, but cannot be treated as
current by a later reader.

Every stored JSON record is read alongside its indexed identity, revision, feature, candidate, and
time columns; a disagreement fails closed. This prevents a corrupted JSON payload from borrowing
authority from a neighboring indexed row.

## Boundary

This storage slice has no HTTP route, authentication middleware, model call, provider adapter,
credential material, download, or execution. A later authenticated API boundary must derive the
verified installed candidate and actor identity server-side before invoking these methods; callers
must never supply a secret or self-assert a model execution result.
