# ADR 0074: Pinned TSDuck runtime packaging boundary

## Decision

TownLight packages TSDuck only as an exact local input to the release build. The
input is the official Windows x64 portable asset
`TSDuck-Win64-3.44-4676-Portable.zip` from TSDuck release `v3.44-4676`, with
source URL and archive SHA-256 recorded in the candidate manifest. The release
build does not download, resolve, or upgrade TSDuck. It requires the archive at
the pinned local toolchain-cache path, rejects a reparse point anywhere from the
archive through its volume root, and checks its filename, byte length, SHA-256,
and bounded archive entry layout before packaging.

The installer carries that exact archive as a non-copy temporary payload. It
hash-checks and validates the archive through the same PowerShell 5.1 helper into
a new owned staging directory, then promotes only its validated `TSDuck` root to
`{app}\runtime\tsduck`; archive entries cannot escape, overwrite GStreamer, or
contain a symlink/reparse payload. Before service start, `stationd release
write-runtime-manifest` creates a bounded full-file manifest at
`%ProgramData%\TownLight Station\tsduck-runtime-manifest.json`; its digest and
the pinned archive identity are written into the installation receipt. Runtime
candidate authorization verifies both runtime manifests, complete file sets,
sizes, hashes, and non-reparse boundaries before it accepts the candidate.

The maintenance updater preserves the complete TSDuck receipt identity and
manifest digest unchanged. This pin is therefore immutable across application-slot
updates; changing it requires a later, explicit transactional runtime migration
with rollback evidence rather than an implicit updater side effect.

Pre-ADR installations that contain neither a TSDuck receipt identity nor a
TSDuck runtime remain valid legacy candidates. A partial TSDuck identity, or any
candidate that records this lane but fails its manifest verification, fails closed.

## Boundaries and non-claims

This lane packages and authenticates a future analyzer runtime only. It does not
launch TSDuck, capture a transport stream, probe a tuner or device, establish
device reachability, or prove transport correctness. A later execution adapter
must add an explicit authorized input boundary and evidence contract.

The source archive is not checked into Git. Until the exact local cache artifact
is present, `installer/build-installer.ps1` fails closed with the missing pinned
path and cannot publish a candidate.
