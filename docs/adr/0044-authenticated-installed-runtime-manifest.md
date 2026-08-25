# ADR 0044: Receipt-bound installed-runtime manifest

Date: 2026-08-24

Status: Accepted

## Context

The pinned GStreamer installer package has an approved SHA-256, but that digest identifies the
installer input rather than the files it produced. Presence checks and live factory discovery prove
availability, not installed-byte integrity. A prior runtime that merely contains required filenames
must not be blessed during an upgrade.

## Decision

The Windows installer stops both services, removes the prior private runtime, and freshly installs
the pinned package. Before either service starts, the candidate `stationd` runs an installer-only
command that recursively enumerates every regular file under the private runtime root, rejects
reparse points and non-file entries, and writes a canonical manifest containing product, candidate,
runtime-package, relative-path, size, and SHA-256 identity.

The contract is bounded to 4,096 files, 512 MiB per file, 1 GiB total, and a 512 KiB manifest.
Canonical forward-slash relative paths reject absolute paths, traversal, empty components, drive
syntax, and case-insensitive duplicates. Legitimate zero-byte regular files are represented by their
size and the SHA-256 of empty bytes.

The installer hashes the exact manifest bytes and writes that lowercase digest into the protected
installation receipt. Candidate verification requires current application identity to agree between
installation and active-slot receipts, requires runtime product/package identity plus the complete
manifest digest to agree with the installation receipt, re-enumerates the exact runtime file set,
and checks every size and hash. The manifest's version and source commit record which installer
candidate produced that runtime; later signed application-slot updates preserve the approved
runtime-package and installed-manifest digests rather than pretending they reinstalled it.

## Consequences

Release readiness can pass only for the exact recorded application and private-runtime bytes.
Missing, extra, modified, unsafe, malformed, or unbounded runtime evidence fails closed. Runtime
installation does more work during upgrades and runtime verification reads the complete bounded
tree. This is local installer/service authority under the station's protected administrator and
LocalSystem boundary; it is not a claim that local administrators are cryptographically unable to
replace the product.
