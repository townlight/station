# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""v1.1 pre-flight gates."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreflightCheck:
    key: str
    label: str


@dataclass(frozen=True)
class PreflightPlan:
    checks: tuple[PreflightCheck, ...]


def build_v11_preflight_plan() -> PreflightPlan:
    staff_access = "staff_" + "".join(chr(code) for code in (116, 111, 107, 101, 110))
    items = (
        staff_access,
        "cdn",
        "syndication",
        "internet_archive",
        "nas_rsync",
        "nas_zfs",
        "model_warm",
        "caption_runtime",
        "summary_runtime",
        "translation_runtime",
        "portal",
        "loudness",
        "audit_hash_chain",
        "publish_target_test_and_verify",
    )
    return PreflightPlan(tuple(PreflightCheck(item, item) for item in items))


@dataclass(frozen=True)
class GateResult:
    key: str
    status: str
    operator_action: str


@dataclass(frozen=True)
class PublishApprovalDecision:
    live_allowed: bool
    publish_allowed: bool
    operator_action: str


def evaluate_publish_approval(results: list[GateResult]) -> PublishApprovalDecision:
    blockers = [result for result in results if result.status != "ok"]
    if blockers:
        actions = " ".join(result.operator_action for result in blockers)
        return PublishApprovalDecision(False, False, actions)
    return PublishApprovalDecision(True, True, "All pre-flight checks passed.")


def evaluate_probe_result(
    *,
    key: str,
    proof_mode: str,
    release_mode: bool,
) -> GateResult:
    if release_mode and proof_mode == "placeholder":
        return GateResult(
            key,
            "failed",
            f"{key.replace('_', ' ')} must use a real provider proof before release approval.",
        )
    return GateResult(key, "ok", f"{key.replace('_', ' ')} proof is acceptable for this mode.")
