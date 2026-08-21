# TownLight Station

TownLight Station is a locally installed Windows broadcast appliance for LPM and PEG operations. It is being built as one traceable product: station control, deterministic media execution, operator workflows, installation, recovery, distribution, and proof all ship from this repository.

The current foundation provides a versioned station-profile API, strict commissioning validation, SQLite persistence in WAL mode, and a native Windows service. It is the first vertical slice of the station control plane, not a feature-complete release.

The repository also contains a supervised isolated channel worker, a length-framed typed command/event protocol, a durable per-channel event journal, and the first machine-proven persistent media graph. The station runtime launches and handshakes the real worker over a private named pipe, validates every identity and sequence, enforces response deadlines, and guarantees process cleanup. The GStreamer graph keeps its encoder, MPEG-TS mux, and UDP output alive while switching raw fallback and program sources. Unknown major versions, stale commands, corrupted journals, malformed frames, missed deadlines, and MPEG-TS continuity errors fail visibly.

## Architecture

- Rust owns the station control plane and isolated media workers.
- A single station service owns authoritative SQLite writes.
- One persistent GStreamer worker per channel will own media timing and output.
- React and TypeScript will provide operator and resident web interfaces.
- The appliance remains locally operable when optional cloud services are unavailable.

See [the architecture overview](docs/architecture/overview.md) for boundaries and non-negotiable design rules.

## Requirements

- Windows 11 or Windows Server 2022 or newer
- Rust 1.97.1 for development
- GStreamer 1.28.6 MSVC x64, development install, for media-engine development and tests

Install GStreamer from the [official Windows download](https://gstreamer.freedesktop.org/download/#windows) and put its `bin` directory first in `PATH` before building. The runtime currently uses the SQLite library included with Windows and does not require a separately installed database server. The product installer will carry the selected GStreamer runtime rather than asking station operators to configure a development SDK.

## Quick start

1. Install Rust and the official GStreamer 1.28.6 MSVC x64 development runtime.
2. Clone this repository.
3. Run `cargo test --offline --workspace`.
4. Run `cargo run -p stationd -- station.db 127.0.0.1:4070`.
5. Open `http://127.0.0.1:4070/health`.

`stationd service --database <absolute-path> --address 127.0.0.1:<port>` is the Service Control Manager entry point used by installation infrastructure. Service mode refuses relative database paths and non-loopback listeners, reports startup and shutdown state to Windows, and handles both requested stops and operating-system shutdown cooperatively. It is not intended to be launched directly from an operator terminal.

## API

`GET /health` reports whether the local database is ready.

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

`GET /api/v1/station` returns the commissioned profile and revision, or a typed `not_commissioned` error.

## Channel worker

`channel-worker <worker-id> <channel-id> <journal-path> <pipe-name> <udp-destination>` connects to the station service through its private duplex Windows named pipe and owns the channel media graph. It reports `Ready` only after fallback output starts, and reports `ShutdownComplete` only after the graph stops. The pipe is restricted to the creating user, Administrators, and SYSTEM, rejects remote clients, and permits only one server instance. `Ping`, `ApplyPlan`, and `Shutdown` are executable; live transitions remain explicitly rejected.

## Media engine

`station-media-engine` programmatically builds one live GStreamer pipeline; it does not use a shell pipeline parser. The current machine slice supplies synthetic program and black fallback legs through `input-selector`, normalizes to 640×360 I420 at 30 fps, keeps OpenH264, `h264parse`, `mpegtsmux`, and `udpsink` persistent, and performs bounded source-switch acknowledgement. Its integration test receives the real UDP stream and checks TS framing, error/discontinuity flags, and per-PID continuity counters across both switch directions. Audio, scheduled files, live inputs, production profiles, and sink fanout are not claimed yet.

## Development

Run formatting, linting, and tests before committing:

```powershell
cargo fmt --check
cargo clippy --offline --all-targets --all-features -- -D warnings
cargo test --offline --workspace
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the proof-first workflow and [LICENSE](LICENSE) for terms.
