# Stable output-branch proof — 2026-08-21

## Candidate identity

- Source commit: `6c566a8f8c7301aa260c729b7c4618088f077833`
- Installer SHA-256: `6F043D08F4AC4366417817491282472B94D8F412426FFC497ED2770002D95E81`
- Installed `stationd.exe`: `770A22FE2CCBB09BA0CE058BB873E0E698DCEB7FC258C7EF036B676E9895A6B0`
- Installed `channel-worker.exe`: `03FE37BCABC7D7107AD1C8E920BC34B75C9C96A600C689E53E40E3692C44794F`
- Installed `station-updater.exe`: `6E093B4147425686A33CA0D686D984E8ADA50DF2E0F126EC2886481FE1BF48E7`
- Bundled media runtime: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

## Automated gates

- Full Rust workspace tests passed across all targets.
- Full Clippy workspace passed across all targets and features with warnings denied.
- The production TypeScript/Vite operator build passed and exposed an optional redundant UDP destination.
- API tests persisted and round-tripped two stable sink identities, rejected an enabled channel with no enabled sink, and returned `409 channel_must_be_stopped` for a live reroute or profile change.
- Existing-database migration preserved all historical channel rows and backfilled each former UDP destination as one required `Primary transport` sink.
- The real orchestration test launched the real worker from two durable sink records and received valid MPEG-TS on both destinations.
- The media-engine test proved one persistent encoder/mux fans out to two receivers. A separate regression starts the fanout without receivers, waits three seconds, attaches two receivers concurrently, captures both for five seconds, and independently discovers progressive H.264 1280×720 on each. The parser now emits codec configuration at an explicit one-second interval for late receivers.

## Installed machine gate

The exact installer upgraded the preserved station database. Both automatic LocalSystem services were running, `/health` was ready, and the public API returned all eight preserved channels through the normalized sink contract.

The reusable `scripts/prove-installed-output-profile.ps1` instrument then configured two required `udp-ts` sinks through the installed API, received them simultaneously from the service-owned worker, validated MPEG-TS packet synchronization, parsed PAT/PMT and video PTS directly, measured both transport rates, required CBR null padding, and used FFprobe JSON to identify H.264 1280×720 progressive video at 60000/1001 on both captures:

```json
{
  "profile_id": "atsc-720p5994",
  "dimensions": "1280x720",
  "frame_rate_contract": "60000/1001",
  "measured_frame_rate": 59.94,
  "video_pts_intervals": 357,
  "measured_transport_bitrate_bps": 7988024,
  "redundant_transport_bitrate_bps": 7988024,
  "captured_bytes": 5997012,
  "redundant_captured_bytes": 5997012,
  "proven_output_branches": 2,
  "null_packets": 30369,
  "service_health": "ready"
}
```

The instrument disabled the proof channel in `finally`. The two sink rows remained durable for audit and reuse, the proof channel was stopped, station health remained ready, runtime safe-to-air was green, and the one separately enabled channel remained on verified fallback.

## Evidence-instrument correction

The initial dual captures exposed an intermittent false negative in `gst-discoverer-1.0`: it sometimes labeled the H.264 PID unknown even when the retained bytes contained valid codec configuration. FFprobe independently identified both retained captures as H.264 Constrained Baseline, 1280×720 progressive, and 60000/1001 with codec extradata. The installed gate therefore uses explicit FFprobe JSON fields for both branches while retaining independent transport, table, PTS, bitrate, and padding checks. This changes the evidence reader, not the acceptance contract.

## Honest boundary

This proves redundant UDP MPEG-TS fanout from operator/API contract through normalized SQLite rows, orchestration, the installed worker, one encoder/mux, two isolated queues, and two independent receivers. It does not yet prove independent downstream receiver telemetry, required-branch contribution to continuous safe-to-air status, destination failover, interlaced formats, SRT, SDI, NDI, HLS, an external cable headend, or the one-week/three-station field gates.
