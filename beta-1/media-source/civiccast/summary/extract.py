# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Deterministic quantitative fact extraction from committed caption cues."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from civiccast.captions import CaptionCue
from civiccast.summary.models import TranscriptRange

_DOLLAR_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?")
_VOTE_TALLY_RE = re.compile(r"\b\d+\s*-\s*\d+\b")


class QuantitativeFact(BaseModel):
    """A regex-derived quantitative fact and its transcript evidence."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    text: str
    source_range: TranscriptRange


def extract_quantitative_facts(cues: list[CaptionCue]) -> list[QuantitativeFact]:
    """Extract v0.6 summary facts from committed caption cues only."""

    facts: list[QuantitativeFact] = []
    for cue in cues:
        text = cue.text
        lowered = text.casefold()
        source = TranscriptRange(
            cue_id=cue.cue_id,
            start_seconds=cue.start_seconds,
            end_seconds=cue.end_seconds,
        )
        if "moved" in lowered or "motion" in lowered:
            facts.append(QuantitativeFact(kind="motion", text=text, source_range=source))
        if "seconded" in lowered or "second " in lowered:
            facts.append(QuantitativeFact(kind="second", text=text, source_range=source))
        if _VOTE_TALLY_RE.search(text) or ("roll call" in lowered and "yes" in lowered):
            facts.append(QuantitativeFact(kind="vote_tally", text=text, source_range=source))
        if _DOLLAR_RE.search(text):
            facts.append(QuantitativeFact(kind="dollar_amount", text=text, source_range=source))
    return facts
