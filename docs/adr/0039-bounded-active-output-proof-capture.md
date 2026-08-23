# ADR 0039: Bounded active-output proof capture

## Status

Accepted.

## Context

The immutable proof authority can evaluate and store decoded post-mux loudness, but it needs evidence from the graph that is actually on air. Rebuilding the graph, inserting a temporary destination branch, blocking the streaming thread, or allowing a capture failure to stop playout would turn a diagnostic operation into an outage risk. A worker restart must not create an apparently successful proof, and a service restart must not lose the fact that proof work was requested.

## Decision

Each distinct loudness target group owns one permanent probe on the sink pad of its post-`mpegtsmux` transport fanout. Inactive probes perform an atomic check and return. An active probe copies transport buffers into a bounded nonblocking queue; lock contention, queue overflow, mapping failure, byte-limit breach, or writer failure fails only the proof and never drops a live transport buffer. The writer must acknowledge that its file is open before capture becomes active. Its queue holds at most 4,096 messages and independently enforces an 8 MiB reserved-byte ceiling; with the locked 1,316-byte transport chunks the exact maximum queued payload is 5,390,336 bytes. A 256 KiB buffered writer drains that queue without turning filesystem latency into streaming-thread work.

Capture commands identify a canonical proof UUID, configured output index, canonical sink UUID, exact -24 or -16 LUFS target, bounded physical capture duration, and a maximum of 256 MiB. An operator requests 3 to 300 seconds of analyzable program. A late 720p59.94 capture can wait one 120-frame GOP (2.002 seconds) for a random-access point, and the independent EBU R128 analyzer reports at one-second intervals. Orchestration therefore adds four seconds of bounded leading transport context—rounding the combined worst case up instead of relying on frame phase—so the protocol permits at most 304 seconds. Commands never carry filesystem paths. The protocol binds index, sink, and target to the private bootstrap configuration and returns immediately with `started`; polling yields `pending`, `ready`, or `failed`. Every reply carries the worker instance and nonzero graph revision. A graph revision change invalidates the capture.

The worker writes only `<proof-id>.ts.part` beneath its canonical managed `output-proofs` directory. The canonical proof root must remain under the journal-owned station data root. The writer hashes and counts the exact post-mux bytes, flushes and synchronizes the file, then atomically renames it to `<proof-id>.ts`. Capture generations prevent delayed callbacks from entering a later job. Abort and terminal publication share one status lock, so exactly one terminal outcome wins; shutdown performs a bounded join and an aborted writer cannot leave a published final artifact. Bootstrap removes only canonical UUID-named stale `.ts.part` files.

`station-storage` owns the durable job lifecycle: `pending`, `claimed`, `capture_pending`, `analyzing`, `completed`, or `failed`. Claims and transitions are compare-and-set operations with bounded leases; expired nonterminal work returns to `pending`, terminal facts cannot be overwritten, and one active job is allowed per channel and target group. Creation holds an immediate transaction while binding the request to the current enabled UDP/SRT sink, exact target, and canonical output-configuration hash. Candidate version and lowercase source commit are required facts.

## Proof boundary

This decision implements the durable request authority and running-worker capture mechanism. [ADR 0040](0040-operator-requested-output-proof-execution.md) exposes the bounded operator endpoint and completes leased orchestration, independent analysis, and atomic immutable proof append. The read API still reports `not_run` until that entire executor path succeeds; it never infers success from a request, capture, target, transport counter, or intermediate job state.

A completed local measurement will still prove only that the candidate produced a decodable target-group transport at the expected integrated loudness. It will not prove destination delivery, packet loss, receiver decode, cable-headend acceptance, or week-long field operation.

## Consequences

- Proof capture cannot rebuild, pause, or block the on-air graph.
- Same-target sinks share the same post-mux evidence stream; different target groups remain isolated.
- A crash or lease expiry preserves requested work without fabricating terminal evidence.
- Artifact identity, byte count, SHA-256, worker instance, graph revision, candidate identity, sink identity, target, and configuration identity remain attributable across the process boundary.
- Durable execution, independent measurement, immutable proof append, and operator controls are connected by ADR 0040; installed-candidate, receiver, headend, and field proof remain separate gates.
