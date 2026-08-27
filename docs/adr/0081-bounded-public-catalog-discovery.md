# ADR 0081: Bounded public catalog discovery projection

## Status

Accepted for the S22 increment.

## Decision

TownLight exposes an unauthenticated, read-only public catalog projection derived from durable station authority. A result requires an exact current Published catalog record, Ready media, current metadata agreeing with its latest immutable history, and the exact historical schema revision to which those values bind. Only fields marked both `public` and `faceted` become typed equality facets. The response redacts media paths, content hashes, meeting body, schema/record identities and revisions, actors, and history.

Public facet keys are globally unique across current schemas and the complete current registry is capped at 128 fields, matching the response parser. Schema creation or revision rejects collisions and overflow atomically before current or history authority changes, so no valid metadata binding can later create a duplicate or oversized public projection.

Reads use a single SQLite snapshot, an indexed opaque public asset identity and bounded keyset candidate windows. Pages contain at most 100 matches; a sparse window may contain zero matches and a continuation. Cursors bind the last examined ordering key, canonical filters, and exact projection generation and are authenticated by an independent random HMAC authority retained for the service boot across per-request API reconstruction. Restart invalidates old cursors with a typed response; the key is never persisted or serialized. Authority or query changes invalidate a cursor rather than risking skips or duplicates. Corrupt mappings, triggers, current/history joins, schema bindings, or capacity fail the whole read.

`GET /api/v1/public/catalog` is unauthenticated and accepts only the documented bounded query. Operator preview parses and renders that exact response with stale-request guards.

## Boundaries

This is a read-time discovery projection, not a persistent full-text/search index. It does not deliver a public website, playback, OTT publication, billing, actual airtime, installed-machine evidence, or field proof.
