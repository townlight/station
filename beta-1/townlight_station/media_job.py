# SPDX-License-Identifier: AGPL-3.0-only
"""Narrow worker-process adapter. Never starts CivicCast app/service/DB."""
import json
from pathlib import Path
import subprocess
import sys

source, folder, output = map(Path, sys.argv[1:4])
profile = json.loads(sys.argv[4])
live = json.loads(sys.argv[5]) if len(sys.argv) > 5 else None
sys.path.insert(0, str(source))
import civiccast
if not Path(civiccast.__file__).resolve().is_relative_to(source.resolve()):
    raise RuntimeError('Wrong media implementation imported')
from civiccast.egress.gst import graph as g
E = g.ElementSpec
has_sidecar = (folder / 'captions.srt').exists()
# Decoder caption metadata otherwise survives into the encoder and is combined
# with the explicitly supplied replacement captions. Drop it before encoding.
# ccextractor removes decoder metadata even without a linked caption consumer;
# cccombiner passes through unchanged when it has no requested caption pad.
caption_policy = (E('ccextractor', props={'remove-caption-meta': True}),) if has_sidecar else ()
program = g.PlaylistLeg(label='program', subchains=((
    *((E('udpsrc', props={'address': live['address'], 'port': live['port'], 'caps': 'video/mpegts,systemstream=true,packetsize=188', 'do-timestamp': True}), E('decodebin')) if live else
      (E('filesrc', props={'location': str(folder / 'source.media')}), E('decodebin'))),
    *caption_policy,
    E('videoconvert'), E('videoscale'), E('videorate'),
    E('capsfilter', props={'caps': f"video/x-raw,format=I420,width={profile['width']},height={profile['height']},framerate={profile['fps']}/1"}),
),), audio_tail=(E('audioconvert'), E('audioresample'),
    E('capsfilter', props={'caps': 'audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved'})))
encoder = g.encode_chain_specs(**profile, gop=profile['fps'])
if not has_sidecar:
    # Preserve decoder caption metadata by serializing it into the new H.264
    # stream. An encoder alone does not establish embedded-caption preservation.
    encoder += (E('h264ccinserter', props={'remove-caption-meta': True}),
        E('h264parse', props={'config-interval': -1}))
graph = g.PlayoutGraph(sources=(program,),
    encoder=encoder,
    audio_encoder=g.audio_encode_specs(codec='voaacenc', sample_rate=48000),
    mux=E('mpegtsmux', name='mux'),
    sinks=((E('queue'), E('filesink', props={'location': str(output)})),),
    captions=g.caption_embed_leg_from_sidecar(str(folder / 'captions.srt')) if has_sidecar else None)
graph_file = output.with_suffix('.graph.json')
graph_file.write_text(g.graph_to_json(graph), encoding='utf-8')
print(json.dumps({'source_import': civiccast.__file__, 'worker': str(source / 'civiccast/egress/gst/worker.py')}), flush=True)
# runpy executes the product worker in this process, preserving its forced-exit
# teardown and avoiding a second interpreter with a different import path.
import runpy
if live:
    import os
    # Load the unchanged donor's bootstrap and graph engine. Station owns only
    # the live stop protocol; EOS drains the mux before the donor tears down.
    loaded = runpy.run_path(str(source / 'civiccast/egress/gst/worker.py'), run_name='station_live_adapter')
    engine_module = loaded['enginemod']
    engine = engine_module.GstPlayoutEngine(graph)
    from gi.repository import GLib, Gst
    stop_marker = output.with_suffix('.stop')
    sent = False
    def check_stop():
        global sent
        if stop_marker.exists() and not sent:
            sent = True
            engine.pipeline.send_event(Gst.Event.new_eos())
        return True
    GLib.timeout_add(100, check_stop)
    result = engine.run_forever()
    print(result, flush=True)
    # An unsolicited source ending is not a completed council recording.
    os._exit(0 if sent and result.get('error') is None and result.get('teardown_clean') else 1)
sys.argv = [str(source / 'civiccast/egress/gst/worker.py'), str(graph_file)]
runpy.run_path(sys.argv[0], run_name='__main__')
