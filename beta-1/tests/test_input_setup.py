import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from townlight_station.device_probe import resolve_device
from townlight_station.devices import save_selection
from townlight_station.live import sources
from townlight_station.backup import verify_backup
from townlight_station.setup_windows import load_configuration


def test_saved_inputs_use_resolved_ids_and_retain_udp(tmp_path, monkeypatch):
    camera, microphone = 'a'*64, 'b'*64
    found = [{'id': camera, 'label': 'Camera', 'kind': 'video'},
             {'id': microphone, 'label': 'Microphone', 'kind': 'audio'}]
    monkeypatch.setattr('townlight_station.devices.discover', lambda _: found)
    cfg = SimpleNamespace(data=tmp_path)
    udp = {'id': 'chamber', 'label': 'Chamber feed', 'kind': 'udp-mpegts', 'address': '127.0.0.1', 'port': 12000}
    (tmp_path/'live-sources.json').write_text(json.dumps([udp]))
    save_selection(cfg, camera, microphone)
    configured = sources(cfg)
    assert configured['chamber'] == udp
    assert configured['local-chamber']['video_id'] == camera
    with pytest.raises(ValueError, match='missing'):
        save_selection(cfg, 'c'*64, microphone)
    assert sources(cfg)['local-chamber']['video_id'] == camera


def test_missing_or_ambiguous_device_never_falls_back():
    with pytest.raises(ValueError):
        resolve_device([], 'a'*64, 'audio')
    device = {'id':'a'*64, 'kind':'audio'}
    with pytest.raises(ValueError):
        resolve_device([device, device], 'a'*64, 'audio')


def test_config_rejects_unrelated_environment_and_wrong_storage(tmp_path):
    path = tmp_path/'settings.json'
    path.write_text(json.dumps({'DATA_ROOT': str(tmp_path), 'DATABASE_URL': 'test', 'CIVICCAST_DATABASE_URL': 'private'}))
    with pytest.raises(ValueError, match='Invalid'):
        load_configuration(path)
    path.write_text(json.dumps({'DATA_ROOT': str(tmp_path/'other')}))
    with pytest.raises(ValueError, match='protected data'):
        load_configuration(path)


def test_windows_configuration_is_account_protected_and_legacy_plaintext_is_replaced(tmp_path):
    import secrets
    from townlight_station.setup_windows import save_configuration
    path = tmp_path/'settings.json'
    password = secrets.token_hex(32)
    values = {'DATA_ROOT':str(tmp_path), 'DATABASE_ADMIN_PASSWORD':password}
    save_configuration(path, values)
    assert password.encode() not in path.read_bytes()
    assert load_configuration(path) == values
    # Earlier private candidate configurations are migrated on their next read.
    path.write_text(json.dumps(values), encoding='utf-8')
    assert load_configuration(path) == values
    assert password.encode() not in path.read_bytes()


def test_backup_rejects_changed_unlisted_and_traversal_files(tmp_path):
    dump = tmp_path/'station.dump'
    dump.write_bytes(b'fixture-dump')
    manifest = {'product':'townlight-station-backup', 'version':1,
                'files':{'station.dump':{'size_bytes':dump.stat().st_size,
                    'sha256':hashlib.sha256(dump.read_bytes()).hexdigest()}}}
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    verify_backup(tmp_path)
    dump.write_bytes(b'changed')
    with pytest.raises(ValueError, match='changed'):
        verify_backup(tmp_path)
    manifest['files']['../outside'] = {'size_bytes':0, 'sha256':'0'*64}
    # Restore original dump first so this check exercises traversal, not its digest.
    dump.write_bytes(b'fixture-dump')
    (tmp_path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Unsafe'):
        verify_backup(tmp_path)


def test_logon_task_accepts_windows_sid_serialization_and_rejects_other_executable(tmp_path, monkeypatch):
    import subprocess
    import win32api
    import win32security
    from xml.sax.saxutils import escape
    from townlight_station.setup_windows import register_task
    python = tmp_path/'pythonw.exe'
    config = tmp_path/'settings.json'
    username = win32api.GetUserNameEx(2)
    sid = win32security.ConvertSidToStringSid(win32security.LookupAccountName(None, username)[0])
    args = subprocess.list2cmdline(['-B', '-m', 'townlight_station.cli', 'background', '--config', str(config)])
    calls = []
    executable = str(python)
    def run(command, **kwargs):
        calls.append(command)
        xml = f'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Principals><Principal><UserId>{sid}</UserId></Principal></Principals><Actions><Exec><Command>{escape(executable)}</Command><Arguments>{escape(args)}</Arguments></Exec></Actions></Task>'
        return SimpleNamespace(returncode=0, stdout=xml)
    monkeypatch.setattr('townlight_station.setup_windows.subprocess.run', run)
    register_task(python, config)
    assert len(calls) == 3
    calls.clear()
    executable = str(tmp_path/'unrelated.exe')
    with pytest.raises(ValueError, match='unrelated scheduled task'):
        register_task(python, config)
    assert len(calls) == 1
