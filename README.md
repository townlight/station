# TownLight Station

TownLight Station is a locally installed Windows broadcast appliance for LPM and PEG operations. It is being built as one traceable product: station control, deterministic media execution, operator workflows, installation, recovery, distribution, and proof will all ship from this repository.

The current foundation provides a versioned station-profile API, strict commissioning validation, an authoritative commissioning report and guided operator workflow, SQLite persistence in WAL mode, a native Windows service, an installed same-origin operator console, durable runtime health/incident authority, encrypted email/SMS/webhook operator alert delivery, editable re-alert policy, service-owned Windows resource sampling, scheduled evidence-bearing self-tests, redacted manifested support-bundle export, and a signed immutable update lifecycle with out-of-process activation, post-update proof, and automatic rollback. It is a working vertical slice of the station control plane, not a feature-complete release.

The repository also contains a supervised isolated channel worker, a length-framed typed command/event protocol, a durable per-channel event journal, and a machine-proven persistent media graph with durable typed output profiles and stable output branches. The station runtime launches and handshakes the real worker over a private named pipe, validates every identity and sequence, enforces response deadlines, and guarantees process cleanup. The GStreamer graph keeps one video encoder alive while switching raw fallback and program sources, then builds one exact loudness-normalization, AAC, and MPEG-TS variant for each distinct output target and fans that variant only to its matching UDP/SRT sinks. Missing targets, unknown major versions, stale commands, corrupted journals, malformed frames, missed deadlines, unsupported profiles or loudness targets, and MPEG-TS continuity errors fail visibly.

## Architecture

- Rust owns the station control plane and isolated media workers.
- A single station service owns authoritative SQLite writes.
- One persistent GStreamer worker per enabled channel owns media timing and output.
- React and TypeScript provide the installed operator interface; resident interfaces will use the same versioned control contracts.
- The appliance remains locally operable when optional cloud services are unavailable.

See [the architecture overview](docs/architecture/overview.md) for boundaries and non-negotiable design rules.
See [the capability ledger](CAPABILITIES.md) for the complete, unchanged product scope and the
honest implementation/proof status of every required section.

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

The exact commissioning candidate `8e9a27d3286815e0b34a0fad961cb2e5dc9a5a84` passed post-install hash/service verification and the complete installed commissioning harness, including decoded CG apply/change/clear, canonical media admission, all four acknowledged dispatch states, service restart, and green recovery. See the [installed commissioning proof](docs/proof/2026-08-21-installed-commissioning.md). Clean-machine, headend, and field-duration gates remain separate.

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

`GET /api/v1/station` returns the commissioned profile and revision, or a typed `not_commissioned` error. `GET /api/v1/station-box-profile` derives the versioned hardware, engine, output, clock, network, backup, and release profile from bounded live probes; `/api/v1/station-box-profile/readiness` returns the same cached authority's fail-closed PEG roll-up. Unknown outbound delivery, clock synchronization, installed-runtime integrity, and unimplemented output classes cannot turn green. `GET /api/v1/headend-profiles` returns the static six-profile catalog, including sources, operator-supplied fields, execution state, and explicit non-claims. `POST /api/v1/channels/{channel_id}/headend-profile` validates and durably stages a network profile only while the channel is stopped; staging does not make an unimplemented output runnable. `GET /api/v1/headend-readiness` and `GET /api/v1/channels/{channel_id}/headend-readiness` combine capability authority with current channel configuration and a fresh worker heartbeat. They distinguish local transport progress from external receiver acceptance; UDP or SRT activity alone cannot make a headend claim green. `GET /api/v1/commissioning/report` derives ordered station, runtime, headend-output, media, schedule-authority, dispatch-execution, and external-proof checks from current authoritative state; it never treats schedule commitment as execution or claims clean-machine/receiver/field proof from configuration.

The loopback control plane also exposes `PUT`/`GET /api/v1/channels`, `GET /api/v1/runtime-safe-to-air`, durable alert listing/acknowledgment, `GET /api/v1/alert-rules`, `PUT /api/v1/alert-rules/{id}`, `PUT`/`GET /api/v1/alert-channels`, `POST /api/v1/alert-channels/{id}/test`, `GET /api/v1/alert-deliveries`, signed update status/manual-check/release-notes routes, durable ingest and transcode job lookup, `PUT`/`GET /api/v1/watch-folders`, per-asset readiness, `GET /api/v1/assets/readiness-dashboard`, bounded upcoming-missing-media queries, asset lookup, schedule draft/prepare/commit/listing, and commit-report lookup. Alert-channel writes validate the destination, require HTTPS for remote webhooks, encrypt the target and credential with Windows machine protection before SQLite, and return only redacted metadata. Send-test creates a resolved informational audit event plus a normal durable delivery, including for a disabled destination, and the delivery ledger never returns its target or credential. Ingest accepts a caller-generated UUID plus an absolute local source path and immediately returns a durable `pending` job with HTTP 202. The daemon requires finite decodable video and audio, atomically copies and verifies the source in a content-addressed source store, then atomically hands the parent job to a durable canonical transcode job. The transcode worker creates a distinct H.264/AAC MPEG-TS derivative, verifies its bytes and streams, measures post-transcode EBU R128 loudness, and atomically publishes only a passing derivative as a ready asset while completing both jobs. Interrupted work is recovered; transient failures use bounded durable retries; terminal failures retain an operator-readable reason. Watch folders support local disks, USB, and temporarily unavailable SMB/NAS paths. They persist scan health and file observations, require matching size and modification time across multiple polls for an operator-configured settle window, deduplicate the same file version after restart, and durably back off transient source failures before a bounded retry. Callers cannot declare arbitrary paths ready. The readiness dashboard verifies current file presence rather than trusting stale database state, and the missing-media query identifies committed items inside a caller-bounded horizon. Preparation actively reports overlaps, missing/unready/short media, and the nearest gap; it also fails closed if a formerly ready file has disappeared. Approval reruns that gate under a SQLite write lock, then atomically stores the operator approval and changes the item from draft to committed. The daemon launches one supervised worker per enabled channel and advances a report through `pending`, `queued`, `acknowledged`, and `completed` only after durable worker acknowledgments for load, take, and fallback return. A restarted worker reconciles queued and on-air database state by reloading and, when still within the scheduled window, retaking the asset.

CG authority is also durable: boards, zones, feed sources, bulletins, approvals, managed content-addressed PNG/JPEG images, and append-only audit records are persisted with revisions. Cache-only resolved preview/current snapshot logic produces deterministic text, clock, feed, bulletin, image, and typed-placeholder content without network access. Channel orchestration atomically expires due scheduled bulletins before resolving the same timestamp's snapshot, while an explicit operator command remains available; both paths record append-only audit evidence. The worker receives bounded resolved snapshots over the protected protocol, and bounded HTTPS feed fetching is wired into the service lifecycle.

## Operator console

Live takeover is an explicit, channel-scoped control path exposed alongside the other loopback routes. A source is a revisioned typed `udp_mpeg_ts` endpoint with a station-local, private, loopback, or multicast bind address and a nonzero UDP port; arbitrary pipeline strings and non-local bind addresses are rejected. The API creates one active session per channel and advances it through `arming → armed → take_requested → on_air → return_requested → returning → completed` (or `failed`), with a bounded arm expiry. SQLite increments the channel authority revision and records every transition in an append-only audit trail under the same write transaction; stale authority or source revisions fail with a compare-and-set conflict. The controller arms the worker, consumes a short-lived token for exactly one take, blocks schedule dispatch while live is on air, and returns at the current wall-clock time to the active schedule item at its calculated offset (zero at an exact start boundary) or to fallback when no window is active. Ended pending/queued items become terminal `missed` records; prepared media remains parked until a valid take is requested. The return audit records the selected target and media offset, and disarm follows only after worker return acknowledgement. A worker restart, disabled channel, expired arm, failed return, clock reversal, excessive boundary crossing, or uncertain worker acknowledgment fails the session and stops the channel rather than inventing on-air truth. See [ADR 0033](docs/adr/0033-wall-clock-schedule-catch-up.md) for the schedule-window and handback contract.

`GET /` serves the production React console directly from `stationd`; it has no CDN, font, or Internet dependency. The responsive interface commissions station identity, reports live readiness, imports media, exposes durable ingest/transcode progress, configures channel outputs, prepares and approves schedule commits, follows dispatch status, and guides commissioning from the authoritative report. Browser storage remembers only the operator's place in the workflow and cannot manufacture completion. Desktop and phone layouts use the same loopback origin and API authority.

The operator surface also manages revisioned CG boards, zones, feeds, bulletins, approvals, managed
PNG/JPEG image uploads and selection, and audit history, with cache-only resolved preview/current
snapshot state and an explicit manual expiration action alongside automatic due expiration. Image
uploads accept raw bytes only, are validated into the content-addressed station library, and expose
no caller-controlled path. The Live view configures UDP MPEG-TS sources, shows authority revisions and
the durable takeover audit, and requires deliberate confirmation for Take Live and Return to Schedule.
Core commissioning and its installed proof harness are implemented; clean-machine acceptance and the
one-/three-station field operation remain separate gates.

Every enabled channel writes a durable five-second runtime heartbeat only after a real protected-pipe worker round trip and verified byte progress on every required output. A verified fallback is green rather than falsely off-air; a missing or stale heartbeat, stopped worker, worker error, or stalled required branch is red. Worker process loss and output stalls are detected even when no schedule command is pending. Restart attempts use 1/5/15/60-second bounded backoff. Process and control failures raise `encoder-death` plus `off-air`; pipeline and required-output progress failures raise `output-stall` plus `off-air`. Repeated samples deduplicate into one incident per condition/resource until recovery resolves them. Operator acknowledgment records who saw an incident but does not claim the underlying condition is fixed. Seeded editable rules own enabled state, severity, output scope, destinations, resolve notices, and bounded re-alert intervals. First failure and resolution atomically create delivery work; repeat samples increment the durable occurrence count and can create at most one distinct re-alert per destination/window. Critical alerts bypass quiet hours, while warning/info delivery is held until the configured UTC window ends. A service-owned lease worker recovers interrupted attempts, retries with bounded exponential backoff, and reaches terminal dead-letter instead of dropping work. Email uses authenticated SMTP with encrypted remote transport, SMS uses a Twilio-shaped authenticated HTTPS request, and webhooks use platform-verified HTTPS plus an HMAC-SHA256 signature; all three have real loopback protocol tests. The health console edits rules, configures destinations, queues explicit tests, and shows durable pending/leased/sent/dead-letter truth with attempt counts. The same service records bounded real CPU/RAM/storage samples, runs durable daily/weekly self-tests on operator-editable schedules in the station profile's IANA timezone, creates downloadable redacted support ZIPs with per-entry hashes and an outer SHA-256, and performs hourly signed-release checks when a trust file is installed. A verified available release raises one paced informational incident and exposes its signed notes; a failed check raises a warning. No package is downloaded or installed automatically. Explicit operator actions stage and prepare a verified immutable slot. An independent installer-owned watchdog then records a durable maintenance transaction, stops the service, snapshots the exact channel-journal tree with a synchronized hash manifest, activates the candidate, proves health and green safe-to-air, and automatically restores both the prior slot and prior journal bytes if candidate proof fails. Restore staging is verified before write-through tree replacement and recovers across either move boundary.

## Channel worker

`channel-worker <worker-id> <channel-id> <journal-path> <pipe-name>` connects to the station service through its private duplex Windows named pipe and owns the channel media graph. The supervisor sends the typed output profile and complete output configuration as the first protected pipe frame; neither destinations nor future transport credentials appear in process arguments. The worker rejects a mismatched bootstrap identity and reports `Ready` only after the exact selected profile's fallback output starts. It reports `ShutdownComplete` only after the graph stops. The pipe is restricted to the creating user, Administrators, and SYSTEM, rejects remote clients, and permits only one server instance. `LoadAsset` verifies the asset's content identity and decodability before adding it to the running graph; `TakeAsset` and `ReturnToSchedule` perform bounded source transitions. Live control uses typed `ArmLive`, `TakeLive`, `ReturnFromLive`, and `DisarmLive` messages. The arm token is redacted in debug output, bound to the session, and consumed on take; session, source identity, revision, and expected sequence must all match. Every accepted load and on-air change is synchronized to the worker journal before acknowledgement. Journal v2 retains every transition verbatim while compacting older five-second heartbeat runs into size-bounded checked sequence/timing/counter summaries; 256 to 512 recent raw heartbeat samples remain. Compactions require a synchronized completion record and atomic replacement, and startup fails closed on uncovered gaps, corruption, incomplete crash artifacts, or wrong identity. The exact installed candidate converted a 7,028,285-byte, 22,651-record legacy v1 journal into 112,061 bytes with continuous coverage, 22,303 summarized heartbeats, 263 raw heartbeats, and every non-heartbeat record retained; see the [installed journal compaction proof](docs/proof/2026-08-22-installed-journal-compaction.md).

## Media engine

`station-media-assets` probes actual streams and finite duration, rejects non-media or incomplete A/V, preserves a verified source object, and produces an independently hashed canonical H.264/AAC transport derivative with target loudness. Only that derivative can become air-ready. `station-media-engine` can then prepare and transactionally replace the decoded program leg while fallback remains live; prepared media stays parked until the scheduled wall-clock offset is known. File pads are aligned to the running clock; both selectors feed one continuous segment; and source-local conversion, resampling, and rate stages enforce the selected profile before the persistent OpenH264/AAC encoders and MPEG-TS output. Audio passes from the selector through the continuous-timeline identity and one shared canonical `audiorate`, which repairs sub-sample transition rounding before encoding. External live audio is rebased to pipeline time by its source-local `clocksync` before it reaches that shared continuity stage. The durable catalog currently includes a 360p30 loopback proof profile and a 720p59.94 progressive HD IP profile with a measured 8 Mb/s CBR transport. The machine gate captures real UDP output, verifies 1280×720 dimensions, 60000/1001 PTS cadence, null-packet padding, and transport rate. The encoded transport can feed multiple durable required UDP destinations or required SRT caller branches through a tee and independent queues without duplicate encoders. SRT readiness requires a connected receiver and observed transmitted bytes; caller mode uses fixed 200 ms latency and can optionally require a machine-protected AES-256 passphrase. Live input currently supports UDP MPEG-TS only: `udpsrc` with 188-byte MPEG-TS caps feeds `tsdemux`, H.264/AAC decode, source-local canonical conversion/rate stages, and a running-time-aligned live branch that joins the persistent video/audio selectors. Arming waits for decoded video and audio before admitting the branch; taking live waits for activity and switches both legs without rebuilding the encoder, mux, or output branches. Returning selects the current valid schedule asset at its wall-clock media offset, or fallback when no schedule window is active, and disarm removes the branch only after it is no longer selected. The exact installed candidate has decoded fallback → live → fallback evidence in the [installed takeover proof](docs/proof/2026-08-21-installed-live-takeover.md) and decoded cross-boundary positive-offset return evidence in the [installed schedule catch-up proof](docs/proof/2026-08-22-installed-live-schedule-catch-up.md). Additional live transports, SDI/NDI, remote SRT input, clean-machine acceptance, headend proof, and field soak remain separate gates.

The first CG renderer slice uses one fixed transparent canvas branch with eight logical serial layers and worker snapshot handoff. An active-to-inactive or active-to-empty authority transition clears the live canvas once after worker acknowledgment, without treating transient storage errors as operator intent. It renders
manual/bulletin text, clocks, content-addressed static PNG/JPEG images and logos, and static lower-third, ticker-region, and bug geometry; decoded UDP
MPEG-TS tests prove text and image apply/change/clear pixels, invalid-update retention, and output continuity. Hostile offline RSS/Atom/iCal
normalization has 19 passing tests covering bounds, unsafe URLs, malformed input, stable identity,
escaping, tags, and timestamps. Static image upload, audited binding, decoded live change, and clear
are installed-machine-proven. Live-video, background-audio, animated ticker, WPE styling,
clean-machine acceptance, and field gates are not done.

## Development

Run formatting, linting, and tests before committing:

```powershell
npm ci --prefix web/operator --ignore-scripts --no-audit
npm run build --prefix web/operator
cargo fmt --check
cargo clippy --offline --all-targets --all-features -- -D warnings
cargo test --offline --workspace
```

The external MPEG-TS live-input regressions also require `ffmpeg.exe` on `PATH`. The worker-level gate that reproduces an already-running feed can be run directly with `cargo test -p channel-worker --test process_contract spawned_worker_live_takeover_preserves_audible_audio -- --exact --test-threads=1`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the proof-first workflow and [LICENSE](LICENSE) for terms.
