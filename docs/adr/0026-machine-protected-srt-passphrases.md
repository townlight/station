# ADR 0026: Machine-protected SRT passphrases

## Status

Accepted — 2026-08-21

## Context

An SRT passphrase is an operating credential, not channel metadata. Putting it in the destination URI, ordinary JSON responses, process arguments, SQLite text, journals, logs, or support exports would make routine station inspection a credential-disclosure path. A credential also cannot be changed underneath a live sender without making the operator's on-air truth ambiguous.

The installed daemon and workers run as `LocalSystem`, while setup and development may occur as the signed-in operator. User-scoped encryption cannot reliably span that boundary.

## Decision

- Public output configuration exposes only `encryption_configured`. It never contains the passphrase.
- Channel writes accept replacements only in a top-level write-only `srt_passphrases` map keyed by stable sink identity. An explicit `clear_srt_passphrases` list is required to remove encryption; omission or an older client's default `false` cannot silently downgrade an existing encrypted sink.
- Passphrases are restricted to 10–79 printable ASCII characters without spaces. They are never accepted in an SRT URI.
- SQLite stores a separate encrypted BLOB keyed by channel and sink. Windows DPAPI protects it with `CRYPTPROTECT_LOCAL_MACHINE | CRYPTPROTECT_UI_FORBIDDEN`. Channel and secret changes commit or roll back together; deleting a sink cascades its secret.
- Rotation or removal requires the channel to be stopped. A stop/start write that does not replace the passphrase preserves the existing protected BLOB.
- Orchestration decrypts only while launching an encrypted sink, converts the value into a debug-redacted protocol type, and sends it through the restricted named-pipe bootstrap. Worker arguments remain limited to identities, journal path, and pipe name.
- The worker sets `srtsink` passphrase plus `pbkeylen=32`, producing the AES-256 SRT contract. A mismatched receiver cannot satisfy readiness, so the branch fails closed through the existing incident and bounded-restart path.
- The operator console uses password controls and never receives a configured passphrase back. Support export reads only public channel records and cannot include the secret BLOB.

## Consequences

An encrypted SRT channel survives a service restart and can be started without resubmitting its credential. Copying the database to another machine does not yield plaintext. The public API, raw SQLite/WAL/SHM files, worker command lines, and support bundle remain redaction-safe under the tested boundary.

The passphrase necessarily exists briefly in daemon and worker memory and inside the GStreamer SRT element while the encrypted sender runs. This design does not claim protection from an already elevated administrator or debugger reading process memory. Passphrase rotation is intentionally a visible stop/edit/start maintenance action. Certificate-based identity, external key management, and remote adverse-network interoperability remain separate gates.
