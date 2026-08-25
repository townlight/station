# ADR 0046: Exact-sink ten-minute output proof

## Status

Accepted — 2026-08-25

## Context

The original output-proof tap lived on each shared loudness/mux variant. That was valid evidence for
the encoded variant, but it was not exact evidence for a selected sink when two destinations shared
the same target and mux contract. Its five-minute and 256 MiB limits also could not carry the
required ten-minute commissioning sample at the runnable 8 Mb/s headend contract.

## Decision

Every configured output branch owns a permanent inactive capture ingress on the source pad of its
final transport queue, immediately before the UDP or SRT sink. The worker binds the requested output
index, sink UUID, loudness target, graph revision, and capture ingress. Same-contract sinks may be
captured independently and concurrently; shared variant bytes are no longer presented as
selected-sink evidence.

The analyzable request remains bounded and now permits 3,000 through 600,000 milliseconds.
Orchestration adds the existing four seconds of physical decode headroom, so the worker ceiling is
604,000 milliseconds. The artifact ceiling is 1 GiB. The operator defaults to a 600,000 ms request
and a 768 MiB limit, which contains the approximately 576 MiB produced by the locked 8 Mb/s mux over
the sample plus headroom while retaining a finite hard stop.

SQLite migrates the immutable proof ledger and durable job table atomically to the expanded bounds,
preserving rows, indexes, unfinished state, and append-only triggers. A commissioning report passes
the ten-minute gate only for the newest enabled-sink proof that is at least 600,000 ms, has a passing
independent decode/loudness result, and still matches the verified installed candidate, exact current
output-configuration hash, and proof-age boundary.

## Consequences

- A selected-sink proof now refers to bytes on that exact configured output branch.
- Capture still proves local bytes before the transport sink; it does not prove network delivery,
  receiver decode, headend acceptance, packet loss, or field operation.
- The larger artifact is deliberate and bounded. Byte-limit breach fails only the proof and cannot
  stop or block the live output branch.
- Historical shorter or shared-variant proofs remain readable but cannot satisfy commissioning.
