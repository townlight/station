# System-resource sampling proof — 2026-08-21

## Automated evidence

- Domain tests reject impossible percentages, used memory above total memory, and nonfinite measurements.
- Storage tests prove fixed-point precision, signed clock-skew round trip, generated sample identity, latest-sample reads, and persistence across reopen.
- A real Windows probe test reads CPU, physical RAM, and free space, verifies a backup write-read-delete cycle, persists the result, and proves no probe file remains.
- API tests prove the latest-resource endpoint returns `null` before the first sample and the exact persisted record afterward.
- The production console build renders CPU, used/total RAM, media free space, backup writability, and sample time.

## Boundary

This is source-level and real local Windows probe evidence. It is not installed-service evidence until an exact candidate records and serves a sample from LocalSystem on the operating station database. GPU/VRAM, external clock-skew measurement, self-tests, and update alerting are not claimed.
