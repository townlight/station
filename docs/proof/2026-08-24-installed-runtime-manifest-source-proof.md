# Installed-runtime manifest source proof — 2026-08-24

## Boundary

This is source-machine and installer-compilation evidence for the current uncommitted slice. It is
not an installed candidate, clean-machine, field, or release proof. The generated proof installer
was not executed because a dirty build artifact cannot establish immutable candidate identity.

## Rust gates

- `stationd`: 20 tests passed, including strict installer-only command parsing.
- `station-maintenance`: 4 unit, 11 activation, and 12 candidate-identity tests passed.
- `station-readiness`: 5 unit, 11 headend, 8 loudness, and 13 profile tests passed.
- `station-api`: 35 tests passed and the explicit Windows inventory lane remained ignored here.
- Strict Clippy passed for all targets in those four packages.
- The complete `cargo test --workspace` gate passed after independently exposed native-media
  teardown and proof-deadline defects were corrected. The only ignored lane is the explicitly
  invoked Windows machine-inventory proof.

The hostile runtime cases cover modified, missing, extra, malformed, duplicate, empty, unbounded,
and receipt-digest-mismatched evidence. Signed application activation preserves the exact runtime
manifest digest.

## Real private-runtime proof

The installer-only `stationd release write-runtime-manifest` command ran against the currently
installed private GStreamer tree with its private DLL search path explicitly bound. With no compiler
time, it completed in 7.84 seconds and produced:

- 829 exact file entries;
- 356,365,891 total bytes;
- a 116,083-byte canonical JSON manifest;
- two legitimate zero-byte files represented with the SHA-256 of empty bytes;
- canonical paths from `bin/FLAC-8.dll` through `unins000.exe`.

The temporary manifest was removed after inspection. This proves bounded enumeration and hashing on
the source machine; it does not authenticate the historically installed runtime because that older
installation predates this receipt-bound manifest contract.

## Installer compilation

`installer/build-installer.ps1 -AllowDirty` passed the 58-requirement static installer contract,
built all three optimized binaries and the operator bundle, and compiled the complete Inno Setup
script successfully with Inno Setup 6.7.3. The resulting dirty proof artifact had SHA-256
`bc2af4211c5dd73752e21f03f38e3b7da98a11180ffcca224d180485aa0d8ee09`. That digest records only
the compilation artifact; it is not a releasable candidate claim.
