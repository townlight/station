# CG feed-fetch lifecycle proof — 2026-08-21

## Commands and results

```text
cargo test --offline -p station-feed-fetch
cargo test --offline -p stationd
cargo clippy --offline -p station-feed-fetch --all-targets -- -D warnings
cargo clippy --offline -p stationd --all-targets -- -D warnings
```

The feed-fetch suite passed `29 passed; 0 failed`; the station daemon suite passed
`6 passed; 0 failed`. Both Clippy gates passed with warnings denied. The stationd checks used the
installed Windows GStreamer SDK and compiled the real daemon dependency graph.

## Scope

The tests and review prove a direct, proxy-disabled HTTPS transport with pinned public-only DNS
answers, redirect refusal, platform TLS verification, conditional validators, bounded headers,
four-second request deadlines, and a 512 KiB decoded-body ceiling. Real temporary SQLite stores
prove 200 replacement, metadata-only 304 refresh, approval preservation, stale-cache preservation
on failure, monotonic timestamps, retry pacing, and nonfatal per-feed errors. Loopback HTTP fixtures
prove validator transmission, redirect refusal, and the body ceiling through a test-only constructor
that is absent from production builds.

The daemon starts one named feed worker in console and Windows-service modes, uses its own database
connection, checks shutdown between feeds, writes a failure receipt, joins on shutdown, and
sets the shared daemon stop flag if an infrastructure failure terminates the worker. Production
transport, parser, and receipt errors use stable text without URLs, credentials, headers, or bodies.

## Boundary

This is a local contract and machine-build proof, not an external-provider certification. It does
not prove a public HTTPS endpoint, authenticated CalDAV, weather/social provider profiles, rate-limit
behavior, installed-station operation, or field soak. Those remain separate provider and field
gates.
