# System-resource sampling proof — 2026-08-21

## Automated evidence

- Domain tests reject impossible percentages, used memory above total memory, and nonfinite measurements.
- Storage tests prove fixed-point precision, signed clock-skew round trip, generated sample identity, latest-sample reads, and persistence across reopen.
- A real Windows probe test reads CPU, physical RAM, and free space, verifies a backup write-read-delete cycle, persists the result, and proves no probe file remains.
- API tests prove the latest-resource endpoint returns `null` before the first sample and the exact persisted record afterward.
- The production console build renders CPU, used/total RAM, media free space, backup writability, and sample time.

## Installed candidate evidence

Exact candidate `a2c7231fabab8c2048f9f0c6e8b934029c76255f` was packaged as installer SHA-256 `0E7D023847B651B4B300EAEFA37570E2F555940A5A01059831DB801B8D4E9E7D`, installed in place, and passed the standard receipt, installed-binary, automatic LocalSystem service, `/health`, private-runtime, and media-factory verification.

`scripts/prove-installed-system-resource-sampling.ps1` then queried only the installed API and proved:

- persisted sample identities advanced from 2 to 3;
- the service-owned interval was 60,087 milliseconds;
- the second sample reported 5.66% CPU and 23.775 of 63.791 GB physical memory;
- media and backup volume free space were each 1,026.458 GB;
- database reachability, service-running truth, and the backup write-read-delete probe were all true; and
- no `.write-probe-*` file remained.

## Boundary

This proves source behavior and installed LocalSystem service behavior for CPU, physical memory, volume capacity, database reachability, and backup writability on this machine. GPU/VRAM, external clock-skew measurement, self-tests, and update alerting are not claimed.
