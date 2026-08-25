# ADR 0043: Bounded Windows machine inventory evidence

Date: 2026-08-24

Status: Accepted

## Context

Reference-station policy needs current GPU, storage, UPS, and broadcast-device facts, but neither
browser input nor friendly-name detection may manufacture qualification. Windows exposes useful
local inventory while leaving important physical and operational relationships unproved.

## Decision

The StationBox Windows boundary owns one fixed PowerShell inventory program. It has no operator or
HTTP-controlled script text, arguments, or fact payloads; the process-owned media path is supplied
only through a dedicated environment variable. Execution is hidden, noninteractive, profile-free,
limited to fifteen seconds and one MiB, and covered by the existing 60-second StationBox cache.

GPU, storage, and device queries fail independently. Their bounded JSON is parsed with unknown-field
rejection, normalized, stripped of serial/PNP identifiers, and then revalidated by the pure
`station-readiness` authority for freshness, sizes, text/list bounds, duplicate facts, impossible
total/free-space relationships, and unavailable groups carrying facts. A query failure marks only
that group unavailable; any malformed or contradictory normalized contract degrades the entire
inventory to unavailable before profile derivation.

Windows observations have deliberately narrow meaning:

- media-volume total capacity may satisfy the 2 TB nominal development predicate;
- for any supported local or mounted-folder media path, the same snapshot resolves the actual
  volume with `Get-Volume -FilePath`, then binds it through `Get-Partition -Volume` → `Get-Disk`;
  the collector re-queries the volume identity, size, and partition disk-number set before
  marking the typed relation verified; only a non-empty verified binding whose every disk reports
  `BusType = NVMe` may satisfy the media-medium predicate, and backing-disk size must cover the
  volume total;
- DeckLink-named PnP observations remain unknown until stable hardware identity and function are authenticated;
- GPU identity/memory remains insufficient for class or throughput;
- visible mirror-like storage remains insufficient for media-volume RAID linkage or recovery;
- an absent binding, mixed/unknown bus type, aggregate NVMe observation without the verified
  binding, or a relation whose backing size does not cover the volume remains unknown/unavailable;
  this slice does not infer redundancy, GPU, UPS, or device function from the relation;
- UPS detection remains insufficient for health, runtime, or power-failure protection;
- Blackmagic driver detection remains insufficient for Desktop Video SDK/runtime compatibility;
- no observation proves SDI function, cabling, signal lock, or external headend acceptance.

Reference catalog version 2 records these changed predicates. Version-1 tier selections are visible
but noncurrent and must be deliberately reselected.

## Consequences

Commissioning can show current, process-owned machine inventory without confusing detection with
qualification. Query or contract failure degrades evidence to unavailable rather than creating a
pass. Installed-service and physical-device proof remain separate later gates.
