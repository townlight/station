# ADR 0052: Caption egress proof authority

## Decision

Caption emission evidence is a separate immutable authority from source tracks and worker cue
acknowledgments. A proof record binds one exact candidate version and 40-character source commit,
channel and enabled UDP/SRT sink configuration hash, committed schedule report and asset, current
CEA-708 policy revision/language/service, current asset-track revision and cue digest, bounded
proof-owned MPEG-TS artifact path/size/hash, and independent decoder identity and results.

SQLite is the sole writer. Before append it re-reads the schedule, policy, track, sink, and canonical
output configuration under an immediate transaction. Any stale or mismatched binding aborts the
write. Database triggers reject update and delete. History therefore remains evidence even after a
later configuration change, but it cannot be interpreted as current without rechecking every bound
identity.

A `verified` outcome requires at least one expected and decoded cue, equal cue counts, and exact
expected/decoded canonical text digests. A mismatch may be recorded only when those facts actually
differ. Worker acceptance, configured output, packet transmission, and an empty decoder result can
never be converted into verified caption evidence.

Expected evidence is derived from the validated current track with a half-open asset-time capture
window. Cues already active at capture start and cues beginning before capture end are included in
track order; expired cues and cues beginning exactly at capture end are excluded. A window extending
beyond the bound media asset or containing no expected cues cannot become a proof request.

The durable execution request stores that expected count/digest with every authority binding before
work begins. One unfinished job may occupy a channel/sink at a time. Claims and state changes use
bounded leases and compare-and-set transitions; expired claimed/capturing/analyzing work returns to
pending with partial capture facts cleared. Completion appends the immutable proof and changes the
job to terminal `completed` in one transaction. A mismatch between proof, capture, or original job
facts rolls back both changes. Terminal failure remains durable and bounded.

The authenticated request boundary accepts only schedule report, enabled sink, duration, and byte
limit. It rejects extra caller-supplied authority facts, requires that report's exact asset to be on
air under a fresh runtime heartbeat, and selects a future cue-bearing half-open window with execution
lead time. Policy, track revision/digest, expected semantic evidence, candidate version/source
commit, and canonical output configuration are all server-derived. Job reads are channel-confined;
proof history is bounded to the newest 100 immutable records.

## Boundaries

The independent decoder accepts only an absolute executable path, hashes the exact executable into
the evidence, enforces capture/output/deadline bounds, parses SRT, removes only known FFmpeg-generated
CEA-708 presentation wrappers, and hashes canonical semantic text. Its real graph test generates,
captures, independently decodes, and matches an MPEG-TS cue. The installer extracts only the pinned
executable and license from a hash-verified BtbN LGPL archive into a private application runtime;
stationd never resolves the decoder from ambient `PATH`.

Orchestration claims the job, serializes it against loudness capture on the same worker tap, renews
the bounded lease, and waits for the exact selected asset-relative start. It fails a missed start,
wrong on-air asset, stale report/policy/track/sink/configuration/candidate, missing exact decoder, or
ambiguous worker response. A valid worker result is independently rehashed, atomically moved from the
worker capture root to `caption-proofs/<job>.ts`, rehashed again, decoded off the control loop, and
written only after a final candidate/configuration recheck. Disabled or controllerless channels fail
unfinished jobs and remove only deterministic job-owned artifacts. Semantic inequality is recorded
as `mismatch`; decoder/runtime failure is not evidence and terminates the job as failed.

The API request, status, immutable-history, and orchestration execution boundaries are implemented.
Installed exact-candidate execution proof remains pending.
Headend carriage and viewer rendering remain external acceptance evidence, not local decode-back
proof.
