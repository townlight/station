# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Loads the pre-rendered in-product manual (never parses markdown/HTML at
runtime -- see civiccast/docsite/__init__.py for the full contract)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from civiccast.docsite.models import ManualDocument

_MANUAL_JSON_PATH = Path(__file__).resolve().parent / "manual.json"


class ManualUnavailableError(RuntimeError):
    """civiccast/docsite/manual.json is missing or unreadable.

    Should not happen on a real install -- the file ships inside the
    ``civiccast`` package via pyproject.toml's wheel force-include, exactly
    like civiccast/records/fixtures/*.ttf -- but a dev checkout that never
    ran ``scripts/render_docsite_manual.py`` will hit this, so the error
    names the exact fix instead of a bare stack trace.
    """


@lru_cache(maxsize=1)
def _load_manual_cached(mtime_ns: int) -> ManualDocument:
    """``mtime_ns`` is only a cache key: it makes ``load_manual()`` pick up a
    freshly-rendered file in a live dev server without needing a restart,
    while still avoiding a JSON parse + pydantic validation on every request.
    """

    try:
        raw = _MANUAL_JSON_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ManualUnavailableError(
            f"{_MANUAL_JSON_PATH} not found. Run: uv run python scripts/render_docsite_manual.py"
        ) from exc
    return ManualDocument.model_validate(json.loads(raw))


def load_manual() -> ManualDocument:
    """Return the current in-product manual document."""

    try:
        mtime_ns = _MANUAL_JSON_PATH.stat().st_mtime_ns
    except FileNotFoundError as exc:
        raise ManualUnavailableError(
            f"{_MANUAL_JSON_PATH} not found. Run: uv run python scripts/render_docsite_manual.py"
        ) from exc
    return _load_manual_cached(mtime_ns)
