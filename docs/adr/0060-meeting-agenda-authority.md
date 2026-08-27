# ADR 0060: Meeting-agenda authority

## Decision

`station-agenda` owns a pure, fail-closed contract for one meeting agenda. An agenda
has a stable agenda, channel, and meeting identity; a bounded title/time; contiguous
ordered agenda items; and contiguous globally ordered media chapters. Each chapter
binds an agenda item to bounded offsets within one declared exact-media sync intent:
the same agenda channel, one schedule item/revision, one media SHA-256, declared
duration, and the actor/time that requested synchronization.

The authority supports three provenance forms:

- `staff_authored` records an actor/time assertion;
- `text_import_claim` records the claimed source hash and importer assertion;
- `pdf_import_claim` records the claimed source hash and importer assertion.

The PDF form remains a claim-only contract and is rejected by the current API. The
operator API supports a bounded UTF-8 plain-text import endpoint for creating a new
agenda only: it hashes the exact UTF-8 bytes of the decoded `source_text` value,
creates one public item per nonempty line, and records a server-owned
`TextImportClaim`. Existing agenda source provenance cannot be rebound. This proves
only bounded text handling and source attribution; it
does not claim PDF parsing, schedule/media synchronization, chapter detection, or
publication.

Writes use explicit compare-and-set intent: creation is revision one with no current
revision, and replacement advances exactly one revision from the named current
revision. Agenda, channel, meeting, and provenance identity cannot be rebound.
Schedule/media sync intent may be revised under that CAS contract so a corrected
future schedule can be represented without replacing the meeting agenda identity.

The public projection is deterministic: validated public items and their chapters
retain ordinal order and expose only public title/summary, chapter title, and media
offsets. It excludes staff-only items, staff notes, import provenance, sync intent,
actors, and all execution claims by construction.

## Durable/API/UI boundary

The current slice persists current/history through SQLite with indexed/JSON
cross-checks, append-only history triggers, typed validation/conflict/rebinding
errors, bounded capacity (256 agendas and 128 revisions per agenda), and exact CAS. Setup Admin and Meeting Operator roles may
read or mutate through authenticated routes; actor/time are server-owned. A public
projection endpoint excludes staff-only items, notes, provenance, and sync intent.
The operator UI covers create/edit/read/history, bounded creation-only import, and explicit loading,
empty, error, and conflict states. This does not establish that a meeting exists,
that a media asset exists or matches its hash, that a schedule revision is current,
that an imported line is faithful to an external document, that a chapter occurs at
the declared point, or that the public projection was published. PDF parsing,
schedule/media synchronization execution, installed-machine evidence, and field
evidence remain future work.
