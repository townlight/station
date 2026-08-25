# Commissioning support-bundle receipt proof — 2026-08-25

## Boundary

Support-bundle export remains generally available for diagnostics. It becomes commissioning
evidence only after the server finds a completed commit report in the same chronological chain as a
completed ingest whose ready bytes remain present. The client cannot name or forge that report.

The server appends a bounded SQLite receipt containing the bundle identity, completed report
identity, archive size, archive SHA-256, creation time, and authenticated operator identity. Update
and delete triggers make receipt rows append-only. Each commissioning report reopens the managed
ZIP by its validated bundle identity, enforces the 16 MiB read bound, and rechecks current length and
SHA-256. A missing, replaced, truncated, expanded, or changed archive cannot pass.

This receipt proves the locally managed diagnostic archive is bound to one completed guided
broadcast. It does not prove the bundle was transmitted to support, reviewed, accepted by a
headend, or retained off station.

## Test-first evidence

The storage RED run failed to compile because `CommissioningSupportBundleReceipt` and its append and
list methods did not exist. After the storage contract was implemented, its focused round trip
passed and rejected both duplicate identity and a missing commit report.

The API RED run completed the real guided-ingest/schedule/dispatch chain and created a support
bundle, but failed because `commissioning_report_id` was absent:

`left: Null; right: "report-guided"`

The completed end-to-end test now proves all three states:

1. the support-bundle check is blocked before a receipt;
2. server-created receipt plus matching current archive bytes passes;
3. changing the ZIP bytes in place, without changing its path or receipt, returns the check to
   blocked.

## Verification

- Complete `station-storage`: 78 passed, 0 failed.
- Complete `station-api`: 80 passed, 0 failed, with two explicit Windows machine lanes ignored in
  the deterministic suite.
- Operator console: 25 contract tests passed.
- TypeScript and Vite production build passed.
- Warning-denied Clippy passed for all `station-storage` and `station-api` targets and features.

## Remaining installed boundary

The exact committed candidate still requires release packaging and installed-machine verification.
The preserved installed station currently has no operator account, so an authenticated live
commissioning-chain exercise requires owner-selected credentials and is not claimed by this source
proof.
