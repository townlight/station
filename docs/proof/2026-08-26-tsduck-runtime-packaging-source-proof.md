# TSDuck runtime packaging source proof

The packaging lane pins the official TSDuck Windows x64 portable release:

- version: `3.44-4676`
- asset: `TSDuck-Win64-3.44-4676-Portable.zip`
- source: `https://github.com/tsduck/tsduck/releases/download/v3.44-4676/TSDuck-Win64-3.44-4676-Portable.zip`
- archive size: `38,476,542` bytes
- archive SHA-256: `b0ca0f963fcd77488b8c32d6f9d85030daa891ed753c36e57bbb458807a44eb1`

The authority for the asset name and digest is the upstream GitHub release record:
<https://github.com/tsduck/tsduck/releases/tag/v3.44-4676>. The release tag
resolves to upstream commit `7ab046d`; the release page identifies the portable
Windows x64 asset and publishes its SHA-256. GitHub does not publish a separate
TSDuck signature for this asset, so the release-page digest is the bootstrapped
upstream trust source, not an independently signed attestation.

The archive was inspected as an upstream portable payload and its included
`LICENSE.txt` identifies the TSDuck BSD 2-Clause License. No TSDuck executable is
installed on the build host (`tsp`, `tsduck-config` unavailable), and the
archive is intentionally not checked into the repository. The release build
therefore requires this exact file at:

`%LOCALAPPDATA%\Temp\TownLightStation-Toolchain-20260826\TSDuck-Win64-3.44-4676-Portable.zip`

If that artifact is absent or differs by name, size, hash, reparse ancestry, or
bounded safe ZIP layout, the build stops before packaging. The offline fixture
harness `scripts/verify-tsduck-runtime.ps1` exercises safe and traversal ZIP
layouts along with the pin's fail-closed cases. No transport capture or device
reachability claim is made by this proof.
