# ADR 0019 — Durable station self-tests

## Status

Accepted on 2026-08-21.

## Decision

TownLight Station runs evidence-bearing daily and weekly checks inside the supervised alerting subsystem. SQLite owns two seeded, operator-editable schedules: daily at 02:00 local and weekly on Sunday at 03:00 local. Schedule evaluation converts the current UTC instant through the commissioned station profile's IANA timezone, including daylight-saving changes. An uncommissioned station temporarily uses the operating-system timezone.

Every scheduled run carries a local calendar period key. A partial unique index on `(kind, schedule_period_key)` for scheduled records guarantees one run per due local period across repeated 30-second ticks and service restarts. Manual runs have no period key and never suppress the scheduled run.

Daily execution checks SQLite integrity, backup-directory write/read/sync/delete behavior, installed station and worker binaries, the private media runtime, station identity, enabled output configuration, fresh healthy output heartbeats, and a recent healthy resource sample. Missing commissioning state produces `warn`; failure of database, backup, executable, or media-runtime checks produces `fail`.

Weekly execution includes the daily set plus a real SQLite `VACUUM INTO` backup, opens the candidate as a separate station database, runs `quick_check`, and removes the database plus any WAL sidecars. It also queues one durable send-test for each enabled operator-alert destination. SRT receiver continuity and cable transport compliance are applicable only after those sink types exist; this slice does not manufacture passing results for absent capabilities.

Each completed run persists its named checks and writes matching JSON evidence through a create/sync/rename sequence. A `fail` raises the seeded `self-test-fail` warning through the same rule, delivery, dedupe, and re-alert authority as every other operational condition. A later pass or warning resolves that firing condition because the core failure has cleared.

## Consequences

- A reboot later in the same due local day catches up once, but cannot create a page storm or duplicate scheduled record.
- Station timezone is operational input, not unused metadata.
- `warn` distinguishes incomplete commissioning from a broken appliance core.
- An evidence path is auditable and durable, while the database remains the query authority.
- Deeper sink-specific checks land with their actual S2/S15/S17 implementations and will extend the weekly executor without weakening current truth.
