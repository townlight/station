# CG bulletin expiration proof — 2026-08-21

## Commands and results

```text
cargo test --offline -p station-storage --test cg_authority expires_only_due_scheduled_bulletins_atomically_and_survives_reopen
cargo test --offline -p station-storage --test cg_authority rejects_timestamp_regression_and_rolls_back_all_due_expirations
cargo test --offline -p station-api --test cg_api expires_due_bulletins_through_idempotent_local_command
cargo clippy --offline -p station-storage -p station-api --tests -- -D warnings
```

Each focused test command passed `1 passed; 0 failed`; the combined storage/API Clippy gate also
passed with warnings denied.

## Scope

The tests prove due-boundary expiration, Scheduled-only state selection, revision increment,
append-only `BulletinExpired` evidence, idempotence, persistence after reopen, strict API request
shape, and atomic rollback when any due bulletin would regress its update timestamp.

## Boundary

Expiration is currently an explicit local API command. This proof does not claim an automatic
scheduler, installed-machine execution, multi-station operation, or field soak.
