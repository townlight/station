# Installed ten-minute selected-sink proof boundary — 2026-08-25

## Boundary

TownLight Station now binds each output proof capture to the exact final transport branch for one
enabled sink, immediately before that branch's UDP or SRT sink. Two sinks with the same transport
contract therefore have independent capture ingress and may be proved concurrently; shared mux
bytes are no longer mislabeled as evidence for a selected sink.

The operator proof ceiling is 600,000 ms. The physical capture ceiling is 604,000 ms to retain the
existing four-second decode headroom, and the artifact ceiling is 1 GiB. Commissioning passes its
`selected_sink_ten_minute_proof` check only when the newest proof for an enabled current sink is at
least 600,000 ms, has effective outcome `pass`, matches the verified installed candidate and source
commit, matches the exact current output-configuration hash, and remains within the existing
24-hour age bound.

This establishes the implementation, persistence contract, exact-sink topology, installation, and
commissioning decision rule. It does **not** claim that this installed machine has already captured
ten continuous minutes from a live station, nor that a receiver or headend accepted the output.

## Test-first evidence

The protocol RED assertion observed the previous 300,000 ms ceiling. The exact-branch RED test also
showed that a second same-contract sink could not be captured independently under the shared mux
tap.

After moving capture to each final output branch, the real GStreamer integration test
`post_mux_capture_is_bounded_decodable_and_does_not_interrupt_playout` passed. It simultaneously
armed two same-contract UDP sinks, required two distinct branch-bound capture results, and verified
that both outputs continued advancing. The commissioning test proves that a current 300,000 ms
proof remains blocked and a current 600,000 ms proof passes with the exact proof identity in the
report evidence.

The engine integration duration is intentionally 12 seconds so the deterministic suite remains
practical. It proves independent final-branch capture and uninterrupted playout, not ten minutes of
machine endurance.

## Source verification

- Complete `station-storage`: 78 passed, 0 failed.
- Complete deterministic `station-api`, serialized to avoid a Windows temporary-file lock race:
  80 passed, 0 failed, with two explicit machine lanes ignored.
- The one test affected by the parallel file-lock race passed alone before the full serialized run.
- `station-orchestration`: 22 passed, 0 failed across its library and bootstrap-output suites.
- Readiness suites: 40 passed, 0 failed across library, headend, loudness, and profile tests.
- Operator console: 25 contract tests passed; the production Vite build passed.
- Warning-denied Clippy passed for all workspace targets and features.
- `cargo fmt --all -- --check` and `git diff --check` passed.
- Installer contract: 75 requirements passed.

## Exact installed candidate

Clean source commit `a539328d252ed4f7c697cbc36bd35f517fadea77` produced
`TownLight-Station-0.1.0-x64-setup.exe` with SHA-256
`9383d9767d1138d1d89edbf5d72ba36b1b1d58ee2606fda1f0c7153e3c8c94b8`.

The prior product and both services were uninstalled while preserving the existing station
database. Before reinstall, that database was 1,241,088 bytes with SHA-256
`092bedf1c3d7123dd1d2588ae3222c0cd473082d856d2888cab6efb419d6081c`.
The exact new installer completed successfully against that preserved state.

`installer/verify-installed.ps1` exited zero and proved:

- `TownLightStation` and `TownLightStationUpdater` are running as LocalSystem;
- health is `ready`;
- the installed receipt names source commit `a539328d252ed4f7c697cbc36bd35f517fadea77`;
- installed `stationd`, `channel-worker`, and `station-updater` hashes match the candidate manifest;
- the private runtime manifest matches and all required media factories are ready.

A read-only query against the live preserved database proves the migration rebuilt both
`channel_output_loudness_proofs` and `output_loudness_jobs` with a 600,000 ms duration ceiling and
a 1,073,741,824-byte artifact ceiling. The service opened that migrated database and reports its
database and orchestration health as ready.

Authentication remains correctly closed at `operator_bootstrap_required`. No credentials were
invented and no operator or station data was changed for this verification.

## Remaining proof

The next machine/operator evidence for this slice is one authenticated, enabled output sink on the
installed candidate completing a continuous 600,000 ms capture, analysis, persisted proof, and
commissioning-report pass while playout remains advancing. Receiver/headend acceptance and the
one-station and three-station field gates remain separate later evidence.
