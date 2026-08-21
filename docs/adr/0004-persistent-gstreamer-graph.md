# ADR 0004: Persistent GStreamer channel graph

Date: 2026-08-20

Status: Accepted

## Context

Linear playout cannot restart the encoder or MPEG-TS mux at every scheduled boundary without risking continuity-counter, timestamp, and decoder disruption. A fallback source must also remain immediately available when scheduled or live material fails. Seeing video once is insufficient evidence; the output stream must remain structurally continuous through source changes.

## Decision

Each channel worker will own one persistent GStreamer graph whose encoded output half remains in `PLAYING` while raw input legs change.

- Rust builds elements through `ElementFactory`; shell-style pipeline parsing is not part of the runtime.
- Program and fallback legs present identical raw caps before paired video and audio `input-selector` elements.
- Source switching occurs before the encoder and MPEG-TS mux.
- Selection is asynchronous: the engine sets the requested pad, then waits with a deadline until GStreamer reports that pad active.
- The initial machine slice uses live synthetic SMPTE program video and black fallback video at 640×360 I420, 30 fps, paired with 48 kHz stereo sine and silence sources.
- Both selectors synchronize against the pipeline clock, cache buffers, and reject backward buffers. `audiorate` repairs sub-sample timestamp rounding before AAC encoding.
- A validated file can be decoded and linked while the fallback leg remains active. Dynamically added file pads receive the current pipeline running-time offset before entering `PLAYING`, and both selectors feed a single-segment timeline so source segment changes cannot reset output PTS.
- The persistent output half is OpenH264 plus AAC → elementary-stream parsers → `mpegtsmux` → UDP.
- The machine contract receives the real UDP output and rejects malformed TS packet sizes, missing sync bytes, missing H.264 or AAC declarations, transport errors, discontinuity flags, per-PID payload continuity jumps, backward PTS, and excessive A/V timing divergence across fallback → program → fallback.
- GStreamer is pinned to the 1.28 API line through `gstreamer` 0.25.3 and is dynamically linked. Runtime packaging will include only the required redistributable runtime and plugins.

## Consequences

The architecture now has executable evidence for the continuity premise that justified a persistent graph. A source switch does not reconstruct the encoder, mux, sink, or pipeline object. The graph also fails visibly if a requested pad does not become active before its deadline.

This slice is intentionally not a complete playout engine. It supports one dynamically loaded, identity-verified real A/V asset plus fallback and one UDP sink. Successive schedule-driven asset replacement, live ingest, canonical broadcast profiles, multi-sink fanout, captions, loudness, CG, recovery, and long-duration/three-channel proof remain required before release claims.
