# Townlight Station — Private beta operator manual

Current local candidate: 29 October 2026. Owned Station code is AGPL-3.0-only. Standalone CivicCast remains a separate product, installation, service and data store. This manual describes the existing CLI/service boundary and browser operator screen; there is no Station first-run wizard or automatic desktop provisioning.

## Provision the existing boundary

Complete the desktop first-run and canonical runtime activation in START-HERE.md first. Use named staff with the required meeting/capture and Records permissions. Station validates the actual local native session; an editable actor name is not authentication.

Provide a dedicated PostgreSQL database whose name ends in `_station`, a dedicated least-privilege login and a separate absolute Station data directory. Protect the database credential and data with the deployment's account permissions. Do not reuse CivicCast's service, database, settings or directories.

For Station's process, configure these explicit variables. Paths below assume the default MSI destination; use the actual destination if changed.

| Variable | Deployment value |
| --- | --- |
| `TOWNLIGHT_STATION_DATA_ROOT` | Dedicated absolute Station data directory, initially empty |
| `TOWNLIGHT_STATION_DATABASE_URL` | `postgresql+psycopg2://ROLE:SECRET@HOST:PORT/NAME_station` |
| `TOWNLIGHT_STATION_SUITE_URL` | Trusted configured Townlight runtime origin |
| `TOWNLIGHT_STATION_MEDIA_SOURCE` | `C:\Program Files\Townlight\_up_\runtime\payload\station\media-source` |
| `TOWNLIGHT_STATION_MEDIA_PYTHON` | `C:\Program Files\Townlight\_up_\runtime\payload\station\runtime\python.exe` |
| `TOWNLIGHT_STATION_MEDIA_RUNTIME` | `C:\Program Files\Townlight\_up_\runtime\payload\station\runtime` |
| `TOWNLIGHT_STATION_FFPROBE` | `C:\Program Files\Townlight\_up_\runtime\payload\station\bin\ffprobe.exe` |
| `TOWNLIGHT_STATION_MAX_SECONDS` | Bounded capture timeout, default 7200 |

Set `TOWNLIGHT_STATION_URL` on the paired Townlight runtime process to Station's trusted origin. Both origins must match the configured listeners. Use explicit loopback HTTP locally; a remote deployment requires the supported HTTPS boundary and separate acceptance. The verified native identity path is a single desktop profile, not remote multi-user/OIDC identity.

The installed normal-profile control Python is `%LOCALAPPDATA%\CivicSuite\runtime\python\python.exe`. Under the configured process environment run:

```powershell
$control = Join-Path $env:LOCALAPPDATA 'CivicSuite\runtime\python\python.exe'
& $control -m townlight_station.cli migrate
& $control -m townlight_station.cli serve --port 8756
# In a separate process with the same Station configuration:
& $control -m townlight_station.cli worker
```

`serve` is a foreground process. Keep the API and worker running under the chosen deployment account. Migrate explicitly before starting the updated roles; the API refuses an incompatible schema. The current schema includes durable scheduling/playout state. The existing `air` role is used when that operating profile is selected. This handoff does not install an automatic Windows SCM service. Service registration and physical equipment commissioning are deployment choices, not silently completed steps.

The normal-profile acceptance launched a separately configured installed Townlight HTTP process on another loopback port, sharing the actual native profile/database, and paired it with Station. It did not verify that setting an environment variable outside the app automatically changes an already-running native supervisor. Configure the paired runtime explicitly before using Station; retain this deployment distinction.

## Configure a trusted live input

In Station's data directory create administrator-managed `live-sources.json`, for example:

```json
[{"id":"chamber","label":"Council chamber feed","kind":"udp-mpegts","address":"127.0.0.1","port":19000}]
```

Use the intended interface and an unused receive port. Restrict the encoder/source network: UDP is not authenticated. The accepted synthetic test used generated video and a sine tone; no camera, microphone, headend, CDN or city network was commissioned.

## Meetings → Station → Records

1. Create the meeting and agenda in Meetings. Save the actual canonical records.
2. Sign in to Townlight. Launch the existing operator command from the staff session:

   ```powershell
   & $control -m townlight_station.cli operator --profile-root (Join-Path $env:LOCALAPPDATA 'CivicSuite') --port 8756
   ```

3. The command validates the native session and opens a short-lived, single-use browser launch ticket. Do not copy the native proof into a URL or another browser. Reopen after sign-out, expiry or API restart.
4. Select the named meeting, assigned agenda and configured live input. Start capture; distinguish waiting for a feed from recording. Stop drains the media pipeline. Wait for verified completion. A failed/incomplete attempt is not a completed downloadable recording.
5. Review the completed recording/provenance. A Records-authorized operator selects an existing Records request and explicitly attaches the recording. The receiver checks bytes/hash and retains a locked private original. Repeating the same handoff produces one canonical document.
6. Records separately performs release decisions, response approval and fulfillment. Attachment does not publish, redact, approve or fulfill a request. Sign out of the operator screen when finished.

Managed recorded-file input and supplied SRT captions remain supported. Preserve original bytes and provenance. This path does not perform automatic meeting speech recognition. Existing scheduling, playout and approval APIs are described in the technical README and retain their earlier scoped receipts; this normal-profile pass did not repeat every API operation.

## Recovery and evidence

Back up Station's database and evidence together, plus the paired Townlight profile through Core's coherent backup procedure. Stop writers before maintenance. The worker marks unfinished attempts interrupted on recovery; it does not silently re-air or reconstruct a lost live signal. Start a new live capture after a lost feed. Keep partial attempts and logs for diagnosis.

The current normal-profile Sandbox receipt verifies a real staff ticket, actual headless operator start/stop, independent audio/video decode, served hash, private Records readback, duplicate prevention and a locked original. Earlier installed media/operational receipts remain separate. No physical equipment, remote identity, automatic service commissioning or long-duration field operation is certified by these tests.
