# Fallback slate source proof — 2026-08-25

This source slice proves the fallback-slate authority and worker boundary. The exact binaries from
this source were subsequently installed and verified; field evidence remains separate.

- Storage tests prove stopped-channel enforcement, current file verification, compare-and-swap
  conflict behavior, revision persistence across reopen, and one append-only audit event per
  successful mutation.
- Protocol tests prove a major-versioned, path-free SHA-256/format bootstrap contract and rejection
  of noncanonical identities.
- API tests prove Setup Admin enforcement, server actor attribution, durable history, and direct
  commissioning consumption.
- A native Windows process test admits a real managed PNG, launches the real channel worker, and
  receives `Ready` plus UDP transport only after the exact slate is rehashed, decoded, fitted, and
  installed in the fallback branch. A second native process test proves a missing selected image
  exits before `Ready`. An independent receiver decodes the emitted MPEG-TS and proves the selected
  green slate pixels are visible at the aspect-fitted center while the uncovered frame stays black.
- Operator tests and the production Vite build cover stopped-channel selection, upload, CAS save,
  current readiness, actor/time, and audit-count presentation.

The exact installer and installed-service results are recorded in
[the installed fallback-slate candidate proof](2026-08-25-installed-fallback-slate.md). The decoded
pixel capture above is a source-built native worker proof, not an authenticated installed-operator
selection. Clean-machine and field exercises remain pending and must not be inferred from either
record.
