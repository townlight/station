# CG bulletin expiration proof — 2026-08-21

## Commands and results

```text
cargo test --offline -p station-storage --test cg_authority expires_only_due_scheduled_bulletins_atomically_and_survives_reopen
cargo test --offline -p station-storage --test cg_authority rejects_timestamp_regression_and_rolls_back_all_due_expirations
cargo test --offline -p station-api --test cg_api expires_due_bulletins_through_idempotent_local_command
cargo test --offline -p station-orchestration
cargo clippy --offline -p station-storage -p station-api --tests -- -D warnings
cargo clippy --offline -p station-orchestration --all-targets -- -D warnings
```

Each focused test command passed `1 passed; 0 failed`; the combined storage/API Clippy gate also
passed with warnings denied. The orchestration suite passed `6 passed; 0 failed`, and its Clippy
gate passed with warnings denied.

## Scope

The tests prove due-boundary expiration, Scheduled-only state selection, revision increment,
append-only `BulletinExpired` evidence, idempotence, persistence after reopen, strict API request
shape, and atomic rollback when any due bulletin would regress its update timestamp. Orchestration
coverage also proves that automatic expiration uses the same timestamp as snapshot resolution,
removes a due bulletin before that snapshot is handed off, remains idempotent across reopen, leaves
not-yet-due and unrelated-channel bulletins unchanged, and surfaces clock regression.

## Boundary

Expiration is available through both an explicit local API command and the existing one-second
channel-orchestration cadence; no second scheduler exists. This proof does not claim
installed-machine execution, multi-station operation, or field soak.
