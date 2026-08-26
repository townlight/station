# ADR 0076: Secure publishing provider execution

## Decision

TownLight executes the four required S3 publishing targets—Portal, Internet Archive, Local NAS,
and YouTube Live—from one durable server-owned job authority. The browser may identify an existing
media asset and target in the request path, but cannot submit candidate identity, revisions,
configuration hashes, credential versions, media paths, hashes, provider responses, outcomes,
actor, or time. Those facts are resolved by the server before enqueue and revalidated inside the
completion transaction.

Target execution configuration is typed. Portal uses an explicit HTTPS endpoint without user-info,
query, or fragment. Internet Archive uses the fixed provider adapter and a bounded item identifier.
YouTube uses the fixed provider adapter and a bounded channel identity. NAS uses an administrator
selected UNC root; execution opens only managed descendants, rejects reparse points throughout the
existing path, copies to a unique partial file, flushes, rehashes, and atomically renames without
following caller-controlled paths.

Provider credentials have a separate append-only authority. Setup Admin may provision, rotate, or
delete a write-only secret for one exact account revision. Windows DPAPI Local Machine protection is
applied before SQLite. Only protected bytes and non-secret attribution/version facts are durable;
API responses, audit, status, receipts, support data, and errors never contain plaintext secrets or
provider response bodies. `credential_configured` is derived from the current decryptable authority,
not accepted as browser truth.

Jobs are bounded, leased, recoverable, and retry transient failures with durable backoff. Production
HTTPS adapters use platform certificate verification, no redirects, fixed provider paths, bounded
request/response sizes and deadlines, and an authorization header scoped to exactly one request.
Provider acknowledgements are retained only as exact SHA-256 digests. YouTube token refresh and use
remain server-side and protected by the same credential boundary. No shell, PATH discovery, or
browser-supplied executable is involved.

Receipts are immutable and distinguish `attempted`, `uploaded`, `delivered`, `published`, and
`externally_verified`. Portal and YouTube satisfy their required execution gate only at `published`
or stronger; Internet Archive and NAS require `delivered` or stronger. These states do not prove
audience reach, playback, editorial correctness, indefinite retention, or field acceptance.

## Failure and testing boundary

The supervised controller records typed durable health/alert evidence on database, claim, adapter,
credential, or fence failures and resolves it only after a later real successful iteration. Private
injected adapters exercise the same production iteration in tests. Candidate, install generation,
account, target, configuration, credential, and media mutation before atomic completion produces no
successful receipt.

No test calls a real provider. Installed-candidate, provider-account, real destination, and field
evidence remain separate acceptance gates.
