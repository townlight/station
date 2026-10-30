# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Cable file-package builder for PEG/headend handoff."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from civiccast.schedule.models import StaffAssetRow

CABLE_PACKAGE_SURFACE_ID = "cable-file-package"
CABLE_CAPTION_EXTENSIONS = (".vtt", ".srt")


class CablePackageError(RuntimeError):
    """Raised when a cable file package cannot be produced safely."""


class CablePackageNotConfiguredError(CablePackageError):
    """Raised when the cable package output was never set up at all.

    Distinct from CablePackageError's other causes (missing source media,
    missing caption sidecar, a copy failure) on purpose: those are real
    problems with a package CivicCast actually tried to build. This one
    means the station never turned cable packaging on in the first place --
    most stations never will, since it is an optional PEG/headend handoff
    surface -- and the publish dashboard renders it as "Not set up
    (optional)" rather than a red "Failed" (see civiccast/publish/service.py
    and docs/USER-MANUAL.md's "What Each Publish Surface Means").
    """


@dataclass(frozen=True)
class CablePackageResult:
    """Result of producing a cable file package."""

    status: str
    package_dir: Path
    zip_path: Path
    verification_hash: str
    manifest_path: Path
    next_step: str


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _clean_segment(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value)
    return cleaned.strip("-") or "civiccast-asset"


def _copy_required_file(source: Path, destination: Path, *, label: str) -> None:
    if not source.exists() or not source.is_file():
        raise CablePackageError(
            f"Cable file package cannot be created because the {label} is missing: {source}. "
            "Add the file, then rerun the cable package step."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _write_sha_sums(package_dir: Path, paths: list[Path]) -> Path:
    sums_path = package_dir / "SHA256SUMS"
    lines = [
        f"{_sha256(path)}  {path.relative_to(package_dir).as_posix()}"
        for path in sorted(paths, key=lambda p: p.relative_to(package_dir).as_posix())
    ]
    sums_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sums_path


def _zip_package(package_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as package_zip:
        for path in sorted(package_dir.rglob("*")):
            if path.is_file() and path != zip_path:
                package_zip.write(path, path.relative_to(package_dir).as_posix())


def build_cable_file_package(
    *,
    asset_id: str,
    title: str,
    media_path: Path,
    caption_path: Path,
    output_dir: Path,
    portal_url: str | None = None,
    loudness_status: str = "not measured by this package builder",
) -> CablePackageResult:
    """Build a local cable handoff package from real media and caption files."""

    safe_asset_id = _clean_segment(asset_id)
    package_dir = output_dir / safe_asset_id
    media_dest = package_dir / "media" / media_path.name
    caption_dest = package_dir / "captions" / caption_path.name
    package_dir.mkdir(parents=True, exist_ok=True)
    _copy_required_file(media_path, media_dest, label="source media")
    _copy_required_file(caption_path, caption_dest, label="caption sidecar")

    manifest = {
        "asset_id": asset_id,
        "title": title,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "profile": "civiccast-cable-file-package-v1",
        "proof_boundary": "file-package-only",
        "media": {
            "path": media_dest.relative_to(package_dir).as_posix(),
            "sha256": _sha256(media_dest),
        },
        "captions": {
            "path": caption_dest.relative_to(package_dir).as_posix(),
            "sha256": _sha256(caption_dest),
        },
        "portal_url": portal_url,
        "loudness_standard": "ITU-R BS.1770 / EBU R128",
        "loudness_status": loudness_status,
        "not_claimed": [
            "NDI output",
            "SDI or DeckLink output",
            "live cable headend delivery",
            "FCC Part 79 field certification",
        ],
    }
    manifest_path = package_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_sha_sums(package_dir, [media_dest, caption_dest, manifest_path])
    zip_path = output_dir / f"{safe_asset_id}-cable-package.zip"
    _zip_package(package_dir, zip_path)
    verification_hash = f"sha256:{_sha256(zip_path)}"
    return CablePackageResult(
        status="ok",
        package_dir=package_dir,
        zip_path=zip_path,
        verification_hash=verification_hash,
        manifest_path=manifest_path,
        next_step=(
            "Send the ZIP package and SHA256SUMS file to the cable headend or automation "
            "operator; do not treat this as live cable-delivery proof."
        ),
    )


def caption_path_for_asset(asset_id: str, captions_dir: Path) -> Path | None:
    """Return the first supported caption sidecar for an asset, if present."""

    safe_asset_id = _clean_segment(asset_id)
    for extension in CABLE_CAPTION_EXTENSIONS:
        path = captions_dir / f"{safe_asset_id}{extension}"
        if path.exists():
            return path
    return None


def build_cable_file_package_for_asset(asset: StaffAssetRow) -> CablePackageResult:
    """Build a cable package from publish asset metadata and operator env config."""

    output_dir = os.environ.get("CIVICCAST_CABLE_PACKAGE_OUTPUT_DIR")
    captions_dir = os.environ.get("CIVICCAST_CABLE_CAPTIONS_DIR")
    if not output_dir or not captions_dir:
        raise CablePackageNotConfiguredError(
            "Cable file package was never set up for this station -- it's an optional PEG/"
            "headend handoff surface most stations don't need. To turn it on, set "
            "CIVICCAST_CABLE_PACKAGE_OUTPUT_DIR and CIVICCAST_CABLE_CAPTIONS_DIR, then retry."
        )
    if not asset.file_path:
        raise CablePackageError(
            "Cable file package needs a local source media path. Repackage or re-ingest this "
            "recording so the asset has a local file_path, then retry."
        )
    caption_path = caption_path_for_asset(asset.asset_id, Path(captions_dir))
    if caption_path is None:
        raise CablePackageError(
            f"Cable file package needs a caption sidecar named {asset.asset_id}.vtt or "
            f"{asset.asset_id}.srt in {captions_dir}. Add captions, then retry."
        )
    return build_cable_file_package(
        asset_id=asset.asset_id,
        title=asset.title,
        media_path=Path(asset.file_path),
        caption_path=caption_path,
        output_dir=Path(output_dir),
        portal_url=asset.manifest_url,
        loudness_status="reuse release loudness gate evidence before headend handoff",
    )
