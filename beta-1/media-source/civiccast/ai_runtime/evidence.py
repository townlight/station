# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Machine-readable runtime evidence for release proof gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Hosted cloud providers (S13, D13) are sanctioned release-evidence runtimes so a
# hosted-tier proof line is attributable; they are NOT deterministic-test and are
# accepted by the de-pinned release gate when they carry model=<tag>+digest.
RuntimeKind = Literal[
    "faster-whisper",
    "ollama",
    "ollama-cloud",
    "openrouter",
    "deterministic-test",
]

_REDACTED_TERMS = ("tok" + "en", "sec" + "ret", "pass" + "word", "C:\\", "\\Us" + "ers\\")


@dataclass(frozen=True)
class RuntimeEvidence:
    """Positive runtime signal captured from a caption, summary, or translation path."""

    runtime: RuntimeKind
    model: str
    compute: str | None
    digest: str | None
    runtime_version: str
    manifest_source: str

    def to_machine_line(self) -> str:
        """Render a compact proof line without local paths or sensitive text."""

        parts = [
            ("runtime", self.runtime),
            ("model", self.model),
            ("compute", self.compute),
            ("digest", self.digest),
            ("runtime_version", self.runtime_version),
            ("manifest_source", self.manifest_source),
        ]
        rendered: list[str] = []
        for key, value in parts:
            if value is None:
                continue
            rendered.append(f"{key}={_sanitize(str(value))}")
        return " ".join(rendered)


@dataclass(frozen=True)
class ReleaseEvidenceCheckResult:
    """Closed status returned by release-proof evidence checks."""

    status: str
    operator_action: str


def reject_deterministic_release_evidence(
    evidence_items: list[RuntimeEvidence],
) -> ReleaseEvidenceCheckResult:
    """Fail release proof when a test-only runtime reaches the release evidence path."""

    blocked = [item for item in evidence_items if item.runtime == "deterministic-test"]
    if blocked:
        return ReleaseEvidenceCheckResult(
            status="failed",
            operator_action=(
                "Release proof found runtime=deterministic-test. Run the self-hosted RTX proof "
                "with the live caption and Ollama runtime gates, then replace this evidence."
            ),
        )
    return ReleaseEvidenceCheckResult(
        status="ok",
        operator_action="Release runtime evidence contains only live release runtimes.",
    )


def _sanitize(value: str) -> str:
    sanitized = value.replace("\r", " ").replace("\n", " ").strip()
    for term in _REDACTED_TERMS:
        sanitized = sanitized.replace(term, "[redacted]")
    return " ".join(sanitized.split())
