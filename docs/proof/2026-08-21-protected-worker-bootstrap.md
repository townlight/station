# Protected worker-bootstrap proof — 2026-08-21

## Candidate identity

- Source commit: `18e062be23e06d3527a419ce28d43289f09c9eb6`
- Installer SHA-256: `2A05D1A9B9B864E90EDD014913C9D023848A145F84777DDB35C29FC2D5BB4044`
- Installed `stationd.exe`: `A638BF258FFB8A96022FC7200DCA09FAD882EFA6E7A09487D5B47C674DCE9C61`
- Installed `channel-worker.exe`: `DC64E92AA74DFEA458AFDE680FCCC6C57F7D6ABE6ABC5CC2030D48BA322BB10A`
- Installed `station-updater.exe`: `6E093B4147425686A33CA0D686D984E8ADA50DF2E0F126EC2886481FE1BF48E7`
- Bundled media runtime: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

## Automated gates

- Full Rust workspace tests passed across all targets.
- Full Clippy workspace passed across all targets and features with warnings denied.
- Protocol 1.2 round-tripped the typed bootstrap frame and continued to reject incompatible majors.
- The direct worker process contract launched the executable with only worker ID, channel ID, journal path, and pipe name; it then sent the output profile and destination through the pipe before receiving `Ready`.
- The real supervisor tests passed launch, protected bootstrap, durable handshake, schedule commands, restart reconciliation, fanout reception, and clean shutdown.
- The worker rejects a bootstrap whose worker/channel identity does not match its process identity, as well as an unsupported profile, malformed output, or empty output set, before media startup.

## Installed machine gate

The exact installer upgraded the running station. Both automatic LocalSystem services returned to running state and the preserved enabled channel recovered to verified fallback.

The installed proof recorded the existing worker process IDs, enabled the two-branch 720p59.94 proof channel through the public API, and observed a distinct new `channel-worker.exe` process within the bounded startup window. It inspected every command-line value Windows exposed and failed if either destination or the output-profile identifier appeared. Windows withheld `CommandLine` for the LocalSystem worker from this inspection context, so the installed check honestly proves process creation and no exposed output values—not independent retrieval of the complete protected process command line. The exact installed binary hash binds that process to the source and direct executable contract above.

The same live worker then completed the full receiving proof:

```json
{
  "profile_id": "atsc-720p5994",
  "dimensions": "1280x720",
  "measured_frame_rate": 59.94,
  "video_pts_intervals": 358,
  "measured_transport_bitrate_bps": 7974381,
  "redundant_transport_bitrate_bps": 7974381,
  "captured_bytes": 5997012,
  "redundant_captured_bytes": 5997012,
  "proven_output_branches": 2,
  "worker_process_arguments_expose_no_output_values": true,
  "worker_command_line_readable": false,
  "null_packets": 30369,
  "service_health": "ready"
}
```

The proof output was disabled in `finally`; installed health remained ready and runtime safe-to-air remained green.

## Honest boundary

This proves that current UDP output configuration crosses the protected named-pipe bootstrap and that the worker cannot become ready before consuming it. It establishes the transport needed for future SRT credentials. It does not yet prove DPAPI-backed SRT secret storage, authenticated SRT, independent retrieval of a LocalSystem process command line, or resistance to an already elevated local administrator reading process memory.
