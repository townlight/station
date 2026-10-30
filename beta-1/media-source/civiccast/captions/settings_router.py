# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Staff API for recorded-caption station settings."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from civiccast.auth.roles import require_any_role
from civiccast.captions.settings import (
    RecordedCaptionSettings,
    RecordedCaptionSettingsStore,
    default_recorded_caption_settings_path,
)

staff_router = APIRouter(prefix="/api/staff/captions", tags=["staff", "captions"])


def get_recorded_caption_settings_store(request: Request) -> RecordedCaptionSettingsStore:
    store = getattr(request.app.state, "recorded_caption_settings_store", None)
    if isinstance(store, RecordedCaptionSettingsStore):
        return store
    try:
        store = RecordedCaptionSettingsStore(default_recorded_caption_settings_path())
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recorded-caption settings are unavailable on this station.",
        ) from exc
    request.app.state.recorded_caption_settings_store = store
    return store


def get_recorded_caption_settings(
    store: Annotated[RecordedCaptionSettingsStore, Depends(get_recorded_caption_settings_store)],
) -> RecordedCaptionSettings:
    try:
        return store.load()
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recorded-caption settings could not be read. Restore a valid settings file or contact support.",
        ) from exc


@staff_router.get(
    "/settings",
    response_model=RecordedCaptionSettings,
    summary="Read recorded-caption station settings",
    dependencies=[Depends(require_any_role("setup_admin", "meeting_operator", "support_admin"))],
)
def read_recorded_caption_settings(
    settings: Annotated[RecordedCaptionSettings, Depends(get_recorded_caption_settings)],
) -> RecordedCaptionSettings:
    return settings


@staff_router.put(
    "/settings",
    response_model=RecordedCaptionSettings,
    summary="Save recorded-caption station settings",
    dependencies=[Depends(require_any_role("setup_admin"))],
)
def save_recorded_caption_settings(
    settings: RecordedCaptionSettings,
    store: Annotated[RecordedCaptionSettingsStore, Depends(get_recorded_caption_settings_store)],
) -> RecordedCaptionSettings:
    try:
        store.save(settings)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recorded-caption settings could not be saved. Check station storage and try again.",
        ) from exc
    return settings


__all__ = [
    "get_recorded_caption_settings",
    "get_recorded_caption_settings_store",
    "staff_router",
]
