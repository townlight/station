# ADR 0040: Operator-requested output-proof execution

Status: Accepted. Exact branch placement and ten-minute bounds are superseded by
[ADR 0046](0046-exact-sink-ten-minute-output-proof.md).

Date: 2026-08-22

## Context

The worker can capture bounded bytes from a running post-`mpegtsmux` target group, and storage can preserve immutable decoded loudness evidence. A capture request is not proof. Completion must survive service interruption, must not trust candidate or output identity supplied by a browser, must not analyze media on the channel-control loop, and must not append evidence after the candidate or channel configuration changes.

## Decision

The loopback API accepts only a sink UUID plus bounded sample duration and byte limit. Requested analyzable program is 3 to 300 seconds and the artifact limit is 1 byte to 256 MiB. Orchestration adds four seconds of physical capture headroom because a file beginning at an arbitrary MPEG-TS boundary can wait up to one 120-frame 720p59.94 GOP (2.002 seconds) for random access and then needs a complete one-second EBU R128 reporting interval. Rounding that combined worst case to four seconds prevents frame-phase luck from deciding whether the analyzer sees the full request; the analyzer still requires the complete operator-requested duration. It derives candidate version and commit from the current installation and active-slot receipts, verifies the active application files, and derives the sink ordinal, exact integer loudness target, and canonical configuration hash from one storage snapshot. The installation receipt's approved media-runtime package hash is returned as canonical lowercase identity. Known historical installer receipts used uppercase hexadecimal; readers accept that equivalent representation, and every maintenance rewrite canonicalizes it to lowercase. Live runtime factories and engine readiness remain separate probes. The API creates an OS-random RFC 4122 version-4 job UUID and returns HTTP 202. Callers cannot submit candidate, target, configuration, filesystem path, worker identity, graph revision, result, or measurement claims.

`station-orchestration` claims the oldest due job transactionally. Expired nonterminal leases recover to pending. The executor validates the complete enabled-sink order and binding, then revalidates the current candidate before capture, after analysis, and immediately at the final append boundary. Formal slot activation stops `stationd` before replacing candidate receipts, so the running executor and receipt commit cannot overlap. It starts and polls the asynchronous worker capture over the protected protocol. Lease renewal is limited to the final renewal window rather than written on every poll. If the start reply is lost, the command sequence is ambiguous and the worker is force-terminated before the job is failed. Later control failure requests abort and accepts only an exact proof-ID `capture_aborted` confirmation; an ambiguous abort terminates the worker rather than allowing capture to continue.

The worker capture result supplies a managed relative path, byte count, SHA-256, worker instance, graph revision, and requested lower-bound duration. The executor requires a non-symlink regular file confined beneath the channel's canonical proof root, enforces the byte ceiling both before and during hashing, and verifies the exact SHA-256. It performs GStreamer discovery and EBU R128 analysis on a background thread. Missing audio, silence with no finite measurement, insufficient analyzed duration, decode failure, analyzer disconnection, or any changed candidate/configuration becomes an explicit failed job.

Only the final storage transaction can append the immutable sink proof and change the job to completed. That transaction rechecks sink, target, and configuration identity. Capture facts can remain on a failed job for diagnosis without becoming proof. Deterministic stale `.ts.part` and recovered-job `.ts` artifacts are cleaned inside the managed root. Every terminal failure after a worker reports ready—including unsafe path, hash, size, duration, or identity failure—attempts deterministic managed cleanup before failing the job and records any cleanup error. Terminal rows cannot be overwritten.

An unfinished job cannot outlive the controller that could execute it. Each reconcile finds `pending`, `claimed`, `capture_pending`, and `analyzing` jobs for disabled channels or enabled channels without an active controller, attempts deterministic managed cleanup, and moves them to `failed` with an operator-readable reason through an expected-state compare-and-set that does not depend on lease freshness. Cleanup failure is included in the bounded durable reason and does not take down orchestration. This prevents an expired job from recovering to `pending` forever after its channel has been disabled.

The operator console lists enabled sink targets separately, applies the same request bounds, polls status, and displays local measurement/result independently from receiver acceptance. It does not convert a job state, transport counter, local pass, or candidate receipt into an external delivery claim. Service and console launch modes both bind the unauthenticated control plane only to loopback.

## Proof boundary

The real integration gate schedules an audible canonical MPEG-TS asset, launches the actual channel worker and persistent graph, takes the asset on air, captures the target group's real post-mux transport, verifies its artifact facts, independently measures loudness, and observes atomic job/proof completion. Unit and process tests cover ordering, lease recovery, abort, artifact cleanup, candidate/configuration change, analysis failure, and immutable completion.

This is repository and machine evidence. It is not yet proof that a newly built installer produces a current accepted candidate on a clean station, that a receiver decoded or accepted the stream, that a cable headend accepted it, or that one and then three stations operated for the required field duration.

## Consequences

- Operator intent is durable and recoverable without granting the browser evidence-writing authority.
- A changed or unverifiable candidate, sink binding, target, or configuration cannot inherit a green result.
- Proof work remains isolated from the timing-critical graph and database control loop.
- Silence and missing measurement fail visibly; they are never rounded, defaulted, or treated as a pass.
- Local decoded loudness and external receiver acceptance remain separate authorities.
