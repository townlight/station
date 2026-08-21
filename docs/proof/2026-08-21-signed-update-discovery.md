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
- Removing installer-owned release trust resolves the source's firing incidents and clears its check schedule. Status then hides the no-longer-authorized offer, and restoring trust is immediately eligible for a fresh automatic check.
- The loopback API exposes honest status, explicit manual check, and signed release notes for exactly the verified offered version. The Health console shows current versus available version and states that no download or installation is automatic.

## Installed candidate evidence

- Source commit: `bc2aa00b74f39acedc0cd4bf962001367f921b54`
- Installer SHA-256: `278240AD25F882C959A217131224557080928F63F34FC5D2ECCBC5D9F0C72553`
- Installed `stationd.exe` SHA-256: `29826A0C64906005D3F8ECED429A9C5A7808DA3F5C0C14A60B020F9813A05C87`
- Installed `channel-worker.exe` SHA-256: `369BD519550007562B246BCE2B6B4EF07D0E6E97D5F7CEBE89E6975B4EB11FB3`
- The installer completed with exit code 0, preserved station data, installed the exact receipt above, and started the automatic `TownLightStation` Windows service as `LocalSystem` with health and media factories ready.
- With no release trust file installed, the API reported `configured: false`, no latest offer, the exact installed version/commit, and HTTP 409 for a manual check.
- A proof-only Ed25519 key signed an exact `0.2.0` release manifest. A dedicated loopback server delivered that envelope to the installed service; its request log contains `GET /stable.json HTTP/1.1`.
- The installed hourly monitor performed the check with trigger `automatic`, returned `update_available`, preserved the signed offered commit `bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`, and served the exact signed Markdown notes.
- The verified offer raised one firing `info` alert for resource `0.2.0`. Discovery created zero `.tlu` or `TownLight-Station-0.2.0` package/staging files.
- The proof trust file was then removed and the service restarted. It returned to `configured: false` with no exposed stale check, zero firing update alerts, and the offer alert resolved. No proof trust file remains under `C:\Program Files\TownLight Station`.
- The machine run exposed a source-lifecycle defect in the first candidate: removing trust hid an offer but retained its one-hour retry timestamp. The source was corrected to clear source-specific status, the entire workspace gate was rerun, a new installer was built, and only the replacement commit above was accepted as the proven candidate.

## Remaining boundary

This proves signed discovery, durable status, automatic scheduling, release-note provenance, alert routing, trust deactivation, packaging, installation, and installed LocalSystem execution. A production release feed/public key is intentionally not yet installed, so the clean deployed state reports `configured: false`. Package download/staging, immutable application slots, the maintenance transaction, post-update proof, and automatic rollback are explicitly not claimed here.
