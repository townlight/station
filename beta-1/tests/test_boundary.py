# SPDX-License-Identifier: AGPL-3.0-only
import json
import os
from pathlib import Path
import tempfile
import unittest
import hashlib
from unittest.mock import patch

from townlight_station.boundary import IDENTITY, Settings, private_path, service_url


class BoundaryTests(unittest.TestCase):
    def config(self, root):
        for name in ('source', 'runtime'):
            (root/name).mkdir()
        executable=root/'runtime/python.exe'
        executable.write_bytes(b'fixture')
        return Settings(root/'data','postgresql+psycopg2://station:fixture@127.0.0.1/townlight_station',
            'http://127.0.0.1:18000',root/'source',executable,root/'runtime',executable)

    def test_cannot_adopt_another_product_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            cfg=self.config(root)
            cfg.data.mkdir()
            (cfg.data/'identity.json').write_text(json.dumps({'product':'civiccast'}))
            before=(cfg.data/'identity.json').read_bytes()
            with self.assertRaisesRegex(ValueError,'another product'):
                cfg.initialize_storage()
            self.assertEqual((cfg.data/'identity.json').read_bytes(),before)

    def test_nonempty_unmarked_storage_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=self.config(Path(temp))
            cfg.data.mkdir()
            (cfg.data/'existing-media.ts').write_bytes(b'keep')
            with self.assertRaisesRegex(ValueError,'unowned'):
                cfg.initialize_storage()
            self.assertFalse((cfg.data/'identity.json').exists())

    def test_no_inherited_credentials_or_civiccast_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=self.config(Path(temp))
            with patch.dict(os.environ, {'CIVICCAST_DATABASE_URL':'private-donor-db',
                'DATABASE_URL':'private-suite-db','OPENAI_API_KEY':'not-a-key','TOWNLIGHT_PROFILE_ROOT':'other-profile'}):
                env=cfg.worker_environment()
            for name in ('CIVICCAST_DATABASE_URL','DATABASE_URL','OPENAI_API_KEY','TOWNLIGHT_PROFILE_ROOT'):
                self.assertNotIn(name,env)
            self.assertEqual(env['CIVICCAST_GSTREAMER_RUNTIME_ROOT'],str(cfg.media_runtime))
            self.assertTrue(Path(env['LOCALAPPDATA']).is_relative_to(cfg.data))

    def test_explicit_station_database_required(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=self.config(Path(temp))
            for db in ('civiccast','civicsuite','civicrecords'):
                other=Settings(**{**cfg.__dict__,'database_url':f'postgresql+psycopg2://localhost/{db}'})
                with self.assertRaisesRegex(ValueError,'own PostgreSQL'):
                    other.validate()

    def test_urls_and_path_escape_fail_closed(self):
        for url in ('http://example.com','http://user:password@127.0.0.1','http://127.0.0.1/private',
            'http://127.0.0.1?token=x','file:///tmp','http://127.0.0.1#fragment'):
            with self.assertRaises(ValueError):
                service_url(url)
        self.assertEqual(service_url('https://city.example/'),'https://city.example')
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                private_path(Path(temp),'..','other')

    def test_product_identity_has_no_civiccast_service_or_update_channel(self):
        self.assertEqual(IDENTITY['service'],'TownlightStation')
        self.assertEqual(IDENTITY['credential_namespace'],'townlight.station')
        self.assertEqual(IDENTITY['update_channel'],'townlight-station')
        self.assertEqual(IDENTITY['license'],'AGPL-3.0-only')

    def test_scm_entry_point_uses_station_identity_without_registration(self):
        from townlight_station.windows_service import StationService
        self.assertEqual(StationService._svc_name_,'TownlightStation')
        self.assertEqual(StationService._svc_display_name_,'Townlight Station')

    def test_snapshot_copy_rejects_changed_bytes_and_growth(self):
        from fastapi import HTTPException
        from townlight_station.api import copy_snapshot
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            source=root/'input'
            source.write_bytes(b'original')
            original_hash=hashlib.sha256(b'original').hexdigest()
            with self.assertRaises(HTTPException) as oversized:
                copy_snapshot(source,root/'oversized',original_hash,3)
            self.assertEqual(oversized.exception.status_code,422)
            source.write_bytes(b'changed')
            with self.assertRaises(HTTPException) as changed:
                copy_snapshot(source,root/'changed',original_hash,100)
            self.assertEqual(changed.exception.status_code,409)

    def test_live_sources_are_fixed_admin_receivers_not_injected_pipelines(self):
        from townlight_station.live import sources
        with tempfile.TemporaryDirectory() as temp:
            cfg=self.config(Path(temp));cfg.data.mkdir()
            path=cfg.data/'live-sources.json'
            valid={'id':'chamber','label':'Council chamber','kind':'udp-mpegts','address':'127.0.0.1','port':19000}
            path.write_text(json.dumps([valid]))
            self.assertEqual(sources(cfg)['chamber'],valid)
            for bad in ({**valid,'id':'../other'}, {**valid,'port':True}, {**valid,'address':'file:///private'},
                        {**valid,'pipeline':'filesrc location=secret'}, {**valid,'kind':'arbitrary-url'}):
                path.write_text(json.dumps([bad]))
                with self.assertRaises(ValueError): sources(cfg)
            path.write_text(json.dumps([valid,valid]))
            with self.assertRaises(ValueError): sources(cfg)


if __name__=='__main__':
    unittest.main(verbosity=2)
