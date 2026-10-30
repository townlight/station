# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Pull-on-demand install of the free BSD-licensed TSDuck toolkit (tsduck.io).

A public-access operator has never heard of TSDuck, so CivicCast fetches and
installs it for them when they enable cable verification — mirroring the
on-demand model pull (``model_download._run_ollama_pull``). On Windows (the
cable-egress target) this unpacks the official *portable* zip into a contained
per-user directory: no admin rights, no system installer, trivially removed.
Integrity is enforced with a pinned SHA-256 (TSDuck publishes no checksum
files) on top of HTTPS from an immutable GitHub release tag. On Linux/macOS,
where install means a root package manager, CivicCast does not silently elevate
— it returns the exact operator-assisted command instead of a fake success.

All network/disk effects go through injected callables so the logic is fully
unit-testable offline; the Windows download→verify→extract→locate→version path
was real-verified on 2026-06-15 (tsp 3.44-4676).
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from civiccast.egress.compliance import (
    _default_version_runner,
    locate_tsduck,
    managed_tsduck_dir,
)

# Pinned release (verified locally 2026-06-15). Bump together with the SHA-256s.
TSDUCK_VERSION = "3.44-4676"
_RELEASE_BASE = f"https://github.com/tsduck/tsduck/releases/download/v{TSDUCK_VERSION}/"

TsduckInstallStatus = Literal[
    "installed", "already-installed", "operator-assisted", "unsupported", "failed"
]


@dataclass(frozen=True)
class _PortableAsset:
    """A Windows portable-zip release asset with a pinned integrity hash."""

    name: str
    sha256: str

    @property
    def url(self) -> str:
        return _RELEASE_BASE + self.name


# Windows portable zips — the only auto-install target (no admin). SHA-256s are
# pinned, not fetched: TSDuck ships no checksum/signature file, so the pin is the
# integrity anchor on top of HTTPS + the immutable release tag.
#
# PROVENANCE / how to bump (do NOT just copy-paste a new hash):
#   1. Download the asset for the new tag from _RELEASE_BASE (github.com/tsduck).
#   2. Record its sha256 here AND bump TSDUCK_VERSION in the same commit.
#   3. Run the network-gated test `test_pinned_sha_matches_release` (set
#      CIVICCAST_TSDUCK_NETWORK_TESTS=1) which re-downloads the pinned asset and
#      asserts the live bytes match this pin — so a typo'd or swapped hash fails
#      mechanically rather than shipping. Pins below recorded 2026-06-15.
_WINDOWS_PORTABLE: dict[str, _PortableAsset] = {
    "amd64": _PortableAsset(
        name=f"TSDuck-Win64-{TSDUCK_VERSION}-Portable.zip",
        sha256="b0ca0f963fcd77488b8c32d6f9d85030daa891ed753c36e57bbb458807a44eb1",
    ),
    "arm64": _PortableAsset(
        name=f"TSDuck-Arm64-{TSDUCK_VERSION}-Portable.zip",
        sha256="47ee41ec033b5349b601add8d723bf661af5ece8d324ec0353211f76357abd0d",
    ),
}

# Normalize the many machine() spellings to our two Windows arch keys.
_ARCH_ALIASES = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "x64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


class TsduckInstallReport(BaseModel):
    """Outcome of a pull-on-demand TSDuck install attempt."""

    model_config = ConfigDict(extra="forbid")

    status: TsduckInstallStatus
    version: str | None = None
    tsp_path: str | None = None
    asset: str | None = None
    message: str


def windows_portable_asset(machine: str) -> _PortableAsset | None:
    """The pinned Windows portable asset for ``machine`` (platform.machine()), or None."""

    key = _ARCH_ALIASES.get(machine.strip().lower())
    return _WINDOWS_PORTABLE.get(key) if key else None


def _operator_assisted_message(system: str, machine: str) -> str:
    arch = _ARCH_ALIASES.get(machine.strip().lower(), machine)
    if system == "Linux":
        deb_arch = "arm64" if arch == "arm64" else "amd64"
        rpm_arch = "aarch64" if arch == "arm64" else "x86_64"
        return (
            "On Linux, install TSDuck with your package manager (it needs root, "
            "so CivicCast will not do it silently). Debian/Ubuntu: download "
            f"tsduck_{TSDUCK_VERSION}.ubuntu24_{deb_arch}.deb from "
            f"{_RELEASE_BASE} and run `sudo apt install ./<file>.deb`. "
            f"RHEL/Fedora: the matching tsduck-{TSDUCK_VERSION}…{rpm_arch}.rpm "
            "with `sudo dnf install ./<file>.rpm`. Then reopen this page."
        )
    if system == "Darwin":
        return (
            "On macOS, install TSDuck with Homebrew: `brew install tsduck`, "
            "then reopen this page. (Homebrew needs your confirmation, so "
            "CivicCast does not run it for you.)"
        )
    return (
        "Automatic TSDuck install is available on Windows. On this platform, "
        "install TSDuck from https://tsduck.io and reopen this page, or set "
        "CIVICCAST_TSDUCK_PATH to an existing tsp bin directory."
    )


# GitHub release-asset URLs 302 to a *.githubusercontent.com CDN; allow only
# those hosts (and github.com) over HTTPS, on the initial URL AND every redirect.
_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "codeload.github.com",
    }
)


def _download_host_allowed(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host in _ALLOWED_DOWNLOAD_HOSTS or host.endswith(".githubusercontent.com")


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-apply the HTTPS + host allow-list to every redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not _download_host_allowed(newurl):
            raise urllib.error.URLError(f"Refusing redirect to an unexpected host: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_file(url: str, dest: Path) -> None:
    """Stream ``url`` to ``dest``. HTTPS-only from the pinned GitHub release host,
    with the host allow-list re-applied across redirects (defense in depth; the
    SHA-256 pin is the integrity anchor regardless of which host serves bytes)."""

    if not url.startswith("https://github.com/tsduck/tsduck/releases/download/"):
        raise ValueError(f"Refusing to download from an unexpected URL: {url}")
    opener = urllib.request.build_opener(_AllowlistRedirectHandler())
    request = urllib.request.Request(url, headers={"User-Agent": "CivicCast"})  # noqa: S310 - https + host validated
    with opener.open(request, timeout=300) as response, dest.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract a zip into ``dest``, rejecting path-traversal (zip-slip) members."""

    dest_root = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.namelist():
            posix_member = PurePosixPath(member)
            windows_member = PureWindowsPath(member)
            if (
                posix_member.is_absolute()
                or windows_member.is_absolute()
                or ".." in posix_member.parts
                or ".." in windows_member.parts
            ):
                raise ValueError(f"Refusing zip member outside target dir: {member}")
            target = (dest / member).resolve()
            if target != dest_root and dest_root not in target.parents:
                raise ValueError(f"Refusing zip member outside target dir: {member}")
        archive.extractall(dest)


class _InstallError(RuntimeError):
    """Internal: a recoverable staging failure; the caller cleans up + reports."""


def _find_tsp(root: Path) -> Path | None:
    """Locate ``tsp``(.exe) under ``root`` deterministically (sorted first match)."""

    matches = sorted(p for exe in ("tsp.exe", "tsp") for p in root.rglob(exe) if p.is_file())
    return matches[0] if matches else None


def install_tsduck(
    *,
    system: str | None = None,
    machine: str | None = None,
    force: bool = False,
    downloader: Callable[[str, Path], None] = _download_file,
    extractor: Callable[[Path, Path], None] = _safe_extract_zip,
    version_runner: Callable[[str], str] | None = None,
    locator: Callable[[], object] | None = None,
) -> TsduckInstallReport:
    """Fetch + install TSDuck on demand. Idempotent; never silently elevates.

    Windows: download the pinned portable zip, verify its SHA-256, extract into a
    fresh staging dir, confirm ``tsp`` is present AND runnable there, then
    atomically swap it into the managed dir. A failure never clobbers a
    previously-good install and never leaves a partial tree, and a stale binary
    can never outlive the freshly-verified bytes. Returns a structured report
    (``already-installed`` / ``installed`` / ``operator-assisted`` /
    ``unsupported`` / ``failed``); never fakes success. Install target is always
    ``managed_tsduck_dir()`` (override via CIVICCAST_TSDUCK_HOME) so the write
    location and ``locate_tsduck``'s search location can never diverge."""

    system = system or platform.system()
    machine = machine or platform.machine()
    run_version = version_runner or _default_version_runner
    locate = locator or (lambda: locate_tsduck(version_runner=version_runner))

    if not force:
        existing = locate()
        if getattr(existing, "installed", False):
            return TsduckInstallReport(
                status="already-installed",
                version=getattr(existing, "version", None),
                tsp_path=getattr(existing, "path", None),
                message="TSDuck is already available; cable verification is ready.",
            )

    if system != "Windows":
        return TsduckInstallReport(
            status="operator-assisted",
            message=_operator_assisted_message(system, machine),
        )

    asset = windows_portable_asset(machine)
    if asset is None:
        return TsduckInstallReport(
            status="unsupported",
            message=(
                f"No pinned TSDuck portable build for Windows '{machine}'. "
                "Install from https://tsduck.io and reopen this page."
            ),
        )

    dest = managed_tsduck_dir()
    staging = dest.parent / f"{dest.name}.staging-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory(prefix="civiccast-tsduck-") as tmp:
        archive = Path(tmp) / asset.name
        try:
            downloader(asset.url, archive)
        except Exception as exc:
            return TsduckInstallReport(
                status="failed",
                asset=asset.name,
                message=f"Download of {asset.name} failed: {exc}",
            )
        actual = _sha256_file(archive)
        if actual.lower() != asset.sha256.lower():
            return TsduckInstallReport(
                status="failed",
                asset=asset.name,
                message=(
                    "Downloaded TSDuck failed its integrity check and was not "
                    f"installed (expected {asset.sha256}, got {actual})."
                ),
            )
        # Stage → verify present+runnable → atomic swap. Any failure here cleans
        # up the staging tree and leaves a previously-good install untouched.
        try:
            shutil.rmtree(staging, ignore_errors=True)
            extractor(archive, staging)
            staged_tsp = _find_tsp(staging)
            if staged_tsp is None:
                raise _InstallError("the archive did not contain a 'tsp' program")
            if not run_version(str(staged_tsp)).strip():
                raise _InstallError("the unpacked 'tsp' did not report a version")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            staging.replace(dest)  # atomic swap on the same filesystem
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            return TsduckInstallReport(
                status="failed",
                asset=asset.name,
                message=f"Could not install {asset.name}: {exc}",
            )

    located = locate()
    if not getattr(located, "installed", False):
        return TsduckInstallReport(
            status="failed",
            asset=asset.name,
            message=(
                "TSDuck was downloaded and unpacked, but the 'tsp' tool could "
                f"not be found under {dest}."
            ),
        )
    located_path = getattr(located, "path", None)
    message = "TSDuck installed. Cable verification is now available."
    if located_path and not str(Path(located_path)).startswith(str(dest)):
        # An operator-set CIVICCAST_TSDUCK_PATH override shadows the managed pull.
        message += (
            " Note: an existing CIVICCAST_TSDUCK_PATH override is taking "
            "precedence over the copy just installed."
        )
    return TsduckInstallReport(
        status="installed",
        version=getattr(located, "version", None),
        tsp_path=located_path,
        asset=asset.name,
        message=message,
    )
