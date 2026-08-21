# Architecture overview

TownLight Station is a Windows appliance with local authority. Internet services may extend it, but loss of the Internet cannot take a commissioned channel off air or destroy its operating record.

## Runtime boundaries

```mermaid
flowchart LR
  UI[Operator web UI] --> API[stationd]
  API --> DB[(SQLite WAL)]
  API --> Store[(Content-addressed media)]
  API <-->|Versioned named-pipe protocol| Worker[One channel worker]
  Worker --> Journal[(Durable as-run journal)]
  Worker --> Outputs[Station outputs]
```

`stationd` is a modular monolith and the only writer to the authoritative station database. A separate persistent worker owns each channel's GStreamer graph, media clock, output switching, and append-only as-run journal. File processing and optional local AI run outside both timing-critical and authoritative paths.

The initial control-plane dependency direction is enforced by the Cargo workspace:

```text
station-domain <- station-storage <- station-api <- stationd
```

The domain crate has no storage, HTTP, media, or Windows-service dependencies. Storage translates domain objects to SQLite. The API owns transport and error contracts. The daemon is composition and process lifecycle only.

`station-media-protocol` is independent of the control-plane crates. It defines bounded length-prefixed command and event envelopes, explicit protocol versions, command identifiers, expected worker sequences, and durable event sequences. See [ADR 0001](../adr/0001-media-worker-protocol.md).

`station-media-journal` owns the append-only per-channel record. `channel-worker` is its sole writer. Every record has a journal magic/version header, bounded length, CRC-32 checksum, worker/channel identity, and strict monotonic event sequence. Writes are flushed and synchronized before the corresponding event is emitted. Restart scans the complete journal and resumes at the next sequence; corruption and partial tails prevent a false clean recovery. See [ADR 0002](../adr/0002-durable-channel-journal.md).

`station-windows-ipc` owns the local control boundary. Each worker connects to one duplex byte-mode named pipe beneath `\\.\pipe\townlight-station\`. Pipe creation rejects duplicate servers and remote clients, and applies a protected ACL limited to SYSTEM, Administrators, and the creating user. Commands and events use the same versioned frames; standard input and output are not protocol transports.

`station-runtime` owns worker process lifecycle from the station side. It creates the exclusive pipe, launches the worker without inherited protocol handles, bounds connection and response waits, accepts only the configured worker/channel identity and next sequence, requires a durable `Ready` handshake, and terminates and reaps the child on every failed launch, timeout, or abandoned session. Clean shutdown is complete only after `ShutdownComplete` and a successful process exit. See [ADR 0003](../adr/0003-supervised-worker-lifecycle.md).

`station-media-engine` owns the GStreamer graph. Its first executable slice builds all elements through factories, feeds synthetic black fallback and program legs into `input-selector` at identical raw caps, and leaves OpenH264, `h264parse`, `mpegtsmux`, and UDP output in one persistent graph across switches. The Windows machine test receives the actual transport stream and validates sync bytes, packet boundaries, transport-error and discontinuity flags, and per-PID continuity counters. See [ADR 0004](../adr/0004-persistent-gstreamer-graph.md).

## Authority rules

| Truth | Owner |
|---|---|
| Configuration, users, assets, schedules, jobs | `stationd` through SQLite |
| Media bytes | Content-addressed NTFS store |
| Current on-air execution | Channel worker |
| What actually aired | Durable per-channel journal |
| Installed version | Signed candidate and installation receipts |

## Product languages

Rust is used for native services, workers, command-line tools, and installation infrastructure. TypeScript and React are used for browser interfaces. Apple, Android, Roku, and web resident clients use their platform-native production toolchains.

## Non-negotiable decisions

- One canonical repository produces one candidate identity.
- SQLite WAL is the local authority; no database server is installed.
- Every channel has one persistent GStreamer production engine.
- FFmpeg may perform bounded file work but never linear playout.
- Media workers cannot write the station database.
- On-air events are journaled durably before they are acknowledged.
- Configuration is typed and audited; environment variables are development bootstrap only.
- Updates use signed immutable slots, health deadlines, and rollback.
- A capability is not complete until its installed-machine scenario passes.

## Current vertical slice

The current spine commissions a station profile through `PUT /api/v1/station`, protects updates with an expected revision, persists it transactionally in SQLite with WAL and foreign-key enforcement, reads it after restart, and exposes database readiness through `GET /health`.

The first channel worker now runs as an independent supervised process. It records readiness, heartbeat, applied-plan revision, rejected-command, and shutdown events durably before emitting them, resumes its event sequence after restart, and exchanges all protocol traffic through an ACL-protected Windows named pipe. Separately, the first persistent GStreamer graph has machine proof for uninterrupted video MPEG-TS continuity across fallback/program switches. Integrating that graph into the worker, then adding audio, real media/live inputs, profile-driven output, and `stationd` channel configuration are the next media milestones.
