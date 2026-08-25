# TownLight Station

TownLight Station is a locally installed Windows broadcast appliance for LPM and PEG operations. It is being built as one traceable product: station control, deterministic media execution, operator workflows, installation, recovery, distribution, and proof will all ship from this repository.

The current foundation provides a versioned station-profile API, strict commissioning validation, an authoritative commissioning report and guided operator workflow, SQLite persistence in WAL mode, a native Windows service, an installed same-origin operator console, durable runtime health/incident authority, authenticated per-channel fallback-slate selection, encrypted email/SMS/webhook operator alert delivery, editable re-alert policy, service-owned Windows resource sampling, scheduled evidence-bearing self-tests, redacted manifested support-bundle export with completed-broadcast receipt binding, and a signed immutable update lifecycle with out-of-process activation, post-update proof, and automatic rollback. It is a working vertical slice of the station control plane, not a feature-complete release.

The repository also contains a supervised isolated channel worker, a length-framed typed command/event protocol, a durable per-channel event journal, and a machine-proven persistent media graph with durable typed output profiles and stable output branches. The station runtime launches and handshakes the real worker over a private named pipe, validates every identity and sequence, enforces response deadlines, and guarantees process cleanup. The GStreamer graph keeps one video encoder alive while switching raw fallback and program sources, then builds one exact loudness-normalization, AAC, and MPEG-TS variant for each distinct output target and fans that variant only to its matching UDP/SRT sinks. Missing targets, unknown major versions, stale commands, corrupted journals, malformed frames, missed deadlines, unsupported profiles or loudness targets, and MPEG-TS continuity errors fail visibly.

Fallback slate selection is now a first-class stopped-channel workflow. `GET` and `PUT` on
`/api/v1/channels/{channel_id}/fallback-slate` expose current selection and immutable history; PUT
requires an authenticated Setup Admin and accepts only a managed, currently verified PNG/JPEG plus
its expected revision. The server owns actor, time, revision, and audit identity. Protocol v5 sends
only the content digest and format to the worker, which rehashes and decodes the exact image before
reporting Ready. Commissioning consumes the same authority plus green current runtime; EAS forced
slate and tones remain separate unfinished S11 work.

The persistent worker is now caption-transport capable. Protocol v6 introduced one bounded typed
cue; protocol v7 adds the durable stopped-channel caption policy to bootstrap before Ready; and
protocol v8 requires an explicit CEA-708 caption clear before every source-role transition, so an
older worker fails closed rather than leaving stale scheduled captions on air. Setup
Admins configure CEA-708/disabled mode, service number, and language through an audited CAS API and
operator workflow. The graph applies the configured service, rejects cues when captions are disabled
or unconfigured, schedules enabled cues against video, and inserts CEA-708/608-compatibility H.264
SEI before the shared encoded video fans out to each MPEG-TS variant. A source proof captures the
exact post-mux branch and decodes the expected text back. The server-side track foundation now binds
each reviewed cue set to the exact content-addressed media identity, language, and CEA service with
CAS revisions, complete append-only snapshots, and on-air mutation refusal. Authenticated asset
routes list compact track summaries and let Setup Admins read or replace exact cue authority while
the server owns actor, time, path identity, and audit identity. The Media console imports and
validates reviewed WebVTT/SRT, edits canonical WebVTT timing, displays immutable revisions, and saves
through that exact contract. After an acknowledged scheduled take, the channel controller selects
the exact policy-matching asset track, derives timing from the committed media offset, catches up an
active cue, and sends bounded relative-delay cues only with worker acknowledgments. A separate
append-only caption egress ledger rejects receipts not bound to the exact schedule report/asset,
enabled sink configuration, current policy revision, current track revision/digest, installed
source commit, managed transport capture, and independent decode-back result. It cannot mark
mismatched or absent decoded evidence verified. Authenticated caption-proof routes now accept only
report, enabled sink, duration, and byte-limit intent; the server derives a fresh exact on-air
schedule/runtime binding, selects a bounded future cue-bearing window, binds the verified installed
candidate and current output configuration, creates one leased job per channel/sink, and exposes
channel-scoped job status plus bounded immutable proof history. The channel controller now leases
those jobs, reserves the worker's final-sink capture tap against
loudness work, waits for the server-selected asset-relative start with a bounded late tolerance,
revalidates on-air/schedule/candidate/configuration authority, atomically promotes the exact capture,
and independently decodes it off the control loop. The installer carries a pinned hash-verified LGPL
FFmpeg executable at a private absolute path; each proof records that executable's measured digest.
The controller prequeues active and bounded-future cues, and requires an acknowledged caption clear
before every source-role transition to synchronously remove them. A cue that expires before worker
admission is surfaced only as transient runtime detail; it is not yet a durable incident or safe-to-air readiness authority. Live ASR,
that durable missed-cue authority, and installed/field evidence remain separate work, so
commissioning still reports the caption authority as blocked.

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

Run Cargo from the workspace root through `scripts\invoke-cargo.ps1`. The wrapper binds Cargo and all
of its descendant processes to the explicit runtime before Windows loads any test executable. The
Windows MSVC Cargo runner provides a second execution boundary and resolves the development runtime at
`%LOCALAPPDATA%\Programs\gstreamer\1.0\msvc_x86_64` by default, binds its exact loader and plugin
directories before launching any built executable, and fails in the terminal before launch if that
runtime is absent. Set `TOWNLIGHT_GSTREAMER_ROOT` to an explicit alternate pinned runtime root when
needed; the runner never accepts a DLL found only through ambient `PATH`.

## Quick start

1. Install Rust and the official GStreamer 1.28.6 MSVC x64 development runtime.
2. Clone this repository.
3. Run `npm ci --prefix web/operator` and `npm run build --prefix web/operator`.
4. Run `scripts\invoke-cargo.ps1 test --offline --workspace`.
5. Run `scripts\invoke-cargo.ps1 run -p stationd -- station.db 127.0.0.1:4070`.
6. Open `http://127.0.0.1:4070/` to commission and operate the station. `GET /health` remains the machine health endpoint.

`stationd service --database <absolute-path> --address 127.0.0.1:<port>` is the Service Control Manager entry point used by installation infrastructure. Service mode refuses relative database paths and non-loopback listeners, reports startup and shutdown state to Windows, and handles both requested stops and operating-system shutdown cooperatively. It is not intended to be launched directly from an operator terminal.

## Installer

`installer\build-installer.ps1` reproducibly restores and builds the operator console, then builds one elevated x64 setup executable in `dist`. It accepts only the pinned official GStreamer 1.28.6 runtime hash and the pinned BtbN LGPL FFmpeg archive/executable/license hashes, embeds `stationd` (including the console), `channel-worker`, both private runtimes, notices, and an immutable source commit, and refuses a dirty release worktree. `-AllowDirty` exists only for local packaging tests.

Installation is health-gated before Inno Setup commits the product registration: the installer stages immutable binaries, installs the private runtime, registers an immediate automatic LocalSystem service with restart recovery, starts it, receives the local readiness response, and writes a candidate receipt. Immediate startup is deliberate: a station must begin channel recovery during boot rather than wait for Windows' delayed-start window. Any failure before formal installation removes the staged service, runtime, and binaries and returns nonzero. Uninstall removes application/runtime files and the service but deliberately preserves station data beneath `%ProgramData%\TownLight Station`.

The full setup executable is deliberately **clean-install/reinstall only**. Before `ssInstall`, it refuses to run when the TownLight application directory, either TownLight service, or this AppId's 64-bit uninstall registration exists, so it cannot overwrite a working installation and later attempt a speculative rollback. It then atomically claims an empty application directory, stages files with no-overwrite copies, and refuses a pre-existing private runtime. Use the installed signed maintenance updater for in-place `.tlu` upgrades. A reinstall after proper uninstall remains supported: Inno removes its uninstall registration while preserved ProgramData, the database, receipts, and manifests alone do not trigger the rejection.

Run `installer\verify-installed.ps1` after installation to compare installed hashes and receipts with the candidate manifest and to prove the service, health endpoint, and required media factories. Development candidates are not code-signed; a trusted Windows signing identity and timestamp are mandatory release gates before external distribution.

The exact commissioning candidate `8e9a27d3286815e0b34a0fad961cb2e5dc9a5a84` passed post-install hash/service verification and the complete installed commissioning harness, including decoded CG apply/change/clear, canonical media admission, all four acknowledged dispatch states, service restart, and green recovery. See the [installed commissioning proof](docs/proof/2026-08-21-installed-commissioning.md).

Exact candidate `b6ae66e71e57cb2d48e7d5de917bc78349bb8d60` passed a fresh network-isolated Windows Sandbox installation with both LocalSystem services, ready health, installed hashes, runtime manifest, private media factories, and pinned caption decoder verified. See the [clean Windows Sandbox installation proof](docs/proof/2026-08-25-clean-windows-sandbox-install.md). Physical double-click operator usability, headend acceptance, and field-duration gates remain separate.

Exact candidate `bd032f00f142dfd3475a76b406391fde611b5788` passed installed Generic UDP SPTS and TelVue HyperCaster IP transport/loudness proofs after an adversarial arbitrary-GOP regression established the bounded four-second decode-headroom contract. See the [installed progressive headend proof](docs/proof/2026-08-23-installed-progressive-headend.md). External receiver acceptance and field-duration gates remain separate.

## API

Caption decode-back requests use `POST /api/v1/channels/{channel_id}/caption-proof` with only
`report_id`, `sink_id`, `capture_duration_ms`, and `max_capture_bytes`. The server requires that
exact committed report to be acknowledged and currently on air with a fresh matching runtime,
selects a bounded future window containing reviewed cues, and derives policy, track, expected text,
verified candidate, and output-configuration authority itself. Unknown caller-supplied authority
fields are rejected. `GET` on the same path returns the newest 100 immutable proofs; `GET
/api/v1/channels/{channel_id}/caption-proof/jobs/{job_id}` returns channel-confined leased status.

`GET /health` reports local database availability plus current-boot orchestration liveness. It returns HTTP 503 until the orchestration loop completes its first reconcile and after a supervisor failure or stale-progress deadline. This is a control-plane health contract, not a safe-to-air or receiver-acceptance claim; those remain on their dedicated runtime and headend-readiness endpoints.

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

`GET /api/v1/station` returns the commissioned profile and revision, or a typed `not_commissioned` error. `GET /api/v1/station-box-profile` derives the versioned hardware, engine, output, clock, network, backup, and release profile from bounded live probes; `/api/v1/station-box-profile/readiness` returns the same cached authority's fail-closed PEG roll-up. Clock readiness uses the configured Windows Time source and requires three measured offsets within two seconds; malformed, unavailable, or excessive-offset evidence cannot turn green. A complete three-sample exchange proves one outbound UDP request/reply path to that configured source, and a hostname-backed exchange also proves working name resolution. It does not prove arbitrary UDP, SRT, headend delivery, or receiver acceptance. Unknown installed-runtime integrity and unimplemented output classes likewise cannot turn green. `GET /api/v1/headend-profiles` returns the static six-profile catalog, including sources, operator-supplied fields, execution state, and explicit non-claims. `POST /api/v1/channels/{channel_id}/headend-profile` validates and durably applies a network profile only while the channel is stopped. Generic UDP SPTS and TelVue HyperCaster IP are locally runnable at their locked progressive 720p59.94, H.264/AAC, `-24 LUFS`, 8 Mb/s, 1,316-byte contract; interlaced profiles remain staging-only and file drop remains unimplemented. `GET /api/v1/headend-readiness` and `GET /api/v1/channels/{channel_id}/headend-readiness` combine capability authority with current channel configuration and a fresh worker heartbeat. They distinguish local transport progress from external receiver acceptance; UDP or SRT activity alone cannot make a headend claim green. Setup Admins append accepted or rejected external evidence through `POST /api/v1/channels/{channel_id}/receiver-receipts`; `GET` on the same path returns immutable history. The server binds each receipt to its authenticated actor, verified installed candidate, exact enabled sink, and current output-configuration hash. Reconfiguration or release change makes old evidence historical, and a later rejection supersedes acceptance without deleting it. `GET /api/v1/channels/{channel_id}/loudness-proof` separately reports configured targets, measured exact-branch post-encode evidence, candidate/configuration expiry, and the same current receiver-acceptance authority for each enabled network sink; absent completed analysis remains `not_run`. `POST /api/v1/channels/{channel_id}/loudness-proof` creates one bounded durable proof job for a selected enabled sink, deriving candidate, target, and configuration identity from current authority rather than trusting caller claims. The default 600-second/768 MiB request satisfies the local commissioning window at the runnable 8 Mb/s contract; hard ceilings remain 604 seconds of physical capture and 1 GiB. `GET /api/v1/channels/{channel_id}/loudness-proof/jobs/{job_id}` exposes channel-scoped progress through claim, capture, analysis, completion, or explicit failure. A request is accepted only when the installed application and recorded approved runtime package identity verify; live engine/factory readiness remains a separate probe. `GET /api/v1/commissioning/report` schema v2 returns server-derived `current_step_id`, required-step progress, `core_complete`, and ordered local/external checks. Its guided proof accepts only one chronological durable chain from completed ingest with present ready bytes through an exact commit report to worker acknowledgment/completion. A current candidate/configuration-bound passing exact-sink proof of at least 600 seconds satisfies its ten-minute local output gate. A support bundle created after that chain completes receives an append-only operator-attributed receipt naming the report, archive size, and SHA-256; the report reopens and rehashes the managed ZIP before passing the check. Absent account/auth, publish-target, automation/model, CEA-708, or TSDuck/device authorities remain required `blocked` checks instead of optimistic completion. Receiver evidence can satisfy only its exact current output binding; clean-machine and one-/three-station field checks remain external `not_verified`. The browser has no completion-write route.

The loopback control plane also exposes `PUT`/`GET /api/v1/channels`, `GET /api/v1/runtime-safe-to-air`, durable alert listing/acknowledgment, `GET /api/v1/alert-rules`, `PUT /api/v1/alert-rules/{id}`, `PUT`/`GET /api/v1/alert-channels`, `POST /api/v1/alert-channels/{id}/test`, `GET /api/v1/alert-deliveries`, signed update status/manual-check/release-notes routes, durable ingest and transcode job lookup, `PUT`/`GET /api/v1/watch-folders`, per-asset readiness, `GET /api/v1/assets/readiness-dashboard`, bounded upcoming-missing-media queries, asset lookup, schedule draft/prepare/commit/listing, and commit-report lookup. Alert-channel writes validate the destination, require HTTPS for remote webhooks, encrypt the target and credential with Windows machine protection before SQLite, and return only redacted metadata. Send-test creates a resolved informational audit event plus a normal durable delivery, including for a disabled destination, and the delivery ledger never returns its target or credential. Ingest accepts a caller-generated UUID plus an absolute local source path and immediately returns a durable `pending` job with HTTP 202. The daemon requires finite decodable video and audio, atomically copies and verifies the source in a content-addressed source store, then atomically hands the parent job to a durable canonical transcode job. The transcode worker creates a distinct H.264/AAC MPEG-TS derivative, verifies its bytes and streams, measures post-transcode EBU R128 loudness, and atomically publishes only a passing derivative as a ready asset while completing both jobs. Interrupted work is recovered; transient failures use bounded durable retries; terminal failures retain an operator-readable reason. Watch folders support local disks, USB, and temporarily unavailable SMB/NAS paths. They persist scan health and file observations, require matching size and modification time across multiple polls for an operator-configured settle window, deduplicate the same file version after restart, and durably back off transient source failures before a bounded retry. Callers cannot declare arbitrary paths ready. The readiness dashboard verifies current file presence rather than trusting stale database state, and the missing-media query identifies committed items inside a caller-bounded horizon. Preparation actively reports overlaps, missing/unready/short media, and the nearest gap; it also fails closed if a formerly ready file has disappeared. Approval reruns that gate under a SQLite write lock, then atomically stores the operator approval and changes the item from draft to committed. The daemon launches one supervised worker per enabled channel and advances a report through `pending`, `queued`, `acknowledged`, and `completed` only after durable worker acknowledgments for load, take, and fallback return. A restarted worker reconciles queued and on-air database state by reloading and, when still within the scheduled window, retaking the asset.

CG authority is also durable: boards, zones, feed sources, bulletins, approvals, managed content-addressed PNG/JPEG images, and append-only audit records are persisted with revisions. Cache-only resolved preview/current snapshot logic produces deterministic text, clock, feed, bulletin, image, and typed-placeholder content without network access. Channel orchestration atomically expires due scheduled bulletins before resolving the same timestamp's snapshot, while an explicit operator command remains available; both paths record append-only audit evidence. The worker receives bounded resolved snapshots over the protected protocol, and bounded HTTPS feed fetching is wired into the service lifecycle.

## Operator console

Live takeover is an explicit, channel-scoped control path exposed alongside the other loopback routes. A source is a revisioned typed `udp_mpeg_ts` endpoint with a station-local, private, loopback, or multicast bind address and a nonzero UDP port; arbitrary pipeline strings and non-local bind addresses are rejected. The API creates one active session per channel and advances it through `arming → armed → take_requested → on_air → return_requested → returning → completed` (or `failed`), with a bounded arm expiry. SQLite increments the channel authority revision and records every transition in an append-only audit trail under the same write transaction; stale authority or source revisions fail with a compare-and-set conflict. The controller arms the worker, consumes a short-lived token for exactly one take, blocks schedule dispatch while live is on air, and returns at the current wall-clock time to the active schedule item at its calculated offset (zero at an exact start boundary) or to fallback when no window is active. Ended pending/queued items become terminal `missed` records; prepared media remains parked until a valid take is requested. The return audit records the selected target and media offset, and disarm follows only after worker return acknowledgement. A worker restart, disabled channel, expired arm, failed return, clock reversal, excessive boundary crossing, or uncertain worker acknowledgment fails the session and stops the channel rather than inventing on-air truth. See [ADR 0033](docs/adr/0033-wall-clock-schedule-catch-up.md) for the schedule-window and handback contract.

`GET /` serves the production React console directly from `stationd`; it has no CDN, font, or Internet dependency. The responsive interface commissions station identity, reports live readiness, imports media, exposes durable ingest/transcode progress, configures channel outputs and stopped-channel headend profiles, blocks unavailable headend execution, shows local transport separately from receiver acceptance, lets a Setup Admin append explicit external receiver evidence, requests and follows bounded post-encode loudness proof, prepares and approves schedule commits, follows dispatch status, and guides commissioning from the authoritative report. Loudness proof requires audible active program: a silent capture produces no finite measurement and fails visibly. A proof whose channel is disabled or loses its controller becomes durable failed truth rather than remaining pending across lease recovery. Local decoded measurement never claims UDP delivery, SRT receiver acceptance, headend acceptance, or field success. Browser storage remembers only the operator's place in the workflow and cannot manufacture completion. Desktop and phone layouts use the same loopback origin and API authority.

The operator surface also manages revisioned CG boards, zones, feeds, bulletins, approvals, managed
PNG/JPEG image uploads and selection, and audit history, with cache-only resolved preview/current
snapshot state and an explicit manual expiration action alongside automatic due expiration. Image
uploads accept raw bytes only, are validated into the content-addressed station library, and expose
no caller-controlled path. The Live view configures UDP MPEG-TS sources, shows authority revisions and
the durable takeover audit, and requires deliberate confirmation for Take Live and Return to Schedule.
The report and installed proof harness expose commissioning truth, but schema v2 deliberately keeps
unimplemented local obligations blocked. Clean-machine acceptance and one-/three-station field
operation remain separate external gates.

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

### Shared Windows Sandbox ownership

Windows Sandbox is a shared host resource because independent TownLight and Claude work may use it.
No harness may assume that it is free or infer ownership from an idle-looking desktop, Hyper-V
service state, a process name, or an old file. Before any TownLight sandbox launch, run
`scripts\invoke-owned-windows-sandbox.ps1 -CheckOnly`, then launch only through the same script with
a committed TownLight-owned `.wsb` configuration beneath `harness\windows-sandbox\configs`.

The launcher serializes the check-and-launch boundary with the machine-global
`Global\Scott.SharedWindowsSandbox.Launch.v1` mutex. Check-only mode reports
`no_active_sandbox_detected`, `townlight_harness`, or `external_or_unclaimed`; it never reports the
resource as free. Every detected process blocks launch. TownLight attribution requires an exact
PID, creation identity, executable, owner namespace, and run-ID match against immutable records
beneath `%ProgramData%\Scott Shared Harness\WindowsSandbox\ownership-v1`. It never stops a process,
removes an ownership record, or overwrites evidence. A future cleanup tool may act only when its own
record matches that same exact identity. Claude-owned processes and files are a permanent no-touch
fence, regardless of apparent age or state. See
[ADR 0045](docs/adr/0045-shared-windows-sandbox-ownership.md).

The clean-machine installer gate is `scripts/prove-clean-install-in-owned-sandbox.ps1`. It requires
one clean source commit and an exact matching candidate manifest, then performs the atomic ownership
check and launch through the guard. The guest receives only read-only candidate, verifier, and
harness mappings; its sole host-writable boundary is
`%ProgramData%\TownLight Station\SandboxEvidence`. The guest shuts itself down after atomically
publishing evidence. A host timeout never terminates or adopts the shared Sandbox.

Run formatting, linting, and tests before committing:

```powershell
npm ci --prefix web/operator --ignore-scripts --no-audit
npm run build --prefix web/operator
scripts\invoke-cargo.ps1 fmt --all -- --check
scripts\invoke-cargo.ps1 clippy --offline --all-targets --all-features -- -D warnings
scripts\invoke-cargo.ps1 test --offline --workspace
```

The external MPEG-TS live-input regressions also require `ffmpeg.exe` on `PATH`. The worker-level gate that reproduces an already-running feed can be run with `scripts\invoke-cargo.ps1 test -p channel-worker --test process_contract spawned_worker_live_takeover_preserves_audible_audio -- --exact --test-threads=1`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the proof-first workflow and [LICENSE](LICENSE) for terms.
