# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Trust artifacts for the native Windows runtime closure (`spec-packaging-closure` D5).

Turns a described packaged tree into the three artifacts the installer ships
and the operator's machine trusts: `runtime-manifest.json` (the SHARED
CONTRACT schema), `SHA256SUMS`, and `LICENSE-BOM.md`. `verify_manifest` is the
D5 install-time cross-check: every manifest entry must exist on disk with a
matching hash, and every on-disk file must appear in the manifest -- no
orphans in either direction.

Everything above the "Filesystem helper" section is deliberately pure: no
filesystem walking, no PE parsing. Callers (the build script, the installer's
post-install verifier) supply an iterable of already-described `FileEntry`
values; the tests supply them directly. The one real filesystem crawl lives
in `hash_directory_tree` at the bottom of the module, clearly separated so
the pure logic stays provable without a staged tree.

AC7 is a hard gate here, not a warning: `build_runtime_manifest` raises
`UnknownLicenseError` the moment any entry's distribution has no known
license, or the entry's license string is empty. Nothing ships with an
unknown-license file count above zero.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from civiccast.native.runtime_licenses import classify_shipped_file

__all__ = [
    "DISTRIBUTION_LICENSE",
    "DuplicatePathError",
    "FileEntry",
    "ManifestMismatchError",
    "UnknownLicenseError",
    "build_runtime_manifest",
    "hash_directory_tree",
    "render_license_bom",
    "render_sha256sums",
    "verify_manifest",
]


# ---------------------------------------------------------------------------
# License policy
# ---------------------------------------------------------------------------

#: Upstream pip distribution -> the SPDX license identifier that governs
#: every file it contributes to the packaged tree. Taken from each
#: distribution's own `dist-info/METADATA` `License:` field: five GStreamer
#: distributions (`gstreamer_libs`, `gstreamer_plugins`,
#: `gstreamer_plugins_libs`, `gstreamer_plugins_restricted`,
#: `gstreamer_python`) declare LGPL-2.1-or-later; `gstreamer_ext_runtime`
#: declares `LicenseRef-Proprietary`.
#:
#: Licensing evidence for the bundled FFmpeg specifically (D3): the shipped
#: `gstlibav.dll` -- which ships from `gstreamer_plugins`, NOT
#: `gstreamer_ext_runtime` -- links an FFmpeg build whose own
#: `avcodec_license()` and `avutil_license()` both return "LGPL version 2.1
#: or later", and whose recorded build configuration has
#: `nonfree=disabled`, `version3=disabled`, with no `libx264`/`libx265`
#: linked -- i.e. the measured build carries no GPL-only component, so the
#: LGPL-2.1-or-later label for `gstreamer_plugins` is evidence-backed, not
#: an assumption from `gst-inspect` metadata alone (spec D3: "gst-inspect
#: license metadata is an INPUT, not the authority").
#:
#: `gstreamer_ext_runtime` measured separately: it contributes ONLY
#: Microsoft VC++ Redistributable files (`msvcp140.dll`, `vcruntime140.dll`,
#: `vcruntime140_1.dll`, ...); its own `dist-info/METADATA` declares
#: `License: LicenseRef-Proprietary`, and the per-FILE classifier in
#: `runtime_licenses.SUPPORT_LIBRARY_LICENSE` independently confirms the
#: three shipped files as `LicenseRef-Microsoft-VCRedist` (Microsoft's
#: VC++ Redistributable EULA) -- neither open source nor GPL. The per-file
#: classifier outranks this coarse map (see `_check_licenses_known`), so
#: that specific value, not this entry, is what actually ships in
#: `runtime-manifest.json` for those three files; this entry is only the
#: fallback for any future file from this distribution the per-file
#: investigation has not yet examined.
#:
#: A distribution absent from this mapping is an UNKNOWN license: AC7 forbids
#: guessing, so `build_runtime_manifest` halts rather than defaulting one in.
DISTRIBUTION_LICENSE: dict[str, str] = {
    "gstreamer_libs": "LGPL-2.1-or-later",
    "gstreamer_plugins": "LGPL-2.1-or-later",
    "gstreamer_plugins_libs": "LGPL-2.1-or-later",
    "gstreamer_plugins_restricted": "LGPL-2.1-or-later",
    "gstreamer_python": "LGPL-2.1-or-later",
    "gstreamer_ext_runtime": "LicenseRef-Proprietary",
    "gstreamer_cli": "LGPL-2.1-or-later",
    # Not an upstream wheel: this is the CivicCast-authored explanatory text
    # `scripts/build_native_runtime_closure.py` writes to
    # `<out>/licenses/README.md` (audit finding fix -- see that module's
    # `write_license_notices`/`LICENSE_NOTICES_DISTRIBUTION`). Governed by
    # this repository's own license, matching every source file's SPDX
    # header, not an upstream distribution's declared license.
    "civiccast_license_notices": "Apache-2.0",
}

#: The `runtime-manifest.json` schema version this module writes and reads.
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnknownLicenseError(RuntimeError):
    """A file entry's distribution has no known license (AC7 hard gate).

    Raised instead of warned: an unknown-license file shipped by accident is
    exactly the provenance gap D3/AC7 exist to prevent, so the build refuses
    rather than producing a manifest with a silent gap in it.
    """


class ManifestMismatchError(RuntimeError):
    """D5's two-directional verify failed.

    The message separates MISSING (in the manifest, absent on disk), ORPHAN
    (on disk, absent from the manifest), and HASH MISMATCH (present in both,
    disagreeing digests) into distinct labelled sections so an operator
    reading the error knows which of the three happened without re-deriving
    it from a diff.
    """


class DuplicatePathError(RuntimeError):
    """The same path appears more than once in a manifest or entry collection.

    A manifest that names one path twice is corrupt by definition -- it is
    claiming two different truths (e.g. two different hashes) about a single
    shipped file. This is an install-time trust boundary: naively collapsing
    duplicates into a dict (keeping whichever record happens to come last)
    would let a corrupt-or-attacker-supplied manifest verify cleanly as long
    as the on-disk file matches *one* of the claimed hashes. Both
    `build_runtime_manifest` (refuse to emit) and `verify_manifest` (refuse
    to trust) raise this instead of silently picking a winner.
    """


# ---------------------------------------------------------------------------
# FileEntry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileEntry:
    """One file in the packaged tree, already described -- no I/O here.

    ``path`` is forward-slash, relative to the tree root (the SHARED
    CONTRACT convention). ``sha256`` is lowercase hex. ``distribution`` is
    the upstream pip distribution (or other named source) that contributed
    the file; ``license`` is that distribution's SPDX identifier, normally
    looked up from `DISTRIBUTION_LICENSE` by the caller that builds the
    entry (see `hash_directory_tree`), but callers may pass any string
    (tests exercise the AC7 gate this way).
    """

    path: str
    sha256: str
    bytes: int
    distribution: str
    license: str


# ---------------------------------------------------------------------------
# build_runtime_manifest
# ---------------------------------------------------------------------------


def _check_licenses_known(entries: Iterable[FileEntry]) -> None:
    """AC7 gate: raise naming every entry with an unknown, empty, or WRONG license.

    "Known distribution + non-empty license" is not enough: the license must
    equal the policy license `DISTRIBUTION_LICENSE` maps for that
    distribution, or the entry is mis-licensed (e.g. an MIT claim for a
    distribution the policy says is LGPL-2.1-or-later) and ships with a false
    provenance record, which is exactly what AC7 exists to prevent.

    The per-FILE classifier outranks the per-distribution map wherever it has
    an answer. Five of the six shipped distributions declare aggregate license
    expressions covering many licenses, so the per-distribution map cannot be
    right for every file in them by construction -- it is retained only as a
    coarse fallback for entries whose path the investigation never examined
    (hand-constructed entries in tests; `hash_directory_tree`, the only
    producer of real entries, refuses an unclassifiable path outright).
    """
    violations: list[str] = []
    for entry in entries:
        per_file = classify_shipped_file(entry.path)
        if not entry.license:
            violations.append(
                f"{entry.path} (distribution {entry.distribution!r}) has an empty license"
            )
        elif per_file is not None and entry.license != per_file:
            violations.append(
                f"{entry.path} declares license {entry.license!r} but its confirmed "
                f"per-file provenance is {per_file!r}"
            )
        elif per_file is not None:
            continue  # per-file evidence agrees; the coarse map has no say
        elif entry.distribution not in DISTRIBUTION_LICENSE:
            violations.append(
                f"{entry.path} comes from unmapped distribution {entry.distribution!r} "
                "(no entry in DISTRIBUTION_LICENSE)"
            )
        elif entry.license != DISTRIBUTION_LICENSE[entry.distribution]:
            violations.append(
                f"{entry.path} declares license {entry.license!r} but distribution "
                f"{entry.distribution!r} policy requires "
                f"{DISTRIBUTION_LICENSE[entry.distribution]!r}"
            )
    if violations:
        raise UnknownLicenseError(
            "Refusing to build runtime-manifest.json -- AC7 requires the "
            "unknown-license file count to be ZERO:\n  " + "\n  ".join(sorted(violations))
        )


def _check_no_duplicate_paths(entries: Iterable[FileEntry]) -> None:
    """Finding 1: refuse to build a manifest that names one path twice."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for entry in entries:
        if entry.path in seen:
            duplicates.add(entry.path)
        seen.add(entry.path)
    if duplicates:
        raise DuplicatePathError(
            "Refusing to build runtime-manifest.json -- duplicate path(s) "
            "(a corrupt manifest claims more than one truth about the same "
            "shipped file):\n  " + "\n  ".join(sorted(duplicates))
        )


def build_runtime_manifest(
    entries: Iterable[FileEntry],
    *,
    gstreamer_version: str,
    lock_sha256: str,
) -> dict[str, Any]:
    """Build the `runtime-manifest.json` document for ``entries``.

    Deterministic (AC1): ``files`` is always sorted by path, independent of
    the input order, so two clean-environment runs over the same inputs
    produce byte-identical JSON once serialized with sorted keys.

    Raises `UnknownLicenseError` (AC7) before returning anything if any
    entry's license is unknown, empty, or does not match the distribution's
    policy license -- never ships a manifest with a provenance gap. Raises
    `DuplicatePathError` (Finding 1) if any path appears more than once --
    never ships a manifest that claims two different truths about one file.
    """
    materialized = list(entries)
    _check_no_duplicate_paths(materialized)
    _check_licenses_known(materialized)

    ordered = sorted(materialized, key=lambda e: e.path)
    files = [
        {
            "path": entry.path,
            "sha256": entry.sha256,
            "bytes": entry.bytes,
            "distribution": entry.distribution,
            "license": entry.license,
        }
        for entry in ordered
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "gstreamer_version": gstreamer_version,
        "lock_sha256": lock_sha256,
        "file_count": len(files),
        "total_bytes": sum(entry.bytes for entry in ordered),
        "files": files,
    }


# ---------------------------------------------------------------------------
# render_sha256sums
# ---------------------------------------------------------------------------


def render_sha256sums(entries: Iterable[FileEntry]) -> str:
    """Render the standard "<hex>  <path>" SHA256SUMS text, sorted by path.

    Byte-identical for the same entries regardless of input order (AC1).
    LF line endings only, trailing newline on the last line, matching the
    conventional `sha256sum` output format so `sha256sum -c SHA256SUMS`
    verifies the tree without any CivicCast-specific tooling.
    """
    ordered = sorted(entries, key=lambda e: e.path)
    lines = [f"{entry.sha256}  {entry.path}\n" for entry in ordered]
    return "".join(lines)


# ---------------------------------------------------------------------------
# render_license_bom
# ---------------------------------------------------------------------------


def render_license_bom(entries: Iterable[FileEntry]) -> str:
    """Render `LICENSE-BOM.md`: a per-distribution summary, then per-file provenance.

    Spec D3 requires per-FILE provenance, not per-package hand-waving, so the
    full file table is mandatory here in addition to the summary -- the
    summary alone would let a mis-licensed individual file hide behind a
    correct-looking distribution rollup.
    """
    ordered = sorted(entries, key=lambda e: e.path)

    by_distribution: dict[str, list[FileEntry]] = {}
    for entry in ordered:
        by_distribution.setdefault(entry.distribution, []).append(entry)

    lines: list[str] = ["# License Bill of Materials", ""]

    lines.append("## Summary by distribution")
    lines.append("")
    lines.append(
        "A distribution is a delivery vehicle, not a licence: every gstreamer wheel "
        "here vendors upstream projects under a mixture of licences. So this column "
        "lists EVERY distinct licence found in the distribution's files, sorted, and "
        "the per-file table below remains the authority for any individual file."
    )
    lines.append("")
    lines.append("| Distribution | Licenses | File count | Total bytes |")
    lines.append("| --- | --- | --- | --- |")
    for distribution in sorted(by_distribution):
        group = by_distribution[distribution]
        licenses = ", ".join(sorted({entry.license for entry in group}))
        lines.append(
            f"| {distribution} | {licenses} | {len(group)} | {sum(e.bytes for e in group)} |"
        )
    lines.append("")

    lines.append("## Per-file provenance")
    lines.append("")
    lines.append("| Path | Distribution | License | SHA256 |")
    lines.append("| --- | --- | --- | --- |")
    for entry in ordered:
        lines.append(f"| {entry.path} | {entry.distribution} | {entry.license} | {entry.sha256} |")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# verify_manifest
# ---------------------------------------------------------------------------


def verify_manifest(manifest: dict[str, Any], entries: Iterable[FileEntry]) -> None:
    """D5's two-directional cross-check: manifest <-> on-disk entries agree.

    ``manifest`` is a document as returned by `build_runtime_manifest` (or
    parsed from `runtime-manifest.json`). ``entries`` describes what is
    actually on disk (normally from `hash_directory_tree`).

    Raises `ManifestMismatchError` naming, in three separate labelled
    sections, every MISSING entry (manifest says it should exist, it
    doesn't), every ORPHAN file (exists on disk, the manifest never
    mentioned it), and every HASH MISMATCH (present in both, digests
    disagree) -- never a single undifferentiated diff.

    Raises `DuplicatePathError` (Finding 1) first, before any of the above,
    if the manifest names the same path more than once -- a naive
    dict-comprehension collapse (`{record["path"]: record for record in
    manifest["files"]}`) would silently keep whichever record came last and
    let a manifest claiming two different hashes for one file verify
    cleanly whenever the on-disk file happens to match either claimed hash.
    This is an install-time trust boundary: failing open here means a
    corrupt-or-attacker-supplied manifest gets a free pass.
    """
    manifest_paths_seen: dict[str, list[dict[str, Any]]] = {}
    for record in manifest["files"]:
        manifest_paths_seen.setdefault(record["path"], []).append(record)
    manifest_duplicates = sorted(
        path for path, records in manifest_paths_seen.items() if len(records) > 1
    )
    if manifest_duplicates:
        raise DuplicatePathError(
            "Refusing to trust the packaged tree -- runtime-manifest.json "
            "names the same path more than once (a corrupt manifest claims "
            "more than one truth about the same shipped file):\n  "
            + "\n  ".join(manifest_duplicates)
        )

    manifest_by_path: dict[str, dict[str, Any]] = {
        path: records[0] for path, records in manifest_paths_seen.items()
    }
    disk_by_path: dict[str, FileEntry] = {entry.path: entry for entry in entries}

    missing = sorted(set(manifest_by_path) - set(disk_by_path))
    orphans = sorted(set(disk_by_path) - set(manifest_by_path))
    mismatched = sorted(
        path
        for path in set(manifest_by_path) & set(disk_by_path)
        if manifest_by_path[path]["sha256"] != disk_by_path[path].sha256
    )

    if not (missing or orphans or mismatched):
        return

    sections: list[str] = []
    if missing:
        sections.append("MISSING (in manifest, absent on disk):\n  " + "\n  ".join(missing))
    if orphans:
        sections.append("ORPHAN (on disk, absent from manifest):\n  " + "\n  ".join(orphans))
    if mismatched:
        detail = "\n  ".join(
            f"{path} (manifest {manifest_by_path[path]['sha256']} != disk {disk_by_path[path].sha256})"
            for path in mismatched
        )
        sections.append("HASH MISMATCH (present in both, digest disagrees):\n  " + detail)

    raise ManifestMismatchError(
        "Refusing to trust the packaged tree -- runtime-manifest.json does not "
        "match the on-disk files:\n\n" + "\n\n".join(sections)
    )


# ---------------------------------------------------------------------------
# Filesystem helper (the one real I/O in this module)
# ---------------------------------------------------------------------------


def hash_directory_tree(root: Path, *, distribution_of: dict[str, str]) -> tuple[FileEntry, ...]:
    """Walk a real directory and describe every file as a `FileEntry`.

    The only filesystem-touching function in this module -- kept separate
    and thin so the pure functions above stay provable without a staged
    tree. ``distribution_of`` maps a forward-slash path relative to
    ``root`` to the distribution that contributed it; a path absent from
    that mapping raises `UnknownLicenseError` immediately (AC7 applies here
    too: an unmapped on-disk file is exactly the provenance gap the gate
    exists to catch, and catching it here is cheaper than catching it in
    `build_runtime_manifest` after a full re-walk).
    """
    entries: list[FileEntry] = []
    for file_path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = file_path.relative_to(root).as_posix()
        distribution = distribution_of.get(rel)
        if distribution is None:
            raise UnknownLicenseError(f"{rel} has no distribution mapping (unmapped on-disk file)")
        # Per-FILE licence, not per-distribution. spec D3 requires per-file
        # provenance and five of the six shipped distributions declare AGGREGATE
        # licence expressions covering many licences -- we ship a pruned subset
        # of each, so the aggregate is an upper bound, never our BOM. Labelling
        # every file with one licence per package is how three Microsoft VC++
        # Redistributable binaries came to be recorded as LGPL-2.1-or-later in a
        # shipped manifest: not a GPL breach, but a false statement in the exact
        # artifact whose entire job is being true.
        #
        # `classify_shipped_file` returns None for anything the provenance
        # investigation did not confirm. That halts the build, per AC7's
        # "unknown-license file count is ZERO (halt otherwise)" -- a guessed
        # licence is worse than a stopped build.
        license_ = classify_shipped_file(rel)
        if not license_:
            raise UnknownLicenseError(
                f"{rel} (from {distribution}) has no confirmed per-file licence. "
                "Establish its provenance and add it to "
                "civiccast.native.runtime_licenses, or exclude the file -- never "
                "guess a licence for a shipped binary."
            )
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        entries.append(
            FileEntry(
                path=rel,
                sha256=digest,
                bytes=file_path.stat().st_size,
                distribution=distribution,
                license=license_,
            )
        )
    return tuple(entries)
