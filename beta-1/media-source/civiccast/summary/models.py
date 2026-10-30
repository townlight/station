# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Closed data contracts for v0.6 sourced meeting summaries."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SummaryStatus = Literal["pending_review", "approved", "rejected", "refused"]
ClaimType = Literal["quantitative", "narrative"]


class TranscriptRange(BaseModel):
    """Transcript cue range that supports one sourced claim."""

    model_config = ConfigDict(extra="forbid")

    cue_id: Annotated[str, Field(min_length=1, max_length=160)]
    start_seconds: Annotated[float, Field(ge=0)]
    end_seconds: Annotated[float, Field(ge=0)]

    @model_validator(mode="after")
    def _end_after_start(self) -> TranscriptRange:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class SourcedClaim(BaseModel):
    """One claim in an AI summary plus its transcript evidence."""

    model_config = ConfigDict(extra="forbid")

    claim_id: Annotated[str, Field(min_length=1, max_length=160)]
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    claim_type: ClaimType
    transcript_ranges: list[TranscriptRange] = Field(default_factory=list)

    @model_validator(mode="after")
    def _quantitative_claims_need_sources(self) -> SourcedClaim:
        if self.claim_type == "quantitative" and not self.transcript_ranges:
            raise ValueError("quantitative claim requires source transcript ranges")
        return self


class ModelProvenance(BaseModel):
    """Model and prompt details used to generate a summary draft."""

    model_config = ConfigDict(extra="forbid")

    model_tag: Annotated[str, Field(min_length=1, max_length=120)]
    model_digest: str | None = None
    ollama_version: str | None = None
    prompt_version: Annotated[str, Field(min_length=1, max_length=120)]
    extraction_version: Annotated[str, Field(min_length=1, max_length=120)]
    runtime_parameters: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime


class MinutesVote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member: Annotated[str, Field(min_length=1, max_length=200)]
    choice: Literal["yes", "no", "abstain", "absent"]


class MinutesMotion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    mover: Annotated[str, Field(min_length=1, max_length=200)]
    seconder: Annotated[str, Field(max_length=200)] = ""
    outcome: Literal["passed", "failed", "withdrawn", "no_vote"]
    votes: list[MinutesVote] = Field(default_factory=list, max_length=100)
    transcript_ranges: list[TranscriptRange] = Field(min_length=1, max_length=100)
    human_confirmed: bool = False


class MinutesSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: Annotated[str, Field(min_length=1, max_length=120)]
    title: Annotated[str, Field(min_length=1, max_length=400)]
    discussion: Annotated[str, Field(max_length=12000)] = ""
    transcript_ranges: list[TranscriptRange] = Field(default_factory=list, max_length=100)
    not_discussed: bool = False
    motions: list[MinutesMotion] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def require_discussion_sources(self) -> MinutesSection:
        if self.discussion.strip() and not self.transcript_ranges:
            raise ValueError("Agenda discussion requires transcript/video source ranges.")
        if self.not_discussed and (self.discussion.strip() or self.motions):
            raise ValueError("An item marked not discussed cannot contain discussion or motions.")
        return self


class MinutesContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[MinutesSection] = Field(min_length=1, max_length=200)
    decisions_reviewed: bool = False

    @model_validator(mode="after")
    def unique_items(self) -> MinutesContent:
        if len({section.item_id for section in self.sections}) != len(self.sections):
            raise ValueError("Minutes agenda items must be unique.")
        return self


class MinutesRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operator_id: str
    edited_at: datetime
    previous_fingerprint: str
    content: MinutesContent


class AgendaMinutes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agenda_id: str
    asset_id: str
    source_summary_id: str | None = None
    source_summary_fingerprint: str | None = None
    transcript_fingerprint: str
    # Source ranges retain original recording seconds. Immutable packaged range
    # gives the public player clock; None means no playback link is claimed.
    playback_start_seconds: float | None = None
    playback_end_seconds: float | None = None
    content: MinutesContent
    revisions: list[MinutesRevision] = Field(default_factory=list)


class SummaryDraft(BaseModel):
    """Operator-reviewable sourced summary."""

    model_config = ConfigDict(extra="forbid")

    summary_id: Annotated[str, Field(min_length=1, max_length=160)]
    meeting_id: Annotated[str, Field(min_length=1, max_length=160)]
    status: SummaryStatus
    narrative: str
    sourced_claims: list[SourcedClaim] = Field(default_factory=list)
    provenance: ModelProvenance
    audit_fingerprint: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    operator_message: str | None = None
    agenda_minutes: AgendaMinutes | None = None


class OperatorApproval(BaseModel):
    """Human approval metadata for a summary legal-record gate."""

    model_config = ConfigDict(extra="forbid")

    summary_id: Annotated[str, Field(min_length=1, max_length=160)]
    operator_id: Annotated[str, Field(min_length=1, max_length=160)]
    operator_display_name: Annotated[str, Field(min_length=1, max_length=200)]
    approved_at: datetime
    approval_note: str | None = None
