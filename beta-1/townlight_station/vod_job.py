# SPDX-License-Identifier: AGPL-3.0-only
"""Narrow public-playback packaging adapter (media runtime process).

Remuxes one verified Station program (MPEG-TS carrying H.264 + AAC) into a
browser-playable MP4, and writes a WebVTT caption track from the supplied
caption sidecar when the program has one. No re-encode, no ASR, no new caption
content: the browser plays what staff approved.

It never starts the donor application, daemon, database or any donor
destination, and it does not build a playout graph -- packaging is a copy, not
a program. The only sink is the Station-owned output file named on the command
line, and the pinned GStreamer closure Station already ships supplies every
element used.

The assembled pipeline is ``parsebin`` (demux + parse, no decode) feeding each
parsed stream into ``mp4mux`` through ``h264parse``/``aacparse``. It is built by
``gst_parse_launch`` so the mux's request pads are negotiated by the parser
itself: hand-linking them produced ``GST_PAD_LINK_NOFORMAT`` and a hang, both
measured on this task before this shape was adopted.

    vod_job.py <media_source> <input.media> <output.mp4> [captions.srt output.vtt]
"""
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
incoming = Path(sys.argv[2])
output = Path(sys.argv[3])
captions_in = Path(sys.argv[4]) if len(sys.argv) > 4 else None
captions_out = Path(sys.argv[5]) if len(sys.argv) > 5 else None

sys.path.insert(0, str(source))
import civiccast  # noqa: E402

if not Path(civiccast.__file__).resolve().is_relative_to(source.resolve()):
    raise RuntimeError('Wrong media implementation imported')

from civiccast.native import gstreamer_runtime  # noqa: E402

gstreamer_runtime.bootstrap_installed_gstreamer_runtime()
import gi  # noqa: E402

gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import GLib, Gst  # noqa: E402

Gst.init(None)


def webvtt_from_srt(text):
    """Convert an SRT sidecar to WebVTT by header + decimal separator only.

    Cue text is carried through verbatim; timestamp values are unchanged. This
    is packaging of supplied captions, never caption generation.
    """
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    out = ['WEBVTT', '']
    for line in lines:
        if '-->' in line:
            out.append(line.replace(',', '.'))
            continue
        if line.strip().isdigit():
            continue
        out.append(line)
    return '\n'.join(out).rstrip('\n') + '\n'


def uri(path):
    """A forward-slash path for a gst_parse_launch property value."""
    return str(path).replace('\\', '/')


description = (
    f'filesrc location="{uri(incoming)}" ! parsebin name=parse '
    'parse. ! queue ! h264parse ! mp4mux name=mux ! filesink location="'
    f'{uri(output)}" '
    'parse. ! queue ! aacparse ! mux.'
)
pipeline = Gst.parse_launch(description)
mux = pipeline.get_by_name('mux')
if mux is None:
    raise RuntimeError('mp4mux was not built')
mux.set_property('faststart', True)

result = {'error': None}
loop = GLib.MainLoop()
bus = pipeline.get_bus()
bus.add_signal_watch()
seen = []


def on_message(_bus, message):
    if message.type == Gst.MessageType.EOS:
        loop.quit()
    elif message.type == Gst.MessageType.ERROR:
        error, debug = message.parse_error()
        result['error'] = f'{error.message} ({(debug or "no detail")[:300]})'
        loop.quit()
    seen.append(message.type.value_nicks)


bus.connect('message', on_message)
GLib.timeout_add(90000, lambda: (result.update(error='packaging timed out'),
                                loop.quit(), False)[2])
pipeline.set_state(Gst.State.PLAYING)
loop.run()
pipeline.set_state(Gst.State.NULL)

captions = None
if captions_in is not None and captions_out is not None and captions_in.is_file():
    captions_out.write_text(webvtt_from_srt(captions_in.read_text(encoding='utf-8', errors='replace')),
                            encoding='utf-8')
    captions = {'path': str(captions_out), 'source': str(captions_in)}

ok = result['error'] is None and output.is_file() and output.stat().st_size > 0
print(json.dumps({'source_import': civiccast.__file__, 'mode': 'remux',
    'pipeline': description.replace(uri(incoming), '<input>').replace(uri(output), '<output>'),
    'result': result, 'messages': seen[-12:], 'captions': captions,
    'bytes': output.stat().st_size if output.is_file() else 0, 'ok': ok}), flush=True)
sys.exit(0 if ok else 1)
