# ADR 0032: Channel-scoped live takeover with a persistent decoded branch

Date: 2026-08-21

Status: Accepted

## Context

Operators need a deliberate way to interrupt scheduled or fallback programming with a verified
live source and then return to the exact source that was active before the interruption. The
operation must not create a second encoder/output path, lose the station's durable authority, or
allow a stale operator action to take a channel live. It must also fail safely when the source,
worker, channel, or return path is no longer trustworthy.

## Decision

The first live-input slice is a typed, revisioned UDP MPEG-TS source. Its endpoint is represented
as `UdpMpegTs { bind_ip, port }`, validated as station-local/private/loopback/link-local or
multicast with a nonzero port, and persisted with an endpoint kind and source revision. No
pipeline strings are accepted. Other live transports remain future work.

SQLite owns a monotonic authority revision per channel, one active takeover session per channel,
and an append-only audit record for every state transition. Request, take, return, and failure
updates use compare-and-set checks for the expected authority revision and expected session state.
The state machine is explicit:

```text
arming → armed → take_requested → on_air → return_requested → returning → completed
   └──────────────────────────────────────────────────────────────────────────────→ failed
```

`stationd` reconciles the durable state with the one persistent worker for the channel. It sends
typed `ArmLive`, `TakeLive`, `ReturnFromLive`, and `DisarmLive` commands through the protected
duplex named pipe. The command carries session identity, source identity, source revision, and
expected protocol sequence. The controller generates a bounded short-lived arm token; the worker
accepts it only for the matching armed session, consumes it on take, and never exposes it in debug
output. Return must be acknowledged before disarm is permitted.

The worker adds a candidate branch to the existing GStreamer graph using `udpsrc` with 188-byte
MPEG-TS caps, `tsdemux`, H.264/AAC decoders, canonical conversion/rate stages, and running-time
alignment. Both decoded legs must produce buffers before the branch is armed. Taking live selects the
video and audio pads together and leaves the encoder, MPEG-TS mux, CG compositor, and required
output branches running. Returning receives the current schedule or fallback target selected under
[ADR 0033](0033-wall-clock-schedule-catch-up.md), then disarm removes the live branch only after it
is no longer selected.

Live feeds may arrive with timestamps established before the station attaches. The first demuxed
video or audio buffer captures one source-to-pipeline offset; that same offset rebases PTS and DTS
on both compressed legs before parsing and decoding, preserving the feed's A/V relationship. The
source-local video rate stage skips directly to its first real timestamp instead of manufacturing
pre-roll from zero, while the source-local audio rate stage owns only input rounding. Decoded
source-health counters are sampled before the clock stages, and neither leg may generate media to
conceal a stalled input. The shared post-selector `audiorate` still owns sub-sample continuity
across source changes.

While live is on air, the worker observes decoded video and audio buffer counters. Two consecutive
non-advancing observations or two seconds without progress is a live-input stall. The worker
rejects the live operation; stationd records output-stall/off-air health, fails the takeover, and
restarts the worker with bounded backoff. Required output health independently continues to use
protected-pipe byte-progress heartbeats.

## Fail-closed rules

- A pre-air arm that expires is disarmed and marked failed.
- A worker restart during on-air live cannot reconstruct uncertain authority; the session is failed
  and the worker starts from its safe source.
- Disabling a channel fails its active takeover before stopping the worker.
- A failed or interrupted return/disarm is failed, never reported as completed.
- A stale authority or source revision is a conflict and cannot overwrite newer operator intent.
- CG authority and the fixed compositor are independent of source selection; valid CG state is
  retained/reseeded across worker recovery and a live transition does not clear it.

## Consequences

The station has one auditable authority for a takeover, one media clock, and one encoded output
transport. Operators can see the source revision, authority revision, expiry, session state,
operator/reason, and audit history in the Live console; destructive Take Live and Return to
Schedule actions require confirmation. The persistent branch avoids encoder and output churn, but
adds bounded decoded-A/V readiness and stall handling before a take can be accepted.

## Explicit non-claims

This decision covers UDP MPEG-TS input only. Additional live transports, SDI/NDI, and remote SRT
input are not implemented here. Wall-clock schedule catch-up and live handback selection are
defined separately by [ADR 0033](0033-wall-clock-schedule-catch-up.md). The exact installed
fallback-to-live-to-fallback decoded evidence is recorded in the [installed live takeover
proof](../proof/2026-08-21-installed-live-takeover.md). The distinct installed
[schedule catch-up proof](../proof/2026-08-22-installed-live-schedule-catch-up.md) crosses an item
boundary under live and decodes the positive-offset successor after return. Neither proof replaces
clean-machine, headend, or field-soak acceptance.
