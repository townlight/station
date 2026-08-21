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

## Boundary

This is source and local Windows execution evidence. Installed LocalSystem evidence is recorded only after an exact candidate is packaged, installed, and exercised through its installed loopback API. The weekly database restore rehearsal and alert-test queue are real. SRT receiver continuity and cable compliance are not claimed until their sink-specific product slices exist.
