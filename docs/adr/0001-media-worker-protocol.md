# ADR 0001: Versioned media-worker protocol

Date: 2026-08-20

Status: Accepted

## Context

The station control plane and each channel worker have different fault and timing domains. Their boundary must remain narrow, testable, and independently evolvable. An in-process API would couple service failure to channel output; an unbounded or unversioned byte stream would turn compatibility and memory safety into runtime guesses.

## Decision

Control commands and observed worker events use typed envelopes from `station-media-protocol`.

- Every payload carries an explicit major/minor protocol version.
- Every command carries station-scoped channel and command identifiers plus the worker sequence it expects.
- Every event carries worker and channel identifiers plus a monotonically increasing sequence.
- Payloads are JSON during the first protocol version so evidence and field diagnostics remain readable.
- Transport frames use a four-byte little-endian payload length followed by the JSON payload.
- Payloads are limited to 64 KiB before allocation or deserialization.
- Unknown major versions, truncated frames, conflicting lengths, and invalid JSON fail closed.
- Minor versions are forward-compatible when added fields are optional.

The transport will be an ACL-protected Windows named pipe. This ADR freezes the wire contract independently of that transport implementation.

## Consequences

The service and worker can be tested against captured frames and can reject unsafe rolling-version combinations before affecting output. Commands remain idempotent through command identifiers and explicit expected sequences. JSON costs more bytes than a binary schema, but the bounded local pipe traffic is small and diagnosability is more valuable at this stage.

If measurement later proves serialization to be material to media timing, the payload encoding may change only behind a new negotiated major version; command and event semantics remain stable.
