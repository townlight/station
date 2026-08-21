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

The transport is a duplex byte-mode Windows named pipe in the product-scoped `\\.\pipe\townlight-station\` namespace. The server permits a single instance, rejects remote clients, and uses a protected ACL granting access only to SYSTEM, Administrators, and the creating user. The worker opens the client handle at security-identification impersonation level. Standard input and output are not part of the protocol.

## Consequences

The service and worker can be tested against captured frames and can reject unsafe rolling-version combinations before affecting output. Commands remain idempotent through command identifiers and explicit expected sequences. JSON costs more bytes than a binary schema, but the bounded local pipe traffic is small and diagnosability is more valuable at this stage.

If measurement later proves serialization to be material to media timing, the payload encoding may change only behind a new negotiated major version; command and event semantics remain stable.
