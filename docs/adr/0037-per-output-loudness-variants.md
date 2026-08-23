# ADR 0037: Per-output loudness variants

## Status

Accepted.

## Context

One channel may simultaneously feed a cable output at -24 LUFS and a streaming output at -16 LUFS. The former graph encoded audio once, muxed once, and split only the completed MPEG-TS transport. Applying a target after that split is impossible, while applying one channel-wide target makes one of the two contracts wrong.

Floating-point values also cannot be the identity of a processing branch. Missing protocol fields must not silently select a plausible default because that can turn a cable output into a streaming-loudness output without an operator-visible failure.

## Decision

The worker protocol requires `loudness_target_lufs` on every output. Because an older worker would otherwise ignore that field and silently run its channel-wide target, this required-field change advances the worker protocol to major version 2; mixed major versions reject one another before graph startup. `LoudnessTarget` stores integer milli-LUFS and serializes as human-scale LUFS. Missing, non-finite, out-of-range, or over-precise wire values fail decoding. Channel configuration currently permits only the two executable variants, -24 and -16 LUFS.

The media graph keeps one selected raw-video path and one H.264 encoder/parser. Encoded video is split once. Selected audio is split by distinct loudness target. Each target group owns a bounded queue, F64LE/192 kHz conversion required by `audioloudnorm`, target-specific normalization, conversion back to S16LE/48 kHz, one AAC encoder/parser, one MPEG-TS mux, and one post-mux tee. Sinks with the same target share that group; different targets cannot share encoded audio or a mux.

Storage persists an explicit sink ordinal, and orchestration preserves that configured enabled-sink order when constructing worker outputs. That order remains the identity used by heartbeat byte counters and required-output health. Existing engine convenience APIs do not infer a generic default; they explicitly derive the target from the selected output profile.

## Proof boundary

Successful graph construction, advancing encoded-audio counters for every target group, and advancing transport counters prove configured execution only. They do not prove that post-mux output contains decodable audio or that the encoded program meets its target. A loudness pass requires an independent bounded capture, MPEG-TS decode, measurement, and evidence record tied to the sink or target group, stream identity, and time. Receiver/headend acceptance remains a separate external gate.

## Consequences

- Mixed cable and streaming targets can execute in one persistent channel worker without duplicating the video encoder.
- Audio normalization, AAC encoding, and MPEG-TS muxing scale with distinct targets, not sink count.
- Unsupported targets and missing protocol fields fail before worker readiness.
- A stalled target group can surface through the existing per-sink progress authority; measured compliance remains pending until the independent probe is built.
