# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CivicCast umbrella package.

Open-source, self-hostable civic broadcast platform. The umbrella package
hosts the `civiccast` CLI, the FastAPI app that aggregates per-module
routers, and re-exports the canonical version string.
"""

from civiccast._version import __version__

__all__ = ["__version__"]
