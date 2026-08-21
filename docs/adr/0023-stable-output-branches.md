# ADR 0023: Stable typed output branches

Date: 2026-08-21

Status: Accepted

## Context

A channel may need concurrent primary and redundant handoffs. Treating a channel as one mutable destination makes redundancy a special case, encourages output-graph replacement while on air, and cannot grow honestly into SRT, SDI, NDI, or streaming delivery. Encoding the same program independently for every equivalent transport also wastes station capacity and risks branch-to-branch drift.

## Decision

Each channel owns an ordered set of durable, typed output sinks. Every sink has a stable UUID, operator label, kind-specific destination, loudness target, required flag, and enabled state.

- Sink rows live in a normalized `channel_output_sinks` table beneath the channel. Existing databases are migrated in place: every historical UDP destination becomes one enabled required `Primary transport` sink without losing the channel row or changing its identity.
- The API and operator console use the sink collection as the public contract. An enabled channel must have at least one enabled sink; identities and labels must be unique; each typed destination is validated before persistence.
- Changes to a running channel's profile or sink collection fail with `409 channel_must_be_stopped`. Configuration never mutates underneath an active worker.
- The current `udp-ts` implementation encodes and multiplexes exactly once, then uses a `tee` and an isolated queue per destination. A slow or failed branch therefore does not introduce another encoder or replace the persistent program graph.
- The supervisor passes the complete enabled destination set to the worker at launch. The worker owns all GStreamer sink branches and reports ready only after the graph reaches its running state.
- All UDP-TS branches currently share the selected encoded audio contract, so their loudness targets must match the output profile. Future branches that genuinely require different audio processing must make that processing explicit rather than claiming unsupported per-sink normalization.
- `required` is durable commissioning intent. Every five-second worker heartbeat reports a byte counter for each ordered branch, and failure of any required counter to advance fails channel safe-to-air and enters supervised restart. This proves local sink progress. UDP still requires external receiver/device telemetry for end-to-end headend truth.

## Consequences

Primary and redundant UDP MPEG-TS outputs are now first-class and machine-testable without duplicate encoding. The normalized model can add SRT, SDI, NDI, HLS, or future sink types without another channel-schema replacement, but none is claimed until its type-specific validation, security boundary, runtime construction, health evidence, and machine gate exist. SRT secrets must not be placed in process arguments; a protected bootstrap channel is required before authenticated SRT is implemented.
