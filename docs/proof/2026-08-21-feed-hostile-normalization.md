# Hostile feed normalization proof — 2026-08-21

## Command

```text
cargo test --offline -p station-feed-normalization
```

## Result

Passed: 19 unit tests, 0 failures. The suite covers malformed XML/iCalendar boundaries, DTD and
unknown-entity rejection, input/item/field bounds, unsafe URLs, stable namespaced identities,
entity and href handling, tags, timestamps, folding/escaping, and unsupported calendar timezone
behavior.

## Boundary

This is offline parser/normalizer evidence only. It does not prove live provider connectivity,
credential behavior, DNS/TLS policy, redirect refusal, or installed feed scheduling.
