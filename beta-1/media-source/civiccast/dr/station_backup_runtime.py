# SPDX-License-Identifier: Apache-2.0
"""Bind operator backups to the installed station's actual data and secrets."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from civiccast.dr.station_backup import StationBackupService
from civiccast.native.station_secrets import (
    NAMESPACES,
    StationSecretVault,
    _unprotect,
    write_protected_secret,
)

if TYPE_CHECKING:
    from civiccast.native.supervisor.install_layout import InstallLayout
    from civiccast.native.upgrade.pg_lifecycle import PgLifecyclePaths

_SERVICE_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "CIVICCAST_STATION_ID",
        "CIVICCAST_PUBLIC_BASE_URL",
        "CIVICCAST_PUBLIC_ANALYTICS_KEY",
        "CIVICCAST_PUBLIC_ANALYTICS_ALLOWED_ORIGINS",
        "CIVICCAST_TRUSTED_PROXY_CIDRS",
        "CIVICCAST_TRUSTED_PROXY_CIDRS_EXACT",
        "CIVICCAST_TRUST_PRIVATE_PROXIES",
        "CIVICCAST_CDN_PROVIDER",
        "CIVICCAST_SMTP_HOST",
        "CIVICCAST_SMTP_FROM",
        "CIVICCAST_SMTP_PORT",
        "CIVICCAST_SMTP_USERNAME",
        "CIVICCAST_SMTP_PASSWORD",
        "CIVICCAST_SMTP_STARTTLS",
        "CIVICCAST_SUBSCRIBE_TOKEN_SECRET",
        "CIVICCAST_SUBSCRIBE_ENCRYPTION_KEY",
        "CIVICCAST_SUBSCRIBE_LEGACY_TOKEN_SECRETS",
        "CIVICCAST_SUBSCRIBE_LEGACY_ENCRYPTION_KEYS",
        "CIVICCAST_SUBSCRIBER_WEBHOOK_SECRET",
        "CIVICCAST_IA_ACCESS_KEY",
        "CIVICCAST_IA_SECRET_KEY",
        "CIVICCAST_IA_COLLECTION",
        "CIVICCAST_IA_ITEM_PREFIX",
        "CIVICCAST_INTERNET_ARCHIVE_ACCESS_KEY",
        "CIVICCAST_AGENDA_SOURCE_TOKEN",
        "CIVICCAST_PROVIDER_API_KEY",
        "CIVICCAST_YOUTUBE_CLIENT_ID",
        "CIVICCAST_YOUTUBE_CLIENT_SECRET",
        "CIVICCAST_YOUTUBE_REFRESH_TOKEN",
        "CIVICCAST_YOUTUBE_PRIVACY",
        "CIVICCAST_R2_ACCOUNT_ID",
        "CIVICCAST_R2_ACCESS_KEY_ID",
        "CIVICCAST_R2_SECRET_ACCESS_KEY",
        "CIVICCAST_R2_BUCKET",
        "CIVICCAST_R2_PUBLIC_BASE_URL",
        "CIVICCAST_BUNNY_STORAGE_ZONE",
        "CIVICCAST_BUNNY_ACCESS_KEY",
        "CIVICCAST_BUNNY_CDN_HOSTNAME",
        "CIVICCAST_FASTLY_REGION",
        "CIVICCAST_FASTLY_ACCESS_KEY_ID",
        "CIVICCAST_FASTLY_SECRET_ACCESS_KEY",
        "CIVICCAST_FASTLY_BUCKET",
        "CIVICCAST_FASTLY_PUBLIC_BASE_URL",
        "CIVICCAST_AKAMAI_REGION",
        "CIVICCAST_AKAMAI_ACCESS_KEY_ID",
        "CIVICCAST_AKAMAI_SECRET_ACCESS_KEY",
        "CIVICCAST_AKAMAI_BUCKET",
        "CIVICCAST_AKAMAI_PUBLIC_BASE_URL",
        "CIVICCAST_OBS_WEBSOCKET_PASSWORD",
        "CIVICCAST_TSA_URL",
        "CIVICCAST_TSA_POLICY_OID",
        "CIVICCAST_TSA_ENABLE",
        "CIVICCAST_CONTROL_ROOM_TSR_URL",
        "CIVICCAST_REMOTE_CONTRIBUTION_VDO_URL",
    }
)
_SERVICE_PATH_ENVIRONMENT = frozenset(
    {
        "CIVICCAST_CONFIG_DIR",
        "CIVICCAST_CERT_ROOT",
        "CIVICCAST_ALERT_CREDENTIALS_FILE",
        "CIVICCAST_SUBSCRIBE_SECRETS_FILE",
        "CIVICCAST_NETWORK_SETTINGS_PATH",
    }
)
_PORTABLE_ENVIRONMENT_MEMBER = "station-service-environment.json"


def build_station_backup_service() -> StationBackupService:
    from civiccast.native.supervisor.install_layout import resolve_install_layout
    from civiccast.native.supervisor.service_env import ensure_database_url_env

    if not os.environ.get("DATABASE_URL"):
        ensure_database_url_env()
    database_url = os.environ["DATABASE_URL"]
    layout = resolve_install_layout()
    root = layout.civiccast_data_root
    profile_root = root / "profile"
    path_environment = _service_environment()
    state = Path(
        path_environment.get("CIVICCAST_STATION_STATE_PATH")
        or Path(path_environment.get("LOCALAPPDATA", str(profile_root)))
        / "CivicCast"
        / "station-state.json"
    ).expanduser()
    backup_state = root / "config" / "station-backups.json"

    def config_files() -> list[Path]:
        environment = _service_environment()
        config_root = Path(environment.get("CIVICCAST_CONFIG_DIR") or profile_root / ".civiccast")
        cert_root = Path(environment.get("CIVICCAST_CERT_ROOT") or config_root / "certs")
        network_path = Path(
            environment.get("CIVICCAST_NETWORK_SETTINGS_PATH")
            or state.with_name("network-settings.json")
        )
        files = {state, network_path}
        for directory in (config_root, cert_root, root / "config"):
            if directory.is_dir():
                files.update(p for p in directory.rglob("*") if p.is_file())
        for name in ("CIVICCAST_ALERT_CREDENTIALS_FILE", "CIVICCAST_SUBSCRIBE_SECRETS_FILE"):
            if environment.get(name):
                files.add(Path(environment[name]).expanduser())
        if state.parent.is_dir():
            files.update(state.parent.glob("*.json"))
        # Backup schedule/progress and machine-bound scheduler key never travel
        # as station configuration.
        return sorted(path for path in files if path != backup_state)

    vault = StationSecretVault()
    passphrase_path = root / "credentials" / "backup-passphrase.dpapi"

    def read_passphrase() -> str:
        if not passphrase_path.is_file():
            raise RuntimeError("Set a recovery passphrase before enabling scheduled backups.")
        return _unprotect(passphrase_path.read_bytes()).decode("utf-8")

    def write_passphrase(value: str) -> None:
        write_protected_secret(passphrase_path, value.encode("utf-8"))

    def export_credentials() -> Mapping[str, str | bytes]:
        records: dict[str, str | bytes] = dict(vault.export_portable())
        records[_PORTABLE_ENVIRONMENT_MEMBER] = json.dumps(
            _portable_service_environment(_service_environment()), sort_keys=True
        ).encode("utf-8")
        return records

    def import_credentials(records: Mapping[str, bytes]) -> None:
        validate_portable_credentials(records)
        vault.import_portable(records)
        environment_record = records.get(_PORTABLE_ENVIRONMENT_MEMBER)
        if environment_record is not None:
            try:
                values = json.loads(environment_record)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Backup contains invalid station environment settings") from exc
            _restore_portable_service_environment(values)

    def validate_credentials(records: Mapping[str, bytes]) -> None:
        validate_portable_credentials(records)

    pg_bin = layout.pg_ctl_path.parent
    return StationBackupService(
        database_url=database_url,
        destination=root / "backups",
        config_paths=config_files(),
        config_paths_provider=config_files,
        state_path=root / "config" / "station-backups.json",
        credential_exporter=export_credentials,
        credential_importer=import_credentials,
        credential_validator=validate_credentials,
        scheduled_passphrase_setter=write_passphrase,
        scheduled_passphrase_provider=read_passphrase,
        scheduled_passphrase_available=passphrase_path.is_file,
        pg_dump_command=[str(pg_bin / "pg_dump.exe")],
        pg_dumpall_command=[str(pg_bin / "pg_dumpall.exe")],
        pg_restore_command=[str(pg_bin / "pg_restore.exe")],
    )


@contextmanager
def offline_station_restore(
    *, installed_database_url: str | None = None, target_database_url: str | None = None
) -> Iterator[None]:
    """Hold the station mutex, restoring managed PostgreSQL only when needed."""
    from civiccast.native.admin_recovery import _offline_station

    with _offline_station() as state_path:
        # CLI runs as an administrator, so use the installed service's settings
        # path instead of the administrator's unrelated personal profile.
        env_names = (*_SERVICE_PATH_ENVIRONMENT, "CIVICCAST_STATION_STATE_PATH")
        previous = {name: os.environ.get(name) for name in env_names}
        service_environment = _read_installed_service_environment()
        for name in _SERVICE_PATH_ENVIRONMENT:
            os.environ.pop(name, None)
        for name in _SERVICE_PATH_ENVIRONMENT:
            if service_environment.get(name):
                os.environ[name] = service_environment[name]
        os.environ["CIVICCAST_STATION_STATE_PATH"] = str(state_path)
        if not os.environ.get("CIVICCAST_CONFIG_DIR"):
            profile = (
                Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "CivicCast" / "profile"
            )
            if profile.is_dir():
                os.environ["CIVICCAST_CONFIG_DIR"] = str(profile / ".civiccast")
        try:
            if installed_database_url and target_database_url:
                from civiccast.native.supervisor.install_layout import resolve_install_layout

                with _managed_postgres_for_restore(
                    installed_database_url,
                    target_database_url,
                    resolve_install_layout(),
                ):
                    yield
            else:
                yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


@contextmanager
def _managed_postgres_for_restore(
    installed_database_url: str,
    target_database_url: str,
    layout: InstallLayout,
    *,
    status_probe: Callable[[PgLifecyclePaths], bool] | None = None,
    start: Callable[[PgLifecyclePaths], None] | None = None,
    stop: Callable[[PgLifecyclePaths], None] | None = None,
) -> Iterator[None]:
    """Start only this station's stopped, local PostgreSQL cluster for restore."""

    paths = _managed_postgres_paths(installed_database_url, target_database_url, layout)
    if paths is None:
        yield
        return
    if status_probe is None or start is None or stop is None:
        from civiccast.native.upgrade.pg_lifecycle import (
            real_start_postgres,
            real_stop_postgres,
        )

        status_probe = status_probe or _managed_postgres_is_running
        start = start or real_start_postgres
        stop = stop or real_stop_postgres
    if status_probe(paths):
        raise RuntimeError(
            "managed PostgreSQL must be stopped before offline restore can take ownership"
        )
    start_attempted = False
    started = False
    try:
        start_attempted = True
        start(paths)
        started = True
        yield
    except BaseException as operation_error:
        should_stop = started
        if start_attempted and not started:
            try:
                should_stop = bool(status_probe(paths))
            except Exception as status_error:
                operation_error.add_note(
                    f"Could not confirm PostgreSQL stopped after start failure: {status_error}"
                )
        if should_stop:
            try:
                stop(paths)
            except Exception as stop_error:
                operation_error.add_note(
                    f"PostgreSQL cleanup stop also failed; leave the station service stopped: {stop_error}"
                )
        raise
    else:
        if started:
            stop(paths)


def _managed_postgres_paths(
    installed_database_url: str, target_database_url: str, layout: InstallLayout
) -> PgLifecyclePaths | None:
    from sqlalchemy.engine import make_url

    from civiccast.db.url import normalize_database_url
    from civiccast.native.upgrade.pg_lifecycle import PgLifecyclePaths

    def endpoint(value: str) -> tuple[str, int, str] | None:
        try:
            parsed = make_url(normalize_database_url(value))
        except Exception:
            return None
        if not parsed.drivername.startswith("postgresql") or not parsed.host:
            return None
        host = parsed.host.casefold().strip("[]")
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return None
        return ("loopback", parsed.port or 5432, parsed.host)

    installed = endpoint(installed_database_url)
    target = endpoint(target_database_url)
    if installed is None or target is None or installed[:2] != target[:2]:
        return None
    return PgLifecyclePaths(
        pg_ctl_path=str(layout.pg_ctl_path),
        data_dir=str(layout.postgres_data_dir),
        host=target[2],
        port=target[1],
    )


def _managed_postgres_is_running(paths: PgLifecyclePaths) -> bool:
    from civiccast.native.pg_ctl_exec import run_pg_ctl_argv

    result = run_pg_ctl_argv(
        [paths.pg_ctl_path, "status", "-D", paths.data_dir],
        timeout_seconds=30.0,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 3:
        return False
    raise RuntimeError(
        f"pg_ctl status could not confirm managed PostgreSQL state (exit {result.returncode})"
    )


def _read_installed_service_environment() -> dict[str, str]:
    """Read the service-scoped Environment value, never machine-wide settings."""

    if os.name != "nt":
        return {}
    import winreg

    from civiccast.native.supervisor.config import SERVICE_NAME

    key_path = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        ) as key:
            raw, kind = winreg.QueryValueEx(key, "Environment")
    except FileNotFoundError:
        return {}
    if kind != winreg.REG_MULTI_SZ or not isinstance(raw, list):
        raise RuntimeError("The installed station service environment is invalid")
    return _parse_environment_entries(raw)


def _service_environment() -> dict[str, str]:
    # An elevated offline CLI must use the installed service's settings rather
    # than accidentally backing up values from the administrator's profile.
    installed = _read_installed_service_environment()
    return installed if installed else dict(os.environ)


def _portable_service_environment(values: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in values.items()
        if name in _SERVICE_ENVIRONMENT_ALLOWLIST and isinstance(value, str)
    }


def _restore_portable_service_environment(raw: object) -> None:
    values = _validated_portable_service_environment(raw)
    if os.name != "nt":
        raise RuntimeError("Restoring station Environment settings requires Windows")
    import winreg

    from civiccast.native.supervisor.config import SERVICE_NAME

    key_path = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"
    access = winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, access) as key:
        try:
            entries, kind = winreg.QueryValueEx(key, "Environment")
            if kind != winreg.REG_MULTI_SZ or not isinstance(entries, list):
                raise RuntimeError("The installed station service environment is invalid")
        except FileNotFoundError:
            entries = []
        retained = [
            entry
            for entry in entries
            if not isinstance(entry, str)
            or entry.partition("=")[0] not in _SERVICE_ENVIRONMENT_ALLOWLIST
        ]
        retained.extend(f"{name}={value}" for name, value in sorted(values.items()))
        winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ, retained)


def _validated_portable_service_environment(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict) or any(
        not isinstance(name, str)
        or name not in _SERVICE_ENVIRONMENT_ALLOWLIST
        or not isinstance(value, str)
        or "\0" in value
        for name, value in raw.items()
    ):
        raise ValueError("Backup contains unsupported station environment settings")
    return raw


def validate_portable_credentials(records: Mapping[str, bytes]) -> None:
    keyring = records.get("station-keyring.json")
    if keyring is not None:
        try:
            raw_keyring = json.loads(keyring)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Backup contains invalid station credentials") from exc
        if not isinstance(raw_keyring, dict) or any(
            name not in NAMESPACES
            or not isinstance(entries, dict)
            or any(
                not isinstance(user, str) or not isinstance(secret, str)
                for user, secret in entries.items()
            )
            for name, entries in raw_keyring.items()
        ):
            raise ValueError("Backup contains invalid station credentials")
    environment = records.get(_PORTABLE_ENVIRONMENT_MEMBER)
    if environment is not None:
        try:
            raw_environment = json.loads(environment)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Backup contains invalid station environment settings") from exc
        _validated_portable_service_environment(raw_environment)


def _parse_environment_entries(entries: list[object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, str):
            continue
        name, separator, value = entry.partition("=")
        if separator and name:
            result[name] = value
    return result
