# Installed fallback-slate candidate proof — 2026-08-25

## Boundary

TownLight Station now has one durable, server-authoritative fallback-slate selection per channel.
Only an authenticated Setup Admin may change it, only while the channel is stopped, and only to a
currently verified managed PNG or JPEG. The server owns the revision, actor, time, and append-only
audit identity. The worker receives only the content digest and image format, reopens and rehashes
the managed object, bounds and decodes it, and cannot report `Ready` if the selected slate is
missing, substituted, corrupt, or invalid.

The fallback branch aspect-fits that exact image over black without coupling it to ordinary CG,
program, live, or future EAS-forced-slate authority. Commissioning consumes the same durable
selection plus current safe-to-air runtime evidence.

## Source verification

The implementation was developed test-first and verified before packaging:

- storage tests cover stopped-channel enforcement, managed-asset readiness, compare-and-swap
  conflicts, revision persistence across reopen, and immutable audit history;
- protocol tests cover the version-5 path-free digest/format contract and malformed identity
  rejection;
- API tests cover Setup Admin authorization, server-owned attribution, durable history, and
  commissioning consumption;
- real native worker tests cover positive selected-PNG startup and UDP output, plus fail-closed
  startup when the selected image is missing;
- an independent MPEG-TS receiver decodes the emitted video and proves the selected green pixels
  are visible in the fitted center while uncovered output remains black;
- the complete Rust workspace, warning-denied Clippy, Rust formatting, Git diff check, all 28
  operator tests, the production Vite build, and all 75 installer-contract requirements passed.

## Exact installed candidate

Clean source commit `1d4df6997ae50845d688a2b82fb8ffee6d9bd204` produced
`TownLight-Station-0.1.0-x64-setup.exe` with SHA-256
`81e887e54de28eb46e67813510f4699459a27794ce129ab70316b6576847f411`.

Before replacement, both installed TownLight services were running and the prior receipt named
source commit `ba81f74b7f73cc0958487c9c0516be8e6fb67129`. The product uninstaller removed only the
application and its two services while preserving `C:\ProgramData\TownLight Station`. After the
services had closed and SQLite had checkpointed, the preserved database was 1,277,952 bytes with
SHA-256 `ffb6aad8e4737be8c33f8cb4d60ad465b5e4ed717f7f778580922cefcc8f7d21`.

The exact installer completed successfully. `installer/verify-installed.ps1` then exited zero and
proved:

- `TownLightStation` and `TownLightStationUpdater` are running automatically as LocalSystem;
- `/health` reports the database and orchestration ready;
- the install receipt names source commit `1d4df6997ae50845d688a2b82fb8ffee6d9bd204`;
- installed `stationd`, `channel-worker`, and `station-updater` hashes match the candidate manifest;
- the receipt-bound private runtime manifest and every installed runtime byte match; and
- the required fallback image overlay and PNG/JPEG decoder factories are available with the rest
  of the media contract.

A read-only inspection of the live preserved database returned `integrity_check=ok`, retained all
43 channel records, found both fallback-slate tables and both audit no-update/no-delete triggers,
and found zero fabricated selections or audit events. `/api/v1/auth/status` reported
`bootstrap_required`; an unauthenticated fallback-slate request returned HTTP 428. No account,
credential, image selection, operator identity, or commissioning result was invented for this
installation proof.

## Remaining proof

This proves the source contract, exact package, installed bytes, database migration, service health,
and fail-closed authentication boundary. It does not claim that an operator has selected a real
station slate on the installed system. That evidence requires a real authenticated Setup Admin and
station image. Clean-machine acceptance, real headend acceptance, and the one-station and
three-station field-duration gates remain separate.
