# Changelog

All notable changes to TownLight Station will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Commissioning support bundles now receive an append-only operator-attributed receipt only when the
  server can bind them to a completed guided broadcast. The commissioning report reopens the
  managed ZIP and verifies its current size and SHA-256; missing or changed bytes fail closed.

- StationBox network readiness now uses the configured Windows Time source for a bounded
  three-sample UDP request/reply proof. A hostname-backed completed exchange proves name resolution;
  SRT, arbitrary destinations, headend delivery, and receiver acceptance remain explicitly unknown.

- A shared-machine Windows Sandbox ownership guard now serializes pre-launch checks, rejects any
  active or unknown sandbox owner, records only exact harness-launched process identity in immutable
  evidence, and contains no broad cleanup or process-termination path.

- Windows development Cargo execution now starts through a checked-in GStreamer environment wrapper,
  with the target runner retained as a second fail-closed boundary, preventing test binaries and their
  child processes from opening modal missing-runtime dialogs.

- Installer preflight now rejects an existing TownLight application tree, service, or matching AppId uninstall registration before `ssInstall` can replace binaries, remove the private runtime, or stop a working service. It atomically claims an empty candidate directory, uses no-overwrite file staging, rejects an existing runtime root, and rolls back only candidate-owned application/services. The full setup executable is clean-install/reinstall-only; signed maintenance updates remain the in-place path, while ProgramData preserved by uninstall still permits reinstall.

- Fresh TownLight Station repository and native Rust control-plane foundation.
- Versioned station-profile read/write API with typed JSON errors.
- SQLite WAL persistence using the database runtime included with Windows.
- Validation for station identity, operator-visible name, and IANA timezone.
- HTTP health endpoint and end-to-end transport tests.
- Optimistic revision checks that reject stale station-profile updates without losing the winning change.
- Acyclic workspace boundaries for the domain model, SQLite storage, HTTP API, and `stationd` process.
- Versioned, bounded command and event frames for the channel-worker boundary, including incompatible-version and truncation rejection.
- Durable per-channel event journals with checksums, strict sequences, identity isolation, synchronized writes, restart recovery, and visible corruption failures.
- Executable `channel-worker` process that journals readiness, heartbeat, plan, rejection, and shutdown events before emitting them.
- Local-only duplex Windows named-pipe transport with a protected ACL, first-instance ownership, scoped names, and process-level command/event contract tests.
- Station-side worker supervisor with bounded launch and response waits, startup handshake validation, strict identity and sequence checks, restart recovery, acknowledged shutdown, and forced cleanup on failure or drop.
- Programmatically built persistent GStreamer video graph with raw fallback/program selection, OpenH264 encoding, uninterrupted MPEG-TS muxing, UDP output, bounded switch acknowledgement, and machine tests that verify TS framing and per-PID continuity across switches.
- Persistent stereo AAC joins the video graph through a synchronized source selector and sample-domain rate adjuster; the UDP proof now rejects missing A/V streams, backward PTS, and excessive A/V timing divergence across source switches.
- Native media validation and atomic SHA-256-addressed ingest with stored-copy revalidation and idempotent duplicate handling.
- Dynamic real-file decode into an already-running channel graph, live-clock source alignment, one-segment output timing, and ten-run fallback/file/fallback transport stress proof.
- Durable worker commands and events for identity-verified asset load, on-air take, and fallback return; the supervised process proof recovers the full transition journal after restart.
- Heap-backed media hashing avoids overflowing the constrained Windows worker main-thread stack.
- Transactional replacement of prepared file legs while fallback stays live, including generation-scoped GStreamer elements, ordered decoder teardown, canonical video-rate repair, and repeated two-asset worker/journal proof.
- Typed schedule and commit-to-air domain with active overlap, missing/unready/short-media, nearest-gap, adjacency, and time-overflow checks.
- SQLite schedule authority and loopback JSON endpoints that rerun the gate under a write lock and atomically persist operator approval before dispatch; race failures leave no report and do not change the draft item.
- Validated channel/output configuration plus daemon-owned per-channel orchestration that preloads approved assets, takes them at their scheduled start, returns to fallback at their scheduled end, and advances dispatch state only after durable worker acknowledgments.
- Crash reconciliation for queued and acknowledged schedule work: a replacement worker reloads the content-addressed asset and retakes it only while its committed time window remains active.
- Forward migration of the dispatch-status constraint so existing approval rows survive introduction of the terminal `completed` state.
- Exact-candidate installed-machine proof of timed asset preload, on-air take, fallback return, worker shutdown, database status, and durable per-channel journal evidence.
- Managed media-ingest API that is the only public route to `ready`: it validates real A/V, atomically copies and hashes into the station library, revalidates stored bytes, and rejects caller-declared readiness or invalid sources without library pollution.
- Durable background media-ingest jobs with visible pending/running/completed/failed state, atomic job-plus-asset completion, duplicate identity rejection, readable failure details, attempt counts, and automatic retry recovery after daemon interruption.
- Media-readiness dashboard and bounded upcoming-schedule missing-media alerts that check the filesystem, plus a Commit-to-Air gate that rejects formerly ready assets whose admitted file disappeared.
- Durable local, USB, and SMB/NAS watch-folder configuration and scan health, with extension filtering, size-and-mtime settle windows, persistent restart-safe observations, deterministic deduplication, post-copy identity verification, and bounded retry backoff for transient source failures.
- Ingest-time EBU R128 integrated-loudness analysis with atomic per-asset persistence, a -24 LUFS ±2 LU gate, operator-readable normalization guidance, per-asset readiness detail, dashboard visibility, and installer enforcement of the required private-runtime analyzer.
- Channel-worker ownership of fallback graph startup and shutdown, with supervisor proof that durable readiness corresponds to real MPEG-TS output.
- Native Windows Service Control Manager hosting for `stationd`, including loopback-only service configuration, cooperative stop and operating-system shutdown, startup status, persistent data-directory creation, and a machine-proven install/start/health/stop lifecycle.
- Self-contained elevated Windows installer with a hash-pinned private GStreamer runtime, delayed-auto service recovery, activation health gate, immutable candidate receipt, failed-install rollback, uninstall, and station-data-preserving reinstall proof.
- HTTP `Expect: 100-continue` support so standard Windows clients can submit API bodies without a request-body deadlock.
- Blocking per-client sockets behind the cooperative nonblocking listener, eliminating a Windows race that aborted clients which did not send request bytes immediately after connecting.
