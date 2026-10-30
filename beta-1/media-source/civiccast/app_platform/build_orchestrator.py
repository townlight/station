# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""OTT app build orchestration (S12 / build step 8, slice 2).

Turns the current :class:`~civiccast.app_platform.models.StationAppConfig` into a
verified, recorded build for one app target: run the build (an **injected**
runner seam — the default invokes the in-tree generic-shell build, the node
toolchain), SHA-256 the produced artifact, and append an immutable
:class:`~civiccast.app_platform.build_models.AppBuildRecord` to the
:class:`~civiccast.app_platform.build_store.AppBuildStore`.

The runner is injected so the orchestration core (snapshot → hash → record) is
unit-testable offline; the default node-toolchain runner is the platform-coupled
seam (its real device/emulator proof is the OTT lab/store lane, not offline
tests). Proof boundary: a record claims only "local artifact, SHA-256 verified".
"""

from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from civiccast.app_platform.build_models import AppBuildRecord
from civiccast.app_platform.build_store import AppBuildStore
from civiccast.app_platform.models import AppBuildTier, AppTarget, StationAppConfig

# The app-shell targets that produce a buildable artifact (cg / epg are config
# feeds, not app shells).
BUILDABLE_APP_TARGETS: frozenset[str] = frozenset(
    {"web_pwa", "roku", "tvos", "fire_tv", "android_tv", "android_mobile", "ios_ipados"}
)

__all__ = [
    "BuildOrchestrationError",
    "BuildRunner",
    "BuiltArtifact",
    "default_shell_build_runner",
    "orchestrate_build",
    "sha256_file",
]


class BuildOrchestrationError(RuntimeError):
    """Raised when a build target is invalid or the artifact is missing."""


@dataclass
class BuiltArtifact:
    """What a build runner produces for one target."""

    artifact_path: str
    entry_point: str
    manifest_json: dict[str, Any] = field(default_factory=dict)


# Produce the artifact for one app target under work_dir; returns its descriptor.
BuildRunner = Callable[[str, Path], BuiltArtifact]


def sha256_file(path: Path) -> str:
    """Stream-hash a file to a lowercase hex SHA-256 (matches the record validator)."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def orchestrate_build(
    *,
    config: StationAppConfig,
    app_target: AppTarget,
    store: AppBuildStore,
    work_dir: Path,
    build_runner: BuildRunner,
    build_tier: AppBuildTier | None = None,
    built_by: str,
    clock: Callable[[], datetime] | None = None,
    id_factory: Callable[[], str] | None = None,
) -> AppBuildRecord:
    """Build one app target, verify its artifact, and record it. Returns the record."""

    if app_target not in BUILDABLE_APP_TARGETS:
        raise BuildOrchestrationError(
            f"{app_target!r} is not a buildable app shell target "
            f"(one of {sorted(BUILDABLE_APP_TARGETS)})"
        )
    now = (clock or (lambda: datetime.now(UTC)))()
    new_id = (id_factory or _default_id)()
    work_dir.mkdir(parents=True, exist_ok=True)

    artifact = build_runner(app_target, work_dir)
    artifact_path = Path(artifact.artifact_path)
    if not artifact_path.is_file():
        raise BuildOrchestrationError(
            f"build runner did not produce an artifact at {artifact.artifact_path!r}"
        )

    profile = config.build_profile
    record = AppBuildRecord(
        record_id=new_id,
        station_id=config.station_id,
        app_target=app_target,
        build_tier=build_tier or profile.tier,
        app_name=profile.app_name,
        icon_url=profile.icon_url,
        splash_url=profile.splash_url,
        channels=[
            {"channel_id": channel.channel_id, "branding": channel.branding.model_dump(mode="json")}
            for channel in config.channels
        ],
        artifact_path=str(artifact_path),
        artifact_sha256=sha256_file(artifact_path),
        entry_point=artifact.entry_point,
        manifest_json=artifact.manifest_json,
        built_at=now,
        built_by=built_by,
    )
    return store.add_build(record)


def _default_id() -> str:
    token = secrets.token_urlsafe(9).replace("-", "").replace("_", "")
    return f"appbld_{token}"


def default_shell_build_runner(shells_dir: Path) -> BuildRunner:
    """The platform-coupled seam: run the in-tree generic-shell node build, then
    zip the requested target's output. Device/emulator proof is the OTT lab lane.
    """

    def run(app_target: str, work_dir: Path) -> BuiltArtifact:
        # "node" is resolved from PATH in the build environment.
        job_dir = work_dir.resolve()
        build_output_dir = job_dir / "shell-build"
        build_cmd = [
            "node",
            str(shells_dir / "scripts" / "build-targets.mjs"),
            "--out-dir",
            str(build_output_dir),
        ]
        try:
            subprocess.run(build_cmd, check=True, cwd=str(shells_dir))  # noqa: S603 - fixed in-tree script, no user input
        except FileNotFoundError as exc:
            raise BuildOrchestrationError(
                "required app build tool 'node' is not available in PATH"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise BuildOrchestrationError("app shell build command failed") from exc
        target_dir_name = app_target.replace("_", "-")
        build_root = build_output_dir
        dist_target = build_root / "targets" / target_dir_name
        if not dist_target.is_dir():
            raise BuildOrchestrationError(f"shell build produced no {target_dir_name!r} target")
        manifest_path = dist_target / "manifest.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        )
        zip_path = job_dir / f"{app_target}.zip"
        _zip_shell_artifact(build_root, dist_target, zip_path)
        manifest_entry = manifest.get("entryPoint") or manifest.get("entry") or "index.html"
        return BuiltArtifact(
            artifact_path=str(zip_path),
            entry_point=PurePosixPath("targets", target_dir_name, str(manifest_entry)).as_posix(),
            manifest_json=manifest,
        )

    return run


def _zip_shell_artifact(build_root: Path, target_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for source_dir in (build_root / "src", build_root / "fixtures", target_dir):
            for path in sorted(source_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(build_root).as_posix())
