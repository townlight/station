# ADR 0041: Bounded progressive headend execution

Status: Accepted

Date: 2026-08-23

## Context

The static catalog and stopped-channel transaction preserve headend intent, but a stored contract
is not executable merely because the current profile defaults happen to resemble it. The worker
bootstrap previously carried only destination and loudness. Enabling a catalog entry at that point
would have allowed persisted mux-rate and packet-size facts to disappear at the process boundary.
The product also needs an operator workflow that shows which contracts can run without presenting
local transmission as proof that an external receiver accepted the program.

## Decision

Only `generic-udp-spts` and `telvue-hypercaster-ip` are locally runnable. Both are restricted to the
existing progressive `atsc-720p5994` H.264/AAC profile, cable `-24 LUFS`, 8,000 kb/s constant-rate
MPEG-TS, 1,316-byte UDP datagrams, and unicast destination rules. The interlaced Comcast and
Harmonic entries remain staging-only. Leightronix file drop remains unimplemented. A runnable
profile with a non-default stored mux rate remains valid while stopped but fails channel enablement.

The private worker protocol 4.0 adds an optional typed headend contract to UDP outputs. Its profile ID
enum can represent only the two runnable entries. The major version marks the schema incompatible
with prior strict readers. Protocol decoding and orchestration independently
require the exact output profile, loudness, mux rate, and packet size before a worker can start.
Protocol decoding also enforces unicast destination semantics and the profile-specific minimum
port. Ordinary UDP and SRT shapes remain structurally unchanged when the optional field is absent.

The channel worker converts the validated bootstrap facts into an explicit media-engine
`MpegTsMuxContract`. Engine variant identity is the tuple of loudness target, transport bitrate,
and packet size; equal tuples share one audio/mux path and different tuples cannot be silently
collapsed. The engine validates the encoded-payload margin, bounded bitrate, whole 188-byte packet
alignment, and UDP payload ceiling before GStreamer initialization, then supplies the validated
bitrate and alignment directly to `mpegtsmux`.

The operator console reads the installed catalog, applies a profile only to a stopped channel,
blocks Start unless the named profile is currently runnable, and displays local transport evidence
separately from receiver acceptance. Catalog-read failure also fails Start closed. Staging-only
profiles remain visible and persistable for planning, with an explicit execution blocker.

The installed proof gate may target either runnable profile. It stages and enables the profile
through the public loopback API, captures real UDP datagrams, requires a clean measured 8 Mb/s
interval and only 1,316-byte packets, and runs the existing exact-candidate post-mux loudness job at
`-24 LUFS`. Cleanup is part of the gate: the unique proof channel must be disabled and confirmed by
API readback. Receiver acceptance must remain unknown or not attempted.

## Proof boundary

Repository tests prove catalog gating, storage-to-bootstrap propagation, protocol rejection,
variant identity, mux construction, worker startup, and real UDP packet size. The installed gate
proves local execution by an installed candidate on this station only when its evidence record is
completed. None of these facts proves that a cable headend decoded or accepted the signal.

External receiver acceptance, clean-machine installation, MPEG-2/AC-3, interlaced output,
file-drop delivery, and the required one-station and three-station field weeks remain independent
gates.

## Consequences

- A stored runnable contract can no longer vanish between SQLite and `mpegtsmux`.
- Generic and TelVue progressive IP handoffs can run without opening any interlaced or file-drop
  claim.
- Custom mux-rate execution remains closed until its end-to-end contract and proof are added.
- Local transport, local loudness, external acceptance, and field success remain separate evidence.
