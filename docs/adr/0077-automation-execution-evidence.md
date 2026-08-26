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

## Boundaries and non-claims

This slice is a pure domain contract. It does not store credentials, enqueue or lease work, open an
artifact, run a local model, call a cloud provider, assess semantic quality, write an output artifact,
or prove an installed-machine execution. Later adapters must hash input and output from retained
managed handles, store secrets only in the Windows-protected credential authority, and append a
receipt atomically with successful job completion.
