# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Continuous channel program log (cable automation CA-1).

Recurring, operator-managed program slots per channel that materialize into
real ``schedule_items`` over a rolling horizon — so the existing schedule →
source-plan → playout-supervisor path plays a 24/7 program log unchanged.
"""
