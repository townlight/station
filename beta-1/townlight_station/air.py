# SPDX-License-Identifier: AGPL-3.0-only
"""Station air adapter: the running on-air program, emitted by the pinned
CivicCast GStreamer engine.

Executed by the *media runtime* interpreter (the same pinned python/GStreamer
closure Station already uses for capture and playout renders), so this is the
only Station file that touches ``gi``. It never starts the donor application,
daemon, database or any donor default destination: the graph is built here from
Station-owned inputs and the only sink is the Station-owned output file named in
the config this process is given.

Two source legs feed one ``input-selector`` -- the donor's own hot-swap
contract -- so switching happens inside a *running* pipeline rather than by
re-launching an encoder:

* leg 0 ``schedule`` - the scheduled program (a verified, committed Station
  playout), repeated as a playlist so it keeps airing until superseded;
* leg 1 ``live``     - the administrator-configured live/override input
  (``live-sources.json``: an explicit UDP MPEG-TS receiver), when configured.
  The feed passes through a small leaky queue so that while the live leg is NOT
  selected its stale packets are dropped instead of backlogged; when the leg is
  selected the air signal jumps to the current live edge.

Station owns every decision: the durable ``station_state.on_air`` row is the
only authority, and the Station air supervisor publishes it to the small control
file this process polls. Nothing here chooses what is on air.

Usage: ``air.py <media_source> <config.json>``
"""
import json
import os
import runpy
import sys
import time
from pathlib import Path

source = Path(sys.argv[1])
config = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
sys.path.insert(0, str(source))
import civiccast

if not Path(civiccast.__file__).resolve().is_relative_to(source.resolve()):
    raise RuntimeError('Wrong media implementation imported')

# Load the unchanged donor's bootstrap and graph engine exactly as the existing
# capture/playout adapter does: worker.py publishes the sibling modules under
# their package names, so the engine and this file share one class identity.
loaded = runpy.run_path(str(source / 'civiccast/egress/gst/worker.py'), run_name='station_air_adapter')
graphmod = loaded['graphmod']
enginemod = loaded['enginemod']
E = graphmod.ElementSpec

profile = config['profile']
width, height, fps = int(profile['width']), int(profile['height']), int(profile['fps'])
video = (
    E('videoconvert'), E('videoscale'), E('videorate'),
    E('capsfilter', props={'caps': f'video/x-raw,format=I420,width={width},height={height},framerate={fps}/1'}),
)
audio_tail = (
    E('audioconvert'), E('audioresample'),
    E('capsfilter', props={'caps': 'audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved'}),
)
# Program-timeline continuity (F-08). ``filesink append=True`` below keeps the
# bytes already aired, and the program keeps ONE timeline across scheduled -> live
# -> scheduled because the muxer never restarts: leg changes are ``reload_program``
# calls on the running pipeline (see ``apply_leg``). Repairing the timestamps after
# a restart is not possible against the pinned muxer -- it writes
# ``running_time + 3600 s`` (its own CLOCK_BASE) and re-derives that on every
# restart; all four repair paths are measured ineffective in
# outputs/beta-fixes-2026-10-28/evidence/area3-continuity-probe.json and
# area3-idle-live-probe.json.
program = config['program']
# One sub-chain per repeat: the donor's playlist leg concatenates them with no
# output restart, so the scheduled program keeps airing across its own duration.
copies = max(1, min(int(config.get('loop_copies', 12)), 32))
schedule_leg = graphmod.PlaylistLeg(
    label='schedule',
    subchains=tuple(
        (E('filesrc', props={'location': program}), E('decodebin'), *video)
        for _ in range(copies)
    ),
    audio_tail=audio_tail,
)
legs = [schedule_leg]


def build_live_leg():
    """Build the live override leg on demand (only while it is actually airing).

    A live leg whose configured input sends no packets holds the whole pipeline's
    preroll (measured: 30 s PrerollTimeoutError with zero output), which is why it
    is not in the starting graph. It is built when the durable decision names it,
    and the engine's ``reload_program`` swaps it in on the running pipeline, so the
    muxer (and therefore the program timeline) never restarts.

    It is a ONE-sub-chain playlist rather than a bare SourceLeg: a SourceLeg's
    optional ``audio`` chain is a static element chain with no decoder, so a
    compressed MPEG-TS live input has no decoded audio pad to feed it and the mux
    never prerolls. A playlist sub-chain ending in ``decodebin`` has its audio pad
    linked into ``audio_tail`` by the engine.
    """
    if not config.get('live'):
        return None
    return graphmod.PlaylistLeg(
        label='live',
        subchains=((
            E('udpsrc', props={
                'address': config['live']['address'],
                'port': int(config['live']['port']),
                'caps': 'video/mpegts,systemstream=true,packetsize=188',
                'do-timestamp': True,
            }),
            # Bounded, leaky: while this leg is NOT selected its stale packets
            # are dropped rather than backlogged, so selecting it jumps the air
            # signal to the current live edge.
            E('queue', props={'max-size-buffers': 0, 'max-size-time': 0,
                'leaky': 2, 'silent': True}),
            # ``async-handling`` (a GstBin property on decodebin) is what lets a
            # SILENT override input coexist with the schedule in one pipeline: the
            # parent stops waiting for this branch, so preroll finishes on the
            # schedule leg. Without it the whole pipeline stalls while no live
            # packets arrive (measured: area3-idle-live-probe.json), which is why
            # this adapter used to be restarted per leg set -- and every restart
            # reset the program timeline (F-08).
            E('decodebin', props={'async-handling': True}), *video),),
        audio_tail=audio_tail,
    )


_live_leg = build_live_leg()
if _live_leg is not None:
    legs.append(_live_leg)

# ``identity sync=True`` paces the emitted program against the pipeline clock so
# both legs air in real time. The only destination is the Station-owned file
# below -- no donor default sink, no external publish target.
graph = graphmod.PlayoutGraph(
    sources=tuple(legs),
    encoder=graphmod.encode_chain_specs(width=width, height=height, fps=fps,
        bitrate_kbps=int(profile['bitrate_kbps']), gop=fps),
    audio_encoder=graphmod.audio_encode_specs(codec='voaacenc', sample_rate=48000),
    mux=E('mpegtsmux', name='mux'),
    sinks=((E('queue'), E('identity', props={'sync': True}),
        # ``append``: a restarted adapter (a new committed program, or a recovered
        # failure) keeps the bytes already aired instead of discarding them.
        E('filesink', props={'location': config['output'], 'append': True})),),
)
engine = enginemod.GstPlayoutEngine(graph)

from gi.repository import GLib  # noqa: E402  (needs the pinned runtime)

control_path = Path(config['control'])
deadline = time.monotonic() + float(config.get('max_seconds', 3600))
state = {'leg': -1, 'eos': False, 'started': time.monotonic()}


def receipt(**fields):
    print(json.dumps(fields, default=str), flush=True)


def apply_leg(leg):
    """Select one leg on the donor's input-selector (video and audio together).

    The selector swap happens INSIDE one running pipeline, so the muxer never
    restarts and the emitted program keeps one continuous timeline across
    scheduled -> live -> scheduled (F-08).
    """
    if leg == state['leg'] or state['eos'] or not 0 <= leg < len(legs):
        return
    if getattr(engine, 'selector', None) is None:
        return  # pipeline not built yet; the next poll tick retries
    engine.swap.swap_to(leg)
    state['leg'] = leg
    receipt(event='air-leg', leg=leg, label=legs[leg].label,
        at_seconds=round(time.monotonic() - state['started'], 3))


def request_stop(reason):
    """Stop the channel the way the donor's own ``stop`` control verb does."""
    if state['eos']:
        return False
    state['eos'] = True
    receipt(event=reason, at_seconds=round(time.monotonic() - state['started'], 3))
    loop = getattr(engine, '_loop', None)
    if loop is not None:
        loop.quit()
        return False
    # No main loop (engine not running yet): exit the poll source so the adapter
    # cannot sit forever with nothing to air.
    return False


def poll():
    """Apply the Station supervisor's durable decision to the running pipeline."""
    try:
        wanted = json.loads(control_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        wanted = None
    if wanted:
        if wanted.get('stop'):
            return request_stop('air-stop')
        try:
            apply_leg(int(wanted.get('leg', 0)))
        except Exception as exc:  # a refused swap must never kill the channel
            receipt(event='swap-error', detail=repr(exc))
    if time.monotonic() > deadline:
        return request_stop('air-deadline')
    return True


receipt(event='air-ready', legs=len(legs), copies=copies,
    program=Path(program).name, live=bool(config.get('live')),
    worker=str(source / 'civiccast/egress/gst/worker.py'))
GLib.timeout_add(100, poll)
result = engine.run_forever()
receipt(event='air-result', leg=state['leg'], result=result)
# The donor worker force-exits after teardown; mirror that so a stalled main
# loop can never leave an orphan encoder behind.
os._exit(0 if isinstance(result, dict) and result.get('error') is None
         and result.get('teardown_clean') else 1)
