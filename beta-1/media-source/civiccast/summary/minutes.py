# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Agenda minutes within the existing sourced-summary approval boundary."""

from __future__ import annotations

from datetime import UTC, datetime

from civiccast.captions.models import CaptionCue
from civiccast.summary.fingerprint import sha256_fingerprint
from civiccast.summary.models import AgendaMinutes, MinutesContent, MinutesRevision, SummaryDraft


def validate_minutes_sources(content: MinutesContent, cues: list[CaptionCue]) -> None:
    approved = {cue.cue_id: cue for cue in cues}
    for section in content.sections:
        ranges = list(section.transcript_ranges)
        for motion in section.motions:
            ranges.extend(motion.transcript_ranges)
        for source in ranges:
            cue = approved.get(source.cue_id)
            if (
                cue is None
                or source.start_seconds < cue.start_seconds
                or source.end_seconds > cue.end_seconds
            ):
                raise ValueError(
                    "Minutes must cite current reviewed English cues from this recording. Reload after caption corrections."
                )


def require_minutes_approval(minutes: AgendaMinutes) -> None:
    if not minutes.content.decisions_reviewed:
        raise ValueError(
            "Confirm that all motions and votes were checked, including items with none."
        )
    for section in minutes.content.sections:
        if not section.not_discussed and (
            not section.discussion.strip() or not section.transcript_ranges
        ):
            raise ValueError(
                "Complete cited discussion or explicitly mark each agenda item not discussed."
            )
        if any(not motion.human_confirmed for motion in section.motions):
            raise ValueError("Each motion and its votes require explicit human confirmation.")


def render_minutes(minutes: AgendaMinutes) -> str:
    parts = ["Agenda-based minutes (approval recorded separately)"]
    for section in minutes.content.sections:
        parts += [
            f"\n{section.title}",
            "Not discussed (staff confirmed)." if section.not_discussed else section.discussion,
        ]
        for source in section.transcript_ranges:
            playback = (
                source.start_seconds - minutes.playback_start_seconds
                if minutes.playback_start_seconds is not None
                else None
            )
            location = (
                f"source {source.start_seconds:g}-{source.end_seconds:g}s, cue {source.cue_id}"
            )
            if (
                playback is not None
                and playback >= 0
                and (
                    minutes.playback_end_seconds is None
                    or source.start_seconds < minutes.playback_end_seconds
                )
            ):
                location += f"; player {playback:g}s"
            parts.append(f"Source: {location}")
        for motion in section.motions:
            parts.append(
                f"Motion: {motion.text} | Mover: {motion.mover} | Seconder: {motion.seconder or 'not recorded'} | Outcome: {motion.outcome} | Human confirmed: {motion.human_confirmed}"
            )
            parts.extend(f"Vote: {vote.member}: {vote.choice}" for vote in motion.votes)
    return "\n".join(parts)


def edited_minutes(
    summary: SummaryDraft, content: MinutesContent, operator_id: str
) -> SummaryDraft:
    assert summary.agenda_minutes is not None
    previous = summary.agenda_minutes
    revision = MinutesRevision(
        operator_id=operator_id,
        edited_at=datetime.now(UTC),
        previous_fingerprint=summary.audit_fingerprint,
        content=previous.content,
    )
    minutes = previous.model_copy(
        update={"content": content, "revisions": [*previous.revisions, revision]}
    )
    return summary.model_copy(
        update={
            "agenda_minutes": minutes,
            "narrative": render_minutes(minutes),
            "audit_fingerprint": sha256_fingerprint(
                {"previous": summary.audit_fingerprint, "minutes": minutes.model_dump(mode="json")}
            ),
        }
    )


def transcript_fingerprint(cues: list[CaptionCue]) -> str:
    return sha256_fingerprint(
        {
            "cues": [
                {"id": c.cue_id, "start": c.start_seconds, "end": c.end_seconds, "text": c.text}
                for c in sorted(cues, key=lambda c: c.cue_id)
            ]
        }
    )
