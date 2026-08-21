# Operator CG image library proof

Date: 2026-08-21

## Scope

This proof covers the local API and production operator-console path for adding a static CG image
to the managed library and selecting its server-issued identity for a zone.

## Evidence

- `POST /api/v1/cg/images` accepts only a raw request body and passes it to the bounded immutable
  storage admission path. No caller path or identity is accepted.
- `GET /api/v1/cg/images` checks current filesystem metadata and returns readiness through the
  bounded verification cache, so a deleted file is not offered as ready merely because SQLite once
  admitted it. Full hash/decode verification repeats on metadata change or within 60 seconds.
- The HTTP reader permits up to 8 MiB only for the exact image-upload route. A real loopback TCP
  parser test sends a body larger than 64 KiB, proves the upload route reaches dispatch, and proves
  an unrelated route rejects the declared body at the parser with HTTP 413.
- API integration tests prove valid PNG upload, duplicate idempotence, listing, malformed rejection,
  missing-body rejection, the 8 MiB ceiling, audited image-zone binding, database reopen, and a
  resolved image in the cache-only preview snapshot.
- The operator production build provides an accessible PNG/JPEG file input, performs immediate MIME
  and byte-size checks, refreshes the managed library, selects the returned digest, displays format,
  dimensions, size, and readiness, disables unusable entries, and preserves the typed image-zone
  write contract.

The verified commands were:

```text
cargo test -p station-api -p station-storage --offline
npm run build --prefix web/operator
cargo clippy -p station-api -p station-storage --all-targets --all-features --offline -- -D warnings
```

The combined API/storage run completed 69 tests with zero failures. The TypeScript and Vite
production build completed successfully.

## Non-claims

This is a development-machine API and production-build proof. It does not yet prove an installed
service/browser upload, decoded on-air pixels from an operator-uploaded file, long-duration
operation, or the one-station and three-station field gates.
