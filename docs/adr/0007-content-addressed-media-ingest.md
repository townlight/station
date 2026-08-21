# ADR 0007: Content-addressed media admission

Date: 2026-08-20

Status: Accepted

## Context

An operator-selected filename is not sufficient authority for unattended playout. The station must reject files it cannot decode, avoid exposing partially copied bytes, detect duplicates, and prove that the worker loaded the same bytes the control plane admitted.

## Decision

`station-media-assets` is the single file-admission boundary.

- GStreamer discovery must complete successfully and report a finite duration plus at least one video and one audio stream.
- The source is streamed into a temporary file in the destination library while SHA-256 is calculated.
- The temporary file is flushed and synchronized before an atomic same-volume rename.
- The destination name is the lowercase SHA-256 identity plus a sanitized source extension.
- Existing content with the same identity is verified instead of duplicated or overwritten.
- The stored object is discovered again; metadata must equal the source result.
- The channel worker rehashes the stored object and requires the command's asset identity to match before decoding it.
- The public API admits media only through this boundary. It derives the database record from the measured stored asset; no route accepts a caller-declared `ready` identity/path pair.

## Consequences

The database can refer to immutable content identity instead of mutable operator paths. Duplicate ingest is idempotent and incomplete copies never receive an asset name. Hashing uses heap storage because the Windows worker main thread cannot safely carry a one-megabyte stack buffer. Admission runs behind the durable job contract in [ADR 0010](0010-durable-background-media-ingest.md). Multipart/browser upload, watch folders, loudness measurement, thumbnails, replacement workflow, retention, and missing-file dashboards remain separate higher-level lifecycle work.
