# ADR 0042: Current-boot orchestration health

Date: 2026-08-23

Status: Accepted

## Context

The loopback API and Windows service can remain alive even if the channel-orchestration thread exits. Database availability alone therefore cannot support a healthy service claim. At the same time, process health is not the same authority as channel output, safe-to-air, receiver acceptance, or field operation; those remain on their existing runtime and readiness surfaces.

## Decision

`stationd` owns one shared, current-boot orchestration-health authority. It begins in `starting`, becomes `ready` only after the orchestration loop completes its first reconcile, becomes `failed` on a returned error or panic, and becomes `stale` if a previously ready loop stops reporting bounded progress. Liveness refreshes during bounded channel work extend an already-ready boot but cannot turn a starting boot ready.

The orchestration thread is supervised. Its error and panic paths record a local receipt, mark current-boot health failed, and request process-wide cooperative stop. The API returns HTTP 503 from `/health` unless orchestration is ready and stops accepting requests when orchestration fails or becomes stale. The Windows service does not publish `Running` until current-boot orchestration readiness succeeds, and a STOP observed during startup wins over a simultaneous readiness observation.

Every subsystem join has a deadline. A deadline is not treated as proof that a worker stopped: the service does not publish `Stopped` while any join is unconfirmed, and the service entry point terminates the process on lifecycle failure so Windows cannot retain detached station work. Failures after orchestration starts request stop and perform the same bounded cleanup. A clean cooperative shutdown publishes `Stopped` only after orchestration, ingest, alerting, update monitoring, and feed fetching have all joined.

The installer allows the service up to 75 seconds to cross the current-boot health gate. The installed verifier parses the response and independently requires top-level status, database state, and orchestration status to be `ready`; it does not depend on JSON property order or an obsolete exact body.

## Proof boundary

Unit tests cover starting, ready, failed, and stale health; shared API state; API-loop termination; orchestration errors and panics; STOP-before-ready; and bounded join timeout. Workspace tests exercise the real orchestration and worker process paths. Installed verification and the progressive headend proof must still pass for the exact packaged candidate before this decision has installed-machine evidence.

`/health` proves local control-plane and orchestration liveness only. `GET /api/v1/runtime-safe-to-air`, channel runtime, headend readiness, output loudness proof, external receiver evidence, and field gates remain separate and cannot be inferred from it.

## Consequences

- A live HTTP listener can no longer hide a dead or wedged orchestration loop.
- A normal but unhealthy output remains diagnosable through the operator API and its dedicated fail-closed readiness authority.
- A worker that cannot confirm shutdown forces process termination instead of a false clean-stop claim.
- Historical health from an earlier process cannot make a new service boot ready.
