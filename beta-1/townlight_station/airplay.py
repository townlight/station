# SPDX-License-Identifier: AGPL-3.0-only
"""Station air supervisor: keeps a real program on air through the pinned
CivicCast GStreamer engine, driven only by Station's durable on-air state.

The durable authority is ``station.station_state.on_air`` (written by the
existing Station on-air routes). This module turns that row into:

* a Station-owned config for the adapter ``air.py`` -- the verified scheduled
  program, the administrator-configured live input, and the leg to air;
* a Station-owned control file (``data/air/control.json``) the running adapter
  polls, so an override and the return-to-schedule happen inside one running
  pipeline rather than by re-launching an encoder;
* the emitted program itself (``data/air/program.media``), the only sink.

Nothing here decides what is on air, and no donor default destination is used:
the adapter's graph has exactly one file sink inside Station storage.
"""
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa

from .boundary import Settings, private_path, sha256
from .live import sources
from .store import Store, playouts, station_state

PROGRAM_FILE = 'program.media'
CONTROL_FILE = 'control.json'
CONFIG_FILE = 'air-config.json'
LOG_FILE = 'air.log'
LEG_SCHEDULE = 0
LEG_LIVE = 1


def _atomic_write(path, text):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, path)


def emitted_timeline_seconds(settings):
    """How many seconds of program timeline the appended file already carries.

    Returns ``0.0`` for a file with no readable timeline (a fresh run, or a probe
    failure), and the highest timestamp the file actually contains otherwise.
    The probe is the configured Station ``ffprobe``; nothing is inferred from the
    wall clock, so the offset follows emitted media, not process lifetime.
    """
    path = private_path(settings.data, 'air', PROGRAM_FILE)
    if not path.is_file() or path.stat().st_size == 0:
        return 0.0
    try:
        result = subprocess.run(
            [str(settings.ffprobe), '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'packet=pts_time,dts_time', '-of', 'csv=p=0', str(path)],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 0.0
    if result.returncode != 0:
        return 0.0
    values = []
    for line in (result.stdout or '').splitlines():
        for field in re.split(r'[,\s]+', line.strip()):
            try:
                number = float(field)
            except ValueError:
                continue
            if number >= 0:
                values.append(number)
    return round(max(values), 3) if values else 0.0


def scheduled_program(store, settings):
    """The verified program the schedule is currently airing.

    The newest committed playout is the scheduled program; its bytes are
    re-verified against the hash recorded when it was rendered, so a modified or
    missing file leaves the station safely off air rather than airing unknown
    bytes.
    """
    with store.engine.connect() as conn:
        rows = conn.execute(sa.select(playouts).where(playouts.c.status == 'complete')
            .order_by(playouts.c.created_at.desc()).limit(20)).mappings().all()
    for row in rows:
        details = row['details']
        if details.get('mode') != 'commit' or not details.get('output_file'):
            continue
        path = private_path(settings.data, 'playouts', row['id'], details['output_file'])
        if not path.is_file() or sha256(path) != details.get('sha256'):
            continue
        return {'path': path, 'playout_id': row['id'], 'sha256': details['sha256']}
    return None


def live_candidate(store, settings):
    """The live input the running graph carries, if the station has one.

    The input named in durable on-air state wins; otherwise a station with
    exactly one configured live input carries it, so an override is a real
    in-pipeline swap rather than a fresh encoder launch. Several configured
    inputs and none named means no live leg (the station airs its schedule).
    """
    current = store.on_air() or {}
    configured = sources(settings)
    named = current.get('live_source_id')
    if named and named in configured:
        return configured[named]
    if len(configured) == 1:
        return next(iter(configured.values()))
    return None


def plan(store, settings):
    """The leg that should be on air right now, from durable Station state only.

    ``live`` asks for the configured override input; anything else (``schedule``,
    ``playout``, missing state) airs the scheduled program -- the fail-safe side.
    """
    current = store.on_air() or {}
    mode = current.get('mode')
    program = scheduled_program(store, settings)
    live = live_candidate(store, settings)
    if mode == 'live' and live:
        return {'mode': 'live', 'leg': LEG_LIVE, 'live_source_id': live['id'],
                'program': program,
                'live': {'address': live['address'], 'port': live['port']}}
    if not program:
        return None
    return {'mode': 'playout' if mode == 'playout' else 'schedule',
            'leg': LEG_SCHEDULE, 'live_source_id': None, 'program': program,
            'live': ({'address': live['address'], 'port': live['port']} if live else None)}


def adapter_config(plan_row, settings):
    """The config for the adapter: Station-owned inputs, one Station-owned sink."""
    return {
        'program': str(plan_row['program']['path']),
        # The live leg is ALWAYS in the graph when the station has a configured
        # input, because leg changes are swaps inside one running pipeline (see
        # air.py) and a restart would reset the emitted program timeline (F-08).
        # An idle live input is harmless: its decodebin is ``async-handling``, so
        # it does not hold preroll (measured without a sender:
        # outputs/beta-fixes-2026-10-28/evidence/area3-idle-live-probe.json).
        'live': plan_row['live'],
        'profile': settings.encoding_profile(),
        'output': str(private_path(settings.data, 'air', PROGRAM_FILE)),
        'control': str(private_path(settings.data, 'air', CONTROL_FILE)),
        'max_seconds': settings.max_seconds,
        'loop_copies': min(32, max(1, round(settings.max_seconds / 5))),
        # The highest PES the appended program already carries: measured in
        # ``_start_child`` after the previous adapter has stopped; 0.0 for a first
        # run. The adapter subtracts the muxer's own CLOCK_BASE itself.
        'emitted_pes_seconds': 0.0,
    }


def recover_stale_override(store):
    """A restart must not silently air a live feed nobody is attending.

    A live override is an operator action taken against a running station. If the
    station process that took it is gone, the safe state after a restart is the
    schedule; the superseded decision is retained as evidence, exactly like the
    worker's interrupted-playout recovery.
    """
    with store.engine.begin() as conn:
        conn.execute(sa.text('SELECT pg_advisory_xact_lock(1953721717)'))
        current = conn.scalar(sa.select(station_state.c.on_air).where(station_state.c.id == 1)) or {}
        if current.get('mode') == 'live':
            conn.execute(station_state.update().where(station_state.c.id == 1).values(on_air={
                'mode': 'schedule', 'recovered_at': datetime.now(timezone.utc).isoformat(),
                'recovered_from': current}))
            return current
    return None


class AirSupervisor:
    """Owns the air adapter child and publishes Station's on-air decision to it."""

    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.child = None
        self.child_key = None
        self.log = None
        self.failures = 0
        private_path(cfg.data, 'air').mkdir(parents=True, exist_ok=True)

    def _control(self):
        return private_path(self.cfg.data, 'air', CONTROL_FILE)

    def _publish(self, plan_row):
        _atomic_write(self._control(), json.dumps({
            'leg': plan_row['leg'], 'mode': plan_row['mode'],
            'live_source_id': plan_row['live_source_id'],
            'playout_id': plan_row['program']['playout_id'],
            'published_at': datetime.now(timezone.utc).isoformat()}, sort_keys=True))

    def _stop_child(self):
        if self.child is not None:
            if self.child.poll() is None:
                try:
                    _atomic_write(self._control(), json.dumps({'stop': True}))
                except OSError:
                    pass
                try:
                    self.child.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=10)
            if self.child.returncode not in (0, None):
                self.failures += 1
                print(f'air adapter exited {self.child.returncode}', flush=True)
        self.child = None
        self.child_key = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def _start_child(self, config):
        # The adapter appends, so a fresh adapter for a new leg set continues the
        # same Station-owned program file; only this supervisor's own start
        # truncates it (see ``run``).
        config = dict(config, emitted_pes_seconds=emitted_timeline_seconds(self.cfg))
        print(f'air adapter appending to the program file after '
              f'{config["emitted_pes_seconds"]}s of emitted program', flush=True)
        config_path = private_path(self.cfg.data, 'air', CONFIG_FILE)
        _atomic_write(config_path, json.dumps(config, indent=2, sort_keys=True))
        self.log = private_path(self.cfg.data, 'air', LOG_FILE).open('a', encoding='utf-8')
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        self.child = subprocess.Popen(
            [str(self.cfg.media_python), '-B', str(Path(__file__).with_name('air.py')),
             str(self.cfg.media_source), str(config_path)],
            env=self.cfg.worker_environment(), stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=flags)
        print(f'air adapter started pid={self.child.pid} '
              f'emitted_pes={config.get("emitted_pes_seconds")}s', flush=True)
        return self.child

    def tick(self):
        """One supervision step: derive the decision, keep the child airing it."""
        plan_row = plan(self.store, self.cfg)
        if plan_row is None:
            self._stop_child()
            return None
        config = adapter_config(plan_row, self.cfg)
        # Only a NEW PROGRAM (or a dead child) needs a fresh graph. Which leg is on
        # air is not part of the key: the running adapter swaps legs inside its own
        # pipeline, which is what keeps one continuous program timeline across
        # scheduled -> live -> scheduled (F-08).
        key = (config['program'], json.dumps(
            {k: config['live'][k] for k in ('address', 'port')} if config['live'] else None,
            sort_keys=True))
        if self.child is None or self.child.poll() is not None or key != self.child_key:
            # Only a different PROGRAM or live INPUT ADDRESS (or a died child) needs
            # a fresh graph. The on-air leg is not part of the key: the running
            # adapter swaps legs inside its own pipeline, which is what keeps one
            # continuous program timeline across scheduled -> live -> scheduled.
            self._stop_child()
            self._start_child(config)
            self.child_key = key
        self._publish(plan_row)
        return plan_row

    def run(self):
        """Supervise until interrupted; always leaves no owned child behind."""
        output = private_path(self.cfg.data, 'air', PROGRAM_FILE)
        if output.exists():
            output.write_bytes(b'')
        try:
            while True:
                try:
                    self.tick()
                except Exception as exc:  # a supervision error must not orphan air
                    self.failures += 1
                    print(f'air supervision error: {type(exc).__name__}: {exc}', flush=True)
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop_child()


def run_air():
    cfg = Settings.from_env()
    cfg.initialize_storage()
    cfg.verify_media()
    store = Store(cfg.database_url)
    store.ready()
    try:
        recovered = recover_stale_override(store)
        if recovered:
            print('air: recovered a stale live override to schedule', flush=True)
        AirSupervisor(cfg, store).run()
    finally:
        store.engine.dispose()
