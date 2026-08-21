# ADR 0013: Durable canonical transcode

## Status

Accepted — 2026-08-20

## Context

Source admission proves that bytes are complete and decodable; it does not make an arbitrary contribution safe or predictable for unattended air. Publishing the source as the scheduled asset would couple playout reliability to every incoming codec, container, geometry, frame rate, and loudness condition. Performing the transform inline would make API latency and daemon restart behavior part of media correctness.

## Decision

Validated source bytes remain in a station-owned, content-addressed source store and never become a ready asset. In one SQLite transaction, source admission moves its parent ingest job to `awaiting_transcode` and creates one deterministic `TranscodeJob`. The job records its source identity and path, canonical format, attempt count, progress, timestamps, output identity, and bounded failure detail.

The transcode worker claims durable work independently of HTTP. GStreamer decodes the source and writes a temporary H.264 video, AAC audio, MPEG-TS derivative using the fixed `h264_720p4mbps_aac192k_ts` profile and loudness normalization. The worker closes the pipeline, validates the resulting streams, hashes the finished bytes, and atomically places them in the playout library. Source and output identities are independently verified and must not be conflated.

Post-transcode loudness is measured from the derivative. One transaction publishes a `ready` asset only when output validation and loudness pass, completes the transcode job with its output identity, and completes the parent ingest with that same identity. A terminal failure fails both jobs without publishing an asset. Interrupted `running` jobs return to `pending`; bounded transient failures retain their reason and retry time.

## Consequences

- The scheduler and channel workers see one predictable playout format rather than arbitrary contribution media.
- A database cannot contain a ready source object, a completed child with an unfinished parent, or a ready derivative without its passing loudness evidence.
- Source preservation, output publication, and recovery are explicit, independently testable boundaries.
- Installed-machine proof must verify different source/output hashes and schedule the derivative through acknowledged completion.
