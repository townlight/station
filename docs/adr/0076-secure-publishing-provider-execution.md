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
YouTube uses fixed Google OAuth and Live Streaming API endpoints plus bounded exact channel,
broadcast, stream, and expected privacy identities. NAS uses an administrator
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
Provider acknowledgements are retained only as exact SHA-256 digests. YouTube refresh-token exchange
uses the fixed Google token endpoint; access tokens remain process-local, bounded, and are sent only
as authorization headers to fixed Google API endpoints. Token refresh and use
remain server-side and protected by the same credential boundary. No shell, PATH discovery, or
browser-supplied executable is involved.

Receipts are immutable and distinguish `attempted`, `uploaded`, `delivered`, `published`, and
`externally_verified`. Portal and YouTube satisfy their required execution gate only when the chain
contains an explicit `published` receipt; a later capability or resource verification cannot skip
that stage. Internet Archive and NAS require an explicit `delivered` stage. These states do not prove
audience reach, playback, editorial correctness, indefinite retention, or field acceptance.

## Failure and testing boundary

The supervised controller records typed durable health/alert evidence on database, claim, adapter,
credential, or fence failures and resolves it only after a later real successful iteration. Private
injected adapters exercise the same production iteration in tests. Candidate, install generation,
account, target, configuration, credential, and media mutation before atomic completion produces no
successful receipt.

No test calls a real provider. Installed-candidate, provider-account, real destination, and field
evidence remain separate acceptance gates.

## YouTube Live execution boundary

The official YouTube contract requires a broadcast bound to exactly one live stream, actual encoder
delivery to the stream's provider-issued RTMPS/HLS ingestion endpoint, observation that the stream is
`active`, transition of the exact broadcast to `live`, and an authoritative read showing the exact
broadcast in `lifeCycleStatus=live` with its configured privacy. TownLight therefore never treats a
channel lookup, OAuth success, resource creation, bind response, `ready` stream, or
`externally_verified` receipt as publication. The current source implements protected OAuth refresh
and exact resource binding verification; until the supervised encoder handoff and live transition
are wired, YouTube execution remains fail-closed and commissioning cannot pass.

Primary contracts: [broadcasts and streams](https://developers.google.com/youtube/v3/live/guides/implementation/broadcasts-and-streams),
[live broadcast lifecycle](https://developers.google.com/youtube/v3/live/life-of-a-broadcast),
[RTMPS ingestion](https://developers.google.com/youtube/v3/live/guides/rtmps-ingestion), and
[server-side OAuth refresh](https://developers.google.com/identity/protocols/oauth2/web-server#offline).
