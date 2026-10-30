# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Single source of truth for the NATIVE Windows product line's own version.

Historically kept deliberately separate from ``civiccast._version``
(native-windows chain J, 2026-08-02), back when the WSL/mainline product line
was still shipped alongside the native line and needed its own,
independently-moving release identity. The owner retired the WSL/Linux lane
entirely on 2026-08-19 (see ``CLAUDE.md``) and, on 2026-08-31, retired the
vestigial WSL *version* machinery too: there is now one product and one
version, and ``civiccast._version.__version__`` and this module's
``__version__`` are required to hold the identical string
(``scripts/policy/check_release_identity.py`` enforces it). Both modules are
kept, rather than collapsed into one, because a dozen-plus pre-existing
policy checks and public docs still import ``civiccast._version`` by name;
collapsing them is tracked as future cleanup, not required for correctness
now that the gate enforces they agree.

The native Windows product ("CivicCast (Native)") is this repository's only
product. This module is what the native line's own identity surfaces
(``tauri.native.conf.json``'s ``"version"``, the installer Rust crate's
``CIVICCAST_VERSION`` constant, and native component pack builders' default
``--product-version``) are kept in lockstep with.

This value is surfaced by the shared backend's ``/health`` and
``/api/version`` endpoints ONLY when the process is actually running as a
native station -- see ``civiccast.native.station_runtime.
load_native_station_environment`` (sets ``CIVICCAST_NATIVE_REPORTED_VERSION``)
and ``civiccast.app``'s ``health``/``get_version`` handlers (read it, falling
back to ``civiccast._version.__version__`` when unset -- i.e. every other
hosting context, including the WSL line, is completely unaffected).
"""

__version__ = "1.0.0-beta.20"
