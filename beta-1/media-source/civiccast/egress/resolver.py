# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Egress resolver contract from planning-brain schedule rows to source plans.

The implementation lives in ``source_plan`` because early E.1 work named the
module around the model it returns. This module keeps the build-plan package
contract explicit without duplicating resolver behavior.
"""

from __future__ import annotations

from civiccast.egress.source_plan import (
    AssetResolver,
    ScheduleItemsProvider,
    ScheduleSourcePlanProvider,
    SlateSourceGenerator,
    build_slate_source_args,
    build_source_plan_from_schedule,
)

__all__ = [
    "AssetResolver",
    "ScheduleItemsProvider",
    "ScheduleSourcePlanProvider",
    "SlateSourceGenerator",
    "build_slate_source_args",
    "build_source_plan_from_schedule",
]
