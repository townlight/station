# Managed CG image overlay proof

Date: 2026-08-21

## Scope

This proof covers immutable PNG/JPEG admission, deterministic snapshot resolution, identity-only
worker handoff, and apply/change/clear on the persistent CG canvas without rebuilding the running
media graph.

## Evidence

- `station-cg` validates lowercase SHA-256 identity, PNG/JPEG format, an 8 MiB byte ceiling,
  4096×4096 dimension ceilings, a 32 MiB decoded-allocation ceiling, complete decode, and APNG
  rejection.
- `station-storage` derives the only permitted filename beneath its canonical CG image root,
  publishes with same-directory flush plus atomic rename, stores no path authority in SQLite, and
  revalidates only board-referenced ready assets through a bounded cache. Missing, corrupt, or
  reparse targets fail to typed readiness states.
- The protected worker bootstrap carries the trusted absolute CG image root. Snapshot image payloads
  carry only asset identity and format.
- The worker independently derives and validates the managed file immediately before apply. Two
  fixed image banks per physical slot permit validation and preparation before the visible bank
  changes.
- The focused real GStreamer UDP test applies a PNG, observes black and colored pixels inside the
  declared region without confusing opaque black with transparency, clears it, reapplies JPEG, and
  proves that missing, wrong-format, and wrong-hash updates retain the prior pixels and revision.
  Required-output byte counters continue increasing across those operations.

The verified commands were:

```text
cargo test -p station-cg --offline
cargo test -p station-storage --offline
cargo test -p station-orchestration --offline
cargo test -p station-media-engine --test persistent_udp --offline -- --test-threads=1
cargo clippy -p station-cg -p station-storage -p station-orchestration -p station-media-engine -p channel-worker --all-targets --all-features --offline -- -D warnings
```

The focused media suite completed 13 tests with zero failures, including
`managed_png_jpeg_image_apply_clear_reapply_and_invalid_retain_pixels`. Storage completed 37 tests
with zero failures, including the bare-database-path regression and three managed-image integration
tests.

## Non-claims

This is a development-machine proof. It does not claim operator upload/selection, installer proof,
24-hour soak, one-station week, three-station week, WPE/HTML rendering, animated images, live video,
or background audio.
