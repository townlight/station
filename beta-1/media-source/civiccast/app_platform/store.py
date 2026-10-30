# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Shared app-platform station and channel config store."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import cast

from civiccast.app_platform.models import (
    AppBuildProfile,
    AppTarget,
    ChannelBranding,
    ChannelBrandingUpdate,
    ChannelOutput,
    ChannelPublicConfig,
    OutputKind,
    StationAppConfig,
    StationAppConfigUpdate,
)
from civiccast.cable.channel import ChannelProfile, default_channel_profiles
from civiccast.installer.storage import default_storage_dir

PUBLIC_APP_TARGETS: tuple[AppTarget, ...] = (
    "web_pwa",
    "roku",
    "tvos",
    "fire_tv",
    "android_tv",
    "android_mobile",
    "ios_ipados",
)
CHANNEL_APP_TARGETS: tuple[AppTarget, ...] = (*PUBLIC_APP_TARGETS, "cg", "epg")
_CONFIG_FILE_NAME = "app-platform-config.json"


class AppPlatformConfigStoreError(RuntimeError):
    """Raised when app-platform config cannot be loaded or saved."""


class AppPlatformConfigStore:
    """In-process station/app config shared by public and staff APIs."""

    def __init__(self, config_path: Path | None = None) -> None:
        self._lock = Lock()
        self._config_path = config_path
        self._config = self._load_config()

    def read_config(self, *, station_name_override: str | None = None) -> StationAppConfig:
        with self._lock:
            config = _with_generated_at(self._config)
        if station_name_override is None:
            return config
        return _with_station_name(config, station_name_override)

    def read_channel(self, channel_id: str) -> ChannelPublicConfig | None:
        with self._lock:
            config = self._config
            for channel in config.channels:
                if channel.channel_id == channel_id:
                    return channel.model_copy(deep=True)
        return None

    def update_station(self, patch: StationAppConfigUpdate) -> StationAppConfig:
        payload = patch.model_dump(exclude_unset=True)
        with self._lock:
            channel_ids = {channel.channel_id for channel in self._config.channels}
            default_channel_id = payload.get("default_channel_id")
            if default_channel_id is not None and default_channel_id not in channel_ids:
                raise ValueError("default_channel_id must reference an existing channel")

            build_payload: dict[str, object] = {}
            if "app_name" in payload:
                build_payload["app_name"] = payload["app_name"]
            elif (
                "station_name" in payload
                and self._config.build_profile.app_name == self._config.station_name
            ):
                build_payload["app_name"] = payload["station_name"]
            if "build_tier" in payload:
                build_payload["tier"] = payload["build_tier"]
            if "store_ready" in payload:
                build_payload["store_ready"] = payload["store_ready"]
            if "store_notes" in payload:
                build_payload["store_notes"] = payload["store_notes"]

            config_payload: dict[str, object] = {}
            for key in (
                "station_name",
                "default_channel_id",
                "support_url",
                "privacy_url",
                "analytics_enabled",
                "ga4_measurement_id",
                "analytics_privacy_notice_url",
                "emergency_status_url",
            ):
                if key in payload:
                    config_payload[key] = payload[key]

            build_profile = AppBuildProfile.model_validate(
                {
                    **self._config.build_profile.model_dump(),
                    **build_payload,
                }
            )
            self._config = StationAppConfig.model_validate(
                {
                    **self._config.model_dump(),
                    **config_payload,
                    "generated_at": datetime.now(UTC),
                    "build_profile": build_profile.model_dump(),
                }
            )
            self._persist_locked()
            return self._config.model_copy(deep=True)

    def update_channel_branding(
        self,
        channel_id: str,
        patch: ChannelBrandingUpdate,
    ) -> ChannelPublicConfig:
        # PR #132 review (second re-review): an operator explicitly saving
        # branding through Channel Ops is what "configured" means -- even
        # when the values they chose happen to equal the compile-time
        # default profile (a plausible choice, e.g. keeping the default
        # color). configured_at is therefore stamped unconditionally on
        # every write through this method, never derived by comparing the
        # saved values against civiccast.cable.channel.default_channel_profiles().
        payload = {**patch.model_dump(exclude_unset=True), "configured_at": datetime.now(UTC)}
        with self._lock:
            channels: list[ChannelPublicConfig] = []
            updated: ChannelPublicConfig | None = None
            for channel in self._config.channels:
                if channel.channel_id != channel_id:
                    channels.append(channel)
                    continue
                branding = ChannelBranding.model_validate(
                    {
                        **channel.branding.model_dump(),
                        **payload,
                    }
                )
                updated = ChannelPublicConfig.model_validate(
                    {
                        **channel.model_dump(),
                        "branding": branding.model_dump(),
                    }
                )
                channels.append(updated)
            if updated is None:
                raise ValueError(f"Channel {channel_id!r} not found")
            self._config = StationAppConfig.model_validate(
                {
                    **self._config.model_dump(),
                    "generated_at": datetime.now(UTC),
                    "channels": [channel.model_dump() for channel in channels],
                }
            )
            self._persist_locked()
            return updated.model_copy(deep=True)

    def _load_config(self) -> StationAppConfig:
        if self._config_path is None or not self._config_path.exists():
            return _default_station_config()
        try:
            payload = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppPlatformConfigStoreError(f"Could not read app-platform config: {exc}") from exc
        return StationAppConfig.model_validate(payload)

    def _persist_locked(self) -> None:
        if self._config_path is None:
            return
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._config_path.with_suffix(".tmp")
            tmp_path.write_text(
                self._config.model_dump_json(indent=2),
                encoding="utf-8",
            )
            if os.name != "nt":
                tmp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            tmp_path.replace(self._config_path)
            if os.name != "nt":
                self._config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            raise AppPlatformConfigStoreError(
                f"Could not write app-platform config: {exc}"
            ) from exc


def channel_public_config(profile: ChannelProfile) -> ChannelPublicConfig:
    return ChannelPublicConfig(
        channel_id=profile.channel_id,
        slug=profile.slug,
        kind=profile.kind,
        branding=ChannelBranding(
            display_name=profile.branding.display_name,
            short_name=profile.branding.short_name,
            color=profile.branding.color,
            logo_text=profile.branding.logo_text,
        ),
        outputs=[
            ChannelOutput(
                kind=_output_kind(output.kind),
                label=output.label,
                target=output.target,
                proof_boundary=output.proof_boundary,
                app_targets=list(CHANNEL_APP_TARGETS),
            )
            for output in profile.outputs
        ],
        programming_rules=profile.programming_rules,
        fallback_behavior=profile.fallback_behavior,
        live_state_url=f"/api/public/app/channels/{profile.channel_id}/live",
        schedule_feed_url=f"/api/public/app/channels/{profile.channel_id}/schedule",
        vod_catalog_url=f"/api/public/app/channels/{profile.channel_id}/catalog",
        cg_feed_url=f"/api/public/app/channels/{profile.channel_id}/cg",
        app_targets=list(CHANNEL_APP_TARGETS),
    )


def default_app_platform_config_path() -> Path | None:
    configured = os.environ.get("CIVICCAST_APP_PLATFORM_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.environ.get("CIVICCAST_ALLOW_EPHEMERAL_STORES") == "1":
        return None
    return (default_storage_dir() / _CONFIG_FILE_NAME).expanduser().resolve()


def _default_station_config() -> StationAppConfig:
    station_name = "CivicCast station"
    channels = [channel_public_config(profile) for profile in default_channel_profiles()]
    return StationAppConfig(
        station_id="civiccast-station",
        station_name=station_name,
        generated_at=datetime.now(UTC),
        default_channel_id=channels[0].channel_id,
        build_profile=AppBuildProfile(
            tier="unbranded",
            app_name=station_name,
            platform_targets=list(PUBLIC_APP_TARGETS),
            store_ready=False,
            store_notes="Reference app-platform config; branded store packaging lands later.",
        ),
        channels=channels,
        support_url="/support",
        privacy_url="/privacy",
        analytics_enabled=False,
        emergency_status_url="/api/public/cg/emergency",
    )


def _with_generated_at(config: StationAppConfig) -> StationAppConfig:
    return StationAppConfig.model_validate(
        {
            **config.model_dump(),
            "generated_at": datetime.now(UTC),
        }
    )


def _with_station_name(config: StationAppConfig, station_name: str) -> StationAppConfig:
    build_profile = config.build_profile.model_copy(update={"app_name": station_name})
    return StationAppConfig.model_validate(
        {
            **config.model_dump(),
            "station_name": station_name,
            "build_profile": build_profile.model_dump(),
        }
    )


def _output_kind(value: str) -> OutputKind:
    if value == "ndi-plan":
        return "ndi_plan"
    return cast(OutputKind, value)
