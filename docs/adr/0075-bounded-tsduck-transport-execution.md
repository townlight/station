# ADR 0075: Bounded TSDuck transport execution

## Decision

`station-transport-executor` is the only execution boundary for the packaged
TSDuck transport lane. Production construction calls
`station-maintenance::read_verified_tsduck_runtime`, which verifies the installed
candidate, fixed `{app}\\runtime\\tsduck` root, pinned official identity, complete
manifest, file hashes, and non-reparse tree before returning the executable
identities; the executor rehashes `bin\\tsp.exe` and `bin\\tsanalyze.exe`
immediately before use. It never
uses `PATH`, a shell, or a command string. Every process invocation receives an
absolute executable and a structured argv array; spawn failure, non-zero exit,
deadline expiry, oversized process output, missing capture, malformed JSON, packet
count mismatch, continuity error, PCR error, missing PAT/PMT streams, or a changed
runtime identity fails closed.

Captures and analyzer output are bounded by the durable authority limits. The
executor persists only typed findings and SHA-256 digests: it never persists raw
process output, executable paths, receiver authorization material, request URLs,
or capture contents. `StationStore` remains the sole receipt writer, so it
revalidates exact channel/sink/target revision, verified candidate version/source
commit, and current configuration hash in its transaction before an append can
succeed. Commissioning continues to consume only that exact-current passed pair.

The receiver adapter supports only the configured HTTPS API profile. It uses a
fixed `/api/v1/townlight/reachability` resource, validated host and port, an
in-memory authorization value supplied by credential provisioning, platform TLS
verification, a ten-second deadline, and an 8 KiB response cap. SNMP/TLS is not
implemented and fails closed. Receiver responses are represented in evidence only
by a SHA-256 identity digest.

## Consequences

The adapter is deliberately not an HTTP API route: an API route can configure and
read the target authority, but cannot submit a passed receipt or start an executor
with caller-provided bindings. A service-owned operator workflow must derive the
binding from the verified candidate and current storage snapshot before calling the
executor. This leaves credential provisioning, managed capture artifact lifecycle,
and a service-owned request queue as explicit next slices; none can be substituted
by browser input.
