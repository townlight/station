# Installed legacy-journal compaction proof

Date: 2026-08-22

Result: Passed

## Candidate identity

- Source commit: `4e937292d365fda4b5f6a366832c07fa690fee9b`
- Installer: `dist/TownLight-Station-0.1.0-x64-setup.exe`
- Installer SHA-256: `4C1D825AF1EAC2C81A96D0E1615757B3C934642A9E752B3BAE280BFD3CF1B2DA`
- `stationd.exe` SHA-256: `9C7F34AD82F780C0C8DDD5ACFC8A92CFCC9B555F0B82762C05547507C798B700`
- `channel-worker.exe` SHA-256: `EDC8760239639CCFBE394537467E34EA11A6F4C7FCA332B9DCE522CC3BE2CEB1`
- `station-updater.exe` SHA-256: `851F2ACECC9ED77A6E24C591839C291EB4F89E5B2D2C38F290B2D6C31BA84F4A`
- Media runtime SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`

The installer exited zero. `verify-installed.ps1` then matched every installed binary to the candidate manifest, found both services running automatically as LocalSystem, found the private media runtime ready, and received ready health from the installed service.

## Real legacy baseline

The pre-upgrade LocalSystem worker had accumulated a real production-format v1 journal at `C:\ProgramData\TownLight Station\channel-journals\70707070-8080-4090-80a0-b0b0b0b0b0b0.tlj`. The service was stopped before the evidence copy, so the baseline contains no partial concurrent append.

- Preserved evidence: `C:\Users\scott\Documents\Codex\installed-legacy-v1-before-4e937292.tlj`
- SHA-256: `0EC748300E9F644BCD24292AFA2213B2945BEDA6FD0EFF88379D2B80B487CB65`
- Length: 7,028,285 bytes
- Physical v1 event records: 22,651
- Raw heartbeat records: 22,558
- Non-heartbeat records: 93
- Next sequence: 22,652
- Compacted ranges/samples: 0 / 0

The committed `inspect_journal` example validated every record checksum, identity, and sequence before installation.

## Installed result

After installing the candidate, the LocalSystem service opened that same journal and compacted it without an artificial threshold or debug mode. The service was stopped briefly a second time to preserve an exact post-compaction copy, then restarted and reverified healthy.

- Preserved evidence: `C:\Users\scott\Documents\Codex\installed-v2-after-4e937292.tlj`
- SHA-256: `998158C3A7A034039111E5BA30D68C256A0D2C4ECC826D4EE44E1BD4DA31AB58`
- Length: 112,061 bytes
- First physical record: `TLJR`, format 2, heartbeat-summary kind
- Compacted heartbeat ranges: 22
- Compacted heartbeat samples: 22,303
- Retained raw heartbeat records: 263
- Retained non-heartbeat records: 95
- Retained physical event records: 358
- Next sequence: 22,662

The exact logical-coverage equation passed:

`22,303 compacted samples + 358 retained event records = 22,661 covered sequences = next sequence 22,662 - 1`

The raw-heartbeat bound also passed (`263 <= 512`). All 93 pre-upgrade non-heartbeat records survived, and two additional lifecycle events written by the candidate were retained. File length fell by 98.41% while preserving continuous sequence truth, checked timing/counter endpoints, and one SHA-256 digest chain per compacted range.

## Supporting gates

- `station-media-journal`: 1 unit and 16 integration tests passed, including incomplete/incorrect compaction artifacts, missing commit, checksum/tail corruption, sequence gaps, v1 reopening, repeated compaction, sparse summary chunking, output endpoint corruption, and replacement failure.
- `station-maintenance`: 4 unit and 10 activation tests passed, including a valid 520-record v1 fixture read by a v1-only reader, exact rollback restoration, candidate-created file removal, failed-stop protection, corrupted-backup protection, interrupted restore, interrupted snapshot publication, path confinement, and the dangling-link boundary where Windows permits creating the fixture.
- `channel-worker`: 6 unit, 5 process, and 6 supervision tests passed with the real installed GStreamer runtime.
- Full workspace tests and doctests passed before the final localized fail-closed hardening; both changed packages were rerun afterward.
- Workspace formatting and all-target/all-feature Clippy passed with warnings denied.
- Installer contract passed all 31 requirements.
- Independent Luna review reported no remaining P1/P2 blocker after the final fixes.

## Limits

This proves installed v1-to-v2 conversion and continued operation on one real Windows station journal. Source-level tests inject interrupted temp writes, ambiguous replacement failure, corrupted rollback evidence, failed candidate stop, and both restore move boundaries. It does not claim a physical power-cut test during `MoveFileExW`, multi-station field soak, or the one-week/three-station beta acceptance gates.
