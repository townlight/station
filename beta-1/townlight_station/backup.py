# SPDX-License-Identifier: AGPL-3.0-only
"""Coherent recorder backup/restore, excluding credentials and native runtimes."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from .boundary import private_path, sha256
from .store import Store, recordings


def _tool(settings, name, args):
    import os
    db = make_url(settings.database_url)
    env = dict(os.environ, PGPASSWORD=db.password)
    root = Path(os.environ['TOWNLIGHT_STATION_POSTGRES_BIN'])
    subprocess.run([str(root/(name+'.exe')), '-h', db.host, '-p', str(db.port), '-U', db.username,
                    '-d', db.database, *args], env=env, capture_output=True, check=True,
                   timeout=1800, creationflags=subprocess.CREATE_NO_WINDOW)


@contextmanager
def maintenance(store):
    # A session lock spans external pg_dump/pg_restore without retaining table locks.
    with store.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        connection.execute(sa.text('SELECT pg_advisory_lock(1953721717)'))
        try:
            if connection.scalar(sa.select(sa.func.count()).select_from(recordings)
                                 .where(recordings.c.status.in_(('queued', 'running')))):
                raise ValueError('Finish active and queued recordings before backup or restore')
            yield connection
        finally:
            connection.execute(sa.text('SELECT pg_advisory_unlock(1953721717)'))


def backup(settings, destination):
    destination = Path(destination).absolute()
    private_path(destination)
    if destination.resolve().is_relative_to(settings.data.resolve()):
        raise ValueError('Backup must be outside the recorder data directory')
    destination.mkdir(parents=True, exist_ok=False)
    store = Store(settings.database_url)
    try:
        with maintenance(store):
            _tool(settings, 'pg_dump', ['--format=custom', '--schema=station', '-f', str(destination/'station.dump')])
            for folder in ('recordings', 'ingest'):
                source = private_path(settings.data, folder)
                for file in source.rglob('*'):
                    private_path(source, *file.relative_to(source).parts)
                shutil.copytree(source, destination/folder)
            for name in ('live-sources.json', 'local-input.json', 'civiccast-feeds.json'):
                source = private_path(settings.data, name)
                if source.is_file():
                    shutil.copy2(source, destination/name)
            files = {p.relative_to(destination).as_posix(): {'sha256': sha256(p), 'size_bytes': p.stat().st_size}
                     for p in destination.rglob('*') if p.is_file()}
            manifest = {'product': 'townlight-station-backup', 'version': 1,
                        'created_at': datetime.now(timezone.utc).isoformat(), 'files': files}
            (destination/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
            return {'backup_directory': str(destination), 'files': len(files),
                    'message': 'Station database and recording files backed up with SHA-256 manifest. Credentials stay with this installation.'}
    finally:
        store.engine.dispose()


def verify_backup(source):
    source = Path(source).absolute()
    private_path(source)
    manifest = json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('product') != 'townlight-station-backup' or manifest.get('version') != 1:
        raise ValueError('Not a supported Station backup')
    files = manifest.get('files')
    if not isinstance(files, dict) or not 1 <= len(files) <= 100000 or 'station.dump' not in files:
        raise ValueError('Invalid Station backup manifest')
    allowed = {'station.dump', 'live-sources.json', 'local-input.json', 'civiccast-feeds.json'}
    for name, expected in files.items():
        relative = Path(name)
        if (not name or ':' in name or '\\' in name or relative.is_absolute() or '..' in relative.parts
            or (name not in allowed and relative.parts[0] not in ('recordings', 'ingest'))):
            raise ValueError('Unsafe backup member')
        path = private_path(source, *relative.parts)
        if not path.is_file() or path.stat().st_size != expected['size_bytes'] or sha256(path) != expected['sha256']:
            raise ValueError('Station backup file is missing or changed: '+name)
    actual = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
    if actual != set(files) | {'manifest.json'}:
        raise ValueError('Station backup has unlisted files')
    return manifest


def restore(settings, source):
    source = Path(source).absolute()
    manifest = verify_backup(source)
    # Recovery copy is made before any replacement, without overwriting older evidence.
    recovery = settings.data.parent/('station-pre-restore-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
    backup(settings, recovery)
    store = Store(settings.database_url)
    try:
        with maintenance(store):
            required = sum(v['size_bytes'] for v in manifest['files'].values())
            if shutil.disk_usage(settings.data).free < required + settings.reserve_bytes:
                raise ValueError('Insufficient restore staging space; existing records were not replaced')
            with tempfile.TemporaryDirectory(prefix='restore-', dir=settings.data/'temp') as staging:
                staging = Path(staging)
                for name in manifest['files']:
                    if name == 'station.dump':
                        continue
                    dst = staging/name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source/name, dst)
                # Native tool applies the dump in one transaction; no credentials restored.
                _tool(settings, 'pg_restore', ['--clean', '--if-exists', '--no-owner', '--no-privileges',
                                              '--single-transaction', '--schema=station', str(source/'station.dump')])
                for name in ('recordings', 'ingest', 'live-sources.json', 'local-input.json', 'civiccast-feeds.json'):
                    target = private_path(settings.data, name)
                    incoming = staging/name
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                    if incoming.exists():
                        incoming.replace(target)
                    elif name in ('recordings', 'ingest'):
                        target.mkdir()
            return {'restored': True, 'recovery_directory': str(recovery),
                    'message': 'Station backup restored. Recheck chamber inputs before the next meeting.'}
    finally:
        store.engine.dispose()
