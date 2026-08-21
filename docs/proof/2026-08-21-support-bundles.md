# Support-bundle proof — 2026-08-21

## Automated evidence

- The support-service test creates a real ZIP from a populated station database containing an encrypted alert credential canary. Neither that canary, the full webhook target, nor an unknown install-receipt field appears in the archive bytes.
- The ZIP contains the typed manifest, installation identity, redacted destination metadata, current runtime truth, and bounded alert/resource/self-test documents. It contains no database file.
- The test opens the ZIP, checks the manifest identity, and verifies every listed entry's byte length and SHA-256 against its actual bytes.
- Storage regressions prove newest-first bounded resource, alert-event, and alert-delivery reads and reject invalid limits.
- The API integration test creates a bundle, downloads identical ZIP bytes, verifies the attachment metadata, opens the manifest, and rejects a path-escape-shaped bundle ID.
- The production console build exposes explicit Create support bundle and Download ZIP actions plus path, archive hash, entry count, byte size, and retention warning.

## Boundary

This is source and local Windows execution evidence. Installed `LocalSystem` creation, download, manifest/hash inspection, redaction-canary audit, retention, and cleanup evidence is recorded only after an exact candidate is packaged and installed.
