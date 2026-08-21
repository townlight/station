# Changelog

All notable changes to TownLight Station will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
- Channel-worker ownership of fallback graph startup and shutdown, with supervisor proof that durable readiness corresponds to real MPEG-TS output.
- Native Windows Service Control Manager hosting for `stationd`, including loopback-only service configuration, cooperative stop and operating-system shutdown, startup status, persistent data-directory creation, and a machine-proven install/start/health/stop lifecycle.
