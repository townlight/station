# ADR 0050: Asset-bound caption-track authority

## Decision

Caption source content is a server-owned domain separate from channel transport policy and worker
execution. `station-captions` defines a track as one bounded, nonempty sequence of asset-relative
cues. Every cue has a lowercase canonical identity, nonnegative start, duration of at most sixty
seconds, bounded plain UTF-8 text with intentional line breaks, and an end no later than the media
asset duration. Cues must be ordered, non-overlapping, and uniquely identified.

SQLite remains the single authority writer. One track may occupy each exact
`(asset SHA-256, language, CEA service)` slot. A track identity is permanently bound to that slot;
changing media bytes creates a different asset identity and never transfers captions silently.
Writes require a currently ready asset, fail while that asset is reported on air, and use an exact
expected revision. Each accepted write replaces the current cue set atomically and appends a
complete immutable snapshot containing metadata, every cue, actor, time, revision, and a
domain-separated SHA-256 digest of cue order, identities, timing, and text. Database triggers reject
updates or deletion of revision headers and revision cues.

Source provenance is explicit as `embedded`, `sidecar`, `generated`, or `external`; provenance does
not itself mean a cue was reviewed, aired, or decoded. The authenticated API owns the
operator/review decision that promotes content into this authority. Any signed-in operator may list
compact per-asset summaries and read one current track plus its complete audit snapshots; only a
Setup Admin may write. The route owns the asset and track identities, authenticated actor, clock,
and audit identity, rejects unknown JSON fields, and caps exact caption-track PUT bodies at 8 MiB.

## Boundaries

This decision proves durable caption-source identity and revision history only. It does not prove
that a scheduled cue was sent, accepted by the worker, inserted into MPEG-TS, decoded from an exact
output, accepted by a headend, or compliant with FCC Part 79. Those are independent dispatch,
decode-back, receiver, and legal boundaries.

Runtime scheduling must use the schedule/take acknowledgment and media offset as its timing
authority. It may translate due asset-relative cues into bounded worker-relative delays, but it may
not use browser time or infer emission from stored cue presence. A worker cue acknowledgment is
ingress evidence, not decode-back proof.

## Consequences

- Multiple languages and services can coexist without ambiguous selection.
- Stale editors and duplicate audit identities cannot partially replace current authority.
- Historical revisions remain reconstructable rather than retaining only a lossy change summary.
- Editing captions during active playout requires stopping or completing that asset first.
- The operator review/import UI parses bounded WebVTT/SRT locally for immediate feedback, assigns new
  canonical cue identities instead of trusting file identifiers, and submits only the typed API
  contract. Dispatch cursor, live ASR, and exact-output proof remain explicit later work.
