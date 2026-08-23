# ADR 0002: Durable per-channel event journal

Date: 2026-08-20

Status: Accepted

## Context

The channel worker owns what is actually on air. It cannot rely on an HTTP call or the control-plane database being available at the moment an output transition occurs. Losing or silently dropping an event would make as-run and compliance evidence untrustworthy.

## Decision

Each channel worker is the sole writer of one logically append-only local journal.

- Every record contains a fixed magic value, journal format version, bounded payload length, and CRC-32 checksum.
- Event-record payloads are complete versioned worker-event frames from `station-media-protocol`; v2 also defines checked heartbeat-summary chunks and an explicit compaction-commit record.
- Worker and channel identity remain constant throughout a journal.
- Event sequences start at one and increase exactly by one.
- The worker appends, flushes, and calls the operating-system data synchronization primitive before emitting the event to the control plane.
- Startup scans and validates the complete retained journal before accepting commands, then resumes at the greatest covered sequence plus one.
- Format v2 retains every non-heartbeat event verbatim. When raw heartbeat records exceed 512, the oldest run is compacted and the newest 256 raw samples remain. A compacted run records its exact sequence range, sample count, first/last monotonic times, first/last ordered output counters, and a rolling SHA-256 digest chain over the removed event frames. Large collections of sparse runs are split across independently bounded summary records rather than eventually exceeding the record ceiling.
- Compaction writes a complete temporary v2 journal ending in a commit record, flushes and synchronizes it, validates its identity, sequence coverage, next sequence, and commit boundary, closes the active handle, and atomically replaces the active file with Windows write-through semantics. A temp is promoted only when the active file is missing, the temp has a valid compaction commit, and its worker/channel identity matches. A stale temp never masks corruption in an existing active journal. An ambiguous replacement failure leaves the writer unavailable and the original journal untouched; it cannot create a replacement file or acknowledge later events.
- Sequence gaps are valid only when checked heartbeat-summary ranges cover every missing sequence. A checksum failure, uncovered gap, overlap, identity change, unsupported version/kind, oversized record, inconsistent summary/output endpoints, absent commit, or partial tail is an explicit recovery failure. It is never treated as a clean end-of-file.
- Existing v1 event-only journals remain readable and can receive v2 records without resetting sequence identity.

## Consequences

The control plane can reconcile retained events idempotently after its own restart or a transport outage. If journal synchronization or atomic compaction fails, the worker does not claim that the event was recorded. Raw heartbeat growth is bounded between 256 and 512 records per channel, while sparse as-run transitions and the summary chunks proportional to those transitions remain verbatim and do not acquire a silent retention deadline. The typed inspection view exposes retained events, compacted ranges, counter endpoints, digest chains, and the next covered sequence. A digest is opaque evidence for comparison or an external verifier that possesses the original frames; the discarded raw frames cannot be reconstructed from it.

CRC-32 detects accidental corruption but is not an authenticity mechanism. Journal files are protected by the worker service account and NTFS ACLs; release evidence uses stronger cryptographic digests.
