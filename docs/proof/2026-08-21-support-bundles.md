# Support-bundle proof — 2026-08-21

## Automated evidence

- The support-service test creates a real ZIP from a populated station database containing an encrypted alert credential canary. Neither that canary, the full webhook target, nor an unknown install-receipt field appears in the archive bytes.
- The ZIP contains the typed manifest, installation identity, redacted destination metadata, current runtime truth, and bounded alert/resource/self-test documents. It contains no database file.
- The test opens the ZIP, checks the manifest identity, and verifies every listed entry's byte length and SHA-256 against its actual bytes.
- Storage regressions prove newest-first bounded resource, alert-event, and alert-delivery reads and reject invalid limits.
- The API integration test creates a bundle, downloads identical ZIP bytes, verifies the attachment metadata, opens the manifest, and rejects a path-escape-shaped bundle ID.
- The production console build exposes explicit Create support bundle and Download ZIP actions plus path, archive hash, entry count, byte size, and retention warning.

## Boundary

The exact candidate from source commit `5be7377a49f73e9fa694c031a1a69a7aef247269` was packaged and installed over the prior candidate:

- Installer SHA-256: `FADECA5B65A8B5131BB335C751143DF7C37F03531DB74BC97A3B6DF27734B062`
- Installed `stationd.exe` SHA-256: `6AFDEF04C4E80F662B2EE0F74DABA295F83B9CF84891A79C6FE0EB03F6F9AAE1`
- Installed `channel-worker.exe` SHA-256: `9DE8C6271534BD85E28E768CBD38F6527B8F6A3371E12C73C8F0ED0E095D2990`
- Installed private media-runtime SHA-256: `059251444D1267B486EBA390B18D25FED87E10315E72F757EC6C7E912FA746B5`
- Windows service: `Running` as `LocalSystem`; health and media-factory probes both `ready`.

`scripts/prove-installed-support-bundles.ps1` then exercised the installed HTTP service and proved:

- The service created a managed archive and downloaded byte-identical content. The proof archive SHA-256 was `F629EE8D9DB42EC79D22F8A46DA26CFA5F99B87EA7B6D9EE5B1586AAEF056712`.
- The download returned ZIP attachment metadata, `Cache-Control: no-store`, and `X-Content-Type-Options: nosniff`.
- The archive contained exactly 12 allowlisted entries. All 11 non-manifest entries matched the byte sizes and SHA-256 values recorded inside `manifest.json`.
- A unique plaintext credential canary and its full webhook target were stored through the installed API but were absent from the raw ZIP and every decoded entry.
- Zero station database or sidecar files were present.
- The archive contained actual installed evidence: current runtime truth, 75 bounded resource samples, and 5 self-test records.
- After 11 bundle creations, retention left exactly 10 ZIPs, removed the oldest, retained the newest, and left zero `.tmp` files.

The support-bundle source slice and installed-machine boundary are therefore proved. This does not claim that the rest of S8—especially update discovery, authenticated update staging, maintenance-mode application, post-update proof, and rollback—is complete.
