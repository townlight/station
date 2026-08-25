# ADR 0051: Schedule-authoritative caption dispatch

## Decision

The channel controller, not the browser, owns caption timing. After the worker acknowledges an exact
scheduled asset take, the controller derives the current asset-relative media offset from the
committed schedule window. It reads the channel's durable CEA-708 language/service policy and the
one current asset track occupying that exact language/service slot. Disabled policy or no matching
track means no cue is expected; it never fabricates caption presence.

Each controller tick surfaces expired cues as transient runtime detail, resumes a cue already active at the current media offset
with zero delay and only its remaining duration, and prequeues future cues within the worker
protocol's bounded sixty-second relative-delay window. A tick sends at most 64 cues. The worker
derives presentation timestamps from its running graph clock, acknowledges every canonical cue
identity, and a missing or mismatched acknowledgment fails the channel visibly. In-memory accepted
identities prevent duplicate sends within one worker lifetime. Worker restart deliberately rebuilds
the cursor from schedule offset because the replacement worker has no prior caption queue.

Caption dispatch state is bound to the commit report, track identity, track revision, and cue digest.
It is discarded when scheduled playout completes or live takeover begins. Asset-track mutation is
already prohibited while that asset is reported on air, so a running schedule cannot silently switch
cue authority underneath the cursor.

## Boundaries

A `CaptionCueAccepted` event proves only that the isolated worker accepted the server-derived cue.
It does not prove MPEG-TS insertion, decode-back, headend carriage, viewer display, or FCC compliance.
Those require current candidate/configuration/policy/track-bound output evidence. Expired-cue runtime
detail is not yet a durable incident or safe-to-air readiness authority. An asset without a
matching reviewed track stays explicitly uncaptioned rather than causing unrelated program playout
to fail.

## Consequences

- Browser time, browser timers, and uploaded absolute timestamps never control on-air caption PTS.
- Catch-up after delayed take or worker replacement is deterministic from the committed schedule.
- Live takeover suspends schedule dispatch; handback re-derives due cues from the current media
  offset.
- Every source-role transition synchronously clears already accepted future cues before the new
  source is selected.
