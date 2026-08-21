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
- Application binaries live in immutable version-and-commit slots. A small out-of-process updater owns the maintenance transaction because the running service cannot replace itself safely. The updater records the previous slot, stops channel execution, flips the active-slot receipt, starts the candidate, and performs bounded post-update proof.
- Successful proof commits the new slot. Failed proof restores the prior slot and proves that restored service before reporting rollback. The authoritative station database and media library are not placed inside an application slot and are never deleted by update cleanup.
- Installation remains an explicit operator action behind a maintenance gate. There is no unattended auto-install or automatic reboot.

## Consequences

Release signing-key custody and build-time public-key injection become release-engineering responsibilities. Authenticode signing and trusted timestamping remain an additional Windows distribution requirement; they do not replace the signed release manifest. The update system will be delivered in proof-sized slices: signed discovery/status first, bounded package staging second, then slot activation and destructive failure/rollback machine tests. No UI label may claim update installation before all three boundaries pass.
