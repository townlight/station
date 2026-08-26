# ADR 0072: Pinned TSDuck runtime packaging boundary

## Decision

TownLight packages TSDuck only as an exact local input to the release build. The
input is the official Windows x64 portable asset
`TSDuck-Win64-3.44-4676-Portable.zip` from TSDuck release `v3.44-4676`, with
source URL and archive SHA-256 recorded in the candidate manifest. The release
build does not download, resolve, or upgrade TSDuck. It requires the archive at
the pinned local toolchain-cache path, checks its filename, byte length, and
SHA-256, and rejects missing, substituted, or reparse-point inputs.

The installer carries that exact archive as a non-copy temporary payload,
extracts it with Windows PowerShell into `{app}\runtime\tsduck`, and checks the
required analyzer files (`tsp.exe`, `tsanalyze.exe`, `tsduck.dll`, `tscore.dll`)
plus the upstream `LICENSE.txt` and `OTHERS.txt`. Before service start,
`stationd release write-runtime-manifest` creates a bounded full-file manifest at
`%ProgramData%\TownLight Station\tsduck-runtime-manifest.json`; its digest and
the pinned archive identity are written into the installation receipt. Installed
verification checks both identities and the exact file set, sizes, hashes, and
non-reparse boundary.

## Boundaries and non-claims

This lane packages and authenticates a future analyzer runtime only. It does not
launch TSDuck, capture a transport stream, probe a tuner or device, establish
device reachability, or prove transport correctness. A later execution adapter
must add an explicit authorized input boundary and evidence contract.

The source archive is not checked into Git. Until the exact local cache artifact
is present, `installer/build-installer.ps1` fails closed with the missing pinned
path and cannot publish a candidate.
