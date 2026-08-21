# Installed schedule-dispatch proof — 2026-08-20

## Candidate identity

- Source commit: `2b4fa16c9e22e27713c0247697669ff3696d4f51`
- Installer SHA-256: `713868F7B12A5C2A7EEC2854C06153D05DDA42314481EF473F32A5B53CE840EB`
- Installed `stationd.exe` SHA-256: `21AD274A0444106302E40BC1DD03FCB54801EBD721C0E8F5C7C97E5F1BD7F20F`
- Installed `channel-worker.exe` SHA-256: `E63FB1C91FA308A405B6617C1055200A65D29E738BF42CDCAADBFD91CC7EE9C2`
- Private media-runtime SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

The candidate passed the 18-item installer contract, installed with exit code 0, preserved the existing station database, started `TownLightStation` automatically, and returned `{"database":"ready","status":"ready"}` from the installed loopback service.

## Scenario

The proof generated a finite MPEG-TS asset containing real H.264 video and AAC audio, hashed it as `8f327dc1ab4429263c78baf3397aa87e7687004746d8f769c2ce0d3985c8e06b`, and submitted the following entirely through the installed HTTP service:

1. An enabled channel with UDP output `127.0.0.1:5599`.
2. The content-identified ready asset.
3. A three-second draft schedule item beginning three seconds in the future.
4. A successful dry-run plan.
5. Operator approval report `installed-93a502d9-15aa-45d7-b3ad-5d6917f43b71`.

The observed report lifecycle was:

```text
pending -> queued -> acknowledged -> completed
```

`queued` appeared only after the worker's durable asset-load acknowledgment. `acknowledged` appeared at the scheduled start after the durable asset take. `completed` appeared after the scheduled end and durable fallback return.

The installed journal at `%ProgramData%\TownLight Station\channel-journals\11111111-2222-4333-8444-555555555555.tlj` independently contained, in order, `ready`, `asset_loaded`, asset `on_air_changed`, fallback `on_air_changed`, and—after disabling the proof channel—`shutdown_complete`. The worker exited and the Windows service remained running.

## Cleanup and retained evidence

The temporary media bytes were removed after worker shutdown, and the asset record was changed to `missing` so the database does not falsely claim the deleted fixture is ready. The proof channel remains configured but disabled. The completed approval report and checksummed worker journal remain in the installed station data as evidence.

This proves the exact installed candidate's single-item schedule-to-air path on this machine. It does not substitute for the required seven-day one-station and seven-day three-station field gates.
