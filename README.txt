> **Townlight Beta 1 — Station delivery update, 31 October 2026.** [Signed Windows installer, source, documentation and acceptance](https://github.com/townlight/townlight/releases/tag/townlight-beta-1-station-delivery). This edition documents the Sandbox-tested Station delivery additions. Existing component versions are retained. Earlier baseline results remain identified as historical evidence; the new acceptance report identifies the new installer.

# Townlight Station

The accepted suite implementation is in `beta-1/`. The existing Rust kernel remains a separate development line, as described in [the accepted boundary decision](docs/architecture/townlight-beta-1-boundary.md).

**Townlight Beta 1** · Existing component versions retained · AGPL-3.0-only

Station adapts a pinned CivicCast media implementation into an independent Townlight boundary. It captures managed files or configured live UDP MPEG-TS, retains canonical meeting/agenda context, and hands verified recording evidence to Records. Scheduling, playout, approval and local playback have distinct controls.

## Start here

Staff should use the operating manual. Administrators should read first-run, configuration and recovery before changing a deployment. Developers should read the exact API/schema reference and matching source companion. This edition accompanies the [published Station delivery update](https://github.com/townlight/townlight/releases/tag/townlight-beta-1-station-delivery).

- [Operating manual](https://townlight.github.io/manuals/station.html) · [Printable PDF](https://townlight.github.io/pdf/Townlight-Station-Manual-Beta-1.pdf) · [Editable Word manual](https://townlight.github.io/docx/Townlight-Station-Manual-Beta-1.docx)
- [API and data dictionary](https://townlight.github.io/api/station.html) · [OpenAPI JSON](https://townlight.github.io/api/station-openapi.json)
- [Product and suite directory](https://townlight.github.io/index.html) · [Terms and full license](https://townlight.github.io/terms.html)

## Exact version and candidate

| Identity | Value |
| --- | --- |
| Public documentation identity | Townlight Beta 1 |
| Installer version | 1.6.0.1 |
| Product component | townlight-station 0.3.0 |
| Native desktop source version | 1.6.0-1 |
| Candidate artifact identity | Station delivery update; use the release SHA256SUMS.txt |
| Corresponding source | Updated Station delivery candidate source companion |
| Current owned software license | AGPL-3.0-only |
| Supported acceptance profile | Windows x64 fresh install; inspections-preview; separate Station configuration |


“Townlight Beta 1” identifies this coordinated beta delivery. It does not renumber the installer, native binary, Python packages or historical receipts. Component versions may differ. This edition accompanies the newly packaged and signed MSI containing the Station delivery additions. The native desktop and other native payload files retain their accepted signed bytes. Historical release pages and manuals describe their own versions and are not the download instructions for this Station delivery candidate.

## What staff can do

- **Prepare meeting context:** Create and save the actual meeting and agenda in Meetings. Assign each selected agenda item to that meeting. A recording request cannot substitute an editable meeting label for the canonical records.
- **Open the operator:** Sign in as authorized staff. Use the configured local operator command with the explicit native profile root. It obtains a short-lived one-use browser ticket; the native proof is not placed in the URL.
- **Choose the input:** Select the meeting, assigned agenda and administrator-configured live-source ID. For recorded-file work, place the source in the managed ingest directory and optionally supply an SRT sidecar. Callers cannot submit arbitrary pipelines or filesystem paths.
- **Capture and stop:** Start capture and distinguish waiting for a signal from active recording. Stop requests pipeline drain. Wait for completed status and validated output; a partial, failed or interrupted attempt is not a completed recording.
- **Review recording evidence:** Inspect the media and retained context/hash. Retry is explicit for failed/interrupted recorded-file jobs. A lost live signal cannot be reconstructed: begin a new live recording and preserve the failed attempt.
- **Hand off to Records:** Choose an existing Records request and attach the reviewed recording. The receiver checks bytes/hash and retains a locked private original. Repeating the same handoff retains one canonical document.
- **Use playout deliberately:** Scheduling, dry-run/commit playout, staff approval and local publication are distinct API operations. Public playback only serves the separately approved packaged output. Nothing in capture or Records handoff automatically publishes it.
- **Close and recover:** Sign out of the operator. Back up Station’s database and media evidence together, separately from Core’s native profile backup. Restart interrupted work deliberately; do not automatically re-air a lost signal.

## Operating procedures and examples

The operating manual now includes these existing procedures, with synthetic inputs, readback and limits. Follow the actual app boundary identified there; a standalone source capability is not automatically a desktop screen.

- [Scheduling playout and recovery](https://townlight.github.io/manuals/station.html#scheduling-playout-and-recovery).
- [Schedule a recording and interpret its state](https://townlight.github.io/manuals/station.html#schedule-a-recording-and-interpret-its-state).
- [Apply the caption profile](https://townlight.github.io/manuals/station.html#apply-the-caption-profile).
- [Check playout and commit deliberately](https://townlight.github.io/manuals/station.html#check-playout-and-commit-deliberately).
- [Take a live override and return](https://townlight.github.io/manuals/station.html#take-a-live-override-and-return).
- [Approve and publish local playback](https://townlight.github.io/manuals/station.html#approve-and-publish-local-playback).
- [Recover without replaying unknown work](https://townlight.github.io/manuals/station.html#recover-without-replaying-unknown-work).
- [Prepare the normally activated synthetic profile](https://townlight.github.io/manuals/station.html#prepare-the-normally-activated-synthetic-profile).
- [Store the independent test configuration](https://townlight.github.io/manuals/station.html#store-the-independent-test-configuration).
- [Provision the Station database once](https://townlight.github.io/manuals/station.html#provision-the-station-database-once).
- [Start and check the paired processes](https://townlight.github.io/manuals/station.html#start-and-check-the-paired-processes).
- [Configure a live input and open the operator](https://townlight.github.io/manuals/station.html#configure-a-live-input-and-open-the-operator).
- [Bounds and shutdown](https://townlight.github.io/manuals/station.html#bounds-and-shutdown).

## Architecture and data

### Runtime and storage

The accepted package targets **Windows x64 fresh installation**. Its native shell uses **Rust 2021, Tauri 2 and Microsoft WebView2**; browser assets are JavaScript built with **Vite 8.1.6**. This describes the native desktop frontend; Station has a separate React operator, specified below or in its product manual. Canonical business workflows use bundled **CPython 3.13.5, FastAPI, Pydantic, SQLAlchemy and Alembic**, with **PostgreSQL 17 / PostGIS** and managed original files. The installed runtime tested here contains FastAPI 0.162.0. Package minimum Python requirements can differ from the bundled executable; the metadata reference preserves both. No stack rewrite is implied by the beta label.

The native transport is fixed to loopback `127.0.0.1:15480`; PostgreSQL uses the configured local runtime port (default `15432`). Optional local model service defaults to `127.0.0.1:15434`. Those are deployment defaults, not internet endpoints. The runtime has health, module and readiness endpoints; readiness is stronger than process existence. The task queue is the existing local Core task infrastructure, not an assertion that a separate Redis service is required for this desktop composition.

The default native profile is `%LOCALAPPDATA%\CivicSuite`; the historical directory name remains part of compatibility. Do not rename it to Townlight. A configured `TOWNLIGHT_PROFILE_ROOT` must be absolute. Local configuration and secrets live under that profile; protected managed files and database state must be backed up coherently. Source/runtime identity is distinct from a product’s display label.

### Data and consistency

Configured live-source IDs; canonical meeting/agenda snapshots; queued recordings and immutable attempts; input/output hashes; schedules; playout jobs and approvals; durable on-air state; publication packages; Records attachment provenance.

Versioned saves use expected prior revision identifiers where defined. Reopen and reconcile on conflict. Exact exports and handoffs use source IDs, current visibility, revision and SHA256. Review belongs to the saved revision and does not transfer silently to edited text. Database changes use the owning product’s migrations; a request or readiness check is not an authorization to mutate schema.

### Capacity and platform statements

This beta has synthetic single-profile workflow evidence. It does not have a published benchmark for concurrent staff capacity, minimum RAM/CPU, large-source limits or a field SLA. Do not turn a developer machine’s specification into a supported minimum. Installer size is 2,092,122,112 bytes before installation; allow additional disk for extracted runtime, database, originals, exports, media and backups. Select equipment/storage after sizing those actual workloads.

The exact [runtime payload lock](https://townlight.github.io/reference/runtime-payload-lock.json) records pinned files, sizes and hashes. The [component metadata](https://townlight.github.io/reference/component-metadata.json) records shipped dependency declarations. These references are more precise than a manually copied dependency list. Third-party and historical license notices must accompany their matching payloads.

### Station operator and media components

The native desktop frontend above is distinct from Station's browser operator. The pinned operator package `townlight-station-operator` is version **0.1.0**, using **React/react-dom 19.1.1**, **TypeScript 5.9.2**, **Vite 7.3.7** and the React Vite plugin 5.0.2. Station serves its built assets through its own operator API. Its Python control/API package remains **townlight-station 0.3.0**, while its retained storage identity remains **0.1.0**. These identities have different purposes and are not renumbered.

The separate pinned CivicCast-derived media engine uses Python/native GStreamer processing and bounded media children. It is not the standalone CivicCast application. Station's control database, data root, service/configuration and update identity remain independent. This describes the current mixed stack; no runtime rewrite or CivicCast migration is implied.

### Station media and lifecycle contract

The package/API version is `0.3.0`. Its retained storage identity has version `0.1.0`; that is a separate storage marker and must not be rewritten to match the package. Product/app IDs are `townlight-station` and `org.townlight.station`; service identity is `TownlightStation`, virtual account `NT SERVICE\TownlightStation`, credential namespace `townlight.station`, database schema `station`, and update-channel identity `townlight-station`. An identity name or SCM entry point does not establish that a Windows service was registered.

The control service is Python; its separate pinned CivicCast-derived engine uses native GStreamer media processing. There is no launch of the standalone CivicCast application. Default encoding is 1920×1080, 30 fps and 6000 kbps; explicit validated configuration may change it. The source lock is verified before use. Media children are isolated and bounded; unfinished attempts become interrupted on recovery.

Recorded-file requests accept a managed ingest filename, optional SRT sidecar and idempotency key. Live requests accept a configured source ID, not a path. Select exactly one file or live source; live capture cannot also supply a sidecar. Inputs are bounded by the configured two-hour default timeout and a 2,147,483,647-byte file limit. Supplied SRT sidecars are bounded to 5,000,000 bytes. The queue admits at most 100 pending/running jobs; live admission is further serialized. A repeated idempotency key must identify the same source bytes, canonical meeting context and encoding profile.

`queued` and `running` do not make content available as completed. Completion requires the worker’s clean media teardown and stream validation. Failed/interrupted file jobs may be explicitly retried; a live gap requires a new capture. Records receives verified bytes and canonical provenance as a locked private original. Local publication is an explicit later operation on separately approved playout; its MP4/WebVTT package is distinct from the retained recording and from a Records release.


| Data contract | Description |
| --- | --- |
| Input | A real Townlight staff session, saved Meetings records, a dedicated Station database/data root, a managed ingest filename or configured live-source ID, and optional supplied SRT captions for recorded-file input. |
| Retained records | Configured live-source IDs; canonical meeting/agenda snapshots; queued recordings and immutable attempts; input/output hashes; schedules; playout jobs and approvals; durable on-air state; publication packages; Records attachment provenance. |
| Output | Completed verified recording derivatives, retained original inputs/provenance, separately approved local playback packages and locked private Records evidence. |
| Connections | Meetings → Station → Records is the accepted recording workflow. Townlight owns current session authority; Station owns its database, storage and media children. Standalone CivicCast remains separate, with no shared configuration, service, data directory or automatic updater fallback. |


## Setup and operation

1. Obtain the identified signed Station delivery candidate and its corresponding-source companion from the beta coordinator. The landing pages in this documentation are not an installer download service.
2. On the intended fresh Windows test installation, verify the MSI hash and publisher signature before installing. Preserve unrelated profiles and recovery material. This candidate has fresh-install acceptance; historical in-place upgrades are unverified.
3. Launch Townlight under the intended named Windows account. Choose the storage folders. Select **Townlight Inspections candidate** (`inspections-preview`) for all eight desktop products. A narrower default selection will not expose all workspaces.
4. Enter the city profile, time zone and contacts. Create the first named local administrator, sign in and choose protected backup storage. **Continue without local AI** is valid for the deterministic workflows.
5. Run Health Check and wait for readiness. Open Townlight. In System Health select **Finish Records Migration** to activate the canonical Python/PostgreSQL runtime. Do not fabricate migration receipts or seed accounts.
6. Create named staff through Settings. Import authorized sources using each product’s source library. All examples in this package are synthetic; they are not municipal authority.
7. Provision Station separately if it is part of the beta exercise. Selecting the desktop composition does not start or configure Station. Configure vision explicitly only if automatic photo drafting is selected.

## Configuration
### Prepare the normally activated synthetic profile

This procedure describes a future isolated beta installation. Documentation generation does not execute it. Complete normal Townlight Setup under the intended Windows account, select `inspections-preview`, sign in, and use **Finish Records Migration**. Keep the native database running and confirm the canonical runtime is ready. Use that account's actual profile path; the default is `%LOCALAPPDATA%\CivicSuite`. Do not create session files, seed accounts or force `domain_enabled=True`.

Station needs its own database and initially empty absolute data directory. It uses the same named Townlight staff authority through an additional configured loopback suite process. It does not launch or modify CivicCast. No SCM installation, registry edit or replacement of the desktop's fixed-origin service is needed.

The examples reserve suite port **15481**, Station port **8756** and synthetic UDP input port **19000**. Choose unused ports and change both configured origins together if needed. Never point these instructions at a city's live profile or data. Use the actual installation directory if Townlight was installed elsewhere.

### Store the independent test configuration

Run the following in a deployment PowerShell session under the same Windows user. Keep this session for startup and shutdown. The dedicated parent holds configuration/logs; its `data` child is Station's initially empty managed store.

```powershell
$profilePath = Join-Path $env:LOCALAPPDATA 'CivicSuite'
$control = Join-Path $profilePath 'runtime\python\python.exe'
$payload = 'C:\Program Files\Townlight\_up_\runtime\payload\station'
$stationRoot = Join-Path $env:LOCALAPPDATA 'TownlightStation-Synthetic'
if (-not (Test-Path -LiteralPath $control)) { throw 'Complete normal Townlight setup first.' }
if (Test-Path -LiteralPath $stationRoot) { throw 'Existing Station directory: inspect it; do not overwrite.' }
New-Item -ItemType Directory -Path $stationRoot | Out-Null
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $stationRoot /inheritance:r /grant:r "${account}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot protect Station configuration directory.' }
New-Item -ItemType Directory -Path (Join-Path $stationRoot 'data'),(Join-Path $stationRoot 'logs') | Out-Null
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$randomBytes = New-Object byte[] 32
$rng.GetBytes($randomBytes)
$rng.Dispose()
$stationPassword = -join ($randomBytes | ForEach-Object { $_.ToString('x2') })
$stationPassword | ConvertTo-SecureString -AsPlainText -Force | ConvertFrom-SecureString |
  Set-Content -LiteralPath (Join-Path $stationRoot 'station-password.dpapi')
Remove-Variable stationPassword,randomBytes
```

The password file uses the current Windows user's DPAPI protection. It is not portable to a different user or machine without an explicit credential/recovery decision. Keep it out of source control, support bundles and public documentation. Reuse this protected configuration on restart rather than creating another database or password.

In the same session, establish the exact process environment. On a later restart, set `$profilePath`, `$control`, `$payload` and `$stationRoot` to these same existing locations, then repeat this environment/start section, **not** the first-time provisioning section.

```powershell
$secret = Get-Content -LiteralPath (Join-Path $stationRoot 'station-password.dpapi') -Raw |
  ConvertTo-SecureString
$stationPassword = [System.Net.NetworkCredential]::new('', $secret).Password
$nativePassword = (Get-Content -LiteralPath (Join-Path $profilePath 'config\secrets\postgres-password.txt') -Raw).Trim()
$encodedNative = [uri]::EscapeDataString($nativePassword)
$env:TOWNLIGHT_PROFILE_ROOT = $profilePath
$env:CIVICSUITE_DATA_DIR = Join-Path $profilePath 'Data'
$env:CIVICSUITE_FILE_STORAGE_DIR = Join-Path $profilePath 'Data\files'
$env:DATABASE_URL = "postgresql+asyncpg://civicsuite:${encodedNative}@127.0.0.1:15432/civicsuite"
$env:TOWNLIGHT_PRODUCT_PROFILE = 'inspections-preview'
$env:PORTAL_MODE = 'private'
$env:CIVICSUITE_RUNTIME_HOST = '127.0.0.1'
$env:CIVICSUITE_RUNTIME_PORT = '15481'
$env:TOWNLIGHT_STATION_URL = 'http://127.0.0.1:8756'
$env:TOWNLIGHT_STATION_SUITE_URL = 'http://127.0.0.1:15481'
$env:TOWNLIGHT_STATION_DATA_ROOT = Join-Path $stationRoot 'data'
$env:TOWNLIGHT_STATION_DATABASE_URL = "postgresql+psycopg2://townlight_beta_station_app:${stationPassword}@127.0.0.1:15432/townlight_beta_station"
$env:TOWNLIGHT_STATION_MEDIA_SOURCE = Join-Path $payload 'media-source'
$env:TOWNLIGHT_STATION_MEDIA_PYTHON = Join-Path $payload 'runtime\python.exe'
$env:TOWNLIGHT_STATION_MEDIA_RUNTIME = Join-Path $payload 'runtime'
$env:TOWNLIGHT_STATION_FFPROBE = Join-Path $payload 'bin\ffprobe.exe'
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
Remove-Variable nativePassword,encodedNative,stationPassword,secret
```

Use the actual native PostgreSQL port/database/user if the deployment differs from the accepted defaults. Environment changes here apply to children of this shell, not the already running native supervisor. Keep secrets out of command arguments and output. The paired runtime deliberately uses normal `verify_activation` through its standard module entry point.

### Provision the Station database once

This first-time step creates only the dedicated synthetic role/database. Its role owns that database and its Station schema, with no superuser/global administration grant. It connects to the normally provisioned local database using the current profile's protected credential. Existing role/database names cause a refusal before creation; do not drop or repurpose them automatically.

```powershell
@'
import os
from pathlib import Path
import psycopg2
from psycopg2 import sql
from sqlalchemy.engine import make_url

url = make_url(os.environ['TOWNLIGHT_STATION_DATABASE_URL'])
profile = Path(os.environ['TOWNLIGHT_PROFILE_ROOT'])
password = (profile/'config/secrets/postgres-password.txt').read_text().strip()
conn = psycopg2.connect(host='127.0.0.1', port=15432, user='civicsuite',
                        password=password, dbname='postgres')
conn.autocommit = True
try:
    with conn.cursor() as cur:
        cur.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (url.username,))
        role_exists = cur.fetchone() is not None
        cur.execute('SELECT 1 FROM pg_database WHERE datname=%s', (url.database,))
        if role_exists or cur.fetchone() is not None:
            raise RuntimeError('Dedicated role/database already exists; inspect before continuing')
        cur.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD %s').format(
            sql.Identifier(url.username)), (url.password,))
        cur.execute(sql.SQL('CREATE DATABASE {} OWNER {}').format(
            sql.Identifier(url.database), sql.Identifier(url.username)))
finally:
    conn.close()
'@ | & $control -B -
if ($LASTEXITCODE -ne 0) { throw 'Station database provisioning did not complete; inspect before retrying.' }
& $control -B -m townlight_station.cli migrate
if ($LASTEXITCODE -ne 0) { throw 'Station migration failed; retain logs and inspect the dedicated schema.' }
```

If database creation fails after the role was created, the operation is partially complete: inspect that exact owned role/database before resuming. Do not rerun against guessed names or remove unrelated objects. On normal restarts, the database already exists; skip creation and use the existing identity. `migrate` is explicit; API requests do not substitute for schema migration.

### Start and check the paired processes

Use the same configured session. The standard suite entry point verifies the existing native activation. Check readiness of that exact paired origin, not only the desktop's canonical port. Startup below creates hidden children with private log files and records their identities; it does not focus windows or install services.

```powershell
$owned = @()
function Start-StationRole([string]$name, [string[]]$arguments) {
  $p = Start-Process -FilePath $control -ArgumentList $arguments -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $stationRoot "logs\$name.out.log") `
    -RedirectStandardError (Join-Path $stationRoot "logs\$name.err.log")
  $script:owned += [pscustomobject]@{ Name=$name; Id=$p.Id; StartTime=$p.StartTime; Path=$p.Path }
  $script:owned | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stationRoot 'owned-processes.json')
  return $p
}
function Wait-StationReady([string]$url, $process) {
  for ($attempt=0; $attempt -lt 40; $attempt++) {
    if ($process.HasExited) { throw "Owned process exited; inspect logs for $url" }
    try {
      $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2
      if ($response.StatusCode -eq 200) { return }
    } catch { }
    Start-Sleep -Milliseconds 250
  }
  throw "Readiness not established: $url; inspect the owned logs"
}
$suite = Start-StationRole 'paired-suite' @('-B','-m','civicsuite_runtime.services')
Wait-StationReady 'http://127.0.0.1:15481/ready' $suite
$api = Start-StationRole 'station-api' @('-B','-m','townlight_station.cli','serve','--port','8756')
Wait-StationReady 'http://127.0.0.1:8756/health' $api
$worker = Start-StationRole 'station-worker' @('-B','-m','townlight_station.cli','worker')
# Only for the on-air profile:
$air = Start-StationRole 'station-air' @('-B','-m','townlight_station.cli','air')
$owned | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stationRoot 'owned-processes.json')
```

Before launching, confirm ports 15481 and 8756 are unused. If readiness fails, inspect the relevant logs and retained profile/schema state; do not start another copy. A port that answers belongs to the intended process only when its ownership is established. A single worker owns a database. `worker --once` performs a bounded pass; it is not the normal continuous worker. The `air` role follows durable on-air state and emits the local program; it does not commission a physical transmitter or CDN. Its currently configured local output is under Station's `air` directory. Back up the dedicated database and media together; Core's native backup does not cover them.

### Configure a live input and open the operator

Create `live-sources.json` under the Station **data** directory with the following fictional input. An actual sender must supply UDP MPEG-TS to that receiver; this configuration alone does not generate a signal.

```json
[{"id":"synthetic-chamber","label":"Synthetic chamber feed","kind":"udp-mpegts","address":"127.0.0.1","port":19000}]
```

At most 32 rows are accepted. IDs are unique 1–64 letters/digits/underscore/hyphen; labels have 1–100 characters; IPv4 and ports 1024–65535 are validated. Each row has exactly the five shown keys. Protect the source network; UDP is not authenticated. Then, under the same signed-in native account:

```powershell
& $control -B -m townlight_station.cli operator --profile-root $profilePath --port 8756
```

This normal operator command opens the operator for a future staff exercise. It reads the native session and exchanges a one-use launch ticket; it does not seed authority or put the native proof in a URL. Ticket expiry is 60 seconds, operator cookie lifetime 900 seconds, with at most 256 outstanding tickets/sessions each. Reopen normally after expiration, API restart or sign-out. Changing database/configuration identities does not repair a staff permission refusal.

### Bounds and shutdown

Default encoding is 1920×1080, 30 fps, 6000 kbps. Optional Station variables permit `MAX_SECONDS` 10–86400 (default 7200), even `WIDTH` 320–3840 and `HEIGHT` 180–2160, `FPS` 1–60 and `BITRATE_KBPS` 128–50000; each is prefixed `TOWNLIGHT_STATION_`. The media source lock and exclusive data identity are verified before use. The dedicated database name must end in `_station`; unowned nonempty storage and escaping/reparse paths are refused. The existing SCM entry point is not a claim of installed service acceptance.

To finish an exercise, stop live capture from the operator and wait for its completed or failed terminal state, cancel unwanted future schedules, sign out and preserve the evidence/logs. Stop only the children you started, checking identity before using a saved PID:

```powershell
foreach ($entry in @($owned | Sort-Object StartTime -Descending)) {
  $process = Get-Process -Id $entry.Id -ErrorAction SilentlyContinue
  if ($process -and $process.StartTime -eq $entry.StartTime -and $process.Path -eq $entry.Path) {
    Stop-Process -Id $process.Id
    $process.WaitForExit()
  }
}
```

Retain the ownership file if the shell must close; parse and verify its actual process paths/start times before acting. Do not terminate the native database/supervisor or CivicCast. Interrupting a worker during a job preserves an interrupted attempt, not completed media; recovery is described in the Station operating procedure. Clear secret-bearing environment values when closing the dedicated deployment shell. Do not delete the database/data root to “reset” an uncertain result.

## API usage
### Native command example

The following JavaScript belongs in the existing trusted Townlight frontend/development environment with `invoke` imported from `@tauri-apps/api/core`. It is not a command for an arbitrary web page or an instruction for nontechnical staff. IDs must come from records created in an isolated synthetic exercise.

```javascript
import { invoke } from '@tauri-apps/api/core';
const result = await invoke('meetings_domain_request', {request: {operation: 'get-meeting', id: meetingId}});
console.log(result);
```

### Trusted local HTTP example

An administrator/developer can inspect an already-configured isolated runtime from a trusted local process. This PowerShell example reads the signed-in synthetic profile’s existing proof into memory; it does not create a user, seed a session, reveal the proof in output or bypass a permission check. Do not use real city data in development examples. A read response does not prove approval or publication.

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8756/health'
# Authenticated recording operations additionally require current native proof.
```

### A synthetic write with subsequent readback

This example demonstrates the existing Meetings HTTP contract in an isolated exercise. It is intentionally fictional. It does not start a service. Its authentication setup is included so it can be run independently under the signed-in synthetic account. If a write times out, inspect current records before repeating it.

```powershell
$profilePath = Join-Path $env:LOCALAPPDATA 'CivicSuite'
$sessionPath = Join-Path $profilePath 'config/local-session.json'
$session = Get-Content -LiteralPath $sessionPath -Raw | ConvertFrom-Json
$headers = @{ 'X-Townlight-Session' = $session.session_hash }
$body = @{
  title = 'Synthetic documentation exercise'
  scheduled_start = '2026-11-05T18:00:00-07:00'
  location = 'Synthetic room'
  meeting_type = 'regular'
} | ConvertTo-Json
$meeting = Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:15480/desktop/v1/meetings/meetings' `
  -Headers $headers -ContentType 'application/json' -Body $body
Invoke-RestMethod `
  -Uri ('http://127.0.0.1:15480/desktop/v1/meetings/meetings/' + $meeting.id) `
  -Headers $headers
Remove-Variable session, headers
```

This write requires Meetings to be enabled and the signed-in staff role to permit it. It is a cross-suite integration example; for this product’s request models, use the complete technical reference below.

### Recording and Records handoff example

An authenticated local client sends `POST http://127.0.0.1:8756/v1/recordings` with `X-Townlight-Session` and this JSON, replacing the synthetic UUIDs with saved synthetic meeting/agenda IDs. Configure `synthetic-chamber` first.

```json
{
  "meeting_id": "11111111-1111-4111-8111-111111111111",
  "agenda_ids": ["22222222-2222-4222-8222-222222222222"],
  "live_source_id": "synthetic-chamber",
  "idempotency_key": "synthetic-docs-capture-001"
}
```

The initial response is `202` with a queued/running identity rather than completed media. Read `/v1/recordings/{identity}`, explicitly stop live capture through `/v1/recordings/{identity}/stop`, and wait for completed status before reading `/content`. Then the authorized Records client calls `POST /desktop/v1/requests/{request_id}/station-recordings` on the paired Suite runtime with:

```json
{
  "recording_id": "33333333-3333-4333-8333-333333333333",
  "expected_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

The shown hash is deliberately a placeholder. Use the actual completed recording’s hash. A mismatched hash must fail. Repeating the identical completed handoff is idempotent; it does not create a release copy or fulfill a request.

## MCP status

No direct product MCP server is exposed by the accepted Townlight desktop or Station boundary. Do not configure an invented `townlight mcp` command or assume an AI client has staff permissions. The integration surfaces for this beta are native IPC, the documented HTTP APIs, source bundle imports, exact exports and the existing Station CLI.

The bundled transcription code includes a **TextFlowKit MCP adapter** as a separate source capability. It offers job-based `transcribe_media`, `get_transcript`, `list_jobs`, `get_job_status` and `export_transcript`, and additional tools declared in that adapter. It uses stdio first and optional Streamable HTTP, with the MCP SDK as an optional extra. It is not automatically launched, authenticated as Townlight staff or accepted as a Townlight product MCP deployment. This documentation does not add such a service.

For a transcription client already provisioned separately, an illustrative tool call is:

```json
{"name":"get_job_status","arguments":{"job_id":"synthetic-existing-job-id"}}
```

Use a job ID returned by that transcription service. This example does not create a Townlight record or bypass its review/Records handoff. The exact optional adapter tool signatures are supplied in the [MCP source reference](https://townlight.github.io/reference/mcp.html). Station meeting capture does not become live speech recognition because this optional donor adapter exists.

## Verification and known limits

| Product | Recorded result | Precisely exercised |
| --- | --- | --- |
| Station | PASS | Actual native staff session and normally created meeting; headless operator live start/stop, decoded generated audio/video, served hash, private Records readback, idempotent locked original and logout. |


The following baseline results come from the previously accepted signed candidate’s fresh normal-profile Sandbox exercise: real native IPC/HTTP/PostgreSQL and selected headless controls. All accounts, source bundles and media were synthetic. The guest install/uninstall and recovery passed, and the owned Sandbox stopped. No host installed application or standalone CivicCast was changed. Those baseline receipts are retained for unchanged workflows. The accompanying ACCEPTANCE.md records the separate installed verification and exact identity of this new Station delivery candidate.

The [workflow matrix](https://townlight.github.io/reference/workflow-matrix.json) identifies raw receipt filenames. The [evidence index](https://townlight.github.io/reference/sandbox-evidence-index.json) retains their original hashes; raw receipts remain in the private beta handoff rather than being silently republished with the documentation site.

The limits remain: actual city field acceptance and equipment/network commissioning; Station automatic provisioning/SCM commissioning; every UI/role combination; field recognition accuracy; and historical in-place upgrades are not certified. They are not five invented feature requirements. Fresh-install controlled beta handoff remains the accepted scope. This package makes no city-deployment, publication or general-availability claim.

There is no Station first-run wizard or automatic activation by the desktop composition. The accepted normal-profile path uses an explicitly configured paired Townlight HTTP process. Physical encoder/network, remote identity, SCM commissioning, CDN delivery and long-duration field quality are not certified. Station does not perform automatic meeting speech recognition.

## Development and repository map

Use the corresponding source for the candidate rather than assuming the public default branch matches it. The source companion contains `installed-owned-source`, product build inputs, desktop Rust/browser sources, locks and `BUILD.md`. It excludes credentials and third-party binary/model payloads. Use the recorded runtime lock when reproducing packaging.

Relevant source contracts:

- `townlight_station/api.py`
- `townlight_station/boundary.py`
- `townlight_station/operations.py`
- `townlight_station/operator.py`
- `townlight_station/worker.py`
- `townlight_station/airplay.py`

The desktop package defines `npm run build`, `npm test`, `npm run test:browser` and its Tauri command. Python products retain their own `pyproject.toml`, tests and migrations; use the matching checkout’s declared dependencies and test configuration. Do not run a sample server against a production profile. This documentation build validates documentation only and reuses the separately identified installed acceptance; it does not claim a new complete application CI run.

## Recovery and support

Save work and use Core’s canonical **Backup Now** before maintenance. That operation stops writers. Preserve the coherent profile/database/document backup and its receipt, then use **Start** to resume. A source export is not a full backup.

Use **Restore Latest Backup** through the supported controls. If the result says **Restore needs service start**, use **Start**, then **Check**, and reopen a known record. Do not repeat restore merely because services need to start. The fresh-profile acceptance verified that a fulfilled request survived restore and a post-backup marker did not.

Keep Station’s dedicated database and media evidence together in its own backup. Core’s native-profile backup must not be assumed to include an external Station data directory. Stop writers during a coordinated backup. Preserve failed media attempts and logs.

On failure, record the time, selected composition, component/installer versions and exact message. Check System Health for stopped, starting, ready or failed dependencies. Use the existing Retry control after readiness; do not create duplicate business records to refresh a stale view. Share only a redacted diagnostic bundle. Preserve secrets, original evidence, recovery material and unrelated installations.

## License and responsible use

Current owned code is AGPL-3.0-only. Preserve full license, matching corresponding source, dependency notices and historical grants. Terms of use do not remove license rights. Source imports do not confer permission to publish third-party material. Review all automatic drafts and use the separate owning-product decision before official action. CivicCast remains independent.


## Station video delivery additions

Staff can upload a verified completed recording to YouTube with explicit approval, title and visibility. Live recordings can simultaneously offer an encrypted SRT feed to the separate CivicCast/PEG operator. Recording continues independently of receiver availability. See the [updated Station manual](https://townlight.github.io/manuals/station.html#completed-recordings-on-youtube-and-live-civiccast-coverage) for setup, API and recovery.
