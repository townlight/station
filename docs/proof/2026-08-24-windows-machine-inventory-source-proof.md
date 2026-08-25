# Windows machine inventory source proof — 2026-08-24

## Candidate boundary

This is a development-machine source proof for the current uncommitted slice. It is not an installed
candidate, clean-machine, physical-device, headend, or field proof.

## Executed proof

The ignored Windows proof lane built the real `station-api` test binary, invoked the same fixed
bounded collector used by StationBox, required at least one collected domain, and emitted the parsed
contract. With the complete signed-driver query enabled, the final collector execution completed in
6.15 seconds inside its fifteen-second deadline. The surrounding build-and-test command took 18.44
seconds; compiler time is not collector runtime.

Observed facts on this machine:

- GPU: AMD Radeon(TM) 8060S Graphics, driver `32.0.31041.1004`, driver-reported memory about 4 GiB;
- media volume total: about 1905.65 GiB (2 TB nominal class);
- visible storage: one 2 TB-class NVMe SSD and one external 8 TB-class USB HDD, both reported healthy;
- no virtual disk, UPS, DeckLink, or Blackmagic signed-driver match was observed.

The executed collector proof does not claim GPU class/throughput, storage redundancy or
media-volume linkage, UPS protection, DeckLink/SDI function, Desktop Video SDK compatibility,
cabling, signal lock, receiver acceptance, or any installed-service/field gate. That run predates
the typed media-volume binding below and remains intentionally non-authoritative for the new
predicate.

## Contract-level linkage proof

The current source contract adds `media_volume_backing_disks` and its typed verification flag to
the version-6 StationBox profile. The fixed PowerShell boundary resolves mounted-folder paths
with `Get-Volume -FilePath`, binds with `Get-Partition -Volume` and `Get-Disk`, and rechecks
volume identity, size, and disk-number set; it supplies no serial, PNP, or operator-controlled
identifiers. The API fixture trims and converts the relation, rejects missing/negative sizes, and
the pure readiness fixture proves that a same-snapshot all-NVMe relation passes only
`MediaStorageMedium` while `RedundantMediaStorage` remains unknown. No native GStreamer-linked
test or live Windows collector was run for this contract-only proof.
