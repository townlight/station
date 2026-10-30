# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Station settings for recorded-caption generation and completion."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import RLock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from civiccast.captions.models import CustomVocabulary

RecordedCaptionLanguage = Literal["en", "es", "zh-Hans", "fil", "vi", "ar"]
CaptionCompletionRequirement = Literal["all_selected_languages", "english_reviewed"]
_TRUE_ENV_VALUES = ("1", "true", "on", "yes", "es")
_FALSE_ENV_VALUES = ("0", "false", "off", "no")


class RecordedCaptionSettings(BaseModel):
    """Operator-selected policy for recorded captions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    vocabulary: CustomVocabulary = Field(default_factory=CustomVocabulary)
    enabled: bool = True
    languages: tuple[RecordedCaptionLanguage, ...] = ("en", "es")
    completion_requirement: CaptionCompletionRequirement = "all_selected_languages"

    @model_validator(mode="after")
    def validate_language_policy(self) -> RecordedCaptionSettings:
        if "en" not in self.languages:
            raise ValueError(
                "English must remain selected because it is the source caption language."
            )
        if len(set(self.languages)) != len(self.languages):
            raise ValueError("Recorded caption languages must not contain duplicates.")
        return self

    @classmethod
    def from_env(cls) -> RecordedCaptionSettings:
        """Use legacy environment values only as defaults until a station saves settings."""

        raw_mode = os.environ.get("CIVICCAST_OFFLINE_CAPTION_JOB", "inline").strip().lower()
        if raw_mode not in {"inline", "off"}:
            raise ValueError(
                f"CIVICCAST_OFFLINE_CAPTION_JOB must be one of inline, off; got {raw_mode!r}."
            )

        languages: tuple[RecordedCaptionLanguage, ...] = ("en", "es")
        raw_spanish = os.environ.get("CIVICCAST_OFFLINE_CAPTION_SPANISH", "").strip().lower()
        if raw_spanish:
            if raw_spanish in _FALSE_ENV_VALUES:
                languages = ("en",)
            elif raw_spanish not in _TRUE_ENV_VALUES:
                raise ValueError(
                    "CIVICCAST_OFFLINE_CAPTION_SPANISH must be one of "
                    f"{', '.join((*_TRUE_ENV_VALUES, *_FALSE_ENV_VALUES))}; got {raw_spanish!r}."
                )
        return cls(enabled=raw_mode == "inline", languages=languages)


class RecordedCaptionSettingsStore:
    """Atomic JSON store for non-secret recorded-caption station settings."""

    def __init__(
        self,
        path: str | Path,
        *,
        defaults: RecordedCaptionSettings | None = None,
    ) -> None:
        self.path = Path(path)
        self.defaults = defaults or RecordedCaptionSettings.from_env()
        self._lock = RLock()

    def load(self) -> RecordedCaptionSettings:
        with self._lock:
            if not self.path.exists():
                return self.defaults
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                return RecordedCaptionSettings.model_validate(raw)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"Recorded-caption settings file is invalid: {self.path}") from exc

    def save(self, settings: RecordedCaptionSettings) -> bool:
        with self._lock:
            had_saved_file = self.path.exists()
            previous = self.load()
            # An explicit first Save must pin the operator's choice even if
            # it equals today's environment-derived defaults.
            if had_saved_file and previous == settings:
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            contents = json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    newline="\n",
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    dir=self.path.parent,
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    temporary.write(contents)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                if os.name != "nt":
                    temporary_path.chmod(0o600)
                temporary_path.replace(self.path)
                return True
            finally:
                if temporary_path is not None and temporary_path.exists():
                    temporary_path.unlink()


def default_recorded_caption_settings_path() -> Path:
    """Return the station-state sibling used for operator-editable config."""

    from civiccast.installer.station_state import station_state_path

    return station_state_path().with_name("offline-caption-settings.json")


__all__ = [
    "CaptionCompletionRequirement",
    "RecordedCaptionLanguage",
    "RecordedCaptionSettings",
    "RecordedCaptionSettingsStore",
    "default_recorded_caption_settings_path",
]
