# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Response models for the in-product operator manual."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ManualTocEntry(BaseModel):
    """One heading in the manual's table of contents."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, max_length=200)]
    level: Annotated[int, Field(ge=1, le=6)]
    title: Annotated[str, Field(min_length=1, max_length=400)]


class ManualDocument(BaseModel):
    """The whole rendered operator manual, ready to display."""

    model_config = ConfigDict(extra="forbid")

    source: Annotated[str, Field(min_length=1, max_length=200)]
    source_sha256: Annotated[str, Field(min_length=64, max_length=64)]
    generated_at: datetime
    toc: list[ManualTocEntry]
    html: Annotated[str, Field(min_length=1)]
