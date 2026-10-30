# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Typed errors for channel egress."""

from __future__ import annotations


class EgressError(RuntimeError):
    """Base error for egress control and playout failures."""


class SinkConnectError(EgressError):
    """Raised when an output sink cannot be reached or prepared."""


class SourcePrepareError(EgressError):
    """Raised when a media source cannot be conformed before air."""


class NoValidSourceError(EgressError):
    """Raised when no program source exists and slate substitution is required."""


class ConfigInvalidError(EgressError):
    """Raised when egress configuration cannot be accepted."""


class SecretUnresolvedError(EgressError):
    """Raised when a sink requires a secret that cannot be resolved."""


class EncoderUnavailableError(EgressError):
    """Raised when the channel's configured hardware encoder is not available on
    this machine and no acceptable fallback applies (native-Windows pre-flight)."""


class PlaylistCapBypassedError(EgressError):
    """Raised when a "program"-kind source plan reaches
    ``gst.bridge.graph_from_config`` carrying more segments than
    ``models.MAX_PLAYLIST_SUBCHAINS``.

    The plan's only producer for this shape
    (``source_plan.build_source_plan_from_schedule``) clamps to this cap
    itself, so a "program"-kind plan arriving here uncapped means that
    clamp was bypassed -- a hand-built ``EgressSourcePlan``, or a future
    producer that forgot to import the shared constant. Fail closed rather
    than silently truncating: the segment count IS the pipeline's shape
    (each segment becomes its own decoder sub-chain), so playing a
    truncated slice of a plan automation and the daemon believe is longer
    would silently desynchronize the pipeline from the rest of the system
    -- exactly the bug a prior version of this fix left in place. A
    "slate"/"cg" fill plan is a different, EXPECTED shape (see
    ``graph_from_config``'s docstring) and does not raise this."""
