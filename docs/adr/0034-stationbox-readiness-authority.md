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
the known required factory and encoder names. The clock and network probe queries the current Windows Time source,
removes only its comma-delimited peer flags, and obtains exactly three one-second
`w32tm /stripchart /rdtsc` offset samples. The fixed hidden PowerShell boundary parses each numeric
offset with the current culture and emits one exact ASCII evidence line. Every finite sample must be
within two seconds for synchronized readiness. Any complete three-sample exchange proves one real
outbound UDP request/reply path to the configured source; when that source is a hostname, the same
exchange proves working name resolution. An IP-literal source leaves DNS unknown. This evidence does
not claim arbitrary UDP, SRT, a configured headend destination, or receiver acceptance. Command
failure, an unsafe source, and malformed or missing samples leave clock and network evidence
unavailable or unknown rather than green. Application and
active-slot receipts must agree, and every application file is rehashed. The approved media-runtime
package identity is bound to a full installed-file manifest whose exact bytes are SHA-256-bound into
the installation receipt. Every runtime file is re-enumerated and rehashed; missing, extra,
modified, unsafe, unbounded, or identity-mismatched evidence keeps release integrity from passing.

The Windows boundary also runs one fixed, hidden PowerShell inventory program with no profile,
no interactive input, a fifteen-second deadline, and a one-MiB stdout cap. The media path crosses only
through a process-owned environment variable; HTTP cannot supply script text, commands, paths, or
facts. Independently guarded CIM/storage queries report adapter identity, driver-reported memory,
media-volume total capacity, visible physical/virtual disk topology, UPS/DeckLink PnP matches, and
Blackmagic signed-driver metadata. Serial and PnP identifiers are discarded. A failed or malformed
group becomes unavailable, never green. Total media capacity may satisfy only its explicit nominal
capacity predicate. Friendly names, mirror-like topology, GPU identity, UPS presence, or driver
presence do not prove stable device identity, RAID linkage, GPU class/throughput, UPS protection,
SDK compatibility, SDI function, cabling, signal lock, or
headend acceptance. This semantic change is reference catalog version 2; old selected-tier intent
remains visible but stale until a Setup Admin reselects against the current catalog.

The test-only probe constructor is compiled only for unit tests. Production callers cannot inject
readiness facts.

## Consequences

StationBox status is now one typed authority that S2 headend checks and S3 commissioning can extend
without inventing new readiness vocabularies. The current machine will correctly remain yellow or
red for unavailable or excessive-offset clock evidence, unproved arbitrary/SRT/headend/receiver
delivery, interlaced output, SDI, and NDI. That is an implementation result, not field evidence:
clean-machine, headend, one-station-week, and three-station-week gates remain external and false until
their evidence is captured.
