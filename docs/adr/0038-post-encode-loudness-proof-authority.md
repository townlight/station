# ADR 0038: Post-encode loudness proof authority

## Status

Accepted.

## Context

An output target reaching the worker and an audio encoder continuing to produce buffers are execution facts, not evidence that a completed MPEG-TS program decodes at the required integrated loudness. Source-asset analysis is also insufficient because normalization, encoding, and muxing occur later. Historical evidence must remain auditable without becoming current after a binary or output configuration changes.

## Decision

`station-readiness` owns the pure sink-proof contract. Targets and measurements use integer milli-LUFS. A completed measurement is evaluated against the exact executable -24 or -16 LUFS target with a fixed +/-2 LUFS tolerance. Stored outcomes are pass, fail, not-run, or invalid. Expiration is derived rather than written over history.

A proof is current only when its validated outcome, candidate version, verified source commit, canonical channel-output configuration hash, and bounded age match current authority. A forged pass whose measurement is outside tolerance is invalid. Local loudness evidence and receiver acceptance remain separate fields; a local pass with unknown receiver acceptance cannot make an external headend claim.

`station-storage` appends proofs to an immutable SQLite ledger. Database triggers reject update and delete. Append holds an immediate transaction while it verifies that the channel and sink still exist, the target still matches, and the current output-configuration hash equals the captured hash. The local-proof append path accepts only `not_attempted` or `unknown` receiver acceptance; verified or rejected receiver claims require a future independent evidence path. Historical rows survive later sink reconfiguration. Optional evidence paths must be relative to the managed proof root; arbitrary absolute or traversing paths are rejected.

`station-media-assets::measure_output_loudness` performs bounded discovery and decodes the captured MPEG-TS through GStreamer `uridecodebin` and `ebur128level`. Empty evidence, missing audio, analyzer failure, and insufficient analyzed duration fail visibly. The measurement includes the analyzer duration used to support the result.

`GET /api/v1/channels/{channel_id}/loudness-proof` reads the configured sinks, configuration hash, and latest proof per sink in one SQLite snapshot, then returns one entry per enabled UDP/SRT sink in configured order. It keeps configured target, measured value, effective status, local evidence, and receiver acceptance distinct. Effective status is `pass`, `fail`, `invalid`, `not_run`, or `expired`: no stored evidence returns `not_run`; an unverified candidate or stale candidate/configuration/age returns `expired`. Current candidate identity bypasses the general readiness cache so activation or rollback cannot inherit a cached green result.

## Proof boundary

This decision implements the proof model, immutable persistence, independent decode/measurement primitive, and read surface. [ADR 0039](0039-bounded-active-output-proof-capture.md) adds bounded capture from the running target group's post-`mpegtsmux` fanout and durable job state. [ADR 0040](0040-operator-requested-output-proof-execution.md) completes the operator request, leased execution, artifact verification, independent analysis, and atomic proof-append path.

Even after that capture path exists, a local post-mux pass will prove only that the candidate produced a decodable target-group transport whose measured integrated loudness was within tolerance. It will not prove UDP delivery, SRT receiver acceptance, per-sink packet loss, cable-headend acceptance, or field operation.

## Consequences

- Configuration, execution health, decoded post-mux measurement, external receiver acceptance, and field proof remain separate authorities.
- A reconfigured output or different/unverified candidate cannot inherit a prior green result.
- Evidence history is append-only and remains readable after reconfiguration.
- A request or captured artifact alone never changes proof status; only durable orchestration completing independent analysis and the atomic immutable append can do so.
