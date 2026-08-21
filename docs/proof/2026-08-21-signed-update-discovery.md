# Signed-update discovery proof — 2026-08-21

## Source evidence

- Release envelopes are capped at 256 KiB and contain exact base64 manifest bytes plus a detached Ed25519 signature and key identifier.
- Verification uses a configured non-weak public key and strict Ed25519 verification before manifest fields become trusted.
- The signed manifest binds product and channel identity, semantic version, source commit, publication/expiry times, a maximum 31-day validity period, minimum updater version, package HTTPS URL, exact byte length, SHA-256, and release notes capped at 64 KiB.
- Unknown fields, signature tampering, unknown keys, expired/future manifests, channel substitution, insecure remote URLs, artifacts over 2 GiB, redirects, invalid semantic versions, and downgrade/equal offers fail closed or remain non-installable.
- Network checks use Windows platform certificate verification, a bounded timeout, zero redirects, an explicit JSON content type, and a response-body ceiling. Loopback HTTP is accepted only to make deterministic machine proof possible.
- SQLite durably stores the newest typed check record and rejects a stale concurrent check overwriting newer truth. A network/trust failure cannot forge an offered release.
- `stationd` owns an automatic hourly due loop. It is inert when no release trust file is installed, and manual checks return a typed conflict instead of pretending to contact a feed.
- A verified newer release creates a rule-owned informational incident. A failed signed check creates a warning incident. Successful checks resolve the check-failure condition, and an up-to-date result resolves the prior available-version condition.
- The loopback API exposes honest status, explicit manual check, and signed release notes for exactly the verified offered version. The Health console shows current versus available version and states that no download or installation is automatic.

## Boundary

This proves signed discovery, durable status, hourly scheduling logic, release-note provenance, and alert routing in source and loopback tests. The current installed candidate does not yet contain this slice. A production release feed/public key is not yet installed, so the deployed state must report `configured: false`. Package download/staging, immutable application slots, the maintenance transaction, post-update proof, and automatic rollback are explicitly not claimed here.
