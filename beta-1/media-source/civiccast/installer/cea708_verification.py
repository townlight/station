# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CEA-708 decode-back verification wired into the S3 commissioning output-proof (S11).

PR #22's commissioning output-proof honestly reported ``cea708_verified: null`` with
a blocker whenever CEA-708 passthrough was requested, because no decode-back check
was wired in yet (``docs/spec/3.0/sections/S3-commissioning-wizard.md``'s
status-correction banner, ``civiccast/installer/commissioning.py``'s
``_NOT_CLAIMED_BOUNDARY``). This module closes that gap with a REAL, fail-closed
check that goes through the product's real caption path end to end:

1. Write a deterministic one-cue WebVTT sidecar (the same timed-text shape the
   channel caption pipeline already produces -- ``civiccast.egress.caption_embed
   .load_caption_cues_from_timed_text`` reads the identical format).
2. Embed it into a bounded, self-contained test-pattern MPEG-TS using the SAME
   GStreamer building blocks the live channel path uses: ``egress/gst/graph.py``'s
   ``demo_test_graph`` + ``caption_embed_leg_from_sidecar`` (the module's own
   docstring names this the "VOD / proof / test" embed leg), run through
   ``egress/gst/worker.py`` as a subprocess via the D2 named-pipe/FIFO control seam
   (``egress/gst/strategy.py``) -- the same building blocks
   ``scripts/prove_native_live_caption_transport.py``'s code already assembles this
   same way for the live appsrc leg (its own execution has not been independently
   verified from this change; only its component pieces are unit-tested in
   ``tests/native/test_live_caption_transport_proof.py``), just with the finite sidecar
   leg instead of the live appsrc leg (commissioning has one known test caption, not
   a live feed). This is NOT a side channel: it is the product's real CEA-708 SEI
   insertion path (``cccombiner``/``h264ccinserter``).
3. Decode the emitted TS back with the existing ENGINE-AGNOSTIC ffmpeg decode-back
   (``civiccast.egress.caption_proof.decode_embedded_captions``) -- the same function
   the S11a ON_AIR proof loop uses -- and compare against the expected cue with
   ``civiccast.egress.caption_embed.evaluate_caption_decode_back``.

Fail-closed throughout: ANY exception embedding (GStreamer engine/plugins not
present on this box, worker subprocess error, timeout) or decoding is caught and
reported as ``verified=False`` with an honest detail string. This module never
reports ``verified=True`` without an actual matching decode of real bytes.

Both legs are independently exercised by the test suite: ``decode_embedded_captions``
against a real, committed MPEG-TS fixture with genuine embedded CEA-608-in-708 SEI
data (``tests/egress/fixtures/cea708_test_caption.mpegts`` -- built and verified
against the real product decode path while developing this module; see
``tests/egress/test_caption_proof.py``), and the embed leg with an injected fake
``embed_runner`` (``tests/installer/test_cea708_verification.py``) since spinning up
the real GStreamer worker requires the bundled native runtime this repo's dev/CI
sandbox does not carry -- that end-to-end path is covered by the
``@pytest.mark.integration`` test here, which skips unless the bundled binaries
are present, and needs a native Windows box with the packaged runtime (or the
WSL/system-GStreamer dev tier) to actually run and prove.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from civiccast.captions.models import CaptionCue
from civiccast.egress.caption_embed import evaluate_caption_decode_back
from civiccast.egress.caption_proof import FfmpegRunner, decode_embedded_captions
from civiccast.egress.gst.graph import (
    caption_embed_leg_from_sidecar,
    demo_test_graph,
    graph_to_json,
)
from civiccast.stream._ffmpeg import run_ffmpeg

TEST_CAPTION_CHANNEL_ID = "commissioning-cea708-proof"
TEST_CAPTION_TEXT = "CIVICCAST CEA-708 COMMISSIONING TEST"
TEST_CAPTION_START_SECONDS = 0.5
TEST_CAPTION_DURATION_SECONDS = 2.5
DEFAULT_EMBED_DURATION_SECONDS = 8
DEFAULT_MUXRATE_KBPS = 2000
DECODE_BACK_DECODER = "ffmpeg-subcc"

CEA708_VERIFICATION_PROOF_BOUNDARY = (
    "cea708-commissioning-embed-through-decode-back: drives the product's real "
    "GStreamer sidecar caption-embed leg (egress/gst/graph.py "
    "caption_embed_leg_from_sidecar) against a bounded self-contained test pattern, "
    "then decodes the emitted TS with the engine-agnostic ffmpeg decode-back "
    "(egress/caption_proof.py decode_embedded_captions); it does not exercise the "
    "live/appsrc caption feed path, a physical SDI/DeckLink caption line, or a real "
    "headend decoder."
)


@dataclass(frozen=True)
class Cea708VerificationResult:
    """Outcome of one embed-through-decode-back CEA-708 proof run."""

    verified: bool
    detail: str
    expected_text: str
    decoded_text: str
    blocker: str | None
    proof_boundary: str = CEA708_VERIFICATION_PROOF_BOUNDARY


def write_test_caption_sidecar(
    path: Path,
    *,
    text: str = TEST_CAPTION_TEXT,
    start_seconds: float = TEST_CAPTION_START_SECONDS,
    end_seconds: float = TEST_CAPTION_START_SECONDS + TEST_CAPTION_DURATION_SECONDS,
) -> Path:
    """Write a deterministic one-cue WebVTT sidecar for the CEA-708 proof.

    This is the product's real caption sidecar shape
    (``civiccast.egress.caption_embed.load_caption_cues_from_timed_text`` reads the
    identical format) -- not a side channel; it feeds the same ``filesrc!subparse``
    embed leg a VOD/proof caption run uses.
    """

    if end_seconds <= start_seconds:
        raise ValueError("end_seconds must be greater than start_seconds")
    path.parent.mkdir(parents=True, exist_ok=True)

    def _stamp(seconds: float) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"

    vtt = f"WEBVTT\n\n1\n{_stamp(start_seconds)} --> {_stamp(end_seconds)}\n{text}\n"
    path.write_text(vtt, encoding="utf-8")
    return path


# (sidecar_path, duration_seconds, muxrate_kbps, work_dir) -> emitted local .ts path.
# Raises on any embed failure; callers are responsible for fail-closed handling.
EmbedRunner = Callable[[Path, int, int, Path], Path]


def _current_process_pipe_sddl() -> str:
    """SYSTEM plus this process's own identity, so a child worker spawned under the
    same token can connect to the pipe this process serves. Duplicated from
    ``scripts/prove_native_live_caption_transport.py``'s ``current_process_pipe_sddl``
    rather than imported -- ``scripts/`` is dev/proof tooling, not part of the
    installable ``civiccast`` package."""

    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    sid, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
    identity = win32security.ConvertSidToStringSid(sid)
    return f"D:P(A;;GA;;;SY)(A;;GA;;;{identity})"


def run_gst_caption_embed_test_pattern(
    sidecar_path: Path,
    duration_seconds: int,
    muxrate_kbps: int,
    work_dir: Path,
    *,
    channel_id: str = TEST_CAPTION_CHANNEL_ID,
    python_executable: str | None = None,
) -> Path:
    """Drive the product's real GStreamer sidecar caption-embed leg to a local file.

    Reuses the same building blocks the live channel path uses
    (``egress/gst/graph.py``'s ``demo_test_graph`` + ``caption_embed_leg_from_sidecar``,
    ``egress/gst/worker.py`` as a subprocess, and the D2 control seam from
    ``egress/gst/strategy.py``) -- the same building blocks
    ``scripts/prove_native_live_caption_transport.py``'s code already assembles this
    same way for the live appsrc leg, just with the sidecar (finite VOD/proof) embed
    leg instead of the live appsrc leg. Not independently verified end to end from
    this change (no GStreamer runtime in this dev/CI sandbox) -- see this module's
    docstring for what is and is not proven.

    Raises ``RuntimeError``/``OSError`` on any failure (GStreamer engine or plugins
    not present, worker process error, timeout, no output bytes) -- this function
    does not itself fail closed; :func:`verify_cea708_decode_back` does that.
    """

    # Lazy: these pull in the gst-adjacent strategy module (pywin32 on Windows),
    # which most commissioning-adjacent callers (and every non-CEA-708 proof run)
    # never need to import at all.
    import civiccast.egress.gst.graph as graph_module
    from civiccast.egress.gst.strategy import (
        GstPlayoutStrategy,
        WindowsWorkerPipeServer,
        _default_worker_launcher,
        _WindowsPipeChannel,
        worker_pipe_name,
    )

    worker_path = str(Path(graph_module.__file__).resolve().parent / "worker.py")

    channel_dir = work_dir / channel_id
    if channel_dir.exists():
        shutil.rmtree(channel_dir)
    channel_dir.mkdir(parents=True)

    output_ts = channel_dir / "captioned.ts"
    graph = replace(
        demo_test_graph(out=str(output_ts), nsrc=1, bitrate_kbps=muxrate_kbps),
        captions=caption_embed_leg_from_sidecar(str(sidecar_path)),
    )
    graph_path = channel_dir / "playout-graph.json"
    graph_path.write_text(graph_to_json(graph), encoding="utf-8")

    strategy = GstPlayoutStrategy()
    if os.name == "nt":
        server = WindowsWorkerPipeServer(
            channel_id, security_descriptor_sddl=_current_process_pipe_sddl()
        )
        pipe_channel = _WindowsPipeChannel(channel_id, server=server)
        pipe_channel.start()
        strategy._pipe_channels[channel_id] = pipe_channel
        control_channel = worker_pipe_name(channel_id)
    else:
        control_channel = str(GstPlayoutStrategy.control_fifo_path(work_dir, channel_id))

    stdout_path = channel_dir / "worker.stdout.log"
    stderr_path = channel_dir / "worker.stderr.log"
    argv = [python_executable or sys.executable, worker_path, str(graph_path), control_channel]
    handle = _default_worker_launcher(argv, stdout_path, stderr_path)
    try:
        # The sidecar-driven graph runs until told to stop (demo_test_graph's
        # sources are continuous videotestsrc, not self-terminating) -- give it the
        # bounded proof window, then request a normal stop over the control seam.
        # Poll rather than blindly sleeping the full window so a worker that fails
        # fast (e.g. the GStreamer engine/plugins are not present on this box) is
        # reported in under a second, not after a multi-second dead wait.
        deadline = time.monotonic() + max(duration_seconds, 1)
        while time.monotonic() < deadline and handle.poll() is None:
            time.sleep(0.2)
        early_exit = handle.poll()
        if early_exit is not None:
            raise RuntimeError(
                f"CEA-708 embed worker exited early with code {early_exit}; see {stderr_path}"
            )
        if not strategy.send_command(work_dir, channel_id, "stop"):
            raise RuntimeError(f"CEA-708 embed worker did not acknowledge stop; see {stderr_path}")
        try:
            handle.process.wait(timeout=15)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"CEA-708 embed worker did not exit after stop; see {stderr_path}"
            ) from exc
        if handle.process.returncode != 0:
            raise RuntimeError(
                f"CEA-708 embed worker exited {handle.process.returncode}; see {stderr_path}"
            )
    finally:
        handle.terminate(grace_seconds=2.0)
        strategy.close_channel(channel_id)

    if not output_ts.exists() or output_ts.stat().st_size == 0:
        raise RuntimeError(f"CEA-708 embed worker produced no output at {output_ts}")
    return output_ts


def verify_cea708_decode_back(
    *,
    duration_seconds: int = DEFAULT_EMBED_DURATION_SECONDS,
    muxrate_kbps: int = DEFAULT_MUXRATE_KBPS,
    work_dir: Path,
    embed_runner: EmbedRunner = run_gst_caption_embed_test_pattern,
    decode_runner: FfmpegRunner = run_ffmpeg,
    text: str = TEST_CAPTION_TEXT,
) -> Cea708VerificationResult:
    """Verify CEA-708 embed -> decode-back through the product's real caption path.

    Fail-closed: catches every exception from embedding OR decoding and reports
    ``verified=False`` with an honest detail string -- never reports
    ``verified=True`` without an actual matching decode of real bytes.
    """

    start_seconds = TEST_CAPTION_START_SECONDS
    end_seconds = min(
        start_seconds + TEST_CAPTION_DURATION_SECONDS,
        max(float(duration_seconds) - 0.5, start_seconds + 1.0),
    )
    expected_cue = CaptionCue(
        cue_id="commissioning-cea708-expected",
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        text=text,
        confidence=1.0,
    )

    sidecar_path = work_dir / TEST_CAPTION_CHANNEL_ID / "captions" / "test.vtt"
    try:
        write_test_caption_sidecar(
            sidecar_path, text=text, start_seconds=start_seconds, end_seconds=end_seconds
        )
        emitted_ts = embed_runner(sidecar_path, duration_seconds, muxrate_kbps, work_dir)
    except Exception as exc:
        return Cea708VerificationResult(
            verified=False,
            detail=f"CEA-708 embed failed: {exc}",
            expected_text=text,
            decoded_text="",
            blocker=f"CEA708_EMBED_FAILED: {exc}",
        )

    try:
        decoded_cues = decode_embedded_captions(
            emitted_ts, runner=decode_runner, source_id=TEST_CAPTION_CHANNEL_ID
        )
    except Exception as exc:
        return Cea708VerificationResult(
            verified=False,
            detail=f"CEA-708 decode-back failed: {exc}",
            expected_text=text,
            decoded_text="",
            blocker=f"CEA708_DECODE_FAILED: {exc}",
        )

    proof = evaluate_caption_decode_back(
        channel_id=TEST_CAPTION_CHANNEL_ID,
        emitted_stream_path=emitted_ts,
        expected_cues=[expected_cue],
        decoded_cues=decoded_cues,
        decoder_name=DECODE_BACK_DECODER,
    )
    decoded_text = " ".join(cue.text for cue in decoded_cues)
    if proof.status == "PASS":
        return Cea708VerificationResult(
            verified=True,
            detail=(
                f"CEA-708 decode-back PASSED: embedded and decoded "
                f"{proof.matched_cue_count} caption(s) matching {text!r} within "
                f"{proof.max_timing_delta_seconds:.2f}s."
            ),
            expected_text=text,
            decoded_text=decoded_text,
            blocker=None,
        )
    return Cea708VerificationResult(
        verified=False,
        detail=(
            f"CEA-708 decode-back FAILED: expected {proof.expected_cue_count} "
            f"caption(s), decoded {proof.decoded_cue_count}, matched "
            f"{proof.matched_cue_count} ({proof.blocker})."
        ),
        expected_text=text,
        decoded_text=decoded_text,
        blocker=proof.blocker or "CEA708_DECODE_BACK_MISMATCH",
    )
