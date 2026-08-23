# ADR 0035: Headend readiness and receiver-proof boundary

Date: 2026-08-22

Status: Accepted

## Context

Station operators need one answer for whether each configured channel and output is locally able to
deliver, while field acceptance belongs to equipment and people outside the station process. A
worker heartbeat, UDP byte count, or connected SRT socket can demonstrate local progress but cannot
demonstrate that a cable headend decoded the program, accepted its tables and timing, or put it on
air. Treating those facts as equivalent would create a false-green commissioning result.

## Decision

`station-readiness` owns a pure, versioned headend derivation. It combines the StationBox capability
profile with typed channel/profile/sink facts and a current runtime observation. Inputs are bounded
and validated for canonical identities, exact named-profile tuples, possible transport rates,
unique sinks, and consistent heartbeat evidence before policy is applied.

The API exposes aggregate and channel-scoped read-only views at
`GET /api/v1/headend-readiness` and
`GET /api/v1/channels/{channel_id}/headend-readiness`. StationBox facts retain their shared
60-second cache, while channel configuration and worker runtime are read for every request. A
stopped, failed, missing, or older-than-15-seconds worker observation therefore blocks local output
without waiting for the hardware cache to expire.

The current worker observation is aggregate evidence that all required local branches are
advancing. For a supported progressive profile, it can mark the local UDP-TS or SRT transport leg
as passing. It cannot mark external receiver acceptance as verified. UDP bytes and an SRT
connection/transmit count remain local transport evidence only. Receiver acceptance may become
verified only from a later authenticated receiver or operator proof record; there is no mutable
database boolean that can manufacture it.

The local `proof-360p30` profile is not a headend contract and cannot report headend-ready. Missing
station identity, a non-passing engine, and configured unsupported or unimplemented capabilities
propagate fail closed. Interlaced, SDI, and NDI configurations remain explicitly blocked. Their
absence does not block an otherwise valid progressive IP configuration that does not select them.

The authoritative commissioning report consumes the headend roll-up as
`headend_output_setup`, but preserves clean-machine, receiver, headend, and field proof as separate
external gates.

## Consequences

Operators can distinguish a bad or stale local output from a locally advancing stream whose
receiver has not yet been verified. The result cannot turn green from configuration or transport
activity alone. This slice does not claim the six headend-profile catalog, profile application,
TSDuck compliance, device reachability, interlaced execution, SDI/NDI execution, test-pattern
proof, or cable-headend acceptance; those remain explicit subsequent product work.
