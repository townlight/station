# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""v1.1 real-AI release metric and fixture-ledger gates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

MetricGateStatus = Literal["ok", "failed"]
FixtureStatus = Literal["approved", "not_used", "blocked"]


@dataclass(frozen=True)
class FixtureLicenseRow:
    """One fixture license or consent row."""

    fixture_id: str
    fixture_type: str
    status: FixtureStatus
    pii_review: str


@dataclass(frozen=True)
class FixtureLicenseLedger:
    """Parsed v1.1 fixture-ledger view used by benchmark gates."""

    rows: tuple[FixtureLicenseRow, ...]
    municipal_fixtures_require_pii_review: bool

    def status_for(self, fixture_id: str) -> FixtureLicenseRow:
        for row in self.rows:
            if row.fixture_id.casefold() == fixture_id.casefold():
                return row
        return FixtureLicenseRow(
            fixture_id=fixture_id,
            fixture_type="unknown",
            status="not_used",
            pii_review="not applicable",
        )

    def has_complete_rows_for(self, fixture_types: list[str]) -> bool:
        present = {row.fixture_type.casefold() for row in self.rows}
        return {fixture_type.casefold() for fixture_type in fixture_types} <= present


@dataclass(frozen=True)
class ReleaseMetricGateResult:
    """Result of evaluating v1.1 AI quality floors."""

    status: MetricGateStatus
    release_floor_summary: dict[str, float]
    operator_action: str


def load_fixture_license_ledger(path: Path) -> FixtureLicenseLedger:
    """Load the committed fixture-ledger markdown into closed row objects."""

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    rows: list[FixtureLicenseRow] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or "---" in line or "fixture id" in line.lower():
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        fixture_id, fixture_type, status, pii_review = cells[:4]
        if status not in {"approved", "not_used", "blocked"}:
            status = "blocked"
        rows.append(
            FixtureLicenseRow(
                fixture_id=fixture_id,
                fixture_type=fixture_type,
                status=cast(FixtureStatus, status),
                pii_review=pii_review,
            )
        )

    return FixtureLicenseLedger(
        rows=tuple(rows),
        municipal_fixtures_require_pii_review=(
            "municipal pii review: required" in text.casefold()
            or any(row.pii_review.casefold() == "required" for row in rows)
        ),
    )


# The legacy per-feature runtime tags. Kept as the DEFAULT so existing callers
# (no operator selection passed) see the unchanged contract. S13 (DONE-6): the
# gate must not pin a single model — pass ``expected_models`` to follow the
# operator's selection instead.
_DEFAULT_EXPECTED_MODELS: dict[str, str] = {
    "captions": "whisper-large-v3",
    "summary": "gemma4:e4b",
    "translation": "translategemma:4b",
}


def _line_proves_model_with_digest(runtime_lines: list[str], model_tag: str) -> bool:
    """True iff some single evidence line carries ``model=<tag>`` AND a content digest.

    Checking per-line (not across the joined blob) prevents a digest on an
    unrelated line from spuriously satisfying a model tag on another.
    """
    needle = f"model={model_tag} "
    for line in runtime_lines:
        # A trailing space disambiguates ``model=gemma4:e4b`` from a hypothetical
        # ``model=gemma4:e4b-cloud``; every evidence field is space-delimited.
        padded = line if line.endswith(" ") else line + " "
        if needle in padded and "digest=sha256:" in line:
            return True
    return False


def evaluate_real_ai_release_metrics(
    *,
    wer_percent: float | None,
    bleu: float | None,
    rouge_l: float | None,
    sourced_claim_refusal_pass_rate: float | None,
    runtime_lines: list[str],
    expected_models: Mapping[str, str] | None = None,
) -> ReleaseMetricGateResult:
    """Evaluate v1.1 release floors with required positive runtime signals.

    ``expected_models`` maps each feature (``captions`` / ``summary`` /
    ``translation``) to the runtime *tag* the operator-selected model emits. The
    gate then requires evidence proving THAT model ran (plus its provenance
    qualifier) rather than a hardcoded trio. It defaults to the legacy local pins,
    so callers that pass no selection are unaffected (S13 §4.2 / DONE-6).
    """

    models = dict(_DEFAULT_EXPECTED_MODELS)
    if expected_models:
        models.update(expected_models)

    summary = {
        "wer_percent": float("inf") if wer_percent is None else wer_percent,
        "bleu": float("-inf") if bleu is None else bleu,
        "rouge_l": float("-inf") if rouge_l is None else rouge_l,
        "sourced_claim_refusal_pass_rate": (
            float("-inf")
            if sourced_claim_refusal_pass_rate is None
            else sourced_claim_refusal_pass_rate
        ),
    }
    line_text = "\n".join(runtime_lines)
    failures: list[str] = []
    if summary["wer_percent"] > 50:
        failures.append("caption WER must be <= 50 percent")
    if summary["bleu"] < 5:
        failures.append("Spanish translation BLEU must be >= 5")
    if summary["sourced_claim_refusal_pass_rate"] < 1.0:
        failures.append("summary sourced-claim/refusal pass rate must be 100 percent")
    # Captions: faster-whisper int8 evidence for the selected model.
    captions_signal = f"runtime=faster-whisper model={models['captions']} compute=int8"
    if captions_signal not in line_text:
        failures.append(f"missing runtime signal: {captions_signal}")

    # Summary + translation: the SELECTED model must appear with a content digest.
    # The provider prefix varies by tier (local runtime=ollama vs hosted
    # runtime=ollama-cloud / openrouter), so we require ``model=<tag>`` together
    # with a ``digest=sha256:`` provenance token rather than pinning the runtime.
    for feature in ("summary", "translation"):
        tag = models[feature]
        if not _line_proves_model_with_digest(runtime_lines, tag):
            failures.append(f"missing runtime signal: model={tag} digest=sha256: ({feature})")

    if failures:
        return ReleaseMetricGateResult(
            status="failed",
            release_floor_summary=summary,
            operator_action="; ".join(failures),
        )
    return ReleaseMetricGateResult(
        status="ok",
        release_floor_summary=summary,
        operator_action="v1.1 real-AI metric floors are satisfied.",
    )
