# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Resolve an environment variable that has been renamed, keeping the spelling
a live station already sets working.

BETA.10 U03. Two station settings were read here under the legacy one-C
``CIVICAST_`` spellings while the station's own service registry -- the
``Environment`` REG_MULTI_SZ under
``HKLM\\SYSTEM\\CurrentControlSet\\Services\\CivicCastSupervisor``,
which the native installer writes -- sets them under ``CIVICCAST_`` (two C's):

- ``CIVICCAST_EGRESS_PREPARATION_TIMEOUT_SECONDS=300``
  (``preparer.preparation_timeout_seconds_from_env``; legacy spelling
  ``CIVICAST_EGRESS_PREPARATION_TIMEOUT_SECONDS``)
- ``CIVICCAST_GSTREAMER_SOURCE_SEGMENT_SECONDS=1800``
  (``source_plan.gstreamer_source_segment_seconds_from_env``; legacy spelling
  ``CIVICAST_GSTREAMER_SOURCE_SEGMENT_SECONDS``)

Both readings fell through to their code defaults, so the station settings
were silently ignored -- invisible only because both registry values happen
to equal those defaults today. A one-character difference that is silently
inert until someone tunes the setting is the failure mode this module exists
to end, so the resolution rule is one implementation both readers share
rather than two copies that can drift apart again.

The rule, in order:

1. ``name`` (the two-C spelling) when it is set to something other than
   whitespace -- the value a current deployment should be using.
2. Else ``legacy_name`` (the one-C spelling), so a station that has this
   variable set today keeps working. Reading it logs a one-time deprecation
   warning naming both spellings.
3. Else ``None`` -- the caller applies its own default.

A whitespace-only value counts as unset at either spelling, matching
``engine_select.selected_engine_name``'s treatment of an empty
``CIVICCAST_EGRESS_ENGINE=``: a blank line in an env file must never win
over a real value at the other spelling.

When BOTH are set to different values, the two-C one wins and a one-time
warning names both, so a deployment that migrated by adding the new name
without removing the old one is told which value is actually in force rather
than left to guess. When both are set to the SAME value there is nothing to
report -- the migration is consistent.
"""

from __future__ import annotations

import logging
import os

__all__ = ["resolve_renamed_env"]


def resolve_renamed_env(
    *,
    name: str,
    legacy_name: str,
    logger: logging.Logger,
    warned: set[str],
) -> tuple[str, str] | None:
    """Return ``(env_name, raw_value)`` for the spelling to read, or ``None``
    when neither is set.

    ``env_name`` is the name the value actually came from, so a caller's own
    invalid-value warning names the variable the operator can go and fix
    rather than a hard-coded one.

    ``warned`` is the caller's module-level set of already-reported messages
    -- passed in rather than held here so the one-time state belongs to the
    module doing the reading, and a test can clear it to exercise the
    warning without reaching into this module's globals.

    Warnings are one-time per (spelling, kind) pair: these readers are called
    once per plan build / per ``SourcePreparer`` construction, and a station
    that has both spellings set would otherwise get the same two lines on
    every rollover for the life of the service.
    """

    primary = os.environ.get(name, "").strip()
    legacy = os.environ.get(legacy_name, "").strip()

    if primary:
        if legacy and legacy != primary:
            key = f"{legacy_name}:conflict"
            if key not in warned:
                warned.add(key)
                logger.warning(
                    "%s=%r and %s=%r are both set and differ; using %s's value (%r). "
                    "Remove the %s spelling once the deployment is migrated.",
                    name,
                    primary,
                    legacy_name,
                    legacy,
                    name,
                    primary,
                    legacy_name,
                )
        return name, primary

    if legacy:
        key = f"{legacy_name}:deprecated"
        if key not in warned:
            warned.add(key)
            logger.warning(
                "%s is the legacy spelling of %s and is deprecated; reading %s=%r "
                "for now. Set %s instead -- the legacy name will stop being read "
                "in a future release.",
                legacy_name,
                name,
                legacy_name,
                legacy,
                name,
            )
        return legacy_name, legacy

    return None
