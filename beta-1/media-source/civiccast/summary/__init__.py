# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Sourced summary contracts for CivicCast v0.6."""

from civiccast.summary.csv_export import render_transcript_csv
from civiccast.summary.extract import QuantitativeFact, extract_quantitative_facts
from civiccast.summary.generate import DeterministicSummaryModel, SummaryGenerationPipeline
from civiccast.summary.models import (
    ModelProvenance,
    OperatorApproval,
    SourcedClaim,
    SummaryDraft,
    TranscriptRange,
)
from civiccast.summary.store import InMemorySummaryStore, PostgresSummaryStore
from civiccast.summary.validate import SourcedClaimValidator, UnsupportedSummaryClaimError

__all__ = [
    "DeterministicSummaryModel",
    "InMemorySummaryStore",
    "ModelProvenance",
    "OperatorApproval",
    "PostgresSummaryStore",
    "QuantitativeFact",
    "SourcedClaim",
    "SourcedClaimValidator",
    "SummaryDraft",
    "SummaryGenerationPipeline",
    "TranscriptRange",
    "UnsupportedSummaryClaimError",
    "extract_quantitative_facts",
    "render_transcript_csv",
]
