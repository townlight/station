# ADR 0081: Bounded public catalog discovery projection

## Status

Accepted for the S22 increment.

## Decision

TownLight exposes an unauthenticated, read-only public catalog projection derived from durable station authority. A result requires an exact current Published catalog record, Ready media, current metadata agreeing with its exact immutable history row, and the exact historical schema revision to which those values bind. A historical value projects only while its key and type remain both `public` and `faceted` in that schema's exact current revision; retiring a field removes it from discovery. The response redacts media paths, content hashes, meeting body, schema/record identities and revisions, actors, and history.

Public facet keys are globally unique across current schemas and the complete current registry is capped at 128 fields, matching the response parser. Schema creation or revision rejects collisions and overflow atomically before current or history authority changes, so no valid metadata binding can later create a duplicate or oversized public projection.

Reads use a single SQLite snapshot, an indexed opaque public asset identity and bounded keyset candidate windows of at most 500 examined rows. The service performs the only full identity backfill and exact derived-ID bijection validation once at boot. Immutable mapping triggers and transactional catalog creation preserve that result; each anonymous request checks only the exact trigger definitions, uses a non-omitting left join, and re-derives IDs only for its bounded candidate window. Pages contain at most 100 matches; a sparse window may contain zero matches and a continuation. Cursors bind the last examined public ordering key, canonical filters, and exact projection generation and are authenticated by an independent random HMAC authority retained for the service boot across per-request API reconstruction. Restart invalidates old cursors with a typed response; the key is never persisted or serialized. Authority or query changes invalidate a cursor rather than risking skips or duplicates. Corrupt mappings, triggers, exact current/history rows, schema bindings, or capacity fail the whole read.

`GET /api/v1/public/catalog` is unauthenticated and accepts only the documented bounded query. Operator preview parses and renders that exact response with stale-request guards.

## Boundaries

This is a read-time discovery projection, not a persistent full-text/search index. It does not deliver a public website, playback, OTT publication, billing, actual airtime, installed-machine evidence, or field proof.
