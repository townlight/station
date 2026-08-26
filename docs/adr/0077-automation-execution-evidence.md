# ADR 0077: Automation execution evidence

## Decision

TownLight records model execution separately from connection-test authority. An execution request
binds one server-owned job to the exact current feature policy, operator-selected adapter, policy and
adapter revisions, configuration hash, installed candidate, input artifact digest and size, and
request time. Captions accept a media artifact; summaries accept a text-bearing artifact; translation
accepts a language-bound text artifact plus a distinct requested target language.

A successful receipt repeats that complete binding and adds an exact output artifact digest, size,
media type, feature-specific structural facts, bounded execution interval, and an execution-engine
proof. A local proof identifies the executable and model artifact by SHA-256. A cloud proof records
digests of the provider request identity and provider response. Raw prompts, content, credentials,
tokens, provider request identifiers, and provider responses are not receipt fields. Unknown fields
are rejected.

Receipt validation fails closed on stale candidate, policy, adapter, or configuration authority;
input substitution; local/cloud engine mismatch; malformed or empty artifacts; cross-feature output
facts; wrong translation target; or an execution interval longer than 24 hours. These contracts make
it possible for later storage and controller code to preserve exact execution provenance without
turning a connection test into an execution claim.

The single SQLite writer persists exact execution requests as idempotent jobs. Enqueue requires a
current passing connection test. Claim and renewal use a bounded worker identity, attempt count, and
lease, and an expired claim can be reclaimed without letting its old owner complete. Completion
rechecks current policy, adapter, configuration, candidate, and connection-test authority inside the
same immediate transaction that appends the immutable receipt and marks the job complete. Indexed
identity, revision, candidate, input, output, size, and time columns are checked against the stored
JSON on every read. Request-binding columns cannot be updated, terminal job rows and receipt rows
reject update or delete, and reads discover receipts by durable job identity so a corrupted receipt
pointer cannot hide terminal evidence. Only the live exact lease owner can record an attempt failure.
A retryable failure returns the job to Pending until the bounded attempt ceiling; a nonretryable
failure or exhausted ceiling produces an immutable Failed terminal row with a bounded categorical
failure code and no receipt.

## Boundaries and non-claims

This slice does not store credentials, open an artifact, run a local model, call a cloud provider,
assess semantic quality, write an output artifact, expose an HTTP route, supervise a controller, or
prove an installed-machine execution. A durable receipt is exact evidence accepted from a future
trusted executor, not source proof that such an executor exists. Later adapters must hash input and
output from retained managed handles and store secrets only in the Windows-protected credential
authority.
