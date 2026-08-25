# Installed external receiver receipt authority — 2026-08-25

## Boundary

TownLight Station now has one append-only external receiver evidence authority per configured output
sink. A Setup Admin may record accepted or rejected evidence from a named headend operator, receiver
telemetry, return-path observation, or visual/audio observation. The server—not the browser—binds the
receipt to the authenticated actor, verified installed candidate, current enabled sink, canonical
channel-output configuration hash, and recording time.

Only the newest receipt for that exact binding is current. Equal-millisecond appends use SQLite
insertion order, so a later rejection deterministically supersedes an earlier acceptance. Candidate
change, output reconfiguration, or sink disablement expires the prior receipt without deleting
history. Local transport evidence and external receiver evidence remain separate API fields.

## Source verification

The implementation was test-first. Storage tests cover accepted/rejected append-only history,
equal-time supersession, candidate/configuration expiry, disabled/stale sink rejection, observation
time and text bounds, and immutable database triggers. API tests prove Setup Admin authorization,
caller inability to forge server bindings, fail-closed unverified-candidate behavior, one-current-row
history, accepted and rejected readiness, shared loudness-view truth, and commissioning consumption.

Verification completed before packaging:

- the complete Rust workspace and doctests passed, including real worker/process and GStreamer
  UDP/SRT/media suites;
- the nondeterministic candidate-identity fixture collision exposed by the parallel workspace gate
  was corrected with a process-local atomic fixture sequence, and its 12-test lane plus the complete
  parallel workspace rerun passed;
- warning-denied Clippy passed for all workspace targets and features;
- `station-api` passed 80 tests with two explicit machine lanes ignored;
- the external receiver storage lane passed all four tests;
- all 27 operator contract tests and the production Vite build passed;
- Rust formatting and Git diff checks passed;
- the installer contract passed all 75 requirements.

## Exact installed candidate

Clean source commit `ba81f74b7f73cc0958487c9c0516be8e6fb67129` produced
`TownLight-Station-0.1.0-x64-setup.exe` with SHA-256
`cf3e19d96208e5cc9952dff039e482f2d27c0adb145bf95b5acb724bb6d07274`.

Before replacement, the installed application and both TownLight services were present. The prior
application/services were removed through the product uninstaller while
`C:\ProgramData\TownLight Station` remained. Immediately before reinstall, the preserved database
was 1,253,376 bytes with SHA-256
`9e95ad756840abfeb93dcc0c134ef67bf46b297eef8121898dc35d062f8f7273`.

The exact installer completed successfully. `installer/verify-installed.ps1` then exited zero and
proved:

- `TownLightStation` and `TownLightStationUpdater` are running automatically as LocalSystem;
- `/health` reports database and orchestration ready;
- the install receipt names source commit `ba81f74b7f73cc0958487c9c0516be8e6fb67129`;
- installed `stationd`, `channel-worker`, and `station-updater` hashes match the candidate manifest;
- the receipt-bound private runtime manifest and installed runtime bytes match; and
- all required media factories are ready.

A read-only SQLite inspection of the live preserved database returned `integrity_check=ok`, found the
`external_receiver_acceptance_receipts` table, found both no-update and no-delete triggers, and found
zero fabricated receipt rows. The existing database still contained its 43 channel records. The
installed unauthenticated receipt route returned HTTP 428, while `/api/v1/auth/status` reported
`bootstrap_required`. No credentials, receiver observation, operator identity, station setting, or
receipt were invented for installation proof.

## Remaining proof

This proves the source contract, migration, exact package, installed bytes, service health, and
fail-closed authentication boundary. It does not claim that an external receiver or cable headend has
accepted a TownLight output. That gate requires a real authenticated Setup Admin to record a real
human/device observation against an enabled installed output. Clean-machine acceptance and the
one-station and three-station field-duration gates remain separate.
