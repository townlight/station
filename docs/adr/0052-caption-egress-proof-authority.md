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

## Boundaries

The independent decoder accepts only an absolute executable path, hashes the exact executable into
the evidence, enforces capture/output/deadline bounds, parses SRT, removes only known FFmpeg-generated
CEA-708 presentation wrappers, and hashes canonical semantic text. Its real graph test generates,
captures, independently decodes, and matches an MPEG-TS cue. The orchestration job that requests the
selected-sink capture, derives the exact expected cue window, invokes this decoder, rechecks the
installed candidate at the final write boundary, and exposes current status through the API remains
unfinished. Headend carriage and viewer rendering remain external acceptance evidence, not local
decode-back proof.
