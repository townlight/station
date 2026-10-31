# SPDX-License-Identifier: AGPL-3.0-only
"""Tail the live TS evidence into an isolated CivicCast SRT listener.

Network backpressure cannot reach the recorder. This child imports only the
pinned donor media runtime; it never starts or controls CivicCast itself.
"""
import json
import os
from pathlib import Path
import sys
import time

source, incoming, status_file, configuration = sys.argv[1:]
incoming, status_file = Path(incoming), Path(status_file)
cfg = json.loads(configuration)
sys.path.insert(0, source)
from civiccast.native.gstreamer_runtime import bootstrap_installed_gstreamer_runtime
bootstrap_installed_gstreamer_runtime()
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
Gst.init(None)
pipeline = Gst.Pipeline.new('station-civiccast-feed')
app = Gst.ElementFactory.make('appsrc')
queue = Gst.ElementFactory.make('queue')
sink = Gst.ElementFactory.make('srtsink')
app.set_property('caps', Gst.Caps.from_string('video/mpegts,systemstream=true,packetsize=188'))
app.set_property('is-live', True)
app.set_property('format', Gst.Format.TIME)
app.set_property('do-timestamp', True)
app.set_property('block', False)
app.set_property('max-bytes', 188*1024)
app.set_property('leaky-type', 2)
queue.set_property('max-size-buffers', 8)
queue.set_property('leaky', 2)
sink.set_property('uri', f"srt://{cfg['address']}:{cfg['port']}?mode=listener&latency=200")
sink.set_property('passphrase', os.environ['TOWNLIGHT_STATION_FEED_SECRET'])
sink.set_property('pbkeylen', 16)
sink.set_property('wait-for-connection', False)
sink.set_property('auto-reconnect', True)
sink.set_property('sync', False)
sink.set_property('async', False)
for element in (app, queue, sink):
    pipeline.add(element)
if not app.link(queue) or not queue.link(sink):
    raise RuntimeError('Cannot link CivicCast SRT feed')
loop = GLib.MainLoop()
offset = sent = 0
last_growth = time.monotonic()
state = {'state': 'waiting-for-receiver', 'bytes_sent': 0}


def persist():
    temp = status_file.with_suffix('.tmp')
    temp.write_text(json.dumps(state), encoding='utf-8')
    try:
        os.replace(temp, status_file)
    except PermissionError:
        # Windows readers briefly hold the old file without delete sharing.
        # Keep the last complete receipt and publish on the next tick.
        pass


def poll():
    global offset, sent, last_growth
    if incoming.with_suffix('.feed-stop').exists():
        state['state'] = 'stopped'
        loop.quit()
        return False
    if incoming.exists():
        size = incoming.stat().st_size
        # Always follow the live edge after congestion, never buffer a meeting.
        if size-offset > 188*2048:
            offset = (size//188-512)*188
        length = min((size-offset)//188*188, 188*512)
        if length > 0:
            with incoming.open('rb') as stream:
                stream.seek(offset)
                data = stream.read(length)
            offset += len(data)
            buffer = Gst.Buffer.new_allocate(None, len(data), None)
            buffer.fill(0, data)
            app.emit('push-buffer', buffer)
        stats = sink.get_property('stats')
        count = stats.get_value('bytes-sent-total') if stats.has_field('bytes-sent-total') else 0
        if count > sent:
            sent, last_growth = count, time.monotonic()
        state.update(state='sending' if sent and time.monotonic()-last_growth < 2 else 'waiting-for-receiver', bytes_sent=sent)
    persist()
    return True


def message(_bus, msg):
    if msg.type == Gst.MessageType.ERROR:
        # No provider URI/secret/debug in a staff-visible receipt.
        state.update(state='failed', error='SRT transport error')
        loop.quit()


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect('message', message)
GLib.timeout_add(50, poll)
try:
    pipeline.set_state(Gst.State.PLAYING)
    loop.run()
finally:
    pipeline.set_state(Gst.State.NULL)
    persist()
sys.exit(1 if state['state'] == 'failed' else 0)
