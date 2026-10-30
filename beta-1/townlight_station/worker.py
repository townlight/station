# SPDX-License-Identifier: AGPL-3.0-only
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4
import sqlalchemy as sa
from .boundary import Settings, private_path, sha256
from .store import Store, playouts, recordings, schedules, station_state

# A scheduled start older than this is not silently aired as if on time.
SCHEDULE_GRACE = timedelta(minutes=10)


def run(once=False):
    cfg = Settings.from_env()
    cfg.initialize_storage()
    cfg.verify_media()
    sys.path.insert(0, str(cfg.media_source))
    from civiccast.platform.child_containment import contain_own_descendants
    containment = contain_own_descendants()
    if not containment.active:
        raise RuntimeError('Station worker requires verified Windows child containment: ' + containment.detail)
    store = Store(cfg.database_url)
    store.ready()
    try:
        with store.worker_lock() as acquired:
            if not acquired:
                raise RuntimeError('Another Station worker already owns this database')
            # The former worker no longer holds the DB lock; its kill-on-close
            # job reaps children. Interrupted attempts require an explicit retry.
            with store.engine.begin() as conn:
                for stale in conn.execute(sa.select(recordings).where(recordings.c.status == 'running')).mappings():
                    detail = stale['details']
                    if detail['attempts']:
                        detail['attempts'][-1].update(status='interrupted',
                            finished_at=datetime.now(timezone.utc).isoformat())
                    conn.execute(recordings.update().where(recordings.c.id == stale['id'])
                        .values(status='interrupted', details=detail))
                for stale in conn.execute(sa.select(playouts).where(playouts.c.status == 'running')).mappings():
                    detail = stale['details']
                    if detail.get('attempts'):
                        detail['attempts'][-1].update(status='interrupted',
                            finished_at=datetime.now(timezone.utc).isoformat())
                    detail['on_air'] = False
                    conn.execute(playouts.update().where(playouts.c.id == stale['id'])
                        .values(status='interrupted', details=detail))
                # An interrupted on-air playout must not look like it is still airing.
                current = conn.scalar(sa.select(station_state.c.on_air).where(station_state.c.id == 1)) or {}
                if current.get('mode') == 'playout':
                    conn.execute(station_state.update().where(station_state.c.id == 1).values(on_air={
                        'mode': 'schedule', 'recovered_at': datetime.now(timezone.utc).isoformat(),
                        'recovered_from': current}))
            dispatched = dispatch_schedules(cfg, store)
            while True:
                row = claim(store.engine, recordings, 'queued')
                if row:
                    process(cfg, store, row)
                playout = claim(store.engine, playouts, 'queued')
                if playout:
                    process_playout(cfg, store, playout)
                if once:
                    return
                if not row and not playout:
                    dispatch_schedules(cfg, store)
                    time.sleep(0.5)
    finally:
        store.engine.dispose()


def claim(engine, table, status):
    with engine.begin() as conn:
        row = conn.execute(sa.select(table).where(table.c.status == status)
            .order_by(table.c.created_at).with_for_update(skip_locked=True).limit(1)).mappings().first()
        if row:
            conn.execute(table.update().where(table.c.id == row['id']).values(status='running'))
    return dict(row) if row else None


def dispatch_schedules(cfg, store):
    """Turn due durable schedules into normal recording jobs. Exactly once."""
    now = datetime.now(timezone.utc)
    made = []
    with store.engine.begin() as conn:
        conn.execute(sa.text('SELECT pg_advisory_xact_lock(1953721717)'))
        due = conn.execute(sa.select(schedules).where(schedules.c.status == 'scheduled',
            schedules.c.start_at <= now).order_by(schedules.c.start_at)
            .with_for_update(skip_locked=True).limit(10)).mappings().all()
        for schedule in due:
            details = dict(schedule['details'])
            late = now - schedule['start_at']
            if late > SCHEDULE_GRACE:
                details.update(missed_at=now.isoformat(), missed_by_seconds=int(late.total_seconds()))
                conn.execute(schedules.update().where(schedules.c.id == schedule['id'])
                    .values(status='missed', details=details))
                continue
            identity = str(uuid4())
            folder = private_path(cfg.data, 'recordings', identity)
            folder.mkdir()
            captured = folder / 'source.media'
            try:
                if details.get('source_file'):
                    source = private_path(cfg.data, 'ingest', details['source_file'])
                    with source.open('rb') as incoming, captured.open('xb') as out:
                        shutil.copyfileobj(incoming, out, 1024 * 1024)
                    input_hash = sha256(captured)
                else:
                    input_hash = None
                caption_hash = None
                if details.get('captions_file'):
                    sidecar = private_path(cfg.data, 'ingest', details['captions_file'])
                    with sidecar.open('rb') as incoming, (folder / 'captions.srt').open('xb') as out:
                        shutil.copyfileobj(incoming, out, 1024 * 1024)
                    caption_hash = sha256(folder / 'captions.srt')
            except Exception as exc:
                shutil.rmtree(folder, ignore_errors=True)
                details.update(dispatch_error=type(exc).__name__, dispatch_failed_at=now.isoformat())
                conn.execute(schedules.update().where(schedules.c.id == schedule['id'])
                    .values(status='failed', details=details))
                continue
            live = details.get('live_source')
            record_details = {'meeting': details['meeting'], 'agenda': details['agenda'],
                'input_sha256': input_hash, 'caption_sha256': caption_hash,
                'caption_policy': 'replace-with-supplied-sidecar' if caption_hash else 'preserve-embedded',
                'encoding_profile': cfg.encoding_profile(), 'visibility': 'private',
                'attempts': [], 'live_source': live, 'capture_kind': 'live' if live else 'file',
                'schedule_id': schedule['id'], 'scheduled_start': schedule['start_at'].isoformat()}
            conn.execute(recordings.insert().values(id=identity, creator=schedule['creator'],
                idempotency_key=f'schedule:{schedule["id"]}', request_hash=schedule['request_hash'],
                meeting_id=schedule['meeting_id'], status='queued', details=record_details))
            details.update(dispatched_at=now.isoformat(), recording_id=identity)
            conn.execute(schedules.update().where(schedules.c.id == schedule['id'])
                .values(status='dispatched', details=details))
            made.append(identity)
    return made


def process(cfg, store, row):
    identity = row['id']
    folder = private_path(cfg.data, 'recordings', identity)
    details = row['details']
    attempt = {'id': str(uuid4()), 'started_at': datetime.now(timezone.utc).isoformat()}
    details['attempts'].append(attempt)
    store.update(identity, 'running', details)
    output = folder / (attempt['id'] + '.ts')
    log = output.with_suffix('.log')
    child = None
    try:
        live = details.get('live_source')
        if not live and sha256(folder / 'source.media') != details['input_sha256']:
            raise ValueError('Retained source integrity failure')
        if details.get('caption_sha256') and sha256(folder / 'captions.srt') != details['caption_sha256']:
            raise ValueError('Retained captions integrity failure')
        cfg.verify_media()
        with log.open('x', encoding='utf-8') as stream:
            child = subprocess.Popen([str(cfg.media_python), '-B', str(Path(__file__).with_name('media_job.py')),
                str(cfg.media_source), str(folder), str(output), json.dumps(details['encoding_profile']), json.dumps(live)], env=cfg.worker_environment(),
                stdout=stream, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            attempt['pid'] = child.pid
            store.update(identity, 'running', details)
            if not live:
                code = child.wait(timeout=cfg.max_seconds)
            else:
                started = progress = time.monotonic()
                size = 0
                stopping = None
                while child.poll() is None:
                    now = time.monotonic()
                    current_size = output.stat().st_size if output.exists() else 0
                    if current_size > size:
                        size, progress = current_size, now
                    current = store.get(identity)
                    if not stopping and (current['control'].get('stop_requested_at') or now-started >= cfg.max_seconds):
                        attempt['end_reason'] = 'operator-stop' if current['control'].get('stop_requested_at') else 'duration-limit'
                        output.with_suffix('.stop').write_text(attempt['end_reason'], encoding='ascii')
                        stopping = now
                    if current_size > cfg.max_bytes:
                        raise RuntimeError('Live capture exceeded the configured byte limit')
                    if stopping and now-stopping > 15:
                        raise RuntimeError('Live capture did not finish draining')
                    if not stopping and now-progress > (20 if size == 0 else 10):
                        raise RuntimeError('Live input absent or stalled; partial evidence retained')
                    time.sleep(.2)
                code = child.returncode
        attempt['exit_code'] = code
        if code or not output.is_file():
            raise RuntimeError('Media worker failed; retained attempt log requires review')
        text = log.read_text(encoding='utf-8', errors='replace')
        if "'error': None" not in text or "'teardown_clean': True" not in text:
            raise RuntimeError('Media worker did not confirm clean output/teardown')
        probe = subprocess.run([str(cfg.ffprobe), '-v', 'error', '-show_streams', '-show_format',
            '-of', 'json', str(output)], capture_output=True, text=True, check=True, timeout=30)
        info = json.loads(probe.stdout)
        if not {'audio', 'video'} <= {s['codec_type'] for s in info['streams']}:
            raise ValueError('Recording must contain both audio and video')
        duration = float(info['format']['duration'])
        if duration <= 0 or output.stat().st_size > cfg.max_bytes:
            raise ValueError('Invalid recording size/duration')
        output.with_suffix('.probe.json').write_text(probe.stdout, encoding='utf-8')
        details.update(output_file=output.name, sha256=sha256(output), size_bytes=output.stat().st_size,
            duration_seconds=duration, content_type='video/mp2t')
        attempt.update(status='complete', finished_at=datetime.now(timezone.utc).isoformat())
        store.update(identity, 'complete', details)
    except Exception as exc:
        if child and child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        attempt.update(status='failed', error=type(exc).__name__, finished_at=datetime.now(timezone.utc).isoformat())
        store.update(identity, 'failed', details)
        print(f'Station recording {identity} failed: {exc}', file=sys.stderr, flush=True)
    finally:
        if child and child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def _probe(cfg, path):
    result = subprocess.run([str(cfg.ffprobe), '-v', 'error', '-show_streams', '-show_format',
        '-of', 'json', str(path)], capture_output=True, text=True, check=True, timeout=30)
    info = json.loads(result.stdout)
    if not {'audio', 'video'} <= {s['codec_type'] for s in info['streams']}:
        raise ValueError('Program output must contain both audio and video')
    duration = float(info['format']['duration'])
    if duration <= 0:
        raise ValueError('Program output has no duration')
    return result.stdout, duration


def process_playout(cfg, store, row):
    """Commit-to-air of a completed recording, or a planning dry run of it."""
    identity = row['id']
    details = dict(row['details'])
    recording = store.get(row['recording_id'])
    attempt = {'id': str(uuid4()), 'started_at': datetime.now(timezone.utc).isoformat()}
    details['attempts'] = [*details.get('attempts', []), attempt]
    details['on_air'] = False
    _write_playout(store, identity, 'running', details)
    try:
        if not recording or recording['status'] != 'complete':
            raise ValueError('Source recording is no longer complete')
        source = private_path(cfg.data, 'recordings', recording['id'], recording['details']['output_file'])
        if not source.is_file() or sha256(source) != details['recording_sha256']:
            raise ValueError('Source recording integrity failure')
        folder = private_path(cfg.data, 'playouts', identity)
        folder.mkdir(parents=True, exist_ok=True)
        if details['mode'] == 'dry-run':
            # Planning check only: proves the source/authority/profile are usable
            # and records the exact pinned implementation. No program is emitted.
            cfg.verify_media()
            stdout, duration = _probe(cfg, source)
            (folder / 'dry-run.probe.json').write_text(stdout, encoding='utf-8')
            details.update(mode='dry-run', verified_source_sha256=sha256(source),
                duration_seconds=duration, media_source_commit=cfg.verify_media(),
                completed_at=datetime.now(timezone.utc).isoformat())
            attempt.update(status='complete', finished_at=details['completed_at'])
            _write_playout(store, identity, 'complete', details)
            return
        program = folder / 'program.media'
        if program.exists():
            program.unlink()
        # The pinned donor graph re-encodes the verified recording into broadcast
        # output, preserving its captions, using the same media adapter as capture.
        staged = folder / 'source.media'
        if staged.exists():
            staged.unlink()
        shutil.copyfile(source, staged)
        log = folder / 'playout.log'
        with log.open('a', encoding='utf-8') as stream:
            child = subprocess.Popen([str(cfg.media_python), '-B', str(Path(__file__).with_name('media_job.py')),
                str(cfg.media_source), str(folder), str(program),
                json.dumps(cfg.encoding_profile()), 'null'], env=cfg.worker_environment(),
                stdout=stream, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            attempt['pid'] = child.pid
            _write_playout(store, identity, 'running', details)
            code = child.wait(timeout=cfg.max_seconds)
        attempt['exit_code'] = code
        if code or not program.is_file():
            raise RuntimeError('Playout worker failed; retained attempt log requires review')
        text = log.read_text(encoding='utf-8', errors='replace')
        if "'error': None" not in text or "'teardown_clean': True" not in text:
            raise RuntimeError('Playout worker did not confirm clean output/teardown')
        stdout, duration = _probe(cfg, program)
        (folder / 'program.probe.json').write_text(stdout, encoding='utf-8')
        staged.unlink(missing_ok=True)
        details.update(mode='commit', output_file=program.name, sha256=sha256(program),
            size_bytes=program.stat().st_size, duration_seconds=duration,
            content_type='video/mp2t', media_source_commit=cfg.verify_media(),
            completed_at=datetime.now(timezone.utc).isoformat(), on_air=True,
            switched_at=datetime.now(timezone.utc).isoformat())
        attempt.update(status='complete', finished_at=details['completed_at'])
        with store.engine.begin() as conn:
            conn.execute(sa.text('SELECT pg_advisory_xact_lock(1953721717)'))
            conn.execute(playouts.update().where(playouts.c.id == identity)
                .values(status='complete', details=details))
            conn.execute(station_state.update().where(station_state.c.id == 1).values(on_air={
                'mode': 'playout', 'playout_id': identity, 'recording_id': recording['id'],
                'since': details['switched_at'], 'sha256': details['sha256']}))
    except Exception as exc:
        details.update(on_air=False, error=type(exc).__name__,
            error_at=datetime.now(timezone.utc).isoformat())
        attempt.update(status='failed', error=type(exc).__name__,
            finished_at=datetime.now(timezone.utc).isoformat())
        _write_playout(store, identity, 'failed', details)
        print(f'Station playout {identity} failed: {exc}', file=sys.stderr, flush=True)


def _write_playout(store, identity, status, details):
    with store.engine.begin() as conn:
        conn.execute(playouts.update().where(playouts.c.id == identity)
            .values(status=status, details=details))
