# ADR 0028: CG authority and deterministic snapshot boundary

Date: 2026-08-21

Status: Accepted

## Context

CG configuration must survive service restarts and concurrent operator edits without allowing a
partially written board to reach playout. The station also needs a stable boundary between
operator-authoritative configuration and runtime data that may be unavailable, stale, or owned by
another subsystem. Treating a feed fetch, asset lookup, schedule lookup, or renderer side effect as
part of a configuration write would make approval, audit, and failure behavior ambiguous.

## Decision

The CG authority layer owns durable, revisioned board, zone, feed-source, bulletin, feed-item
approval, and audit records in SQLite. Writes use optimistic revisions and one transaction; an
accepted write and its append-only audit event commit together, while a conflict, invalid
cross-entity reference, or audit failure rolls the entire transaction back. Board and zone
configuration is validated against the selected built-in template, channel identity, enabled feed
source, and managed asset readiness where those dependencies are available.

Managed CG image authority is content-addressed and path-independent. SQLite stores only the
SHA-256 identity, MIME/format, byte size, dimensions, and readiness. `StationStore` owns the exact
canonical CG image root and derives the only permitted filename from identity and format.
Admission fully decodes bounded PNG/JPEG content before publishing it atomically. Snapshot
resolution loads only board-referenced image identities and downgrades missing or failed
verification to typed readiness placeholders.

The pure `station-cg` snapshot resolver is the boundary to playout. Given an immutable board,
template, zone, feed-source, bulletin, and timestamp input, it validates ownership and ordering,
filters airable bulletins and allowed tags, and returns a deterministic `CgResolvedSnapshot`.
External or not-yet-owned data is never invented: the snapshot carries a typed
`CgResolvedContent::Placeholder` with a source and reason (for example, unavailable feed data,
schedule data, image readiness, or live-source readiness). This keeps the renderer contract total
and inspectable while preserving the distinction between “no data” and “configuration failure.”

The six built-in templates are the initial supported template authority: fullscreen, L-bar,
lower-third, bug, ticker, and emergency overlay. Custom uploaded templates are not introduced by
this decision.

## Consequences

Operator writes have durable identity, compare-and-set conflict detection, and an auditable history.
Snapshot generation is repeatable for identical inputs and cannot partially publish a malformed
board. The renderer can consume one typed contract and deliberately render a safe placeholder for
unresolved external sources. Feed/bulletin moderation and tag policy can be tested independently
from the media graph.

Ready-image verification uses a cache capped at 256 identities. File existence, file type, length,
and modification time are checked on each snapshot load; full read/hash/decode is repeated after a
change or at most 60 seconds. Reparse targets fail closed.

The authority model and snapshot resolver remain separate from network I/O, credential handling,
asset readiness services, schedule resolution, and GStreamer lifecycle. Those integrations must
consume the boundary rather than mutate it implicitly.

## Explicit non-claims

This ADR does not claim any of the following are complete:

- authenticated CalDAV/social-provider compatibility and credentials;
- WPE/HTML styling, live-video compositing, or background-audio mixing through the loudness path;
- program-aware schedule interstitial resolution;
- a 24-hour CG soak, installed proof, or field-station proof.

These are follow-on implementation and proof gates, not reasons to weaken the authority or snapshot
contract.
