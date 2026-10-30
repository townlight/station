# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""User-defined custom metadata fields (S22).

Operator-defined, typed metadata fields on assets — additive metadata that never
alters core asset behavior (the S22 key-claim boundary; absence of any custom field
is always valid). This package owns the ``custom_field_defs`` / ``custom_field_values``
tables (migration ``0054``), the typed-validation service, and the staff/public API.
It is the dependency root of the franchise-reporting cluster: it feeds S19
(saved-search filters on custom fields) and S23 (hours-by-category reporting).
"""
