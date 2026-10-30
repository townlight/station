# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Privacy-safe aggregate analytics reporting."""

from civiccast.analytics.models import AnalyticsReport
from civiccast.analytics.store import AnalyticsStore

__all__ = ["AnalyticsReport", "AnalyticsStore"]
