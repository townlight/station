# ADR 0004: Persistent GStreamer channel graph

Date: 2026-08-20

Status: Accepted

## Context

Linear playout cannot restart the encoder or MPEG-TS mux at every scheduled boundary without risking continuity-counter, timestamp, and decoder disruption. A fallback source must also remain immediately available when scheduled or live material fails. Seeing video once is insufficient evidence; the output stream must remain structurally continuous through source changes.

## Decision

Each channel worker will own one persistent GStreamer graph whose encoded output half remains in `PLAYING` while raw input legs change.

- Rust builds elements through `ElementFactory`; shell-style pipeline parsing is not part of the runtime.
- Program and fallback legs present identical raw caps before `input-selector`.
- Source switching occurs before the encoder and MPEG-TS mux.
- Selection is asynchronous: the engine sets the requested pad, then waits with a deadline until GStreamer reports that pad active.
- The initial machine slice uses live synthetic SMPTE program video and black fallback video at 640×360 I420, 30 fps.
- The persistent output half is OpenH264 → `h264parse` → `mpegtsmux` → UDP.
- The machine contract receives the real UDP output and rejects malformed TS packet sizes, missing sync bytes, transport errors, discontinuity flags, and per-PID payload continuity jumps across fallback → program → fallback.
- GStreamer is pinned to the 1.28 API line through `gstreamer` 0.25.3 and is dynamically linked. Runtime packaging will include only the required redistributable runtime and plugins.

## Consequences

The architecture now has executable evidence for the continuity premise that justified a persistent graph. A source switch does not reconstruct the encoder, mux, sink, or pipeline object. The graph also fails visibly if a requested pad does not become active before its deadline.

This slice is intentionally not a complete playout engine. It has video-only synthetic sources and one UDP sink. Audio, schedule-driven file decode, live ingest, canonical broadcast profiles, multi-sink fanout, captions, loudness, CG, recovery, and long-duration/three-channel proof remain required before release claims.
