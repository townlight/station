# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Spreadsheet-formula-injection guard for CSV exports.

Any CSV a station hands to a human (underwriting affidavits, audience reports)
may be opened in Excel or LibreOffice, which treats a cell beginning with
``= + - @`` as a formula. Prefix such values with an apostrophe so they render
as literal text instead of executing.
"""

from __future__ import annotations

FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@")


def csv_safe(value: str) -> str:
    """Return ``value`` safe to place in a CSV cell for spreadsheet apps.

    A value that would be interpreted as a formula (leading ``= + - @``) gets an
    apostrophe prefix so Excel/LibreOffice render it as literal text. Anything
    else is returned unchanged, so this is a no-op on normal ids and text.
    """
    if value.startswith(FORMULA_TRIGGER_CHARS):
        return f"'{value}"
    return value


__all__ = ["FORMULA_TRIGGER_CHARS", "csv_safe"]
