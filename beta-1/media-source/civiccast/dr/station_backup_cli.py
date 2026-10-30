# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Typer commands for routine encrypted station backup and offline restore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from civiccast.dr.station_backup import StationBackupService

backup_app = typer.Typer(
    name="backup",
    help="Create, verify, inventory, or restore encrypted station backups.",
    no_args_is_help=True,
)


def _service() -> StationBackupService:
    # The runtime adapter owns path resolution, keyring access, and PostgreSQL
    # tool discovery so CLI commands cannot guess service locations.
    from civiccast.dr.station_backup_runtime import build_station_backup_service

    return build_station_backup_service()


def _passphrase(*, confirm: bool = False) -> str:
    phrase: str = typer.prompt("Recovery passphrase", hide_input=True)
    if len(phrase) < 12:
        raise typer.BadParameter("must contain at least 12 characters")
    if confirm:
        repeated: str = typer.prompt("Confirm recovery passphrase", hide_input=True)
        if phrase != repeated:
            raise typer.BadParameter("recovery passphrases do not match")
    return phrase


@backup_app.command("create")
def create_backup() -> None:
    """Create an encrypted backup using the configured destination and media roots."""

    artifact = _service().create_backup(_passphrase(confirm=True))
    typer.echo(
        f"Created encrypted station backup: {artifact.path.name} ({artifact.size_bytes} bytes)"
    )


@backup_app.command("verify")
def verify_backup(archive: Path) -> None:
    """Authenticate and verify every file in an archive."""

    result = _service().verify_backup(archive, _passphrase())
    typer.echo(
        f"Verified {result.backup_id}: {result.database_engine}, "
        f"{result.files_verified} files authenticated"
    )


@backup_app.command("inventory")
def inventory_backup(archive: Path) -> None:
    """Show the source paths that must be mapped on a replacement station."""

    typer.echo(json.dumps(_service().inspect_backup(archive, _passphrase()), indent=2))


@backup_app.command("restore")
def restore_backup(
    archive: Path,
    target_paths_json: Annotated[
        Path,
        typer.Option(
            "--target-paths-json", help="JSON map of old absolute paths to new absolute paths"
        ),
    ],
    confirmation: Annotated[str, typer.Option("--confirmation", help='must be exactly "RESTORE"')],
    target_database_url: Annotated[
        str | None,
        typer.Option(
            "--target-database-url",
            envvar="CIVICAST_TARGET_DATABASE_URL",
            help="replacement database URL (prefer protected station configuration or environment)",
        ),
    ] = None,
) -> None:
    """Restore while the station service and database writers are stopped."""

    if confirmation != "RESTORE":
        raise typer.BadParameter('must be exactly "RESTORE" after checking the replacement paths')
    raw: Any = json.loads(target_paths_json.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(
        isinstance(source, str) and isinstance(target, str) for source, target in raw.items()
    ):
        raise typer.BadParameter("path map must be a JSON object of absolute path strings")
    mappings = {source: Path(target) for source, target in raw.items()}
    phrase = _passphrase()

    from civiccast.dr.station_backup_runtime import offline_station_restore

    service = _service()
    effective_database_url = target_database_url or service.database_url
    with offline_station_restore(
        installed_database_url=service.database_url,
        target_database_url=effective_database_url,
    ):
        result = service.restore_backup(
            archive,
            phrase,
            target_database_url=effective_database_url,
            target_paths=mappings,
            writers_stopped=True,
        )
    typer.echo(
        f"Restored {result.backup_id}: {result.database_engine} database and "
        f"{result.files_restored} mapped files"
    )
