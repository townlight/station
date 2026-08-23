# ADR 0021: Signed immutable update lifecycle

Date: 2026-08-21

Status: Accepted

## Context

An unattended station must learn that an update exists without allowing a network response to become executable authority. It must never silently install while a channel may be on air. The current bootstrap installer activates one application directory and can remove a failed candidate, but it cannot restore an exact prior version. That is adequate for a first installation and inadequate for product updates.

## Decision

TownLight Station uses one fail-closed update state machine with separate discovery, staging, maintenance, activation, proof, and rollback phases.

- The hourly background action downloads only a bounded release envelope. It never downloads or applies a package automatically.
- The envelope carries the exact manifest bytes plus a detached Ed25519 signature and key identifier. The station accepts only a key in its compiled trust set and uses strict verification. The signed manifest binds product identity, release channel, semantic version, source commit, publication and expiry times, minimum updater version, package HTTPS URL, exact byte length, SHA-256, and bounded Markdown release notes.
- Unknown fields, unknown or weak keys, invalid signatures, expired manifests, future-dated manifests, channel substitution, insecure remote URLs, oversized packages, invalid versions, and downgrade/equal offers fail closed. Loopback HTTP exists only for deterministic machine proof.
- A manual operator action downloads a package into a managed staging directory using a byte ceiling, then checks the signed length and SHA-256 before the package can become staged.
- A second explicit action prepares but does not activate the package. Version 2 `.tlu` files are uncompressed ZIP containers with one strict internal manifest and exactly two role-fixed payloads: `stationd.exe` and `channel-worker.exe`. The outer signed release binds the complete container; the internal manifest binds product, version, source commit, and each payload's exact size and SHA-256. The independent updater watchdog is installer-owned and is never claimed as activated by a slot update; releases that require a newer updater direct the operator to a fully verified installer through `minimum_updater_version`. Historical version 1 three-payload manifests remain readable only so existing active receipts and maintenance ledgers can be audited and recovered; they cannot be newly prepared or activated.
- Preparation rejects unknown, duplicate, encrypted, compressed, non-file, traversal, oversized, missing, identity-mismatched, or hash-mismatched entries. It extracts only allowlisted base names into a same-volume quarantine directory, synchronizes every file, marks the complete file set read-only, re-verifies it, and atomically publishes one `slot-<version>-<commit>` directory. Existing slots are never overwritten and are reusable only after a full hash and permission check.
- Application binaries live in immutable version-and-commit slots. A small out-of-process updater owns the maintenance transaction because the running service cannot replace itself safely. The updater records the previous slot, stops channel execution, starts and proves the candidate against the still-current receipts, stops it again for a quiescent receipt-generation commit, then restarts and proves the committed candidate before declaring success. Any failure in commit, restart, or the second proof restores both prior receipts, the prior slot, and the journal snapshot before proving rollback.
- After the station service stops and before a candidate is configured, maintenance snapshots the complete `channel-journals` tree into transaction-owned storage. A bounded manifest records every relative path, type, size, and SHA-256; files and the manifest are synchronized before candidate start. This protects rollback across journal-format changes rather than assuming the prior binary can read bytes written by the candidate.
- Failed proof stages and verifies the entire journal snapshot before touching the live tree, then uses write-through same-volume directory moves with a transaction-owned quarantine. Recovery converges after interruption before or after either move, restores the prior slot, and proves that restored service before reporting rollback. Candidate-created journal files are removed by restoring the exact prior tree; an absent prior tree remains absent. Corrupt backup evidence fails before the candidate tree is touched. Snapshot storage is discarded only after durable `committed` or `rolled_back` state and cleanup is retryable.
- Successful proof commits the new slot. The authoritative station database and media library are not placed inside an application slot and are never deleted by update cleanup.
- Installation remains an explicit operator action behind a maintenance gate. There is no unattended auto-install or automatic reboot.

## Consequences

Release signing-key custody and build-time public-key injection become release-engineering responsibilities. Authenticode signing and trusted timestamping remain an additional Windows distribution requirement; they do not replace the signed release manifest. The update system will be delivered in proof-sized slices: signed discovery/status first, bounded package staging second, then slot activation and destructive failure/rollback machine tests. No UI label may claim update installation before all three boundaries pass.
