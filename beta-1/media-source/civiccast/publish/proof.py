# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""External provider proof orchestration for release gates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

ProviderProofStatus = Literal[
    "not_configured",
    "needs_live_proof",
    "proof_passed",
    "proof_failed_redaction",
    "skipped_optional",
]


@dataclass(frozen=True)
class ProviderProofRequirement:
    """One provider proof item the operator can understand and act on."""

    provider: str
    label: str
    surface_id: str
    credential_reference: str | None
    required_for_public_records: bool
    proof_goal: str
    setup_hint: str


@dataclass(frozen=True)
class ProviderProofReadiness:
    """Fail-closed readiness state for one provider proof requirement."""

    provider: str
    label: str
    surface_id: str
    status: ProviderProofStatus
    ready_for_public_release: bool
    credential_configured: bool
    required_for_public_records: bool
    evidence_reference: str | None
    message: str
    next_step: str


@dataclass(frozen=True)
class ProviderEvidenceRecord:
    """Redacted proof record for one external provider surface."""

    provider: str
    status: str
    proof_mode: str
    redacted_evidence: str


@dataclass(frozen=True)
class ExternalProviderProofResult:
    """Combined external proof result."""

    records: tuple[ProviderEvidenceRecord, ...]


def provider_proof_requirements() -> tuple[ProviderProofRequirement, ...]:
    """Return the v1.4 provider proof checklist in operator-facing terms."""

    return (
        ProviderProofRequirement(
            provider="internet_archive",
            label="Internet Archive",
            surface_id="internet-archive",
            credential_reference="internet-archive",
            required_for_public_records=True,
            proof_goal="Publish a controlled public-record asset to the configured IA target.",
            setup_hint="Configure the IA access key/value, run a controlled publish proof, and save the redacted result.",
        ),
        ProviderProofRequirement(
            provider="youtube_live",
            label="YouTube Live",
            surface_id="youtube-live",
            credential_reference="youtube-live",
            required_for_public_records=True,
            proof_goal="Start and stop a controlled live stream with no secret values in logs.",
            setup_hint="Configure OAuth client and refresh access, run a live stream proof, and save the redacted result.",
        ),
        ProviderProofRequirement(
            provider="youtube_vod",
            label="YouTube VOD",
            surface_id="youtube-vod",
            credential_reference="youtube-vod",
            required_for_public_records=True,
            proof_goal="Upload a controlled VOD asset to the configured channel.",
            setup_hint="Configure YouTube VOD access, run a controlled upload proof, and save the redacted result.",
        ),
        ProviderProofRequirement(
            provider="nas_rsync",
            label="Local NAS rsync",
            surface_id="local-nas-rsync",
            credential_reference="local-nas-rsync",
            required_for_public_records=True,
            proof_goal="Write a controlled archive package to the local NAS rsync target.",
            setup_hint="Configure the NAS target, verify rsync exists, run a controlled copy proof, and save the redacted result.",
        ),
        ProviderProofRequirement(
            provider="nas_zfs",
            label="Local NAS ZFS",
            surface_id="local-nas-zfs",
            credential_reference="local-nas-zfs",
            required_for_public_records=True,
            proof_goal="Verify the local archive target can be protected by the approved ZFS workflow or approved deferral.",
            setup_hint="Configure the ZFS-capable target or record the approved deferral before public release proof.",
        ),
        ProviderProofRequirement(
            provider="email_double_opt_in",
            label="Subscriber email double opt-in",
            surface_id="subscriber-notifications",
            credential_reference="subscriber-notifications",
            required_for_public_records=True,
            proof_goal="Send a controlled double opt-in email without exposing SMTP secrets.",
            setup_hint="Configure sender/SMTP details, run a controlled subscription proof, and save the redacted result.",
        ),
        ProviderProofRequirement(
            provider="webhook_hmac",
            label="Webhook HMAC",
            surface_id="webhook-hmac",
            credential_reference="webhook-hmac",
            required_for_public_records=False,
            proof_goal="Deliver a controlled webhook with a verified HMAC signature.",
            setup_hint="Configure the signing value, run a controlled webhook proof, and save the redacted result.",
        ),
        ProviderProofRequirement(
            provider="podcast_rss",
            label="Podcast RSS",
            surface_id="podcast-rss",
            credential_reference=None,
            required_for_public_records=False,
            proof_goal="Generate and validate a controlled podcast RSS feed.",
            setup_hint="Run the RSS validation proof and save the redacted result.",
        ),
    )


def build_provider_proof_plan(
    *,
    configured_credentials: Iterable[str] = (),
    passed_evidence: Mapping[str, str] | None = None,
    redacted_evidence: Iterable[str] = (),
    skipped_optional_providers: Iterable[str] = (),
) -> tuple[ProviderProofReadiness, ...]:
    """Evaluate provider proof readiness without treating credentials as proof."""

    configured = set(configured_credentials)
    evidence = passed_evidence or {}
    redacted = set(redacted_evidence)
    skipped = set(skipped_optional_providers)
    return tuple(
        evaluate_provider_proof_readiness(
            requirement,
            configured_credentials=configured,
            passed_evidence=evidence,
            redacted_evidence=redacted,
            skipped_optional_providers=skipped,
        )
        for requirement in provider_proof_requirements()
    )


def evaluate_provider_proof_readiness(
    requirement: ProviderProofRequirement,
    *,
    configured_credentials: set[str],
    passed_evidence: Mapping[str, str],
    redacted_evidence: set[str],
    skipped_optional_providers: set[str],
) -> ProviderProofReadiness:
    """Return a plain readiness state for a single provider proof item."""

    evidence_reference = passed_evidence.get(requirement.provider)
    credential_configured = (
        requirement.credential_reference is None
        or requirement.credential_reference in configured_credentials
        or requirement.provider in configured_credentials
        or requirement.surface_id in configured_credentials
    )
    if evidence_reference and requirement.provider not in redacted_evidence:
        return ProviderProofReadiness(
            provider=requirement.provider,
            label=requirement.label,
            surface_id=requirement.surface_id,
            status="proof_failed_redaction",
            ready_for_public_release=False,
            credential_configured=credential_configured,
            required_for_public_records=requirement.required_for_public_records,
            evidence_reference=evidence_reference,
            message=f"{requirement.label} proof exists, but redaction has not been confirmed.",
            next_step="Redact or replace the proof artifact before it can count for release.",
        )
    if evidence_reference:
        return ProviderProofReadiness(
            provider=requirement.provider,
            label=requirement.label,
            surface_id=requirement.surface_id,
            status="proof_passed",
            ready_for_public_release=True,
            credential_configured=credential_configured,
            required_for_public_records=requirement.required_for_public_records,
            evidence_reference=evidence_reference,
            message=f"{requirement.label} has a redacted live proof artifact.",
            next_step="Keep the proof artifact with the release evidence.",
        )
    if (
        not requirement.required_for_public_records
        and requirement.provider in skipped_optional_providers
    ):
        return ProviderProofReadiness(
            provider=requirement.provider,
            label=requirement.label,
            surface_id=requirement.surface_id,
            status="skipped_optional",
            ready_for_public_release=True,
            credential_configured=credential_configured,
            required_for_public_records=False,
            evidence_reference=None,
            message=f"{requirement.label} was intentionally skipped for this release.",
            next_step="Do not claim this provider in release notes until proof exists.",
        )
    if not credential_configured:
        return ProviderProofReadiness(
            provider=requirement.provider,
            label=requirement.label,
            surface_id=requirement.surface_id,
            status="not_configured",
            ready_for_public_release=False,
            credential_configured=False,
            required_for_public_records=requirement.required_for_public_records,
            evidence_reference=None,
            message=f"{requirement.label} is not configured.",
            next_step=requirement.setup_hint,
        )
    return ProviderProofReadiness(
        provider=requirement.provider,
        label=requirement.label,
        surface_id=requirement.surface_id,
        status="needs_live_proof",
        ready_for_public_release=False,
        credential_configured=True,
        required_for_public_records=requirement.required_for_public_records,
        evidence_reference=None,
        message=f"{requirement.label} credentials exist, but live proof has not passed.",
        next_step=requirement.proof_goal,
    )


def run_external_provider_proof(
    *,
    mode: str,
    allow_mocks: bool,
) -> ExternalProviderProofResult:
    """Emit redacted release records for every v1.1 external surface."""

    proof_mode = "release" if mode == "release" and not allow_mocks else "blocked"
    records = tuple(
        ProviderEvidenceRecord(
            provider=provider,
            status="credential_gate",
            proof_mode=proof_mode,
            redacted_evidence=(
                f"{provider} reached the controlled release proof boundary; "
                "configure the test account or local target and rerun proof."
            ),
        )
        for provider in (
            "internet_archive",
            "youtube_live",
            "youtube_vod",
            "nas_rsync",
            "nas_zfs",
            "email_double_opt_in",
            "webhook_hmac",
            "podcast_rss",
        )
    )
    return ExternalProviderProofResult(records=records)
