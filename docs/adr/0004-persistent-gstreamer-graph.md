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
- Both selectors synchronize against the pipeline clock, cache buffers, and reject backward buffers. Stored-file and live-input branches use source-local `audiorate` before the selector to repair their own sub-sample timestamp rounding.
- A validated file can be decoded and linked while the fallback leg remains active. A flush seek establishes the requested media offset. Each file leg's `clocksync(sync-to-first)` schedules decoded buffers against current pipeline running time, while the selector source pads receive that current running time as their common offset so `input-selector` compares the new segment against the active timeline. The media offset is not subtracted from the pad offset because the seek segment has already accounted for it. Both selectors feed a single-segment timeline so source segment changes cannot reset output PTS.
- Preparing a successor asset is transactional: generation-scoped elements reach decode readiness before replacing the program target; the retired branch is disconnected, driven to `NULL`, awaited, and only then removed. Source-local rate stages normalize replaceable branches. Shared video and audio rate stages preserve cadence and monotonic timestamps after selection; external branches must first rebase their source timestamps exactly once so the common audio stage repairs rounding rather than bridging an unbounded discontinuity with silence.
- The persistent output half is OpenH264 plus AAC → elementary-stream parsers → `mpegtsmux` → UDP.
- The machine contract receives the real UDP output and rejects malformed TS packet sizes, missing sync bytes, missing H.264 or AAC declarations, transport errors, discontinuity flags, per-PID payload continuity jumps, backward PTS, and excessive A/V timing divergence across fallback → program → fallback.
- GStreamer is pinned to the 1.28 API line through `gstreamer` 0.25.3 and is dynamically linked. Runtime packaging will include only the required redistributable runtime and plugins.

## Consequences

The architecture now has executable evidence for the continuity premise that justified a persistent graph. A source switch does not reconstruct the encoder, mux, sink, or pipeline object. The graph also fails visibly if a requested pad does not become active before its deadline.

This slice is intentionally not a complete playout engine. It supports successive dynamically loaded, identity-verified real A/V assets plus fallback and one UDP sink. Schedule-time dispatch, live ingest, canonical broadcast profiles, multi-sink fanout, captions, loudness, CG, recovery, and long-duration/three-channel proof remain required before release claims.
