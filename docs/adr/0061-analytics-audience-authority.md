# ADR 0061: Analytics and audience authority

## Decision

`station-analytics` owns a pure, privacy-minimized authority for audience events,
UTC rollups, snapshots, OTT beacon trust assertions, and year-over-year comparison
inputs. Intake carries only a server event UUID, channel UUID, content SHA-256,
delivery (`live` or `vod`), event kind, UTC millisecond time, and at most eight
allowlisted low-cardinality dimensions. Dimension values are bounded lowercase
tokens; arbitrary keys, email-like strings, IP-shaped strings, free-form user data,
and raw personal identifiers have no field in the contract and fail validation.

An OTT assertion is accepted only when a future trusted intake boundary presents an
enabled source identity at its exact revision. The authority stores no token, cookie,
device identifier, account identifier, IP address, or client fingerprint. A source
assertion establishes only that its future authenticator chose to admit an event; it
is not evidence that a client beacon was received or authentic.

Rollups use explicit half-open UTC windows and require every supplied event to share
one exact channel, content, delivery, event kind, and dimension set. `live` and
`vod` cannot coalesce. A canonical SHA-256 identity covers all aggregate-defining
fields, count, and sorted event identities, so a changed count/window/dimension or
event set cannot borrow another aggregate identity. Snapshots contain bounded,
strictly ordered unique rollups for one exact UTC window and use CAS revision rules. Year-over-year inputs
require equal-duration UTC windows whose start times are 365–366 days apart.

## Boundaries and non-claims

This slice has no browser/client beacon code, network listener, persistent event
ledger, credential verifier, retention policy, dashboard, chart, board report, PDF,
API, UI, or delivery mechanism. It does not establish viewer counts, unique people,
watch duration, audience demographics, beacon receipt, consent, data retention,
rollup persistence, report publication, or year-over-year business conclusions.
Future boundaries must authenticate/authorize OTT sources, enforce privacy and
consent policy, persist append-only trusted inputs through the single SQLite writer,
perform retention/deletion, render reports, and collect installed-machine and field
evidence.
