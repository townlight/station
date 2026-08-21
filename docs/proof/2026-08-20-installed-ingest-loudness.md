# Installed ingest-loudness proof — 2026-08-20

## Candidate identity

- Source commit: `62cd99090b80134c908a091cd6baa954eb44fdc2`
- Installer SHA-256: `03BADECC1C43FA268E0A283B04979340D502A30514A1C46F1F3E977255BB02B4`
- Installed `stationd.exe` SHA-256: `92830A2FE2CF0093B01091F3F9728F1662D9A2445BAF02FE4187CA93EEC51F34`
- Installed `channel-worker.exe` SHA-256: `EE764D0AC8F269DACB72B51DFC7B67234734CDC43B5BF9A8F161A4228B9DD0F9`
- Private media-runtime SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

The candidate passed the 19-requirement installer contract, installed with exit code zero, preserved the existing station database, and passed the expanded elevated verifier. That verifier confirmed the exact receipt and binary hashes, the running `LocalSystem` service, ready health, and all required media factories including `ebur128level` from the private runtime.

## Installed scenario

Known real H.264/AAC media was submitted to the installed service as durable job `6b3ad9e1-941c-4f5f-8518-31b3e398029f`. The request returned HTTP 202; the background worker completed on its first attempt and converged on content identity `39f07788314d8bd53cc7d4d57d34a9e14af800a09ead5cdd6600980652dfdd6d` in the station-owned library.

The installed analyzer measured integrated loudness at `-2.653 LUFS`. Against the ingest target of `-24.000 LUFS` with ±2 LU tolerance, the persisted gate correctly reported `failed` and supplied this operator detail:

```text
Measured -2.65 LUFS; ingest target is -24.00 LUFS ±2.00 LU. Normalize before air.
```

Both `GET /api/v1/assets/{asset_id}/readiness` and the readiness dashboard returned the same status, measurement, target, and detail. The asset remained valid finite media, the job and loudness analysis were durable, and the service remained ready. No passing value was invented for the intentionally out-of-target fixture.

## Scope

This proves the exact installed candidate can measure, persist, and expose ingest-time loudness on this Windows station. It does not claim that out-of-target audio is already normalized; the following durable transcode/conform slice must create corrected air-ready media and make Commit-to-Air enforce that result.
