# ADR 0034: StationBox readiness authority

Date: 2026-08-22

Status: Accepted

## Context

Commissioning, headend handoff, support, and later device integrations all need the same answer to
whether a station is ready. Independent booleans would allow those surfaces to disagree or turn
green from configuration alone. Readiness must distinguish measured facts, derived product policy,
and external proof that this process cannot create for itself.

## Decision

`station-readiness` owns a versioned, serialization-safe StationBox profile and its deterministic
derivation. It performs no I/O. A probe input carries station identity, current hardware, installed
engine factories, typed output support, clock, network, backup, and release facts. Validation rejects
stale, malformed, impossible, duplicate, or internally inconsistent facts before policy is applied.
Required dimensions roll up fail-closed to red, yellow, or green; unsupported and unimplemented
outputs remain explicit rather than disappearing from the result.

`station-api` is the Windows probe boundary. `GET /api/v1/station-box-profile` returns the complete
profile and `GET /api/v1/station-box-profile/readiness` returns its PEG roll-up. Both routes consume
one mutex-protected result cached for 60 seconds, so concurrent local requests cannot multiply the
resource sample, backup write probe, receipt hashing, or GStreamer subprocess work.

Installed-engine inspection runs only the private `gst-inspect-1.0.exe`, with private plugin paths,
a five-second deadline, discarded stderr, and a concurrently enforced byte limit. It accepts only
the known required factory and encoder names. The current network probe proves local route selection,
not outbound delivery, so it remains unknown. A successful localized `w32tm` status query is not
treated as synchronization proof, so clock synchronization remains unknown. Application receipts
and active binary hashes can agree, but the current media-runtime receipt identifies the bundled
runtime installer rather than installed runtime bytes; release integrity therefore remains unknown
until an authenticated installed-file manifest exists.

The test-only probe constructor is compiled only for unit tests. Production callers cannot inject
readiness facts.

## Consequences

StationBox status is now one typed authority that S2 headend checks and S3 commissioning can extend
without inventing new readiness vocabularies. The current machine will correctly remain yellow or
red for unverified clock, outbound delivery, release runtime integrity, interlaced output, SDI, and
NDI. That is an implementation result, not field evidence: clean-machine, headend, one-station-week,
and three-station-week gates remain external and false until their evidence is captured.
