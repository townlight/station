# TownLight Station

TownLight Station is a locally installed Windows broadcast appliance for LPM and PEG operations. It is being built as one traceable product: station control, deterministic media execution, operator workflows, installation, recovery, distribution, and proof all ship from this repository.

The current foundation provides a versioned station-profile API, strict commissioning validation, SQLite persistence in WAL mode, a native Windows service, an installed same-origin operator console, durable runtime health/incident authority, and encrypted email/SMS/webhook operator alert delivery. It is a working vertical slice of the station control plane, not a feature-complete release.

The repository also contains a supervised isolated channel worker, a length-framed typed command/event protocol, a durable per-channel event journal, and the first machine-proven persistent media graph. The station runtime launches and handshakes the real worker over a private named pipe, validates every identity and sequence, enforces response deadlines, and guarantees process cleanup. The GStreamer graph keeps its encoder, MPEG-TS mux, and UDP output alive while switching raw fallback and program sources. Unknown major versions, stale commands, corrupted journals, malformed frames, missed deadlines, and MPEG-TS continuity errors fail visibly.

## Architecture

- Rust owns the station control plane and isolated media workers.
- A single station service owns authoritative SQLite writes.
- One persistent GStreamer worker per enabled channel owns media timing and output.
- React and TypeScript provide the installed operator interface; resident interfaces will use the same versioned control contracts.
- The appliance remains locally operable when optional cloud services are unavailable.

See [the architecture overview](docs/architecture/overview.md) for boundaries and non-negotiable design rules.

## Requirements

- Windows 11 or Windows Server 2022 or newer
- Rust 1.97.1 for development
- Node.js 22 and npm for operator-console and installer builds
- GStreamer 1.28.6 MSVC x64, development install, for media-engine development and tests

Install GStreamer from the [official Windows download](https://gstreamer.freedesktop.org/download/#windows) and put its `bin` directory first in `PATH` before building. The runtime currently uses the SQLite library included with Windows and does not require a separately installed database server. The product installer will carry the selected GStreamer runtime rather than asking station operators to configure a development SDK.

## Quick start

1. Install Rust and the official GStreamer 1.28.6 MSVC x64 development runtime.
2. Clone this repository.
3. Run `npm ci --prefix web/operator` and `npm run build --prefix web/operator`.
4. Run `cargo test --offline --workspace`.
5. Run `cargo run -p stationd -- station.db 127.0.0.1:4070`.
6. Open `http://127.0.0.1:4070/` to commission and operate the station. `GET /health` remains the machine health endpoint.

`stationd service --database <absolute-path> --address 127.0.0.1:<port>` is the Service Control Manager entry point used by installation infrastructure. Service mode refuses relative database paths and non-loopback listeners, reports startup and shutdown state to Windows, and handles both requested stops and operating-system shutdown cooperatively. It is not intended to be launched directly from an operator terminal.

## Installer

`installer\build-installer.ps1` reproducibly restores and builds the operator console, then builds one elevated x64 setup executable in `dist`. It accepts only the pinned official GStreamer 1.28.6 runtime hash, embeds `stationd` (including the console), `channel-worker`, the private media runtime, notices, and an immutable source commit, and refuses a dirty release worktree. `-AllowDirty` exists only for local packaging tests.

Installation is health-gated before Inno Setup commits the product registration: the installer stages immutable binaries, installs the private runtime, registers an immediate automatic LocalSystem service with restart recovery, starts it, receives the local readiness response, and writes a candidate receipt. Immediate startup is deliberate: a station must begin channel recovery during boot rather than wait for Windows' delayed-start window. Any failure before formal installation removes the staged service, runtime, and binaries and returns nonzero. Uninstall removes application/runtime files and the service but deliberately preserves station data beneath `%ProgramData%\TownLight Station`.

Run `installer\verify-installed.ps1` after installation to compare installed hashes and receipts with the candidate manifest and to prove the service, health endpoint, and required media factories. Development candidates are not code-signed; a trusted Windows signing identity and timestamp are mandatory release gates before external distribution.

## API

`GET /health` reports whether the local database is ready.

`PUT /api/v1/station` validates and stores the singleton station profile:

```json
{
  "station_id": "3f5f721f-96c7-48b1-b061-1bf1ad1e62c2",
  "display_name": "KTLT Community Television",
  "timezone": "America/Denver",
  "expected_revision": 0
}
```

The response includes the new `revision`. Send that value as `expected_revision` on the next update; a stale update receives `409 revision_conflict` instead of overwriting another operator's work.

`GET /api/v1/station` returns the commissioned profile and revision, or a typed `not_commissioned` error.

The loopback control plane also exposes `PUT`/`GET /api/v1/channels`, `GET /api/v1/runtime-safe-to-air`, durable alert listing/acknowledgment, `PUT`/`GET /api/v1/alert-channels`, durable ingest and transcode job lookup, `PUT`/`GET /api/v1/watch-folders`, per-asset readiness, `GET /api/v1/assets/readiness-dashboard`, bounded upcoming-missing-media queries, asset lookup, schedule draft/prepare/commit/listing, and commit-report lookup. Alert-channel writes validate the destination, require HTTPS for remote webhooks, encrypt the target and credential with Windows machine protection before SQLite, and return only redacted metadata. Ingest accepts a caller-generated UUID plus an absolute local source path and immediately returns a durable `pending` job with HTTP 202. The daemon requires finite decodable video and audio, atomically copies and verifies the source in a content-addressed source store, then atomically hands the parent job to a durable canonical transcode job. The transcode worker creates a distinct H.264/AAC MPEG-TS derivative, verifies its bytes and streams, measures post-transcode EBU R128 loudness, and atomically publishes only a passing derivative as a ready asset while completing both jobs. Interrupted work is recovered; transient failures use bounded durable retries; terminal failures retain an operator-readable reason. Watch folders support local disks, USB, and temporarily unavailable SMB/NAS paths. They persist scan health and file observations, require matching size and modification time across multiple polls for an operator-configured settle window, deduplicate the same file version after restart, and durably back off transient source failures before a bounded retry. Callers cannot declare arbitrary paths ready. The readiness dashboard verifies current file presence rather than trusting stale database state, and the missing-media query identifies committed items inside a caller-bounded horizon. Preparation actively reports overlaps, missing/unready/short media, and the nearest gap; it also fails closed if a formerly ready file has disappeared. Approval reruns that gate under a SQLite write lock, then atomically stores the operator approval and changes the item from draft to committed. The daemon launches one supervised worker per enabled channel and advances a report through `pending`, `queued`, `acknowledged`, and `completed` only after durable worker acknowledgments for load, take, and fallback return. A restarted worker reconciles queued and on-air database state by reloading and, when still within the scheduled window, retaking the asset.

## Operator console

`GET /` serves the production React console directly from `stationd`; it has no CDN, font, or Internet dependency. The responsive interface commissions station identity, reports live readiness, imports media, exposes durable ingest/transcode progress, configures channel outputs, prepares and approves schedule commits, follows dispatch status, and derives its commissioning checklist from running station state. Desktop and phone layouts use the same loopback origin and API authority.

Every enabled channel writes a durable five-second runtime heartbeat. A verified fallback is green rather than falsely off-air; a missing or stale heartbeat, stopped worker, or worker error is red. Worker process loss is detected even when no schedule command is pending. Restart attempts use 1/5/15/60-second bounded backoff, and `encoder-death` plus `off-air` incidents deduplicate repeated failures into one event per condition/resource until recovery resolves them. Operator acknowledgment records who saw an incident but does not claim the underlying condition is fixed. First failure and resolution now atomically create one delivery per enabled destination; repeats only increment the durable occurrence count. Critical alerts bypass quiet hours, while warning/info delivery is held until the configured UTC window ends. A service-owned lease worker recovers interrupted attempts, retries with bounded exponential backoff, and reaches terminal dead-letter instead of dropping work. Email uses authenticated SMTP with encrypted remote transport, SMS uses a Twilio-shaped authenticated HTTPS request, and webhooks use platform-verified HTTPS plus an HMAC-SHA256 signature; all three have real loopback protocol tests. Alert-rule editing, re-alert windows, operator send-test/status UI, resource sampling, self-tests, and update alerting remain subsequent health slices.

## Channel worker

`channel-worker <worker-id> <channel-id> <journal-path> <pipe-name> <udp-destination>` connects to the station service through its private duplex Windows named pipe and owns the channel media graph. It reports `Ready` only after fallback output starts, and reports `ShutdownComplete` only after the graph stops. The pipe is restricted to the creating user, Administrators, and SYSTEM, rejects remote clients, and permits only one server instance. `LoadAsset` verifies the asset's content identity and decodability before adding it to the running graph; `TakeAsset` and `ReturnToSchedule` perform bounded source transitions. Every accepted load and on-air change is synchronized to the worker journal before acknowledgement. Live transitions remain explicitly rejected.

## Media engine

`station-media-assets` probes actual streams and finite duration, rejects non-media or incomplete A/V, preserves a verified source object, and produces an independently hashed canonical H.264/AAC transport derivative with target loudness. Only that derivative can become air-ready. `station-media-engine` can then prepare and transactionally replace the decoded program leg while fallback remains live. File pads are aligned to the running clock; both selectors feed one continuous segment; and sample-rate adjusters enforce canonical video/audio timelines before the persistent OpenH264/AAC encoders and MPEG-TS output. The integration proof generates and ingests two distinct real A/V files, airs both in sequence with fallback between them, and rejects transport errors, discontinuities, counter jumps, backward PTS, excessive A/V divergence, or unsafe decoder teardown. A second process-level proof drives an approved timed item through the real worker and twice restarts that worker to prove state reconciliation. Live inputs, production profiles, and sink fanout are not claimed yet.

## Development

Run formatting, linting, and tests before committing:

```powershell
npm ci --prefix web/operator --ignore-scripts --no-audit
npm run build --prefix web/operator
cargo fmt --check
cargo clippy --offline --all-targets --all-features -- -D warnings
cargo test --offline --workspace
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the proof-first workflow and [LICENSE](LICENSE) for terms.
