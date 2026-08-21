# ADR 0025: Required SRT caller output

Date: 2026-08-21

Status: Accepted

## Context

Stations need a reliable IP contribution path beyond best-effort UDP. SRT is present in the pinned private GStreamer runtime, but merely constructing `srtsink` would create false readiness: caller mode can remain disconnected while the rest of the pipeline is playing. Credentials embedded in an SRT URI would also leak into public configuration and diagnostic surfaces.

## Decision

`srt` is a typed stable output-sink kind with the following initial contract:

- The public destination is exactly `srt://IP:port`. URI options, fragments, user information, and credentials are rejected.
- The branch uses caller mode, 200 ms latency, automatic reconnect, one isolated queue, and the same channel-wide MPEG-TS produced by the persistent encoder/mux.
- Every enabled SRT sink is required in this phase. The engine polls the sink's runtime statistics and refuses worker readiness unless every SRT branch connects and sends bytes within five seconds. An absent receiver therefore fails red instead of producing a false healthy-fallback state.
- The output profile and SRT URI cross the protected named-pipe bootstrap. They do not appear in worker process arguments.
- Unencrypted SRT is explicit in this slice. Passphrases are not accepted in public URIs or stored in plaintext. Encrypted SRT requires a separate write-only API backed by DPAPI machine protection before it can be claimed.

## Consequences

Operators can configure a real SRT caller through the same channel/sink API and console as UDP. Existing databases migrate transactionally from the UDP-only sink constraint without losing rows. Startup now depends on the required SRT receiver being reachable; the supervisor's existing failure/backoff/incident path handles an unavailable listener. Optional SRT, rendezvous/listener modes, stream IDs, encryption, independent continuous branch telemetry, and remote interoperability remain separate gates.

