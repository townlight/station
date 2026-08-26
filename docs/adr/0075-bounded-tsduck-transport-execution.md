# ADR 0075: Bounded TSDuck transport execution

## Decision

`station-transport-executor` exposes the stationd controller entry point for the
packaged TSDuck transport lane. Its implementation is compiled inside
`station-storage`, where claim, capture attachment, lease renewal, failure, receipt
append, and completion are private methods unavailable to any other production
crate. Production construction calls
`station-maintenance::read_verified_tsduck_runtime`, which verifies the installed
candidate, fixed `{app}\\runtime\\tsduck` root, pinned official identity, complete
manifest, file hashes, and non-reparse tree before returning the executable
identities; the executor rehashes `bin\\tsp.exe` and `bin\\tsanalyze.exe`
immediately before use. It never
uses `PATH`, a shell, or a command string. Every process invocation receives an
absolute executable and a structured argv array; spawn failure, non-zero exit,
deadline expiry, oversized process output, missing capture, malformed JSON, packet
count mismatch, continuity error, PCR error, missing or ambiguous PAT/PMT/service
binding, or a changed runtime identity fails closed. The sealed analyzer input and
its directory ancestry remain open without write/delete sharing for the complete
child lifetime. `tstables` JSON is returned through the bounded stdout pipe rather
than a replaceable output pathname.

Captures and analyzer output are bounded by the durable authority limits. The
executor persists only typed findings and SHA-256 digests: it never persists raw
process output, executable paths, receiver authorization material, request URLs,
or capture contents. The private storage-owned controller remains the sole receipt
writer, so it
revalidates exact channel/sink/target revision, verified candidate version/source
commit/install generation, sealed capture bytes/hash/duration, analyzer and tables
digests, and current configuration hash in its transaction before an append can
succeed. Renewable leases cover child execution and bounded post-exit pipe drain;
expired work revokes its execution capability before recovery. Commissioning
continues to consume only that exact-current passed pair.

The receiver adapter supports only the configured HTTPS API profile. It uses a
fixed `/api/v1/townlight/reachability` resource, validated host and port, an
in-memory authorization value supplied by credential provisioning, platform TLS
verification, a ten-second deadline, and an 8 KiB response cap. SNMP/TLS is not
implemented and fails closed. Receiver responses are represented in evidence only
by a SHA-256 identity digest.

## Consequences

The adapter is deliberately not an HTTP API route. An authenticated Setup Admin can
request a server-derived queue job and read its status, but no route accepts
candidate, target, artifact, timestamp, finding, or passed-receipt authority. Legacy
queue rows without an install generation are terminalized atomically so they cannot
starve current work. Managed capture quarantine is ownership-marked, structurally
and temporally bounded, and crash tombstones are recovered without following
reparse points.
