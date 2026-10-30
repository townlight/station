# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit
from sqlalchemy.engine import make_url

IDENTITY = dict(product="townlight-station", app_id="org.townlight.station",
    service="TownlightStation", service_account=r"NT SERVICE\TownlightStation",
    credential_namespace="townlight.station", database_schema="station",
    update_channel="townlight-station", version="0.1.0", license="AGPL-3.0-only")


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def private_path(root, *parts):
    root = root.absolute()
    candidate = root.joinpath(*parts)
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError('Path escapes Station storage')
    for path in (candidate, *candidate.parents):
        if path.exists() and (path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Reparse points are not permitted in Station paths')
    return candidate


def service_url(value):
    url = urlsplit(value)
    if url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('Service URL must be a bare configured origin')
    if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in ('127.0.0.1', '::1')):
        raise ValueError('Use HTTPS or explicit loopback for service connections')
    if not url.hostname:
        raise ValueError('Missing service host')
    return value.rstrip('/')


@dataclass(frozen=True)
class Settings:
    data: Path
    database_url: str
    suite_url: str
    media_source: Path
    media_python: Path
    media_runtime: Path
    ffprobe: Path
    max_seconds: int = 7200
    max_bytes: int = 2_147_483_647
    width: int = 1920
    height: int = 1080
    fps: int = 30
    bitrate_kbps: int = 6000

    @classmethod
    def from_env(cls):
        prefix = 'TOWNLIGHT_STATION_'
        get = lambda name: os.environ[prefix + name]
        result = cls(Path(get('DATA_ROOT')), get('DATABASE_URL'), service_url(get('SUITE_URL')),
            Path(get('MEDIA_SOURCE')), Path(get('MEDIA_PYTHON')), Path(get('MEDIA_RUNTIME')),
            Path(get('FFPROBE')), int(os.environ.get(prefix+'MAX_SECONDS', '7200')),
            width=int(os.environ.get(prefix+'WIDTH', '1920')), height=int(os.environ.get(prefix+'HEIGHT', '1080')),
            fps=int(os.environ.get(prefix+'FPS', '30')), bitrate_kbps=int(os.environ.get(prefix+'BITRATE_KBPS', '6000')))
        result.validate()
        return result

    def validate(self):
        for path in (self.data, self.media_source, self.media_python, self.media_runtime, self.ffprobe):
            if not path.is_absolute():
                raise ValueError('Station paths must be explicit absolute paths')
            private_path(path)
        service_url(self.suite_url)
        db = make_url(self.database_url)
        if db.drivername != 'postgresql+psycopg2' or not db.database or not db.database.endswith('_station'):
            raise ValueError('Station requires its own PostgreSQL database ending in _station')
        if not 10 <= self.max_seconds <= 86400:
            raise ValueError('Invalid media timeout')
        if (not 320 <= self.width <= 3840 or not 180 <= self.height <= 2160 or self.width % 2 or self.height % 2
            or not 1 <= self.fps <= 60 or not 128 <= self.bitrate_kbps <= 50000):
            raise ValueError('Invalid encoding profile')
        # No guessed CivicCast profile, service or DB defaults, even if inherited.
        if self.data.resolve().is_relative_to(self.media_source.resolve()) or self.data.resolve().is_relative_to(self.media_runtime.resolve()):
            raise ValueError('Mutable Station data must be separate from source/runtime')
        if not self.media_python.is_file() or not self.ffprobe.is_file():
            raise ValueError('Configured executables are unavailable')

    def encoding_profile(self):
        return {key: getattr(self, key) for key in ('width', 'height', 'fps', 'bitrate_kbps')}

    def initialize_storage(self):
        self.validate()
        self.data.mkdir(parents=True, exist_ok=True)
        identity = private_path(self.data, 'identity.json')
        if not identity.exists() and any(self.data.iterdir()):
            raise ValueError('Refusing to adopt nonempty unowned storage')
        if identity.exists() and json.loads(identity.read_text()) != IDENTITY:
            raise ValueError('Storage belongs to another product/version; explicit migration required')
        if os.name == 'nt':
            import win32api
            import win32security
            import struct
            sid_hash = hashlib.sha1(IDENTITY['service'].upper().encode('utf-16-le')).digest()
            service_sid = 'S-1-5-80-' + '-'.join(map(str, struct.unpack('<5I', sid_hash)))
            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
            try:
                sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
                caller = win32security.ConvertSidToStringSid(sid)
            finally:
                token.Close()
            sddl = f'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{service_sid})(A;OICI;FA;;;{caller})'
            descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(sddl, 1)
            win32security.SetFileSecurity(str(self.data), win32security.DACL_SECURITY_INFORMATION |
                win32security.PROTECTED_DACL_SECURITY_INFORMATION, descriptor)
        else:
            self.data.chmod(0o700)
        try:
            with identity.open('x', encoding='utf-8') as stream:
                json.dump(IDENTITY, stream)
        except FileExistsError:
            if json.loads(identity.read_text()) != IDENTITY:
                raise ValueError('Storage belongs to another product/version; explicit migration required')
        for name in ('ingest', 'recordings', 'temp'):
            private_path(self.data, name).mkdir(exist_ok=True)

    def verify_media(self):
        lock = json.loads(Path(__file__).with_name('media-lock.json').read_text())
        for name, digest in lock['files'].items():
            path = private_path(self.media_source, name)
            if sha256(path) != digest:
                raise ValueError(f'Pinned media implementation differs: {name}')
        return lock['source_commit']

    def worker_environment(self):
        # An allowlist prevents inherited provider credentials/DB/profile overrides.
        keep = ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'PROCESSOR_ARCHITECTURE')
        env = {k: os.environ[k] for k in keep if k in os.environ}
        for name in ('TEMP', 'TMP', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
            path = private_path(self.data, 'temp', name)
            path.mkdir(parents=True, exist_ok=True)
            env[name] = str(path)
        env.update(PYTHONDONTWRITEBYTECODE='1', CIVICCAST_GSTREAMER_RUNTIME_ROOT=str(self.media_runtime),
            GST_REGISTRY=str(private_path(self.data, 'gst-registry.bin')))
        return env
