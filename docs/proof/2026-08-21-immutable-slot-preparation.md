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

## Installed candidate evidence

- Frozen installed source: `b3f132a4825cb32e03915b203e435cee42c29004`
- Installer SHA-256: `3C6D91AD3E6923F02614AA811E17BBCD734AB12C5C3BED72D311375C6A636DDB`
- Installed `stationd.exe` SHA-256: `F639DF684969282080DA0644262B2040A116E37A5766B88317AE6BA0A263C3CB`
- Installed `channel-worker.exe` SHA-256: `873DF0DD9002096D5C231D0B6225055D156B374555F19D58FA46F80EE767ADB0`
- Installed GStreamer runtime SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`
- The LocalSystem service accepted the automatic signed `0.2.0` offer, and discovery created zero package or slot files.
- After explicit staging, the proof server had observed exactly one feed request and one package request. The staged 1,137-byte package matched SHA-256 `FB4910AB69A34E63D1EF1144F49F762BE51BD10AAB54C690828233F1000DCF28` and was read-only.
- Explicit preparation published `slot-0.2.0-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb` beneath the installed application root with exactly four read-only files: one slot manifest and all three exact hash-verified payloads.
- Both download quarantine and slot quarantine were empty after publication.
- Removing installer-owned trust and restarting cleared the check, stage, and prepare records and resolved the update alert.
- Final cleanup inspection found no trust fixture, staged package, prepared slot, quarantine residue, or firing update alert. The candidate remained healthy under LocalSystem.

## Boundary

This proves strict package validation and immutable slot preparation in source, tests, and the exact installed candidate. The out-of-process maintenance transaction, active-slot switching, candidate health/identity proof, and automatic rollback are not claimed here.
