# Native-media workspace gate — 2026-08-24

## Boundary

This is source-machine integration evidence, not installed-candidate, clean-machine, headend,
receiver-acceptance, or field proof. Native tests ran with the pinned development GStreamer `bin`
directory on `PATH` and its plugin directory bound explicitly.

## Defects exposed and corrected

- The station-profile integration suite could exceed sub-second HTTP test deadlines while running
  concurrently. Test clients now retain bounded five-second read deadlines and bounded three-second
  cooperative-stop deadlines.
- Canonical transcode returned before its GStreamer pipeline had proved the `NULL` state and ignored
  failure to remove its temporary output. Production now waits up to five seconds for stopped state
  and fails closed unless the temporary file is removed within a bounded five-second release window.
- A live-audio proof had been relaxed to six audible tail chunks after an FFmpeg discovery boundary,
  which could admit a long silent interval followed by three seconds of tone. It now requires at
  least twelve decoded half-second chunks, permits only the first boundary chunk to be partial, and
  requires every later chunk to be audible while preserving sample-count and overall-energy gates.
- Scheduled `ReturnToSchedule` used the seven-second base command timeout even though it performs a
  media teardown. Production and direct supervisor proof now use the existing bounded 30-second
  media-teardown budget.

## Evidence

- `station-media-assets`: 7 integration tests passed.
- `station-profile-api`: 32 integration tests passed concurrently.
- `channel-worker/process_contract`: 9 native process tests passed concurrently.
- `channel-worker/supervision`: 6 native supervisor/orchestration tests passed concurrently.
- `station-orchestration`: 19 unit and 3 bootstrap-output tests passed.
- `cargo test --workspace`: passed across the complete workspace, including all native media,
  protocol, storage, activation, API, runtime, and doctest lanes. The single ignored test is the
  separately invoked Windows machine-inventory proof lane.
