# TSDuck runtime packaging source proof

The packaging lane pins the official TSDuck Windows x64 portable release:

- version: `3.44-4676`
- asset: `TSDuck-Win64-3.44-4676-Portable.zip`
- source: `https://github.com/tsduck/tsduck/releases/download/v3.44-4676/TSDuck-Win64-3.44-4676-Portable.zip`
- archive size: `38,476,542` bytes
- archive SHA-256: `b0ca0f963fcd77488b8c32d6f9d85030daa891ed753c36e57bbb458807a44eb1`

The archive was inspected as an upstream portable payload and its included
`LICENSE.txt` identifies the TSDuck BSD 2-Clause License. No TSDuck executable is
installed on the build host (`tsp`, `tsduck-config` unavailable), and the
archive is intentionally not checked into the repository. The release build
therefore requires this exact file at:

`%LOCALAPPDATA%\Temp\TownLightStation-Toolchain-20260826\TSDuck-Win64-3.44-4676-Portable.zip`

If that artifact is absent or differs by name, size, hash, or reparse status,
the build stops before packaging. The offline fixture harness
`scripts/verify-tsduck-runtime.ps1` covers the pin and fail-closed cases. No
transport capture or device reachability claim is made by this proof.
