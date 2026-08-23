# ADR 0039: Bounded active-output proof capture

## Status

Accepted.

## Context

The immutable proof authority can evaluate and store decoded post-mux loudness, but it needs evidence from the graph that is actually on air. Rebuilding the graph, inserting a temporary destination branch, blocking the streaming thread, or allowing a capture failure to stop playout would turn a diagnostic operation into an outage risk. A worker restart must not create an apparently successful proof, and a service restart must not lose the fact that proof work was requested.

## Decision

Each distinct loudness target group owns one permanent probe on the sink pad of its post-`mpegtsmux` transport fanout. Inactive probes perform an atomic check and return. An active probe copies transport buffers into a bounded nonblocking queue; lock contention, queue overflow, mapping failure, byte-limit breach, or writer failure fails only the proof and never drops a live transport buffer.

Capture commands identify a canonical proof UUID, configured output index, canonical sink UUID, exact -24 or -16 LUFS target, duration from 3 to 300 seconds, and a maximum of 256 MiB. Commands never carry filesystem paths. The protocol binds index, sink, and target to the private bootstrap configuration and returns immediately with `started`; polling yields `pending`, `ready`, or `failed`. Every reply carries the worker instance and nonzero graph revision. A graph revision change invalidates the capture.

The worker writes only `<proof-id>.ts.part` beneath its canonical managed `output-proofs` directory. The canonical proof root must remain under the journal-owned station data root. The writer hashes and counts the exact post-mux bytes, flushes and synchronizes the file, then atomically renames it to `<proof-id>.ts`. Capture generations prevent delayed callbacks from entering a later job. Abort and terminal publication share one status lock, so exactly one terminal outcome wins; shutdown performs a bounded join and an aborted writer cannot leave a published final artifact. Bootstrap removes only canonical UUID-named stale `.ts.part` files.

`station-storage` owns the durable job lifecycle: `pending`, `claimed`, `capture_pending`, `analyzing`, `completed`, or `failed`. Claims and transitions are compare-and-set operations with bounded leases; expired nonterminal work returns to `pending`, terminal facts cannot be overwritten, and one active job is allowed per channel and target group. Creation holds an immediate transaction while binding the request to the current enabled UDP/SRT sink, exact target, and canonical output-configuration hash. Candidate version and lowercase source commit are required facts.

## Proof boundary

This decision implements the durable request authority and the running-worker capture mechanism. It does not yet expose an operator write endpoint, claim jobs in `station-orchestration`, run the independent analyzer, or append the final immutable sink proof. Until that executor slice exists and runs successfully, the read API continues to report `not_run` rather than inferring success from a capture, target, transport counter, or job state.

A completed local measurement will still prove only that the candidate produced a decodable target-group transport at the expected integrated loudness. It will not prove destination delivery, packet loss, receiver decode, cable-headend acceptance, or week-long field operation.

## Consequences

- Proof capture cannot rebuild, pause, or block the on-air graph.
- Same-target sinks share the same post-mux evidence stream; different target groups remain isolated.
- A crash or lease expiry preserves requested work without fabricating terminal evidence.
- Artifact identity, byte count, SHA-256, worker instance, graph revision, candidate identity, sink identity, target, and configuration identity remain attributable across the process boundary.
- Durable execution, independent measurement, immutable proof append, and operator controls remain the next slice.
