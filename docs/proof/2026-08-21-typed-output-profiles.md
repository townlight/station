# Typed output-profile proof — 2026-08-21

## Candidate identity

- Source commit: `205ba78deb32e4641e3f5ba780dcd2c433203e1f`
- Installer SHA-256: `15F8A9A353A6E68BE3C4A253DFC99B8FF017483D17F26FC8895019201FBB17E7`
- Installed `stationd.exe`: `75DEE7A2D0C67BFCBEB71554F3DF48BBBCDC13F018CBFF6396ED8D0FF001E842`
- Installed `channel-worker.exe`: `6EBCEF649AB726A2E3F5B712A30D3F29E8A92A7517A03875F6268C020F4F812C`
- Installed `station-updater.exe`: `0C5682E2DFFC3156B668E5A24A5A34B56EB9FB1B027529A73C691C7001D090E1`
- Bundled media runtime: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

## Automated gates

- Full Rust workspace: all targets passed.
- Full Clippy workspace: all targets and features passed with warnings denied.
- Operator TypeScript and production Vite build passed.
- Existing-database migration preserved a channel row and assigned `proof-360p30`.
- API persistence and catalog proof passed, including the `409 channel_must_be_stopped` guard for live transport changes.
- The real supervisor launched the real worker executable with `atsc-720p5994`, completed its durable handshake, aired two validated assets through the persistent graph, returned to fallback, restarted, and shut down cleanly.
- The media-engine capture gate proved 1280×720 dimensions, 60000/1001 PTS cadence, an 8 Mb/s CBR envelope, null-packet padding, MPEG-TS continuity, H.264 video, AAC audio, and bounded A/V timing.

## Installed machine gate

The exact installer upgraded the preserved station database. Both automatic LocalSystem services ran, install and active-slot receipts matched the candidate, `/health` was ready, and every required GStreamer factory loaded from the private runtime.

The reusable `scripts/prove-installed-output-profile.ps1` instrument then selected `atsc-720p5994` through the installed public API and captured output produced by the service-owned worker:

```json
{
  "profile_id": "atsc-720p5994",
  "dimensions": "1280x720",
  "frame_rate_contract": "60000/1001",
  "measured_frame_rate": 59.94,
  "video_pts_intervals": 180,
  "measured_transport_bitrate_bps": 8051127,
  "captured_bytes": 3029432,
  "null_packets": 15341,
  "service_health": "ready"
}
```

The proof instrument disabled its output in `finally`. A final installed-state check showed the proof channel disabled and runtime safe-to-air green.

## Honest boundary

This proves one progressive HD MPEG-TS/UDP profile from UI/API contract through SQLite, supervision, worker process, GStreamer, installed runtime, and receiving capture. It does not prove interlaced 1080i/480i, SDI, NDI, SRT, redundant fanout, SCTE signaling, external headend compatibility, or the one-week/three-station field gates.
