# CG decoded UDP MPEG-TS pixel proof — 2026-08-21

## Scope

The focused engine proof starts the real persistent `PersistentPlayout`, captures its UDP
MPEG-TS, consumes the transport's audio and video paths, decodes video to raw frames, and compares
the declared normalized region across baseline, applied text, and cleared states. Frame dimensions
and stride are taken from decoded caps. The test drains queued frames between mutations so a
pre-update frame cannot satisfy the gate.

## Command and result

```text
cargo test --offline -p station-media-engine --test persistent_udp cg_ -- --test-threads=1
```

Result: `3 passed; 0 failed`. The three CG tests cover transport continuity during apply/clear,
decoded UDP pixel change in the declared region plus clear-to-baseline, and invalid-update
retention with same-board/revision content replacement.

## Bugs caught by the gate

The initial receiver failed with GStreamer `not-linked` because the demux audio path was not
consumed. The test also caught base-pad z-order and branch chroma-key ordering errors. Those were
corrected before the passing result; the failures are retained as development history, not as
final product evidence.

## Boundary

This proves the focused local media-engine path through compositor, encoder, MPEG-TS, UDP capture,
decode, apply, and clear. It does not prove an installed service, external receiver
interoperability, multi-station operation, image/WPE/live-video/audio CG, or field soak.
