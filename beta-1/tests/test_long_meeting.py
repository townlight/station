from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from townlight_station.boundary import Settings
from townlight_station.operator import install_operator


def test_default_policy_can_hold_eight_hours():
    fields = Settings.__dataclass_fields__
    assert fields['max_seconds'].default == 8 * 60 * 60
    assert fields['max_bytes'].default >= 32 * 1024**3


def test_operator_renewal_checks_authority_and_extends_session(monkeypatch):
    import townlight_station.operator as operator
    clock = [0.0]
    monkeypatch.setattr(operator.time, 'monotonic', lambda: clock[0])
    valid = [True]

    class Suite:
        def get(self, path, proof):
            if not valid[0] or proof != 'a' * 64:
                raise HTTPException(401, 'Signed out')
            return {'actor_id': 'clerk'}

    app = FastAPI()
    install_operator(app, SimpleNamespace(), Suite())
    with TestClient(app) as client:
        headers = {'Origin': 'http://testserver', 'X-Station-UI': '1'}
        ticket = client.post('/v1/operator-ticket', headers={'X-Townlight-Session': 'a'*64}).json()['ticket']
        assert client.post('/operator/session', json={'ticket': ticket}, headers=headers).status_code == 200
        clock[0] = 800
        assert client.post('/operator/renew', json={}, headers=headers).status_code == 200
        clock[0] = 1000
        assert client.post('/operator/renew', json={}, headers=headers).status_code == 200
        valid[0] = False
        assert client.post('/operator/renew', json={}, headers=headers).status_code == 401
        valid[0] = True
        # Revoked browser authority cannot resurrect just because Suite is available.
        assert client.post('/operator/renew', json={}, headers=headers).status_code == 401


def test_recording_disk_budget_is_bounded_and_actionable(tmp_path, monkeypatch):
    from townlight_station.storage_policy import require_capacity, storage_status
    monkeypatch.setattr('townlight_station.storage_policy.shutil.disk_usage',
                        lambda _: SimpleNamespace(free=100 * 1024**3))
    settings = SimpleNamespace(data=tmp_path, bitrate_kbps=6000, max_seconds=28800,
                               max_bytes=64*1024**3, reserve_bytes=1024**3)
    assert storage_status(settings)['estimated_recording_bytes'] > 20*1024**3
    require_capacity(settings)
    monkeypatch.setattr('townlight_station.storage_policy.shutil.disk_usage',
                        lambda _: SimpleNamespace(free=1024**3))
    with pytest.raises(ValueError, match='space'):
        require_capacity(settings)
