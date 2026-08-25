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

The import forms are deliberately claims, not execution outcomes. They have no
parser name, extracted-byte count, parse-success flag, synchronization status, or
publication status. Accepting one only says an authenticated future boundary may
associate the agenda with a source hash; it never says the source was safely parsed,
that its contents were faithfully extracted, or that its chapters reached media.

Writes use explicit compare-and-set intent: creation is revision one with no current
revision, and replacement advances exactly one revision from the named current
revision. Agenda, channel, meeting, and provenance identity cannot be rebound.
Schedule/media sync intent may be revised under that CAS contract so a corrected
future schedule can be represented without replacing the meeting agenda identity.

The public projection is deterministic: validated public items and their chapters
retain ordinal order and expose only public title/summary, chapter title, and media
offsets. It excludes staff-only items, staff notes, import provenance, sync intent,
actors, and all execution claims by construction.

## Boundaries and non-claims

This slice has no persistence, authentication, PDF/text parser, file access,
schedule/media lookup, worker command, synchronization executor, chapter detector,
public endpoint, renderer, or UI. It does not establish that a meeting exists, that
a media asset exists or matches its hash, that a schedule revision is current, that
an import is safe, that a chapter occurs at the declared point, or that the public
projection was published. Later slices must obtain actor/time authority server-side,
persist append-only history through the single SQLite writer, safely parse bounded
imports, verify/execute schedule/media synchronization, and collect installed-machine
and field evidence.
