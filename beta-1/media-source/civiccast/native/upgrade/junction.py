# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The ``current`` junction convention and its atomic-ish flip.

**Convention (defined by this slice -- no prior junction convention existed in
the repo; recorded as a decision in
``.agent-runs/native-windows/ws5-installer/evidence/wp3-junction-convention-decision.md``).**

The native install root holds versioned application trees and one directory
junction that names the live one::

    C:\\Program Files\\CivicCast (Native)\\
        app\\1.0.0-rc15\\      <- a laid tree
        app\\1.0.0-rc16\\      <- the newly laid tree
        current  --J-->  app\\1.0.0-rc16     <- the junction the service runs from

The service, shortcuts, and ARP registration always reference ``current\\...``;
an upgrade lays ``app\\<new>\\`` beside the old one and re-points ``current`` at
it, so the switch of "which version is live" is a single junction re-point, and
a rollback is the inverse re-point -- no file churn on the hot path.

Why a **directory junction** (``mklink /J``) and not a symlink or a rename:

* A junction needs **no elevation** to create (unlike a symlink, which needs
  either admin or Developer Mode). The engine runs elevated during install, but
  the flip must also be creatable in an unelevated temp dir so it can be TESTED
  for real (the seam is exercised, not faked). ``mklink /J`` satisfies that.
* A junction is resolved by the OS transparently, so a running service holding
  ``current\\civiccast.exe`` open does not pin the OLD target once re-pointed for
  the NEXT start -- versus renaming the live directory, which fails while files
  are open.
* Re-pointing is remove-old-link + create-new-link. That is not a single atomic
  syscall, but the journal makes it recoverable: ``previous_junction_target`` is
  recorded BEFORE the flip, so a kill between remove and create is repaired by
  re-creating the link at whichever target the journal says should be live.

This module implements the flip with the real filesystem so a temp-dir test
proves the actual behavior; it uses ``os``/``pathlib`` primitives that work on
POSIX too (a POSIX symlink stands in for the Windows junction in CI, which is
exactly the semantics the orchestrator depends on: "``current`` names a target
directory, re-pointable"). On Windows with a real junction the same code path
is used via ``_mklink_junction``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

CURRENT_LINK_NAME = "current"


def current_link(install_root: str | Path) -> Path:
    return Path(install_root) / CURRENT_LINK_NAME


def read_current_target(install_root: str | Path) -> str | None:
    """Return the absolute target ``current`` points at, or None if unset.

    Reads the link/junction reparse target. Returns None when the link does
    not exist (a fresh install before the first flip).
    """

    link = current_link(install_root)
    if not link.exists() and not link.is_symlink():
        return None
    try:
        # Path.readlink handles POSIX symlinks and Windows junctions/symlinks.
        target = link.readlink()
    except OSError:
        # Not a link (e.g. a real directory left by a botched install) -- treat
        # as "no valid junction target" so the caller lays a fresh one.
        return None
    # A Windows junction target comes back with the extended-length device
    # prefix (\\?\C:\...). Strip it so the returned path compares equal to a
    # plain resolved path the caller recorded; resolve() normalizes the rest.
    text = str(target)
    for prefix in ("\\\\?\\", "\\??\\"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return str(Path(text).resolve())


def _remove_link(link: Path) -> None:
    """Remove an existing junction/symlink without recursing into its target.

    A junction is a directory reparse point: ``os.rmdir`` unlinks the junction
    itself and NEVER deletes the files inside its target -- using
    ``shutil.rmtree`` here would be catastrophic (it would follow into and
    delete the live app tree). ``Path.unlink`` covers the POSIX symlink case.
    """

    if link.is_symlink():
        link.unlink()
    elif link.exists():
        # A Windows directory junction reports is_dir() True and is_symlink()
        # False on some Python versions; rmdir unlinks the reparse point only.
        link.rmdir()


def _mklink_junction(link: Path, target: Path) -> None:  # pragma: no cover - Windows-only
    """Create a real Windows directory junction via ``mklink /J`` (no elevation)."""

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell-string, paths are ours
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607 - cmd builtin needs the shell exe
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"mklink /J failed (exit {result.returncode}): {stderr}")


def point_current_at(install_root: str | Path, target: str | Path) -> None:
    """Re-point ``install_root/current`` at ``target`` (create or replace).

    Remove-then-create; the journal's ``previous_junction_target`` makes the
    non-atomic window recoverable (see module docstring). Raises if ``target``
    is not an existing directory -- pointing the live link at a missing tree
    would take the service down.
    """

    root = Path(install_root)
    root.mkdir(parents=True, exist_ok=True)
    target_path = Path(target).resolve()
    if not target_path.is_dir():
        raise RuntimeError(f"junction target does not exist or is not a directory: {target_path}")

    link = current_link(root)
    _remove_link(link)

    if os.name == "nt":
        _mklink_junction(link, target_path)  # pragma: no cover - Windows-only
    else:
        link.symlink_to(target_path, target_is_directory=True)
