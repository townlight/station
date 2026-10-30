# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""v1.0 public-record retention preset subset.

The presets are operator-facing defaults, not legal advice. They intentionally
carry source URLs and review notes so a records officer can confirm local
requirements before enabling automatic purges.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class RetentionPreset:
    """Retention default for a public-meeting recording in one state."""

    state_code: str
    state_name: str
    recording_minimum_days: int
    disposition_trigger: str
    source_label: str
    source_url: str
    review_note: str


V1_0_RETENTION_PRESETS: Final[dict[str, RetentionPreset]] = {
    "CA": RetentionPreset(
        state_code="CA",
        state_name="California",
        recording_minimum_days=30,
        disposition_trigger="30 days after the local-agency recording is made",
        source_label="California Government Code section 54953.5",
        source_url=(
            "https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml"
            "?lawCode=GOV&sectionNum=54953.5."
        ),
        review_note="Confirm whether the recording is the official record or subject to litigation hold.",
    ),
    "TX": RetentionPreset(
        state_code="TX",
        state_name="Texas",
        recording_minimum_days=90,
        disposition_trigger="90 days after approval of official minutes",
        source_label="Texas State Library Local Schedule GR, GR1000-03",
        source_url="https://www.tsl.texas.gov/slrm/localretention/schedule_gr",
        review_note=(
            "Confirm permanent retention when the recording substitutes for official minutes."
        ),
    ),
    "FL": RetentionPreset(
        state_code="FL",
        state_name="Florida",
        recording_minimum_days=730,
        disposition_trigger="2 anniversary years after adoption of official minutes",
        source_label="Florida General Records Schedule GS1-SL",
        source_url="https://files.floridados.gov/media/703328/gs1-sl-2020.pdf",
        review_note="Confirm whether the meeting is an official meeting under local policy.",
    ),
    "NY": RetentionPreset(
        state_code="NY",
        state_name="New York",
        recording_minimum_days=1825,
        disposition_trigger="5 years for qualifying posted open-meeting video recordings",
        source_label="New York LGS-1 / Open Meetings video recording guidance",
        source_url="https://www.archives.nysed.gov/sites/archives/files/lgs-1-2022.pdf",
        review_note="Some recordings used only to prepare minutes have shorter schedules; confirm use.",
    ),
    "PA": RetentionPreset(
        state_code="PA",
        state_name="Pennsylvania",
        recording_minimum_days=365,
        disposition_trigger="1 year after approval of official minutes",
        source_label="Pennsylvania Municipal Records Manual, AL-24",
        source_url=(
            "https://www.pa.gov/content/dam/copapwp-pagov/en/phmc/documents/"
            "archives/records-management/documents/2019-Municipal-Records-Manual-rev-with-links.pdf"
        ),
        review_note="Confirm municipal class and whether recordings have continuing administrative value.",
    ),
    "IL": RetentionPreset(
        state_code="IL",
        state_name="Illinois",
        recording_minimum_days=18 * 30,
        disposition_trigger="default v1.0 review floor pending agency schedule confirmation",
        source_label="Illinois Local Records Commission process",
        source_url="https://www.ilsos.gov/departments/archives/records-management/local-records.html",
        review_note=(
            "Confirm the approved local schedule with the records officer before disposal."
        ),
    ),
    "OH": RetentionPreset(
        state_code="OH",
        state_name="Ohio",
        recording_minimum_days=18 * 30,
        disposition_trigger="default v1.0 review floor pending approved RC-2 schedule",
        source_label="Ohio History Connection local retention schedules and forms",
        source_url=(
            "https://www.ohiohistory.org/research/local-government-records-program/"
            "local-retention-schedules-forms/"
        ),
        review_note="Confirm the approved RC-2 schedule with the records officer before disposal.",
    ),
    "GA": RetentionPreset(
        state_code="GA",
        state_name="Georgia",
        recording_minimum_days=90,
        disposition_trigger="90 days after minutes are prepared and verified",
        source_label="Georgia Archives local schedule LG-01-006",
        source_url="https://www.georgiaarchives.org/records/localgov_print_all",
        review_note="Confirm whether minutes and approved attachments require permanent retention.",
    ),
    "NC": RetentionPreset(
        state_code="NC",
        state_name="North Carolina",
        recording_minimum_days=30,
        disposition_trigger="after official minutes are approved unless recording is official minutes",
        source_label="North Carolina General Records Schedule for Local Government Agencies",
        source_url="https://archives.ncdcr.gov/general-records-schedule-local-government-agencies/open",
        review_note=(
            "Confirm whether recordings serve as official minutes before setting disposal."
        ),
    ),
    "MI": RetentionPreset(
        state_code="MI",
        state_name="Michigan",
        recording_minimum_days=365,
        disposition_trigger="1 year minimum for required public-meeting recordings",
        source_label="Michigan Open Meetings Act sound recordings / local retention schedules",
        source_url="https://www.legislature.mi.gov/Laws/MCL?objectName=mcl-15-269a",
        review_note="Confirm the approved local retention schedule before destruction.",
    ),
}


def get_retention_preset(state_code: str) -> RetentionPreset:
    """Return the v1.0 retention preset for a two-letter state code."""
    key = state_code.upper()
    try:
        return V1_0_RETENTION_PRESETS[key]
    except KeyError as exc:
        raise KeyError(f"unsupported v1.0 retention preset state: {state_code!r}") from exc


def list_retention_presets() -> tuple[RetentionPreset, ...]:
    """Return v1.0 presets in stable state-code order."""
    return tuple(V1_0_RETENTION_PRESETS[key] for key in sorted(V1_0_RETENTION_PRESETS))
