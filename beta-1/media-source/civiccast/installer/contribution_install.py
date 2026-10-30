# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S17 remote-contribution commissioning install (build step 9 slice 3d-ii).

Turnkey-installs the **self-hosted VDO.Ninja** signalling/app for the
remote-contribution tier (S17 §10 / §3, S3 dependency), pinned and verifiable,
without ever vendoring its AGPL source into the CivicCast Apache tree:

* VDO.Ninja ships **source-only** releases (no uploaded assets), and GitHub's
  auto-generated source archives have **unstable** byte hashes — so the honest
  integrity anchor is the **git commit SHA** of the pinned tag (immutable),
  installed via a shallow clone into a per-user **managed dir** (not the repo).
  The clone stages → verifies HEAD == the pinned commit → atomically swaps, so a
  drifted/MITM'd clone never becomes the live copy (the tsduck stage-verify-swap
  discipline). The unmodified project runs as a separate process (S17 §7), so no
  AGPL obligation attaches to CivicCast core.
* **coturn** is a system service (BSD-3, permissive); it is NOT a download-a-zip
  install. We return the operator-assisted OS package command (no silent
  elevation) on Linux/macOS. coturn has no native Windows build, and the
  WSL2 lane that used to run it there was retired under the owner's "no
  linux" decision (2026-08-19): coturn is now a DOCUMENTED EXTERNAL TURN
  server on Windows -- point CIVICCAST_TURN_HOST/CIVICCAST_TURN_PORT at a
  coturn instance running on a separate host (or a managed TURN provider),
  and leave CIVICCAST_COTURN_COMMAND unset.

A real end-to-end clone is an LPM rung-2 proof (S17 §8 item 4); the stage/verify/
swap + idempotency + drift-rejection logic is proven here with an injected git
runner. The pinned commit is re-checkable upstream with ``git ls-remote`` (a
gated network test).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from pydantic import BaseModel, ConfigDict

from civiccast.installer.mediamtx_install import install_mediamtx, relay_paths, relay_verified

# Scott decision S17 §10.4 — pinned VDO.Ninja release. v30.2 (published
# 2026-05-12); the commit SHA is the immutable integrity anchor (a lightweight
# tag, so the ref resolves straight to this commit). Upgrades are reviewed events:
# bump both, re-run ls-remote to confirm the new tag→commit, re-test.
VDO_VERSION = "v30.2"
VDO_PINNED_COMMIT = "d5a8b5669d1cb0ff75f984499f7025b745657808"
VDO_REPO = "https://github.com/steveseguin/vdo.ninja"

_HOME_ENV = "CIVICCAST_REMOTE_CONTRIBUTION_HOME"

# A git runner: (args, cwd) -> stdout. Raises on non-zero. Injected in tests.
GitRunner = Callable[[list[str], Path | None], str]


def managed_contribution_dir() -> Path:
    """Per-user managed location for the pinned VDO.Ninja install (no admin,
    easily removed). Override with ``CIVICCAST_REMOTE_CONTRIBUTION_HOME``."""
    configured = os.environ.get(_HOME_ENV)
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "CivicCast" / "remote-contribution"
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return root / "civiccast" / "remote-contribution"


class ContributionInstallReport(BaseModel):
    """Outcome of a remote-contribution commissioning install."""

    model_config = ConfigDict(extra="forbid")

    vdo_installed: bool
    vdo_path: str | None = None
    vdo_version: str = VDO_VERSION
    vdo_commit: str = VDO_PINNED_COMMIT
    vdo_verified: bool = False
    relay_installed: bool = False
    relay_verified: bool = False
    relay_path: str | None = None
    relay_command: str = ""
    coturn_action: str = ""
    launch_hint: str = ""
    detail: str = ""


class ContributionInstallError(RuntimeError):
    """Raised when the pinned VDO.Ninja clone cannot be verified."""


class RelayFields(TypedDict):
    relay_installed: bool
    relay_verified: bool
    relay_path: str | None
    relay_command: str


def _default_git_runner(args: list[str], cwd: Path | None = None) -> str:
    git_exe = shutil.which("git")
    if git_exe is None:
        raise ContributionInstallError("Git is required to install pinned VDO.Ninja source.")
    try:
        completed = subprocess.run(  # noqa: S603 -- fixed argv, resolved git, never shell
            [git_exe, *args],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=600,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ContributionInstallError("The pinned VDO.Ninja download did not complete.") from exc
    return completed.stdout.strip()


def _resolve_head(git: GitRunner, repo_dir: Path) -> str | None:
    try:
        return git(["rev-parse", "HEAD"], repo_dir).strip()
    except Exception:
        return None


def _coturn_action() -> str:
    if os.name == "nt":
        return (
            "coturn has no native Windows build. Configure an external TURN server -- "
            "a coturn instance on a separate Linux/BSD host with your realm + a public "
            "IP/port, or a managed TURN provider -- then set CIVICCAST_TURN_HOST and "
            "CIVICCAST_TURN_PORT to that server's address. Leave CIVICCAST_COTURN_COMMAND "
            "unset: this station cannot launch or manage a coturn process on Windows."
        )
    return (
        "Install coturn from your OS package manager (no silent elevation): "
        "`sudo apt install coturn` / `sudo dnf install coturn` / `brew install coturn`, "
        "then set CIVICCAST_COTURN_COMMAND to its launch line."
    )


_LAUNCH_HINT = (
    "VDO.Ninja is served as a static site + its signalling server (see the cloned "
    "repo's README). Point CIVICCAST_VDO_COMMAND at the launch line and "
    "CIVICCAST_REMOTE_CONTRIBUTION_VDO_URL at the served base URL. Publish the "
    "MediaMTX WHIP listener through HTTPS, set "
    "CIVICCAST_REMOTE_CONTRIBUTION_MEDIAMTX_URL to that public base URL, then "
    "set CIVICCAST_REMOTE_CONTRIBUTION=on. The verified managed MediaMTX "
    "binary starts automatically unless CIVICCAST_MEDIAMTX_COMMAND overrides it."
)


def install_remote_contribution(
    *,
    git_runner: GitRunner | None = None,
    dest: Path | None = None,
    include_relay: bool = False,
) -> ContributionInstallReport:
    """Clone the pinned VDO.Ninja into the managed dir and verify the commit.

    Idempotent: a present clone already at the pinned commit is left as-is. A
    clone at any other commit (or a fresh install) stages into a sibling dir,
    verifies HEAD == the pin, then atomically swaps it into place — a drifted or
    tampered clone never becomes the live copy. Raises ContributionInstallError
    if the staged clone does not match the pin.
    """
    git = git_runner or _default_git_runner
    target = dest or (managed_contribution_dir() / "vdo.ninja")
    coturn = _coturn_action()

    def relay_fields() -> RelayFields:
        relay_home = target.parent / "mediamtx"
        if include_relay and os.name == "nt":
            try:
                install_mediamtx(dest=relay_home)
            except Exception as exc:
                raise ContributionInstallError(
                    f"MediaMTX relay installation failed: {exc}"
                ) from exc
        binary, config = relay_paths(relay_home)
        verified = relay_verified(relay_home)
        return {
            "relay_installed": binary.is_file(),
            "relay_verified": verified,
            "relay_path": str(binary) if binary.is_file() else None,
            "relay_command": f'"{binary}" "{config}"' if verified else "",
        }

    # Idempotent: already installed + verified.
    if target.is_dir() and _resolve_head(git, target) == VDO_PINNED_COMMIT:
        return ContributionInstallReport(
            vdo_installed=True,
            vdo_path=str(target),
            vdo_verified=True,
            **relay_fields(),
            coturn_action=coturn,
            launch_hint=_LAUNCH_HINT,
            detail=f"VDO.Ninja {VDO_VERSION} already installed and verified.",
        )

    staging = target.parent / f"{target.name}.staging"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.parent.mkdir(parents=True, exist_ok=True)

    git(["clone", "--depth", "1", "--branch", VDO_VERSION, VDO_REPO, str(staging)], None)
    head = _resolve_head(git, staging)
    if head != VDO_PINNED_COMMIT:
        shutil.rmtree(staging, ignore_errors=True)
        raise ContributionInstallError(
            f"Cloned VDO.Ninja {VDO_VERSION} resolved to {head!r}, "
            f"expected the pinned commit {VDO_PINNED_COMMIT!r}; refusing to install."
        )

    # Atomic-ish swap: verified bytes replace any prior copy only after verify.
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    staging.replace(target)
    return ContributionInstallReport(
        vdo_installed=True,
        vdo_path=str(target),
        vdo_verified=True,
        **relay_fields(),
        coturn_action=coturn,
        launch_hint=_LAUNCH_HINT,
        detail=f"VDO.Ninja {VDO_VERSION} cloned and verified at the pinned commit.",
    )


def contribution_install_status(
    *, dest: Path | None = None, git_runner: GitRunner | None = None
) -> ContributionInstallReport:
    """Report whether the pinned VDO.Ninja is present + verified (no install)."""
    git = git_runner or _default_git_runner
    target = dest or (managed_contribution_dir() / "vdo.ninja")
    verified = target.is_dir() and _resolve_head(git, target) == VDO_PINNED_COMMIT
    relay_home = target.parent / "mediamtx"
    binary, config = relay_paths(relay_home)
    relay_ok = relay_verified(relay_home)
    return ContributionInstallReport(
        vdo_installed=target.is_dir(),
        vdo_path=str(target) if target.is_dir() else None,
        vdo_verified=verified,
        relay_installed=binary.is_file(),
        relay_verified=relay_ok,
        relay_path=str(binary) if binary.is_file() else None,
        relay_command=f'"{binary}" "{config}"' if relay_ok else "",
        coturn_action=_coturn_action(),
        launch_hint=_LAUNCH_HINT,
        detail=(
            f"VDO.Ninja {VDO_VERSION} present and verified."
            if verified
            else f"VDO.Ninja {VDO_VERSION} not installed (or commit drifted)."
        ),
    )


__all__ = [
    "VDO_PINNED_COMMIT",
    "VDO_REPO",
    "VDO_VERSION",
    "ContributionInstallError",
    "ContributionInstallReport",
    "GitRunner",
    "contribution_install_status",
    "install_remote_contribution",
    "managed_contribution_dir",
]
