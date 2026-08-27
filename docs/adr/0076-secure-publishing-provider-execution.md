# ADR 0076: Secure publishing provider execution

## Decision

TownLight executes Portal, Internet Archive, Local NAS, and YouTube Live from one durable server-owned job
authority. Local NAS publication uses handle-anchored, no-reparse finalization. The browser may identify an existing
media asset and target in the request path, but cannot submit candidate identity, revisions,
configuration hashes, credential versions, media paths, hashes, provider responses, outcomes,
actor, or time. Those facts are resolved by the server before enqueue and revalidated inside the
completion transaction.

Target execution configuration is typed. Portal uses an explicit HTTPS endpoint without user-info,
query, or fragment. Internet Archive uses the fixed provider adapter and a bounded item identifier.
YouTube uses fixed Google OAuth and Live Streaming API endpoints plus bounded exact channel,
broadcast, stream, and expected privacy identities. A Local NAS target retains an administrator-selected
UNC root and publishes only the exact configured destination `<sha256>.mp4`; it never follows
reparse points or overwrites an existing destination.

The NAS adapter validates the complete existing and missing root ancestry with no-follow metadata,
retains no-delete directory and source-file handles, copies only from the retained source handle,
and hashes the exact bytes before and after a durable flush. A unique job-owned temporary file is
created with `CREATE_NEW`; failures cancel at heartbeat/deadline boundaries and remove that exact
temporary handle. Finalization uses `FileRenameInfo` with replacement disabled, so a destination
created or substituted by another actor fails closed. A successful receipt is only `delivered` for
the exact configured root and media hash; it is not proof of NAS availability, replication, or
operator/field acceptance.

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
that stage. Internet Archive and Local NAS require an explicit `delivered` stage. These states do not prove
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
delivery to the stream's provider-issued RTMPS endpoint, observation that the stream is active and
healthy, transition of the exact broadcast to `live`, complete delivery of the exact media, and an
authoritative terminal read of that exact broadcast. TownLight therefore never treats a channel
lookup, OAuth success, resource creation, bind response, `ready` stream, momentary `live` state, or
`externally_verified` receipt as publication. The production controller refreshes OAuth, verifies the
exact configured resources, and starts the verified installed channel worker under a bounded Windows
Job Object. A bounded stdin-only command supplies the provider-issued RTMPS authority and key without
putting either in argv or the environment. The worker retains no-write/no-delete handles for the
managed media root and exact media file, verifies its byte/hash/duration binding, streams that retained
handle through an in-process GStreamer pipeline, and succeeds only after clean EOS with the same final
byte/hash identity. The controller additionally requires an active, good-health stream with no
configuration issues, transitions the exact broadcast through `live` and `complete`, and appends
`published` only after the full worker result and authoritative exact terminal provider observation.
GStreamer necessarily receives an RTMPS location and may retain arbitrary native copies while the
pipeline runs. TownLight does not claim to overwrite, clear, or otherwise zeroize those third-party
allocations: assigning a later property value cannot prove that an earlier allocation or internal copy
was erased.

The enforceable product boundary is instead one-shot process containment. The parent supplies the
bounded command only through a private stdin pipe, never through argv or environment; it disables
GStreamer debug output, discards worker stderr, bounds stdout to one fixed schema and 4 KiB, and
places the child in a `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` Job Object before giving it the command.
The controller bounds stream-activation and live-observation loops to 60 seconds each with 30-second
HTTP requests, then gives media delivery a hard maximum of 24 hours (including pipeline startup and
teardown). It force-terminates and reaps the Job Object on every timeout, heartbeat failure, or
enclosing error, and the hidden worker exits immediately after its one secret-free result. The worker
also suppresses Rust panic rendering in this mode, uses only fixed failure codes, and zeroizes
Rust-owned secret buffers on every return path. Thus TownLight creates no product-owned log, stderr,
result, receipt, event, or durable diagnostic containing the stream key, and no successful or failed
attempt leaves a worker process alive beyond the controller's containment path. Tests cover the hard
parent delivery deadline, terminal cleanup on controller failures, secret-free result, and one-shot
no-panic-output/exit contract.

This is a precise threat model, not an absolute memory-erasure guarantee. It protects against ordinary
TownLight process persistence and later unprivileged access after the contained worker exits. It does
not protect against an administrator, debugger, kernel or endpoint-security agent, externally enabled
Windows Error Reporting/local crash dumps, hibernation/pagefile forensics, a compromised GStreamer
library, or provider-side retention during the authorized RTMPS session. Those OS- and provider-owned
paths can retain native memory despite process exit and remain explicit residual risk. Tests inject the
HTTP, process, and clock boundaries into this same lifecycle; they do not call Google, start the
installed encoder, or prove a real account, installed candidate, playback, or audience reach.

Primary contracts: [broadcasts and streams](https://developers.google.com/youtube/v3/live/guides/implementation/broadcasts-and-streams),
[live broadcast lifecycle](https://developers.google.com/youtube/v3/live/life-of-a-broadcast),
[RTMPS ingestion](https://developers.google.com/youtube/v3/live/guides/rtmps-ingestion), and
[server-side OAuth refresh](https://developers.google.com/identity/protocols/oauth2/web-server#offline).
