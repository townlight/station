# Immutable application-slot preparation proof — 2026-08-21

## Source evidence

- `POST /api/v1/updates/prepare` is a separate explicit operator action after signed discovery and verified package staging. It does not stop channels, change the service command, or execute candidate bytes.
- The coordinator accepts only the current installed version's verified offer, the exact matching staged record, its still-trusted key identifier, and an unexpired preparation time.
- Package schema 1 is a stored ZIP containing exactly `package-manifest.json`, `payload/stationd.exe`, `payload/channel-worker.exe`, and `payload/station-updater.exe`.
- The internal manifest denies unknown fields and must exactly match signed product, semantic version, and source commit. Its file set is fixed and unique; every payload carries a bounded nonzero size and SHA-256.
- Archive inspection rejects missing or extra entries, duplicate names, directories, symlinks, encryption, compression, traversal names, and declared sizes outside the accepted bounds before extraction.
- Extraction writes only fixed base names into a unique same-volume slot quarantine, streams and hashes each payload, requires exact byte counts, synchronizes files, makes the complete set read-only, and re-verifies the slot before an atomic directory rename.
- The final slot name is `slot-<version>-<source-commit>`. An existing slot is never overwritten and is idempotently reusable only after its manifest, exact file set, sizes, hashes, types, and read-only state all pass again.
- SQLite stores only the newest valid prepared-slot record. Update status exposes it only while it matches the current staged offer and the complete slot still verifies. Removing release trust clears prepared authority along with check and stage authority.
- The operator console distinguishes download/staging from slot preparation and states that preparation does not activate or install the candidate.

## Executed tests

- Exact package extraction, read-only publication, complete re-verification, and idempotent reuse.
- Extra/traversal entry rejection before extraction with no escaped file and no slot residue.
- Internal payload digest mismatch rejection with slot-quarantine cleanup.
- Refusal to reuse or overwrite a tampered existing slot.
- Signed feed discovery through real loopback HTTP, exact package staging, immutable slot preparation, durable status, and trust-removal state clearing in one coordinator flow.
- Prepared-slot persistence across database reopen and refusal of a stale record overwrite.
- API behavior for honest unconfigured update state, including the prepare route.
- Complete workspace tests, all-target/all-feature clippy with warnings denied, and the production operator-console build.

## Boundary

This proves strict package validation and immutable slot preparation in source and machine-level tests. Installed-candidate proof, the out-of-process maintenance transaction, active-slot switching, candidate health/identity proof, and automatic rollback are not claimed here.
