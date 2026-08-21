# Encrypted alert-destination proof — 2026-08-21

## Scope

This proof covers webhook destination validation, public redaction, Windows machine-protected persistence, database reopen, and secret recovery for the future delivery worker.

## Automated evidence

- A loopback API test writes a webhook target and signing secret, receives only `target_redacted` plus `credential_configured`, restarts the API, and receives the identical public row.
- The same test opens the delivery-only storage boundary after restart and proves Windows can recover the exact target and credential.
- A hostile storage test scans the SQLite database, WAL, and shared-memory files and rejects either target or credential appearing in plaintext.
- A validation test rejects a remote `http://` webhook with HTTP 422 and proves no destination row was created.
- Two injection tests reject control characters in HTTPS provider URLs and SMTP endpoints with HTTP 422 and prove no destination row was created.

## Boundary

This is local machine proof of destination storage, not proof of network delivery or human receipt. Email/SMS/webhook adapters, HMAC request construction, durable delivery attempts, quiet-hours release, retry/dead-letter, resolve notifications, and operator send-test remain unclaimed until their own behavioral and installed-machine evidence passes.
