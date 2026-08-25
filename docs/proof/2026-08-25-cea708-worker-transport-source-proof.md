# CEA-708 worker transport source proof — 2026-08-25

## Proven

- Protocol version 6 round-trips a typed bounded CEA-708 cue and rejects malformed identity,
  timing, duration, and text fields.
- The real channel worker dispatches an accepted cue into the persistent media engine and returns a
  distinct acknowledgment or typed rejection without rebuilding the graph.
- The engine uses `tttocea708` service 1 with CEA-608 compatibility, `cccombiner`, and
  `h264ccinserter` between the one shared H.264 encoder and output-variant fanout.
- A real 360p30 graph captured four seconds from the exact post-mux branch, continued advancing
  transport bytes during injection, and FFmpeg decode-back recovered the exact text
  `TownLight CEA-708 proof` from the emitted MPEG-TS.
- The installed GStreamer 1.28.6 runtime was independently inventoried and contains the insertion
  closure plus `ccextractor`, `h264ccextractor`, `cea608tott`, and `cea708overlay` for the future
  packaged proof analyzer.

## Verification

The protocol suite, all station-media-engine tests, and all channel-worker unit, process,
supervision, and output-proof tests passed. The real media suite covered 19 persistent-graph tests,
including decoded fallback/CG pixels, file and live switching, UDP/SRT output, encryption, caption
decode-back, and transport continuity. Warning-denied Clippy and Rust formatting passed for the
changed crates and all their targets/features.

## Not yet proven

There is no durable caption track/source model, asset-bound cue preservation, authenticated
operator workflow, live ASR path, packaged decode-proof executor, current per-channel proof
receipt, clean-machine exercise, headend acceptance, or field evidence in this tranche. The
commissioning caption check therefore remains blocked.
