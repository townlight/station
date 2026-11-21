import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from townlight_station import input_test


@pytest.mark.parametrize(('startup_seconds', 'audio_duration', 'error'), [
    (9, 10, None),
    (None, None, 'received no media'),
    (9, 1.4, 'ten seconds of both picture and sound'),
])
def test_preview_requires_fresh_capture_and_full_audio_and_video(
    tmp_path, monkeypatch, startup_seconds, audio_duration, error,
):
    folder = tmp_path / 'temp' / 'input-test'
    folder.mkdir(parents=True)
    output = folder / 'preview.ts'
    output.write_bytes(b'old media')
    preview = folder / 'preview.mp4'
    preview.write_bytes(b'old preview')
    clock = 0.0

    def sleep(seconds):
        nonlocal clock
        clock += seconds
        if startup_seconds is not None and clock >= startup_seconds and not output.exists():
            output.write_bytes(b'fresh media')

    class Child:
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            if self.returncode is None:
                assert output.with_suffix('.stop').is_file()
                assert clock - startup_seconds >= input_test.PREVIEW_SECONDS
                self.returncode = 0
            return self.returncode

        def kill(self):
            self.returncode = 1

    child = Child()

    def popen(*args, **kwargs):
        assert not output.exists()
        assert not preview.exists()
        return child

    def run(command, **kwargs):
        assert not preview.exists()
        if command[0].endswith('ffmpeg.exe'):
            assert command[command.index('-t') + 1] == '10'
            Path(command[-1]).write_bytes(b'fresh preview')
        else:
            return SimpleNamespace(stdout=json.dumps({'streams': [
                {'codec_type': 'video', 'duration': '10'},
                {'codec_type': 'audio', 'duration': str(audio_duration)},
            ]}))

    monkeypatch.setattr(input_test, 'time', SimpleNamespace(monotonic=lambda: clock, sleep=sleep))
    monkeypatch.setattr(input_test, 'sources', lambda _: {'local-chamber': {'kind': 'windows-devices'}})
    monkeypatch.setattr(input_test.subprocess, 'Popen', popen)
    monkeypatch.setattr(input_test.subprocess, 'run', run)
    settings = SimpleNamespace(data=tmp_path, media_python=tmp_path / 'python.exe', media_source=tmp_path,
                               ffprobe=tmp_path / 'ffprobe.exe', encoding_profile=dict, worker_environment=dict)

    if error:
        with pytest.raises(ValueError, match=error):
            input_test.record_test(settings)
        assert not preview.exists()
    else:
        assert input_test.record_test(settings)['ready']
        assert preview.read_bytes() == b'fresh preview'
    assert child.poll() == (1 if startup_seconds is None else 0)
    assert not (folder / 'preview.partial.mp4').exists()
