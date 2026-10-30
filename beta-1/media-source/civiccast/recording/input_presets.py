# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Configured and locally detected SDI/HDMI recording inputs."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from civiccast.recording.models import RecordingSource
from civiccast.stream._ffmpeg import run_ffmpeg

_PRESETS_ENV = "CIVICCAST_RECORDING_INPUT_PRESETS_JSON"
_DECKLINK_INDEXED = re.compile(r"^\s*(?:\[[0-9]+\]\s*)?['\"]([^'\"]+)['\"]\s*$")
_DSHOW_VIDEO = re.compile(r'^.*?"([^"]+)"\s*\(video\)\s*$', re.IGNORECASE)
_NON_SLUG = re.compile(r"[^a-z0-9]+")


class RecordingInputPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    label: str = Field(min_length=1, max_length=160)
    source_kind: Literal["sdi", "hdmi"]
    backend: Literal["decklink", "dshow"]
    device_name: str = Field(min_length=1, max_length=300)
    audio_device_name: str | None = Field(default=None, max_length=300)
    format_code: str | None = Field(default=None, max_length=40)
    origin: Literal["configured", "detected"] = "configured"

    @model_validator(mode="after")
    def _validate_backend_shape(self) -> RecordingInputPreset:
        values = [self.device_name, self.audio_device_name or ""]
        if any("\x00" in value or "\r" in value or "\n" in value for value in values):
            raise ValueError("recording input device names cannot contain control characters")
        if self.backend == "dshow":
            if self.source_kind != "hdmi":
                raise ValueError("DirectShow recording presets are HDMI inputs")
            if self.format_code is not None:
                raise ValueError("format_code applies only to DeckLink presets")
            if any(":" in value for value in values):
                raise ValueError("DirectShow recording device names cannot contain ':'")
        elif self.audio_device_name is not None:
            raise ValueError("audio_device_name applies only to DirectShow presets")
        return self


def parse_decklink_devices(output: str) -> list[str]:
    devices: list[str] = []
    in_sources = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if "sources for decklink" in line.lower() or "decklink devices" in line.lower():
            in_sources = True
            continue
        indexed = _DECKLINK_INDEXED.match(raw_line)
        if indexed and (in_sources or raw_line.lstrip().startswith("[")):
            devices.append(indexed.group(1).strip())
            continue
        if in_sources and line and ":" not in line and not line.lower().startswith("auto-detected"):
            devices.append(line)
    return list(dict.fromkeys(devices))


def parse_dshow_video_devices(output: str) -> list[str]:
    return list(
        dict.fromkeys(
            match.group(1).strip()
            for line in output.splitlines()
            if (match := _DSHOW_VIDEO.match(line)) is not None
        )
    )


def _preset_id(backend: str, device_name: str) -> str:
    stem = _NON_SLUG.sub("-", device_name.lower()).strip("-") or "input"
    return f"{backend}-{stem}"[:120].rstrip("-")


class RecordingInputPresetCatalog:
    def __init__(
        self,
        configured: list[RecordingInputPreset] | None = None,
        *,
        ffmpeg_runner: Callable[[list[str]], Any] | None = None,
    ) -> None:
        self._configured = configured or []
        self._ffmpeg_runner = ffmpeg_runner or (lambda args: run_ffmpeg(args, timeout=10.0))
        self._cached: list[RecordingInputPreset] | None = None

    @classmethod
    def from_env(cls, **kwargs: Any) -> RecordingInputPresetCatalog:
        raw = os.environ.get(_PRESETS_ENV, "").strip()
        if not raw:
            return cls([], **kwargs)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{_PRESETS_ENV} must be a JSON array: {exc}") from exc
        if not isinstance(payload, list):
            raise ValueError(f"{_PRESETS_ENV} must be a JSON array")
        configured = [RecordingInputPreset.model_validate(row) for row in payload]
        return cls(configured, **kwargs)

    def list_presets(self, *, refresh: bool = False) -> list[RecordingInputPreset]:
        if self._cached is not None and not refresh:
            return list(self._cached)
        discovered: list[RecordingInputPreset] = []
        probes: tuple[tuple[list[str], Literal["decklink", "dshow"]], ...] = (
            (["-hide_banner", "-sources", "decklink"], "decklink"),
            (["-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"], "dshow"),
        )
        for args, backend in probes:
            try:
                result = self._ffmpeg_runner(args)
                output = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}"
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                continue
            names = (
                parse_decklink_devices(output)
                if backend == "decklink"
                else parse_dshow_video_devices(output)
            )
            for name in names:
                discovered.append(
                    RecordingInputPreset(
                        preset_id=_preset_id(backend, name),
                        label=name,
                        source_kind="sdi" if backend == "decklink" else "hdmi",
                        backend=backend,
                        device_name=name,
                        origin="detected",
                    )
                )
        merged = {row.preset_id: row for row in discovered}
        # Explicit station configuration wins over an auto-detected row with
        # the same id, allowing a DeckLink connector to be labeled HDMI or to
        # carry a required format_code.
        merged.update({row.preset_id: row for row in self._configured})
        self._cached = list(merged.values())
        return list(self._cached)

    def resolve_args(self, source: RecordingSource) -> list[str] | None:
        if source.kind not in {"sdi", "hdmi"}:
            return None
        preset = next(
            (
                row
                for row in self.list_presets()
                if row.preset_id == source.input_id and row.source_kind == source.kind
            ),
            None,
        )
        if preset is None:
            return None
        if preset.backend == "decklink":
            format_args = ["-format_code", preset.format_code] if preset.format_code else []
            return ["-f", "decklink", *format_args, "-i", preset.device_name]
        dshow_input = f"video={preset.device_name}"
        if preset.audio_device_name:
            dshow_input += f":audio={preset.audio_device_name}"
        return ["-f", "dshow", "-i", dshow_input]


__all__ = [
    "RecordingInputPreset",
    "RecordingInputPresetCatalog",
    "parse_decklink_devices",
    "parse_dshow_video_devices",
]
