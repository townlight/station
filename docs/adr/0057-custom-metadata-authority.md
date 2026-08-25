# ADR 0057: Custom metadata authority

## Decision

`station-metadata` owns a pure, fail-closed contract for one revisioned custom
metadata schema and its per-asset value records. A schema has a stable canonical
identity and revision, with at most 128 uniquely identified, lowercase-keyed
fields. Each field declares whether its value may appear publicly and whether it
may participate in a search facet. The initial field kinds are bounded text,
integer number, Unix-millisecond date/time, boolean, and controlled choice.
Controlled choices also have stable canonical identities and lowercase keys.

Schemas are written with explicit compare-and-set intent: creation is revision one
with no expected revision, and replacement advances exactly one revision from the
current expected revision. An existing field identity cannot be rebound to another
key or type, and an existing controlled-choice identity cannot be rebound to
another key. This narrow initial contract also refuses field/choice removal; later
durable schema history may add explicit retirement semantics rather than silently
changing the interpretation of existing values.

An asset value record has its own stable identity and CAS revision, binds one exact
asset SHA-256 and one exact schema identity/revision, and stores at most one value
per schema field. Validation requires the supplied exact schema revision, rejects
unknown/duplicate fields, enforces field constraints and required values, and
rejects record identity rebinding to another asset or schema. Public and search
facet projections are deterministic: validated selected values are emitted in
ascending field-key order, and controlled choices project their stable key rather
than a mutable label.

## Boundaries and non-claims

This is not SQLite persistence, append-only history, an API, authentication or
authorization, asset catalog integration, text indexing service, public renderer,
or operator UI. It does not assert that an asset exists, that supplied values are
truthful, that a projected facet has been indexed, or that a public flag has been
published. Later slices must persist schema/value revisions through the single
SQLite writer, derive actor/time authority server-side, preserve enough historical
schema data to read old records, integrate with asset lifecycle/search/public
surfaces, and obtain installed-machine and field evidence.
