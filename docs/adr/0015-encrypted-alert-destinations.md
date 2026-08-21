# ADR 0015: Encrypted operator alert destinations

## Status

Accepted — 2026-08-21

## Context

An unattended station cannot call for help until it knows where to send an operational alert. Email recipients, phone numbers, webhook paths, provider identities, tokens, passwords, and signing secrets are sensitive operating configuration. Returning them from the loopback API or storing them as ordinary SQLite text would expose them to browser code, support exports, database inspection, and accidental logs.

The installed daemon runs as LocalSystem while development and proof runs execute as the signed-in user. A user-scoped secret cannot reliably cross that service boundary after installation.

## Decision

`AlertChannel` is the only public/readable contract. It carries identity, kind, label, enabled state, optional UTC quiet hours, a redacted target, credential-presence state, and timestamps. It cannot carry a target or credential.

The corresponding target, provider endpoint, sender, provider identity, and credential are encoded together and protected with Windows `CryptProtectData` using `CRYPTPROTECT_LOCAL_MACHINE | CRYPTPROTECT_UI_FORBIDDEN`. SQLite receives only the protected BLOB. The daemon reverses that operation only inside the delivery boundary; the API never returns the result. Database, WAL, and shared-memory files therefore contain neither target nor credential plaintext.

Remote webhook and SMS-provider URLs require HTTPS. Plain HTTP is accepted only for `localhost` or `127.0.0.1` machine tests. Email and SMS targets are validated and redacted by kind. Quiet hours must be supplied as a complete UTC start/end pair.

## Consequences

- Destination reads are safe for the operator console and future support bundles.
- The installed LocalSystem service can use credentials configured before or after installation on the same PC.
- Copying only the station database to another computer does not copy usable plaintext credentials.
- Administrators on the same Windows machine remain inside the trust boundary; filesystem ACLs and Windows machine protection work together rather than pretending to defend against the machine owner.
- This ADR proves configuration storage, validation, redaction, and recovery. Durable dispatch is a separate boundary governed by [ADR 0016](0016-durable-operator-alert-delivery.md); send-test and operator delivery-status surfaces remain unclaimed here.
