# ADR 0049: Typed CEA-708 worker transport

## Decision

CEA-708 insertion belongs inside the persistent per-channel media graph, after the shared H.264
encoder and before output-variant fanout. Protocol version 6 added one bounded `PushCaptionCue`
command carrying a canonical cue identity, a worker-relative scheduling delay, a finite duration,
and bounded UTF-8 text. Protocol version 7 adds the server-authoritative channel caption policy to
bootstrap: disabled/CEA-708 mode, service number, language, and policy revision all cross the private
boundary before the worker reports Ready. The worker derives the actual presentation timestamp from
its running graph clock; callers cannot inject a wall-clock or arbitrary media timestamp.

The graph converts timed text directly to the configured CEA-708 service, includes CEA-608 compatibility bytes,
schedules caption metadata against video frames, inserts that metadata as H.264 SEI, reparses the
stream, and only then fans the same encoded video into the per-loudness MPEG-TS variants. Caption
insertion therefore cannot diverge by sink and does not rebuild the encoder, muxers, or transport
branches.

The accepted factory closure is `appsrc → tttocea708 → cccombiner → h264ccinserter`. The packaged
runtime must also retain extraction/conversion factories for independent decode-back. The graph
fails to start when that required closure is unavailable; it does not silently emit a
caption-incapable substitute.

## Boundaries

This decision establishes a real caption-capable worker, durable audited transport policy, operator
configuration, and emitted-stream proof seam. It does not create durable caption-source authority,
asset-bound tracks, live ASR, or a current commissioning proof. The `cea708_captions` commissioning
check remains blocked until those remaining server-owned authorities exist and a current
candidate/configuration/policy-bound exact-output decode-back passes.

TownLight may claim only that a tested cue was embedded in emitted H.264/MPEG-TS and decoded back at
the declared local boundary. It does not claim FCC Part 79 compliance or cable-headend acceptance.

## Consequences

Caption policy and cue validation are enforced at protocol encode/decode and at the engine boundary.
Malformed identities, empty or oversized text, excessive delays, and unbounded durations fail
closed. A successful worker acknowledgment means the cue entered the running caption source; it is
not itself proof that a receiver decoded the cue. Decode-back remains an independent artifact.
