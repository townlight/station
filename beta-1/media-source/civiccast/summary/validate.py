# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Sourced-claim validation against committed transcript cue ranges."""

from __future__ import annotations

from civiccast.captions import CaptionCue
from civiccast.summary.models import SourcedClaim


class UnsupportedSummaryClaimError(ValueError):
    """Raised when a summary claim is not supported by committed cue data."""


class SourcedClaimValidator:
    """Validate sourced claims against committed caption cues."""

    def __init__(self, cues: list[CaptionCue]) -> None:
        self._cues = {cue.cue_id: cue for cue in cues}

    def validate_claim(self, claim: SourcedClaim) -> SourcedClaim:
        for source_range in claim.transcript_ranges:
            cue = self._cues.get(source_range.cue_id)
            if cue is None:
                raise UnsupportedSummaryClaimError(
                    f"Claim {claim.claim_id!r} cites no committed transcript cue: "
                    f"{source_range.cue_id}"
                )
            if (
                source_range.start_seconds < cue.start_seconds
                or source_range.end_seconds > cue.end_seconds
            ):
                raise UnsupportedSummaryClaimError(
                    f"Claim {claim.claim_id!r} timestamp evidence falls outside "
                    "the committed transcript cue range."
                )
        return claim

    def validate_claims(self, claims: list[SourcedClaim]) -> list[SourcedClaim]:
        return [self.validate_claim(claim) for claim in claims]
