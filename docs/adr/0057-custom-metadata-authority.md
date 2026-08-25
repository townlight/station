# ADR 0057: Custom metadata authority

## Decision

`station-metadata` owns a pure, fail-closed contract for revisioned custom
metadata schemas and their per-asset value records. A schema has a stable canonical
identity and revision, with at most 128 uniquely identified, lowercase-keyed
fields. Each field declares whether its value may appear publicly and whether it
may participate in a search facet. The initial field kinds are bounded text,
integer number, Unix-millisecond date/time, boolean, and controlled choice.
Controlled choices also have stable canonical identities and lowercase keys.

Schemas are written with explicit compare-and-set intent: creation is revision one
with no expected revision, and replacement advances exactly one revision from the
current expected revision. An existing field identity cannot be rebound to another
key or type, and an existing controlled-choice identity cannot be rebound to
another key. This narrow initial contract also refuses field/choice removal; a later
explicit retirement authority may preserve historical interpretation without
silently deleting fields or choices.

`station-storage` is the sole SQLite writer for this authority. It persists each
current schema and value record alongside separately indexed identity/revision
columns, then atomically appends an immutable, attributed snapshot for every
accepted revision. Schema and value history reads are explicitly limited to 1–128
records; current schema and per-asset value lists fetch one extra row and reject
authority that exceeds their respective 64-schema and 128-value limits. Every
read validates the deserialized record and requires every indexed identity,
schema-revision, and record revision column to agree with it. Malformed JSON or
row/JSON disagreement is corruption and fails closed.

An asset value record has its own stable identity and CAS revision, binds one exact
asset SHA-256 and one exact schema identity/revision, and stores at most one value
per schema field. Validation requires the supplied exact schema revision, rejects
unknown/duplicate fields, enforces field constraints and required values, and
rejects record identity rebinding to another asset or schema. Public and search
facet projections are deterministic: validated selected values are emitted in
ascending field-key order, and controlled choices project their stable key rather
than a mutable label.

## Boundaries and non-claims

`station-api` exposes authenticated, bounded management reads for current schemas,
per-asset records, and both immutable histories. Setup Admin alone can use schema
CAS writes; an enabled Setup Admin or Meeting Operator can use asset-value CAS
writes. The API derives schema and record revisions, schema-revision binding,
actor, and server time itself. Schema and record identity arrive only from a
canonical route path, and request contracts cannot carry audit identity, actor,
server time, or a claimed schema revision. Typed storage errors preserve the
boundary between invalid requests and capacity limits (`422`), stale or conflicting
authority (`409`), missing authority (`404`), and storage corruption or failures
(`500`).

Asset-value writes verify the media asset and current schema exist before
preserving the immutable binding. Storage re-resolves that exact current schema
revision after `BEGIN IMMEDIATE`, in the same transaction as record CAS, so an
API-resolved schema that changes before persistence fails with a revision conflict
and rolls back. An existing metadata record cannot be rebound to another asset or
schema, and a second record cannot claim the same asset/schema binding.

The operator console provides Setup Admin schema creation and revision management,
plus per-asset value editing for enabled Setup Admin and Meeting Operator accounts.
It locks stale writes instead of silently overwriting another operator's revision:
record conflicts reload current asset-record authority automatically, while schema
conflicts require the operator to use **Read current** before retrying. Actor, time,
record revision, and schema-revision binding remain server-owned throughout.

This does not yet integrate metadata with the full asset lifecycle, text/search
indexing, or public projection/rendering. It does not assert that supplied values
are truthful, that a projected facet has been indexed, or that a public flag has
been published. It has not obtained installed-machine or field evidence.
