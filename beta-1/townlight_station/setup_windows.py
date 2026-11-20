# SPDX-License-Identifier: AGPL-3.0-only
"""Per-user Windows recorder provisioning and owned background process lifecycle.

The logon task runs in the user's device-access session, outside the desktop's
job object. It owns a separate PostgreSQL cluster and never borrows CivicCast data.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from .boundary import Settings, private_path

CONFIG_KEYS = {'DATA_ROOT', 'DATABASE_URL', 'SUITE_URL', 'MEDIA_SOURCE', 'MEDIA_PYTHON',
               'MEDIA_RUNTIME', 'FFPROBE', 'CONTROL_PYTHON', 'POSTGRES_BIN',
               'MAX_SECONDS', 'MAX_BYTES', 'RESERVE_BYTES', 'WIDTH', 'HEIGHT', 'FPS', 'BITRATE_KBPS', 'DATABASE_ADMIN_PASSWORD'}


def load_configuration(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Station configuration path must be absolute')
    private_path(path.parent, path.name)
    values = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(values, dict) or set(values) - CONFIG_KEYS or any(not isinstance(v, str) for v in values.values()):
        raise ValueError('Invalid Station configuration')
    if Path(values.get('DATA_ROOT', '')).resolve() != path.parent.resolve():
        raise ValueError('Station configuration must belong to its protected data directory')
    for key, value in values.items():
        os.environ['TOWNLIGHT_STATION_'+key] = value
    return values


def task_name(data):
    import hashlib
    return 'Townlight Station '+hashlib.sha256(str(data.resolve()).casefold().encode()).hexdigest()[:16]


def verify_task_owner(xml, python, args, username):
    import win32security
    tree = ET.fromstring(xml)
    ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
    existing = tree.findtext('.//t:Exec/t:Arguments', namespaces=ns)
    command = tree.findtext('.//t:Exec/t:Command', namespaces=ns)
    owner = tree.findtext('.//t:Principal/t:UserId', namespaces=ns)
    try:
        existing_sid = win32security.ConvertStringSidToSid(owner) if owner and owner.startswith('S-1-') else win32security.LookupAccountName(None, owner)[0]
        same_owner = existing_sid == win32security.LookupAccountName(None, username)[0]
    except Exception:
        same_owner = False
    if existing != args or not command or Path(command).resolve() != Path(python).resolve() or not same_owner:
        raise ValueError('An unrelated scheduled task owns this name; no task was replaced')


def register_task(python, config):
    import win32api
    from xml.sax.saxutils import escape
    name = task_name(config.parent)
    username = win32api.GetUserNameEx(2)  # NameSamCompatible; no password storage.
    args = subprocess.list2cmdline(['-B', '-m', 'townlight_station.cli', 'background', '--config', str(config)])
    # Check ownership before updating a task with this deterministic name.
    scheduler = str(Path(os.environ['SYSTEMROOT'])/'System32/schtasks.exe')
    query = subprocess.run([scheduler, '/Query', '/TN', name, '/XML'],
                           capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if query.returncode == 0:
        verify_task_owner(query.stdout, python, args, username)
    xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>Townlight owned meeting recorder: {escape(str(config))}</Description></RegistrationInfo>
<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(username)}</UserId></LogonTrigger></Triggers>
<Principals><Principal id="Recorder"><UserId>{escape(username)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><Enabled>true</Enabled><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><StartWhenAvailable>true</StartWhenAvailable><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
<Actions Context="Recorder"><Exec><Command>{escape(str(python))}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(str(python.parent))}</WorkingDirectory></Exec></Actions></Task>'''
    task_file = config.parent/'logon-task.xml'
    task_file.write_text(xml, encoding='utf-16')
    subprocess.run([scheduler, '/Create', '/TN', name, '/XML', str(task_file), '/F'],
                   capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    subprocess.run([scheduler, '/Run', '/TN', name], capture_output=True,
                   check=True, creationflags=subprocess.CREATE_NO_WINDOW)


def stop_task(config):
    import win32api
    values = load_configuration(config)
    config = Path(config)
    scheduler = str(Path(os.environ['SYSTEMROOT'])/'System32/schtasks.exe')
    name = task_name(config.parent)
    query = subprocess.run([scheduler, '/Query', '/TN', name, '/XML'], capture_output=True,
                           text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if query.returncode != 0:
        status = subprocess.run([str(Path(values['POSTGRES_BIN'])/'pg_ctl.exe'), '-D', str(config.parent/'postgres'), 'status'], capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if status.returncode == 3 and not (config.parent/'background-health.json').exists():
            return {'stopped': True, 'message': 'Recorder is already stopped; data is preserved.'}
        raise ValueError('Recorder task is unavailable; no unrelated process was stopped')
    python = Path(values['CONTROL_PYTHON']).with_name('pythonw.exe')
    args = subprocess.list2cmdline(['-B', '-m', 'townlight_station.cli', 'background', '--config', str(config)])
    verify_task_owner(query.stdout, python, args, win32api.GetUserNameEx(2))
    from .store import Store
    from .backup import maintenance
    store = Store(values['DATABASE_URL'])
    try:
        with maintenance(store):
            (config.parent/'background.stop').write_text('stop', encoding='ascii')
    finally:
        store.engine.dispose()
    # Graceful process termination closes API/workers and cleanly stops PostgreSQL.
    deadline = time.monotonic()+180
    while time.monotonic() < deadline:
        health = config.parent/'background-health.json'
        if not health.exists():
            break
        time.sleep(.5)
    else:
        raise RuntimeError('Recorder did not stop cleanly; task and data were preserved for recovery')
    subprocess.run([scheduler, '/Delete', '/TN', name, '/F'], capture_output=True,
                   check=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    return {'stopped': True, 'message': 'Recorder stopped and its owned logon task removed. Data and backups are preserved.'}


def provision(payload, data, suite_url):
    payload, data = Path(payload).resolve(), Path(data).absolute()
    private_path(data)
    config = data/'settings.json'
    if config.exists():
        values = load_configuration(config)
        if values['SUITE_URL'] != suite_url:
            raise ValueError('Existing Station is paired with another Suite origin')
    else:
        password = secrets.token_hex(32)
        values = dict(DATA_ROOT=str(data),
            DATABASE_URL=f'postgresql+psycopg2://station_app:{password}@127.0.0.1:15435/townlight_station',
            DATABASE_ADMIN_PASSWORD=secrets.token_hex(32),
            SUITE_URL=suite_url, MEDIA_SOURCE=str(payload/'station/media-source'),
            MEDIA_PYTHON=str(payload/'station/media-runtime/python.exe'),
            MEDIA_RUNTIME=str(payload/'station/media-runtime'),
            FFPROBE=str(payload/'station/bin/ffprobe.exe'),
            CONTROL_PYTHON=str(payload/'python/python.exe'), POSTGRES_BIN=str(payload/'postgres/bin'))
        for k, v in values.items():
            os.environ['TOWNLIGHT_STATION_'+k] = v
        cfg = Settings.from_env()
        cfg.initialize_storage()  # Refuses unowned nonempty data and applies private ACLs.
        cfg.verify_media()
        config.write_text(json.dumps(values, indent=2), encoding='utf-8')
    paths = dict(MEDIA_SOURCE=str(payload/'station/media-source'),
                 MEDIA_PYTHON=str(payload/'station/media-runtime/python.exe'),
                 MEDIA_RUNTIME=str(payload/'station/media-runtime'), FFPROBE=str(payload/'station/bin/ffprobe.exe'),
                 CONTROL_PYTHON=str(payload/'python/python.exe'), POSTGRES_BIN=str(payload/'postgres/bin'))
    values.update(paths)
    for k, v in values.items():
        os.environ['TOWNLIGHT_STATION_'+k] = v
    cfg = Settings.from_env()
    cfg.verify_media()
    # Validate media environment and plugins in the actual native runtime.
    from .devices import discover
    discover(cfg)  # Empty devices is permitted; a saved network source may be used.
    temporary = config.with_suffix('.tmp')
    temporary.write_text(json.dumps(values, indent=2), encoding='utf-8')
    temporary.replace(config)
    pythonw = Path(values['CONTROL_PYTHON']).with_name('pythonw.exe')
    if not pythonw.is_file():
        raise ValueError('Installed background Python is missing; repair the Townlight installation')
    (data/'background.stop').unlink(missing_ok=True)
    register_task(pythonw, config)
    import httpx
    deadline = time.monotonic()+240
    while True:
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                response = client.get('http://127.0.0.1:8756/health')
                if response.status_code == 200 and response.json().get('product') == 'townlight-station' and response.json().get('instance') == task_name(data):
                    break
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError('Recorder is configured but not ready. Review its private PostgreSQL, serve and worker logs, then retry setup.')
        time.sleep(1)
    return {'configured': True, 'task': task_name(data), 'data_directory': str(data),
            'message': 'Recorder starts at Windows sign-in. Keep Windows signed in during recording.'}


def background(config):
    import psycopg2
    from sqlalchemy.engine import make_url
    from .store import Store
    values = load_configuration(config)
    cfg = Settings.from_env()
    cfg.initialize_storage()
    cfg.verify_media()
    sys.path.insert(0, str(cfg.media_source))
    from civiccast.platform.child_containment import contain_own_descendants
    containment = contain_own_descendants()
    if not containment.active:
        raise RuntimeError('Recorder cannot contain its own children')
    db = make_url(cfg.database_url)
    pg = Path(values['POSTGRES_BIN'])
    cluster = private_path(cfg.data, 'postgres')
    cluster.mkdir(exist_ok=True)
    password_file = private_path(cfg.data, 'postgres-password.tmp')
    # PostgreSQL lock plus the scheduled task's IgnoreNew policy prevent duplicate ownership.
    if not (cluster/'PG_VERSION').exists():
        password_file.write_text(values['DATABASE_ADMIN_PASSWORD']+'\n', encoding='utf-8')
        try:
            subprocess.run([str(pg/'initdb.exe'), '-D', str(cluster), '-U', 'station_owner',
                '--auth=scram-sha-256', '--encoding=UTF8', '--pwfile='+str(password_file)],
                capture_output=True, check=True, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
        finally:
            password_file.unlink(missing_ok=True)
    handles, children = [], []
    def start(name, argv):
        log = (cfg.data/(name+'.log')).open('a', encoding='utf-8')
        handles.append(log)
        child = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        children.append(child)
        return child
    try:
        server = start('postgres', [str(pg/'postgres.exe'), '-D', str(cluster),
                                   '-h', '127.0.0.1', '-p', str(db.port)])
        deadline = time.monotonic()+180
        while True:
            try:
                connection = psycopg2.connect(host=db.host, port=db.port, user='station_owner',
                    password=values['DATABASE_ADMIN_PASSWORD'], dbname='postgres', connect_timeout=2)
                connection.autocommit = True
                if server.poll() is not None:
                    connection.close()
                    raise RuntimeError('Recorder port is already owned by another database; nothing was stopped')
                with connection.cursor() as cursor:
                    cursor.execute('SHOW data_directory')
                    if Path(cursor.fetchone()[0]).resolve() != cluster.resolve():
                        connection.close()
                        raise RuntimeError('Recorder database identity does not match the configured data directory')
                break
            except psycopg2.OperationalError:
                if server.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('Recorder database did not start; inspect its private PostgreSQL log') from None
                time.sleep(.5)
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                from psycopg2 import sql
                cursor.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (db.username,))
                if not cursor.fetchone():
                    cursor.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %s')
                                   .format(sql.Identifier(db.username)), (db.password,))
                cursor.execute('SELECT 1 FROM pg_database WHERE datname=%s', (db.database,))
                if not cursor.fetchone():
                    cursor.execute(sql.SQL('CREATE DATABASE {} OWNER {}')
                                   .format(sql.Identifier(db.database), sql.Identifier(db.username)))
        finally:
            connection.close()
        store = Store(cfg.database_url)
        try:
            store.migrate()
        finally:
            store.engine.dispose()
        for role in ('serve', 'worker', 'uploads'):
            # The API and workers receive only the application login, not the cluster owner secret.
            saved_admin = os.environ.pop('TOWNLIGHT_STATION_DATABASE_ADMIN_PASSWORD', None)
            try:
                start(role, [values['CONTROL_PYTHON'], '-B', '-m', 'townlight_station.cli', role])
            finally:
                if saved_admin is not None:
                    os.environ['TOWNLIGHT_STATION_DATABASE_ADMIN_PASSWORD'] = saved_admin
        while all(child.poll() is None for child in children) and not (cfg.data/'background.stop').exists():
            health = cfg.data/'background-health.json'
            temporary = health.with_suffix('.tmp')
            temporary.write_text(json.dumps({'updated_at': time.time(), 'pid': os.getpid()}), encoding='utf-8')
            temporary.replace(health)
            time.sleep(1)
        if not (cfg.data/'background.stop').exists():
            raise RuntimeError('Recorder child exited; logon task will perform bounded restart')
    finally:
        for child in reversed(children[1:]):
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=20)
        if children and children[0].poll() is None:
            subprocess.run([str(pg/'pg_ctl.exe'), '-D', str(cluster), '-m', 'fast', 'stop'],
                           capture_output=True, timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
            children[0].wait(timeout=30)
        for log in handles:
            log.close()
        (cfg.data/'background-health.json').unlink(missing_ok=True)
