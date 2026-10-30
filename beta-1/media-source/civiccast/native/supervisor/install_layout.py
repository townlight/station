# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The SINGLE source of truth for the installed native layout (audit A1).

On a real install the supervisor runs as LocalSystem inside
``<INSTDIR>\\runtime\\pythonservice.exe`` with CWD ``System32`` and a stock
PATH -- the installer writes NO PATH changes. Every child-process path the
supervisor emits must therefore be ABSOLUTE and derived from the layout the
installer actually produces, never a bare executable name or a CWD-relative
directory.

Verified installer ground truth (do not trust comments elsewhere; these are
the producing modules):

* ``civiccast/native/provision/__main__.py`` (``resolve_provision_paths``):
  the postgres cluster is ``<PROGRAMDATA>\\CivicCast\\data\\pgdata``; the
  server-binaries pack extracts under
  ``<INSTDIR>\\packs\\native-server-binaries\\`` with its executables at
  ``payload\\bin\\`` (``initdb_path`` default). NATS JetStream was removed
  from the product (owner decision 2026-08-20, ADR 0023); this layout no
  longer carries a NATS config or server path.
* ``scripts/build_native_server_pack.py``: the pack payload carries
  ``bin/pg_ctl.exe`` (payload manifest + pins).
* The embedded Python lives at ``<INSTDIR>\\runtime\\python.exe``; the service
  host executable is ``<INSTDIR>\\runtime\\pythonservice.exe`` (same dir), so
  ``sys.executable``'s grandparent IS the install root
  (``station_runtime.station_environment_for_python`` already relies on the
  same ``<root>/runtime/<python>.exe`` shape).

Local-AI (task #57 D2) ground truth -- the ollama runtime has TWO verified
staging conventions in the installer sources, both rooted at the install
root, and the binary exactly one:

* ``apps/installer/src-tauri/src/native_activation.rs``
  (``validate_staged_runtime_layout``): the promoted five-pack station
  carries the reviewed ollama binary at ``dependencies\\ollama\\ollama.exe``
  and the COMPOSED model store (``compose_ollama_model_store``: gemma4:12b +
  gemma4:e4b + translategemma:4b manifests/blobs hard-linked out of the
  signed model components) at ``models\\ollama\\``. The installer's own
  production D2 self-test (``main.rs``'s ``NativeOllamaSelfTestServer``)
  starts exactly that binary with ``OLLAMA_MODELS`` pointed at exactly that
  store.
* ``apps/installer/src-tauri/src/acquisition_catalog.rs``
  (``local_ai_model_root``): the acquisition download experience (task #56)
  stages gemma4:12b's manifest+blobs, in Ollama's own on-disk grammar, at
  ``packs\\local-ai-model\\models\\`` -- the same ``packs\\`` folder the
  other acquisition components use. That flow wires NO ollama binary
  component (``PRODUCTION_CATALOG_IDS`` carries none), so on an
  acquisition-only install the binary path below may simply not exist --
  callers must gate on existence and degrade, never assume.

ffmpeg/ffprobe ground truth: ``apps/installer/src-tauri/src/native_activation.rs``
(``validate_staged_runtime_layout``'s ``required_files`` list) pins
``dependencies/ffmpeg/bin/ffmpeg.exe`` as a required file of the promoted
staged runtime -- the same ``dependencies\\<tool>\\`` convention
``ollama_exe_path`` above already uses. ``ffprobe.exe`` ships beside
``ffmpeg.exe`` in the same ``bin`` directory (standard ffmpeg distribution
layout; not independently listed in ``required_files`` but staged by the
same packaging step). Pure path arithmetic like every other field here:
existence is the CALLER's gate.

``resolve_install_layout`` derives everything from ``sys.executable`` and
``%PROGRAMDATA%`` (the env var is honored -- ``service.py`` previously
hardcoded ``C:\\ProgramData`` for the log root; that divergence is closed by
routing the log root through here). Pure path arithmetic: nothing is created
and nothing must exist at resolution time, so the resolver also runs under a
test-fabricated tree. On a NON-installed interpreter (a dev venv) the derived
paths are structurally correct but point at a tree that does not exist; the
production service host is always the installed ``runtime`` shape.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_SERVER_PACK_SUBDIR = ("packs", "native-server-binaries", "payload", "bin")
_SERVER_PACK_TSDUCK_SUBDIR = ("packs", "native-server-binaries", "payload", "tsduck", "bin")
_CIVICCAST_SUBDIR = "CivicCast"

#: Where the compiled operator console / resident portal actually land on an
#: installed station. Verified producing modules (do not trust comments
#: elsewhere):
#:
#: * ``scripts/build_native_app_payload.py`` builds both portals into
#:   ``civiccast/apps/<portal>/dist`` and its ``assert_civiccast_wheel_layout``
#:   REQUIRES ``civiccast/apps/portal-operator/dist/index.html`` and
#:   ``civiccast/apps/portal-public/dist/index.html`` inside the civiccast
#:   wheel (and rejects every other apps-tree file), so the dists are wheel
#:   members, not a separate staging step.
#: * The wheel is installed into the embedded interpreter, whose ``python.exe``
#:   is ``<root>/runtime/python.exe`` -- so the package (and its ``apps``
#:   subtree) lands under ``<root>/runtime/Lib/site-packages/civiccast``.
#:
#: Pure path arithmetic like every other derivation in this module: existence
#: is the CALLER's gate (``civiccast.app._configured_static_dir`` is the one
#: that checks, and now says so loudly when it fails).
_SITE_PACKAGES_APPS_SUBDIR = ("runtime", "Lib", "site-packages", "civiccast", "apps")


def operator_console_dist_dir(root: Path | str) -> Path:
    """``<root>\\runtime\\Lib\\site-packages\\civiccast\\apps\\portal-operator\\dist``.

    ``root`` is the install root OR the promoted version root -- both shapes
    put the embedded interpreter at ``<root>/runtime``, so one derivation
    serves ``resolve_install_layout`` and
    ``civiccast.native.station_runtime.load_native_station_environment``
    alike, instead of two conventions that can drift apart.
    """

    return Path(root).joinpath(*_SITE_PACKAGES_APPS_SUBDIR, "portal-operator", "dist")


def public_portal_dist_dir(root: Path | str) -> Path:
    """``<root>\\runtime\\Lib\\site-packages\\civiccast\\apps\\portal-public\\dist``."""

    return Path(root).joinpath(*_SITE_PACKAGES_APPS_SUBDIR, "portal-public", "dist")


def packaged_portal_dist_dirs(package_file: Path | str | None = None) -> tuple[Path, Path]:
    """``(operator, public)`` dists, derived from the ``civiccast`` package's
    OWN location -- the same source of truth the running interpreter uses.

    Chain L (TESTER2 request-0050c). The two functions above do root
    ARITHMETIC: they reconstruct where the package *should* be from a root
    they are handed. That arithmetic is correct for the layout the installer
    produces today (``native_pack_staging::pack_extraction_destination``
    bridges ``native-app-payload`` to ``<INSTDIR>\\runtime``, and
    ``native_packs.rs`` strips the ``payload/`` archive prefix, so the pack's
    ``payload/Lib/site-packages/civiccast/apps/<portal>/dist`` lands at
    ``<INSTDIR>\\runtime\\Lib\\site-packages\\...``) -- but it is a SECOND
    description of a layout the interpreter already knows first-hand, and a
    second description is a thing that can drift.

    This one cannot drift: the portals are members of the ``civiccast``
    package, so ``civiccast.__file__``'s parent IS the package root the
    child's ``import civiccast`` resolves to, whatever root shape (fresh
    install, promoted version junction, dev checkout) put it there.

    FALLBACK: an interpreter with no package file at all (frozen/zipimport)
    has no ``__file__`` to derive from. There the proven root arithmetic
    above stands in, resolved from ``sys.executable``. On the real layout the
    two agree by construction -- this is one convention with a stand-in for a
    shape the embedded interpreter does not have, not two conventions.

    Pure path arithmetic like everything else here: existence is the CALLER's
    gate (``civiccast.app._configured_static_dir`` is the one that checks,
    and says so at ERROR level when it fails).
    """

    if package_file is None:
        import civiccast

        package_file = getattr(civiccast, "__file__", None)
    if package_file is None:  # pragma: no cover - no frozen interpreter ships today
        root = resolve_install_root()
        return operator_console_dist_dir(root), public_portal_dist_dir(root)
    package_root = Path(package_file).parent
    return (
        package_root / "apps" / "portal-operator" / "dist",
        package_root / "apps" / "portal-public" / "dist",
    )


def default_program_data_root() -> Path:
    """The ProgramData ROOT (e.g. ``C:\\ProgramData``), honoring the
    ``PROGRAMDATA`` env var -- the same convention ``provision.__main__`` and
    ``children.default_egress_work_dir`` use."""

    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))


def default_civiccast_data_root(program_data_root: Path | str | None = None) -> Path:
    """``<PROGRAMDATA>\\CivicCast`` -- resolved at CALL time so the env var is
    honored, the SAME root ``install-progress.log`` lands in
    (``main.rs::acquisition_download_root``) and every other
    ``civiccast_data_root`` deriver in this module already computes inline.
    A single public name so a caller that just needs this one root (e.g. the
    supervisor's start-failure marker) does not have to reach for the full
    ``InstallLayout`` or re-hardcode ``"CivicCast"`` a second time."""

    root = Path(program_data_root) if program_data_root is not None else default_program_data_root()
    return root / _CIVICCAST_SUBDIR


def default_log_root(program_data_root: Path | str | None = None) -> Path:
    """``<PROGRAMDATA>\\CivicCast\\logs`` -- resolved at CALL time so the env
    var is honored (audit A1: ``service.DEFAULT_LOG_ROOT`` hardcoded the
    drive-letter path)."""

    return default_civiccast_data_root(program_data_root) / "logs"


@dataclass(frozen=True)
class InstallLayout:
    """Every filesystem location the production supervisor wires into its
    child specs, stop commands, pid reader, and logging -- all absolute."""

    install_root: Path
    python_path: Path  # <install_root>\runtime\python.exe (child interpreter)
    server_bin_dir: Path  # <install_root>\packs\native-server-binaries\payload\bin
    pg_ctl_path: Path  # <server_bin_dir>\pg_ctl.exe
    # The TSDuck subset shipped in the SAME native-server-binaries pack, next
    # to (not inside) server_bin_dir -- scripts/build_native_server_pack.py's
    # _tsduck_sources() packs it at tsduck/bin/ under the same payload/ root
    # (see TSDUCK_BIN_PINS). civiccast.egress.compliance.locate_tsduck adds
    # this as a fallback candidate so the shipped tsp.exe is found even when
    # neither CIVICCAST_TSDUCK_PATH nor a pull-on-demand managed install nor
    # PATH names one.
    tsduck_bin_dir: Path  # <install_root>\packs\native-server-binaries\payload\tsduck\bin
    tsp_exe_path: Path  # <tsduck_bin_dir>\tsp.exe
    program_data_root: Path  # e.g. C:\ProgramData
    civiccast_data_root: Path  # <program_data_root>\CivicCast
    postgres_data_dir: Path  # <civiccast_data_root>\data\pgdata
    log_root: Path  # <civiccast_data_root>\logs
    # Local-AI runtime (task #57 D2) -- see the module docstring's ground
    # truth. Pure path arithmetic like every other field: existence is the
    # CALLER's gate (an acquisition-only install has no ollama.exe at all).
    ollama_exe_path: Path  # <install_root>\dependencies\ollama\ollama.exe
    ollama_models_dir: Path  # <install_root>\models\ollama (composed 5-pack store)
    local_ai_pack_models_dir: Path  # <install_root>\packs\local-ai-model\models
    # Chain H1: where the FIRST-RUN acquisition flow downloads to. The
    # installed GUI is non-elevated and cannot write anywhere under
    # ``install_root``; it writes here instead, and the two trees carry the
    # SAME relative layout (``packs\<component>\...``) so a component found
    # staged and one downloaded are interchangeable to every consumer. Pure
    # path arithmetic like every other field: existence is the CALLER's gate.
    acquired_packs_root: Path  # <civiccast_data_root>\packs
    acquired_local_ai_models_dir: Path  # <acquired_packs_root>\local-ai-model\models
    # ffmpeg/ffprobe -- the control-plane child's only working on-air path
    # needs these resolvable, absolute, and off the stock LocalSystem PATH
    # the installer never modifies. Staged at ``dependencies\ffmpeg\bin\``,
    # the SAME convention ``native_activation.rs``'s
    # ``validate_staged_runtime_layout`` pins for ``ffmpeg.exe`` (its
    # required-files list carries ``dependencies/ffmpeg/bin/ffmpeg.exe``
    # verbatim). Pure path arithmetic like ``ollama_exe_path`` above:
    # existence is the CALLER's gate, never assumed here.
    ffmpeg_bin_dir: Path  # <install_root>\dependencies\ffmpeg\bin
    ffmpeg_exe_path: Path  # <ffmpeg_bin_dir>\ffmpeg.exe
    ffprobe_exe_path: Path  # <ffmpeg_bin_dir>\ffprobe.exe
    # Operator media (recordings/uploads) -- NOT credential-bearing, so it
    # gets a plain directory beside ``data\egress`` (which inherits its DACL
    # with no bespoke ACL) rather than the PROTECTED
    # SDDL treatment reserved for the credential-bearing state roots
    # (provision/journal.py, native/pgdata_acl.py).
    upload_dir: Path  # <civiccast_data_root>\data\uploads
    # The packaged front door. The control plane serves /operator/ and / ONLY
    # when CIVICCAST_OPERATOR_CONSOLE_DIST / CIVICCAST_PUBLIC_PORTAL_DIST point
    # at these (civiccast/app.py's _mount_packaged_portals); nothing on a
    # native station ever set them, so both surfaces 404'd. See
    # _SITE_PACKAGES_APPS_SUBDIR for the verified producing modules. Pure path
    # arithmetic: existence is the CALLER's gate.
    operator_console_dist: Path
    public_portal_dist: Path


def resolve_install_root(executable: Path | str | None = None) -> Path:
    """The install root, derived from the (service host) executable:
    ``<install_root>\\runtime\\python[service].exe`` -> the grandparent."""

    exe = Path(executable if executable is not None else sys.executable)
    return exe.parent.parent


def resolve_install_layout(
    *,
    executable: Path | str | None = None,
    program_data_root: Path | str | None = None,
) -> InstallLayout:
    """Resolve the full installed layout from ``executable`` (default
    ``sys.executable``) and ``program_data_root`` (default ``%PROGRAMDATA%``)."""

    install_root = resolve_install_root(executable)
    pd_root = (
        Path(program_data_root) if program_data_root is not None else default_program_data_root()
    )
    civiccast_root = pd_root / _CIVICCAST_SUBDIR
    server_bin_dir = install_root.joinpath(*_SERVER_PACK_SUBDIR)
    tsduck_bin_dir = install_root.joinpath(*_SERVER_PACK_TSDUCK_SUBDIR)
    return InstallLayout(
        install_root=install_root,
        python_path=install_root / "runtime" / "python.exe",
        server_bin_dir=server_bin_dir,
        pg_ctl_path=server_bin_dir / "pg_ctl.exe",
        tsduck_bin_dir=tsduck_bin_dir,
        tsp_exe_path=tsduck_bin_dir / "tsp.exe",
        program_data_root=pd_root,
        civiccast_data_root=civiccast_root,
        postgres_data_dir=civiccast_root / "data" / "pgdata",
        log_root=civiccast_root / "logs",
        ollama_exe_path=install_root / "dependencies" / "ollama" / "ollama.exe",
        ollama_models_dir=install_root / "models" / "ollama",
        local_ai_pack_models_dir=install_root / "packs" / "local-ai-model" / "models",
        acquired_packs_root=civiccast_root / "packs",
        acquired_local_ai_models_dir=civiccast_root / "packs" / "local-ai-model" / "models",
        ffmpeg_bin_dir=install_root / "dependencies" / "ffmpeg" / "bin",
        ffmpeg_exe_path=install_root / "dependencies" / "ffmpeg" / "bin" / "ffmpeg.exe",
        ffprobe_exe_path=install_root / "dependencies" / "ffmpeg" / "bin" / "ffprobe.exe",
        upload_dir=civiccast_root / "data" / "uploads",
        operator_console_dist=operator_console_dist_dir(install_root),
        public_portal_dist=public_portal_dist_dir(install_root),
    )


def ollama_model_store_candidates(layout: InstallLayout) -> tuple[Path, Path, Path]:
    """The staged ``OLLAMA_MODELS`` store locations, in PREFERENCE order:

    1. the activation flow's composed ``models\\ollama`` store (it carries all
       three reviewed tags and is validated by the activation self-test),
    2. the installer-staged ``<install_root>\\packs\\local-ai-model\\models``
       (gemma4:12b only today),
    3. chain H1: the FIRST-RUN acquisition flow's own writable destination
       under ProgramData, which is where a non-elevated GUI actually lands
       what it downloads. Last, so anything the ELEVATED installer delivered
       is always preferred over anything a user-writable directory holds.

    Pure -- callers pick the first that actually exists."""

    return (
        layout.ollama_models_dir,
        layout.local_ai_pack_models_dir,
        layout.acquired_local_ai_models_dir,
    )


__all__ = [
    "InstallLayout",
    "default_log_root",
    "default_program_data_root",
    "ollama_model_store_candidates",
    "operator_console_dist_dir",
    "packaged_portal_dist_dirs",
    "public_portal_dist_dir",
    "resolve_install_layout",
    "resolve_install_root",
]
