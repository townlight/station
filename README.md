# TownLight Station

TownLight Station is a locally installed Windows broadcast appliance for LPM and PEG operations. It is being built as one traceable product: station control, deterministic media execution, operator workflows, installation, recovery, distribution, and proof all ship from this repository.

The current foundation provides a versioned station-profile API, strict commissioning validation, SQLite persistence in WAL mode, and a runnable HTTP daemon. It is the first vertical slice of the station control plane, not a feature-complete release.

The repository also contains a runnable isolated channel worker, a length-framed typed command/event protocol, and a durable per-channel event journal. The worker records each event and synchronizes it to disk before emitting the event to the control plane. Unknown major versions, stale commands, corrupted journals, and malformed frames fail visibly.

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

The runtime currently uses the SQLite library included with Windows. It does not require a separately installed database server.

## Quick start

1. Install the Rust toolchain.
2. Clone this repository.
3. Run `cargo test --offline --workspace`.
4. Run `cargo run -p stationd -- station.db 127.0.0.1:4070`.
5. Open `http://127.0.0.1:4070/health`.

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

`channel-worker <worker-id> <channel-id> <journal-path>` reads framed commands from standard input during the current transport-development phase. It emits framed events to standard output only after appending and synchronizing them to the channel journal. `Ping`, `ApplyPlan`, and `Shutdown` are executable; live transitions are explicitly rejected until the persistent media graph is present.

## Development

Run formatting, linting, and tests before committing:

```powershell
cargo fmt --check
cargo clippy --offline --all-targets --all-features -- -D warnings
cargo test --offline --workspace
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the proof-first workflow and [LICENSE](LICENSE) for terms.
