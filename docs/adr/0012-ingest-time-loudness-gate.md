# ADR 0012: Ingest-time loudness gate

## Status

Accepted — 2026-08-20

## Context

Finite A/V and codec decodability do not establish that a file is safe to air. PEG sources arrive at inconsistent levels, and an operator needs an honest badge before scheduling. Loudness analysis must not rely on a development-only executable or claim success when measurement fails.

## Decision

`station-media-assets` measures integrated loudness with GStreamer's `ebur128level` analyzer. The pipeline decodes audio and video without a real-time sink, retains the final finite global measurement, fails visibly on bus errors or a 120-second processing deadline, and always returns the pipeline to `NULL`.

The canonical target is -24 LUFS with ±2 LU tolerance. Storage uses integer milli-LUFS to avoid floating-point authority. `AssetLoudnessAnalysis` records `ok`, `failed`, or `not_checked`, the target, optional measurement, and bounded operator detail. Under ADR 0013, only the canonical derivative's passing measurement is published; the ready asset, completed transcode job, completed ingest job, and analysis are one SQLite transaction.

A loudness miss does not destroy or mislabel otherwise valid source media. Canonical processing normalizes it before measuring the derivative. A measurement or normalization miss prevents publication and remains an operator-visible job failure; it never becomes a false pass. Per-asset readiness and the dashboard expose the durable processing state. Egress profiles will own final per-sink target selection.

The installer requires the private runtime's `gstrsaudiofx.dll`, and installed verification requires the `ebur128level` factory.

## Consequences

- Operators see measured LUFS and the target before air.
- Source admission remains lossless and content-addressed.
- Commit-to-Air sees only a verified derivative whose post-transcode loudness passed.
