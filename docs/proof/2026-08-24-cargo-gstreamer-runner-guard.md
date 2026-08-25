# Windows Cargo GStreamer runner guard — 2026-08-24

## Boundary

This is a source-workspace execution-safety contract, not installed-candidate or field evidence. It
prevents Windows Cargo test/run executables and test-spawned descendants from reaching the loader
with an ambiguous or missing GStreamer runtime. It does not replace the install receipt, installed
runtime manifest, service environment, or clean-machine gates.

## Failure reproduced

The contract test was added before the runner and failed with `Cargo GStreamer runner configuration
is missing.` A direct Cargo integration-test launch without the explicit runtime had previously
created modal Windows loader errors for hashed `target\debug\deps` test executables and their
`channel-worker.exe` child.

## Correction

- `scripts/invoke-cargo.ps1` binds the Cargo process itself, so every descendant inherits the exact
  runtime even when a test launches another built executable directly.
- `.cargo/config.toml` additionally binds Windows MSVC execution to the checked-in PowerShell runner.
- The runner accepts only `TOWNLIGHT_GSTREAMER_ROOT` or the pinned development-install location,
  verifies the loader DLL and plugin directory before launch, prepends the exact runtime `bin`, and
  replaces both GStreamer plugin variables.
- A missing explicit override fails in the terminal before the executable is launched. Ambient
  `PATH` is never accepted as runtime identity.
- Cargo must be invoked through the wrapper from the workspace root; direct Cargo invocation and
  direct execution of generated `target` artifacts are outside the engineering contract.

## Evidence

- `scripts/verify-cargo-gstreamer-runner.ps1`: passed wrapper and runner valid-runtime propagation
  plus missing-runtime rejection cases.
- `scripts/invoke-cargo.ps1 test --offline -p channel-worker --test process_contract live_audio_gate_
  -- --test-threads=1`, with no caller-supplied GStreamer environment, passed both selected tests
  without a Windows loader dialog.
- A guarded replay of the complete `channel-worker` and `station-api` packages passed, including the
  exact hashed `supervision` and `station_profile_api` executables previously seen in loader-error
  dialogs. Repeated desktop window inventories during and after execution found no loader dialog.
- `scripts/invoke-cargo.ps1 clippy --offline --workspace --all-targets --all-features -- -D warnings`
  passed.
- A later full-workspace guarded replay again remained free of loader dialogs. Under the peak
  concurrent native-media load it exposed a separate one-second socket deadline in the
  `Expect: 100-continue` test. That test now uses the same bounded five-second read deadline as the
  adjacent HTTP contracts and passed ten consecutive isolated runs while the installed services
  remained active.
- The same replay also exposed a transient Windows file-sharing lock during generated-media cleanup.
  API integration cleanup now uses the repository's bounded two-second retry contract. The complete
  `station-api` package then passed with the installed services active.
- After those harness corrections, the complete `station-api` package and every remaining workspace
  package passed under the wrapper. Together with the guarded complete `channel-worker` replay, this
  covers the full workspace. Desktop inventories remained free of loader dialogs throughout and at
  completion.
