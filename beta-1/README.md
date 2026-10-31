> [Townlight Beta 1 Station delivery update](https://github.com/townlight/townlight/releases/tag/townlight-beta-1-station-delivery) · [Current manual](https://townlight.github.io/manuals/station.html). New live CivicCast feed and completed-recording YouTube uploads are documented in [DELIVERY-INTEGRATIONS.md](DELIVERY-INTEGRATIONS.md). The exact source archive preserves original private preparation notes.

# Townlight Station service boundary

This AGPL-3.0-only package owns the Station service/API, PostgreSQL job ledger,
private recording store and media adapter. It uses a pinned CivicCast media
implementation; it does not start CivicCast's application or installer. The
standalone CivicCast product remains independent.

## Identity and configuration

Product/app ID: `townlight-station` / `org.townlight.station`. Windows service:
`TownlightStation`; virtual account: `NT SERVICE\TownlightStation`. Credential
namespace and update-channel identity are `townlight.station` and
`townlight-station`. There is no automatic updater or upstream CivicCast update
fallback. Unknown storage identities/versions require an explicit migration.

Configure the following environment **only for the Station process/service**:

| Variable prefix `TOWNLIGHT_STATION_` | Value |
|---|---|
| `DATA_ROOT` | Absolute dedicated Station data directory; initially empty |
| `DATABASE_URL` | `postgresql+psycopg2://<station-role>:<secret>@<host>:<port>/<name>_station` |
| `SUITE_URL` | Trusted Townlight runtime origin; HTTPS or explicit loopback HTTP |
| `MEDIA_SOURCE` | Separate pinned CivicCast source payload matching `media-lock.json` |
| `MEDIA_PYTHON` | Absolute compatible native media Python executable |
| `MEDIA_RUNTIME` | Runtime root containing `dependencies/gstreamer` |
| `FFPROBE` | Absolute ffprobe executable |
| `CONTROL_PYTHON` | Station control Python executable; required by SCM entry point |
| `WIDTH`, `HEIGHT`, `FPS`, `BITRATE_KBPS` | Defaults 1920, 1080, 30, 6000; captured into each job |
| `MAX_SECONDS` | Bounded media-process timeout; default 7200 |

The Townlight runtime must separately receive `TOWNLIGHT_STATION_URL` pointing
to the trusted Station origin and use the paired Records integration change.
No redirects, environment HTTP proxy discovery, guessed service URL or cached
authorization fallback is used. Do not expose these services over plain HTTP
outside loopback. The current native identity integration targets a single
Townlight desktop profile; multi-user remote/OIDC deployment is further work.

Provision a dedicated PostgreSQL database and least-privilege Station login.
Run `townlight-station migrate` explicitly before `serve` or `worker`. The API
checks schema version without performing migrations. Station touches only its
`station` schema. Media children receive no database or provider credentials.

For an isolated source runtime, add this directory to `PYTHONPATH` and use:

```text
python -m townlight_station.cli migrate
python -m townlight_station.cli serve --port 8756
python -m townlight_station.cli worker
```

`worker --once` processes at most one queued job and is useful for acceptance.
Only one worker owns a database at a time. Windows child containment is required;
failure to establish it stops worker startup. A crashed owner's native children
are reaped; a later worker marks unfinished attempts interrupted. Retrying is
an explicit authenticated operation, not an endless automatic crash loop.

`townlight_station.windows_service.StationService` is the separate SCM entry
point, with bounded child restarts and independent API/worker subprocesses.
Service registration, service-account provisioning, installer signing and SCM
lifecycle acceptance are deployment work. They were not executed by this task.
Do not reuse or reconfigure the CivicCast service. The service API uses loopback
port 8756 and fails on a conflict rather than stopping another listener.

## Recorded-file workflow and authority

1. A clerk creates a meeting and assigns agenda items through Meetings.
2. An operator places a source media file, and optionally a reviewed SRT file,
   in Station's managed `ingest` directory. The source recording path is never
   accepted from a remote caller. Live UDP capture uses the operator workflow below;
   direct camera-device adapters and public broadcasting remain separate work.
3. `POST /v1/recordings` takes `meeting_id`, optional `agenda_ids`, `source_file`,
   optional `captions_file` and `idempotency_key`. Forward the current
   `X-Townlight-Session` proof. Townlight validates identity/role, and Station
   retrieves canonical meeting/agenda snapshots through Meetings HTTP routes.
4. Station copies bounded immutable inputs, records hashes/actor/profile and
   queues a durable job. A repeated key must identify the same bytes, snapshot
   and encoding profile. The queue admits at most 100 pending/running jobs.
5. The worker calls the pinned native GStreamer implementation in a child
   process. Completion requires clean worker teardown and audio/video stream
   metadata validation. Acceptance also independently decodes the output. Failed/interrupted output is not downloadable as complete.
6. `GET /v1/recordings/{id}` returns status/provenance; `/content` returns verified
   recording bytes to an authorized clerk or Records staff member. A supplied
   sidecar replaces decoder captions in the derivative; without one, original
   captions are reinserted. Original input bytes remain retained. There is no
   automatic speech recognition or claim of universal caption quality here.
7. A Records officer calls
   `POST /desktop/v1/requests/{request_id}/station-recordings` with
   `recording_id` and `expected_sha256`. Records obtains the bytes from its
   configured Station service, checks size/hash, and attaches canonical locked
   evidence plus meeting/agenda/actor provenance. Retry produces one attachment.
   This is private evidence; it creates no release copy or public publication.

`POST /v1/recordings/{id}/retry` permits only failed/interrupted recorded-file jobs and requires
clerk capability. All authenticated operations revalidate the current Townlight
session. Authority outage fails closed for new requests; queued media work can
finish independently. PostgreSQL remains a required job-store dependency.

## Validation scope

The task's `outputs/station-boundary` retains real PostgreSQL migrations, actual
loopback HTTP acceptance, native worker crash/containment/retry, decoded captions,
private Records handoff, duplicate prevention and process-restart evidence.
Source is pinned; the media runtime is the separately verified retained Python
3.12.10/GStreamer 1.28.5 pack, not a newly rebuilt signed current installer.

The original recorded-file evidence is complemented by live/UI acceptance in
`outputs/station-live`. These are source/package milestones. It does
not certify the Windows service under SCM, installation/update/coexistence,
long-running live playout, camera/headend/vendor connectivity, installed desktop integration,
public playback, AI transcription, multi-city isolation, or beta-city readiness.
See the nine-product plan for the remaining acceptance.

## Live capture and operator screen (0.2.0)

Station accepts administrator-configured **UDP MPEG-TS** video/audio feeds. This is a real live receiver, not a device simulator or an arbitrary pipeline field. In the private Station data directory, create `live-sources.json`, for example:

```json
[{"id":"chamber","label":"Council chamber feed","kind":"udp-mpegts","address":"127.0.0.1","port":19000}]
```

Use the intended network interface address and an unused receive port for an external encoder. Only trusted administrators should edit this file. Receive traffic is not authenticated by UDP; restrict the source/network at deployment. Camera, encoder and network commissioning remain site-specific. Direct USB-camera and headend adapters are not claimed by this increment.

Run the explicit `migrate` command before starting the updated API and worker. Schema v2 adds durable stop controls and preserves v1 evidence. The storage identity marker stays at its existing 0.1 format; the package/API version is 0.2.0. Back up the database and evidence together before a deployed upgrade. No installed upgrade was performed in this work.

After signing into Townlight, launch the browser from the staff's local session:

```powershell
townlight-station operator --profile-root 'C:\explicit\townlight-profile' --port 8756
```

This command requires the explicit profile path but does not require Station's database/media credentials. It reads the local native proof, asks Station to validate it with Townlight, and opens a single-use launch ticket. The native proof is never put in the URL or browser storage. Tickets expire after 60 seconds; operator sessions use HttpOnly/SameSite cookies, expire after 15 minutes, and still validate the current Townlight session for every protected action. Sign out/revocation or API restart requires reopening. This launch command is source tooling; an installed desktop launch button is not delivered here.

The React screen offers named meetings, assigned agenda items and live inputs. Start is disabled while Station is busy. It distinguishes waiting for input from writing media; Stop sends EOS, drains the mux and verifies the result. Automatic duration limits are displayed. A missing or lost feed fails visibly with partial attempt files retained. Live gaps cannot be replayed by retrying an old capture. Start a new recording instead. Completed captures include the input configuration, meeting/agenda snapshots, actor, stop attribution, attempts and verified media hash.

Records staff open Station using their own current Townlight session and select a named Records request. Attachment is private, independently hash-verified and idempotent; it does not approve disclosure or publish anything. Clerks without Records permission do not see the attachment controls, and server authorization also rejects the operation.

The operator UI source is in `operator-ui/`. Its dependency lock comes from the existing Station React/Vite stack. Rebuild with `npm ci` then `npm run build` in that directory. Build output is bundled in `townlight_station/ui/`; React/React DOM/scheduler MIT notices are included. Full installed-device commissioning, playback preview, continuous broadcasting, publication, transcript review, remote identity and long-duration field acceptance remain open.

## Basic operating workflow — schedule, playout, approval, playback (0.3.0)

Schema v3 adds durable `station.schedules`, `station.playouts` and a single-row
`station.on_air` state. Run the explicit `migrate` before `serve`/`worker`; v1/v2
evidence is preserved and an unmigrated schema is refused. The storage identity
marker keeps its 0.1 format.

**Scheduling.** `POST /v1/schedules` takes `meeting_id`, optional `agenda_ids`,
one of `source_file`/`live_source_id`, an optional reviewed `.srt`, an explicit
timezone-aware `start_at` and an `idempotency_key`. Only a capture-capable staff
session may create one; read-only roles and anonymous callers are refused. The
worker claims due schedules under an advisory lock and dispatches each into an
ordinary recording job **exactly once** — a repeated key returns the same
schedule, a key reused for different evidence is a conflict. A schedule more than
ten minutes late is marked `missed` and is never silently aired as if on time.
`POST /v1/schedules/{id}/cancel` cancels only a not-yet-dispatched schedule.

**Recording** is unchanged in substance: the dispatched job copies bounded
immutable inputs, runs the pinned media worker, and completes only on clean
teardown plus audio/video probe. Caption handling is the current minimum profile:
a supplied sidecar **replaces** captions, otherwise embedded captions are
**preserved**. There is no ASR here and no live-caption claim while that profile
is unresolved.

**Playout is not publication.** `POST /v1/playouts` takes `recording_id` and
`mode` of `dry-run` or `commit`. A dry run verifies the source, profile and pinned
implementation and emits **no** program output. A commit re-encodes the verified
recording into real broadcast output (MPEG-TS with audio, video and preserved
captions), hashes it and switches `station.on_air` to that playout.
`POST /v1/on-air/return-to-schedule` explicitly returns the station to schedule
mode. No OTT, EAS, underwriting, publish-provider or multi-station path is used.

**Staff-approved local playback.** `POST /v1/playouts/{id}/approve` records the
approving actor, role and note; `POST /v1/playouts/{id}/publish` requires that
approval and then serves the byte-identical program at `/v1/public/playback/{id}`
(local loopback fixture only). Before approval *and* before publication that route
returns 404 — an unapproved or unpublished playout is indistinguishable from a
missing one, and approval alone does not publish. Publishing is idempotent. No
external publication or Records disclosure approval is created anywhere here.

**Recovery.** On start the single owning worker marks previously `running`
recordings and playouts `interrupted` (with the attempt closed) and, if the station
was left on air, returns `on_air` to schedule mode while retaining the superseded
state under `recovered_from`. Nothing is resumed blindly and no media is re-aired.

**Verification performed (synthetic, source stage).** `tests/accept_operational.py`
runs three foreground stages (`setup`, `schedule`, `playout`) against a disposable
task-owned PostgreSQL cluster, a synthetic authority fixture and the real media
worker; `tests/verify_recovery.py` stages the exact state a killed worker leaves
and asserts restart reconciliation. `tests/test_operations.py` covers the request
contract and the approval-before-publication ordering without a database. See
`outputs/beta-plan/station-operational-01/`.

**Not claimed.** Physical headend/receiver output, a specific portal/CDN publish
target, live ASR/live-caption profile, HLS/VOD packaging, long-duration continuous
broadcasting, operator-screen controls for schedule/playout (API and worker only
in this increment), installed service/SCM behavior and any city data.
