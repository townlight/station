# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S8 operational alerting hub.

Push alerting to the operator (email / SMS / webhook). All sections route
conditions here via ``record_alert_condition``; S8 owns rule-match, dedupe,
delivery, and the continuous runtime safe-to-air signal.
"""
