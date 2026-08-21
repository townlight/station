# ADR 0002: Durable per-channel event journal

Date: 2026-08-20

Status: Accepted

## Context

The channel worker owns what is actually on air. It cannot rely on an HTTP call or the control-plane database being available at the moment an output transition occurs. Losing or silently dropping an event would make as-run and compliance evidence untrustworthy.

## Decision

Each channel worker is the sole writer of one append-only local journal.

- Every record contains a fixed magic value, journal format version, bounded payload length, and CRC-32 checksum.
- The payload is the complete versioned worker-event frame from `station-media-protocol`.
- Worker and channel identity remain constant throughout a journal.
- Event sequences start at one and increase exactly by one.
- The worker appends, flushes, and calls the operating-system data synchronization primitive before emitting the event to the control plane.
- Startup scans and validates the complete journal before accepting commands, then resumes at the next sequence.
- A checksum failure, identity change, sequence gap, unsupported version, oversized record, or partial tail is an explicit recovery failure. It is never treated as a clean end-of-file.

## Consequences

The control plane can reconcile events idempotently after its own restart or a transport outage. If journal synchronization fails, the worker does not claim that the event was recorded. A crash between physical output change and journal synchronization remains a hardware-timing risk to address when the persistent media graph is integrated; output transitions will be structured so their journal intent and observed outcome are distinct events.

CRC-32 detects accidental corruption but is not an authenticity mechanism. Journal files are protected by the worker service account and NTFS ACLs; release evidence uses stronger cryptographic digests.
