# ADR 0018: Service-owned system resource sampling

## Status

Accepted — 2026-08-21

## Decision

The operator-alerting thread samples host resources immediately at service start and every 60 seconds thereafter. It reads real Windows processor times, physical-memory status, and volume free space. It also creates, syncs, reads, verifies, and removes a probe file in the station backup directory. GPU/VRAM and clock-skew values remain explicitly unavailable rather than fabricated until their dedicated probes land.

Samples are validated domain records and persisted in SQLite as bounded fixed-point integers, avoiding nonfinite database values and platform-dependent floating-point drift. Insertion identity—not wall-clock order—defines the latest sample, so a backward clock adjustment cannot hide new operating truth. Retention is capped at 10,080 samples (seven days at one-minute cadence).

The sampler is inside the running station process, so a successful write proves database reachability and service liveness. If the database itself is unreachable it cannot truthfully persist that fact in the same database; the HTTP health boundary and future external watchdog own that failure mode. Media or backup free space below 10 GB, or a failed backup write probe, raises the rule-owned `disk-low` condition; recovery resolves it.

## Consequences

- The Health console and API show measured host truth from the installed service, not browser estimates.
- Backup writability is exercised, not inferred from directory existence.
- Sampling failure terminates the alerting subsystem and therefore fails the service rather than silently presenting stale green data.
- GPU, NTP clock skew, daily/weekly self-tests, and update status remain later boundaries.
