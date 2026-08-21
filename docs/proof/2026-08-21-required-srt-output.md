# Required SRT caller-output proof — 2026-08-21

## Candidate identity

- Source commit: `0b96f96785d910c6fdf8e01cf083416cdc0c2956`
- Installer SHA-256: `82BC5B92917782519A12EF11C374ECFDCC9162946F7F6553DA03381AC514DDBD`
- Installed `stationd.exe`: `7D9C5DD90876D463461276485948BDC622D389EED4E284CA031174838FDFE03D`
- Installed `channel-worker.exe`: `07755BF239CC18833D0E05A38D915FC0A7B0E5B0A61EAE7890D28DF7D6B3348C`
- Installed `station-updater.exe`: `76D567902AAA1AF1C31E9E2EE2F57FB1A6225D7E9FDBC6490F811763D5485339`
- Bundled media runtime: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

## Automated gates

- The full Rust workspace test suite passed across all targets. The media-engine suite includes a real SRT caller/listener capture and a separate absent-listener test that requires startup to fail closed within the deadline.
- Full Clippy passed across the workspace, all targets, and all features with warnings denied.
- The production TypeScript/Vite operator build passed with the typed SRT transport selector and destination guidance.
- API tests persisted and round-tripped SRT beside UDP. Validation rejects query options, fragments, user information, credentials, and non-IP hosts, and requires enabled SRT branches to be required.
- The existing UDP-only sink table migrated transactionally to the expanded kind constraint without losing its historical UDP row. The upgraded installed database retained all nine historical channels and returned the new SRT row through the public API.
- The protected worker bootstrap carries the typed SRT destination through the restricted named pipe. Worker process arguments remain limited to worker identity, channel identity, journal path, and pipe name.
- The installer contract now requires both `gstsrt.dll` and its native `srt.dll` transport library. Post-install verification loads the `srtsink` factory from the private runtime.

## Installed machine gate

The exact installer upgraded the preserved station in place. Both automatic services ran as `LocalSystem`; the source receipt, active-slot receipt, and all three installed executable hashes matched the candidate manifest; `GET /health` returned ready; and the expanded private-runtime factory gate passed.

The reusable `scripts/prove-installed-srt-output.ps1` instrument then started an independent installed-runtime `srtsrc` listener, waited for its UDP socket to be bound, and configured one required SRT caller through the installed public channel API. The service-owned worker reached verified healthy fallback only after the SRT branch connected and transmitted. A raw receiver capture was checked for whole 188-byte MPEG-TS packets and sync bytes, then inspected independently with FFprobe:

```json
{
  "channel_id": "3157bcad-9395-43c5-bc77-915c77c67717",
  "sink_kind": "srt",
  "srt_mode": "caller",
  "latency_ms": 200,
  "encryption": "none",
  "dimensions": "1280x720",
  "frame_rate": "60000/1001",
  "video_codec": "h264",
  "audio_codec": "aac",
  "audio_sample_rate_hz": 48000,
  "measured_transport_bitrate_bps": 8003597,
  "captured_bytes": 5206472,
  "service_health": "ready"
}
```

The instrument disabled the proof channel in `finally`. The SRT configuration remained durable and disabled for audit/reuse. Final safe-to-air truth was green, the separately enabled proof channel remained on verified fallback, and no critical or warning alert remained active.

## Evidence-instrument correction

The first external receiver used `filesink` and reported zero live file length even while GStreamer debug showed continuous 1,316-byte SRT messages arriving and TownLight Station reported a fresh green worker heartbeat. Windows stdio buffering made that a false-negative evidence reader. The final instrument directs `fdsink` to a process-owned raw standard-output capture, which exposes received bytes immediately and passed transport, rate, and codec validation. This changes only the independent proof receiver; the installed production candidate is the same fail-closed SRT sender.

## Honest boundary

This proves one unencrypted required SRT caller from operator/API contract through SQLite migration, protected process bootstrap, service-owned worker, installed private runtime, real loopback listener, MPEG-TS integrity, and independent codec/rate inspection. It does not prove encryption, stream IDs, rendezvous or listener output modes, optional SRT branches, continuous post-start per-branch telemetry, automatic destination failover, interoperability with a remote third-party receiver, adverse-network behavior, or the one-week/three-station field gates.
