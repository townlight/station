# Station self-test proof — 2026-08-21

## Automated evidence

- Domain tests reject a passing result with any false named check, a scheduled result without a period identity, and invalid daily/weekly schedule shapes.
- Storage tests prove seeded 02:00 daily and Sunday 03:00 weekly schedules, operator edits across reopen, bounded history, one scheduled result per period, database `quick_check`, and a create/open/check/delete restore rehearsal with no database or WAL artifact left behind.
- Executor tests build a healthy synthetic station and prove all implemented daily checks pass, matching JSON evidence exists, and the durable record round-trips exactly.
- Scheduler tests prove repeated ticks produce one record for one local day.
- A cross-date-line regression uses `Pacific/Auckland` and a fixed UTC instant to prove the stored station profile timezone—not the Windows host timezone—owns the local schedule date.
- Failure-lifecycle tests remove required installed artifacts, prove a `self-test-fail` incident fires through the seeded rule, restore the artifacts, and prove the incident resolves even though uncommissioned optional checks still classify the next run as warning.
- API tests prove schedule list/edit validation, manual daily execution, and durable history reads.
- The production console build renders last daily/weekly status, every named check, local schedule controls, and daily/weekly Run now actions.

## Installed-candidate evidence

`scripts/prove-installed-self-tests.ps1` passed against the candidate built from source commit `9f022660a238a0909d0109b3230af36ef2651fd1` and installer SHA-256 `F5790A4D89626D999E902554E3575DCECD7D97A39C50A5EE34DCCD4EA4AFBA0C`.

- The Windows service was running as `LocalSystem`, and its install receipt matched the candidate manifest.
- The installed schedules were daily at 02:00 local and weekly Sunday at 03:00 local.
- A real installed channel worker reached `healthy_fallback`, and service-owned resource sample 46 was fresh and healthy.
- A manual daily run passed all 9 implemented checks. Matching durable JSON evidence was written under `C:\ProgramData\TownLight Station\self-tests`.
- A manual weekly run passed all 11 implemented checks, including a real SQLite backup/open/`quick_check` restore rehearsal and a durable test queued to an enabled proof webhook.
- The alert delivery worker sent the weekly test, the loopback receiver verified its HMAC-SHA256 signature, and the durable delivery ledger reached `sent`.
- Both self-test IDs were returned by the installed history API. No restore database or WAL sidecar remained after the rehearsal.
- The proof disabled its temporary output and alert destinations during cleanup.

## Boundary

The weekly database restore rehearsal and alert-test delivery are real. SRT receiver continuity and cable compliance are not claimed until their sink-specific product slices exist.
