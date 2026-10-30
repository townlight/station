# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Atomic rolling WAV segments for the GStreamer live-caption audio fork.

Item 88 (measured in sandbox run 17, soak-a6d7871-20260906-213332Z, Opus
diagnosis): every worker reached PLAYING and pushed real TS output at
~3.2 Mbps for 26-100s, then output stopped and the 10s stall watchdog killed
the worker. Root cause: this writer used to do its blocking I/O (WAV close,
``flush``, ``os.fsync``, atomic ``replace``) directly on the appsink's
``new-sample`` callback -- which runs on the GStreamer STREAMING thread, the
same thread the caption-audio-tap ``queue`` element's default (non-leaky,
1s-deep) buffering can only absorb for so long. Once ``fsync``/rename took
long enough (two ffmpeg jobs + a Whisper ASR pass sharing the box), the
queue backed up, the tee it forks from stalled, and the mpegtsmux audio pad
that shares the same tee starved -- taking the CHANNEL off air over a
caption side-channel that was never supposed to be able to do that.

The fix: this module now only ever does fast, in-memory work
(``write_pcm_s16le`` appends PCM bytes to a plain ``bytearray`` under a
lock that guards ONLY that bookkeeping, never I/O) and hands a finished
segment's bytes to ``SegmentWriterThread``, a dedicated background thread
that owns all the blocking I/O. The hand-off itself
(``SegmentWriterThread.submit``) is non-blocking: a bounded queue that
drops the OLDEST pending segment (not the newest, and never by blocking)
when full -- the drop is COUNTED on the caller's thread (a plain lock
increment) but LOGGED only from the writer thread itself (see
``_maybe_log_drop``), at most once a minute, so no ``print`` (a real,
if usually fast, I/O call) ever runs on the streaming thread either.
A slow disk now degrades caption continuity -- a dropped few seconds of
caption audio -- instead of ever being able to stall the tee, and
therefore the mux, and therefore the channel.

Round-2 review BLOCKER: stopping this writer must itself be bounded. A
wedged disk (the exact failure this module exists to survive) can leave
``SegmentWriterThread`` stuck inside a single blocking ``_publish`` call
indefinitely -- Python cannot interrupt a thread out of a blocking syscall,
so ``stop()``/``close()`` cannot force that thread to exit. What they CAN
guarantee is that the CALLER is never blocked past a bound: ``stop()`` signals
a ``threading.Event`` (never an unbounded queue ``put`` -- a full queue would
otherwise make even the STOP signal block) and joins with a timeout. If the
thread is still alive after that bound, ``stop()``/``close()`` log a WARNING
and return ``"abandoned"`` rather than waiting further; the (daemon) thread is
left running in the background and may finish later or never. Callers that
need "did everything really land on disk" must check the return value --
``"drained"`` is the only value that promises that.

Companion fix on the GStreamer graph side (``engine.py``'s
``_build_audio_tap`` / ``_audio_tap_element_specs``): the
``caption_audio_tap_queue`` element is now ``leaky=2`` (downstream -- drop
the OLDEST buffered data rather than block upstream) with its ``max-size-time``
default disabled (see that module's own comment for why bytes/buffers stay at
GStreamer's stock defaults), and the appsink is ``drop=True``. Both layers
exist because they guard different failure points: the queue protects the tee
from a slow appsink callback; this module's own bounded queue protects the
appsink callback from a slow disk. Removing either layer re-opens a path for
the caption side-channel to take the channel off air.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, Final, Literal

TAP_SAMPLE_RATE_HZ: Final[int] = 16_000
_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(r"chunk-(\d{6})\.wav(?:\.partial)?\Z")

#: The segment filename format (``chunk-{index:06d}.wav``) is a fixed
#: six-digit field -- ``_SEGMENT_RE`` above only ever matches exactly six
#: digits. Restored from the pre-item-88 ``_open_segment`` (removed in that
#: rewrite without carrying the guard forward): past this index the filename
#: would silently grow a seventh digit, becoming invisible to
#: ``_discover_next_index`` on the next restart (a different worker/writer
#: could then reuse -- and collide with -- an already-published index).
#: Effectively unreachable in practice (a channel would need ~58 YEARS of
#: continuous 5s segments to get here), but a silent, undiscoverable-filename
#: failure mode is worse than a loud, immediate one.
_MAX_SEGMENT_INDEX: Final[int] = 999_999

#: How many finished-but-unpublished segments the writer thread's queue holds
#: before it starts dropping the OLDEST one to accept a new one. 8 segments
#: at the default 5s ``segment_seconds`` is 40s of caption audio buffered
#: against transient disk contention -- generous headroom without letting an
#: indefinitely wedged disk grow unbounded memory.
_DEFAULT_QUEUE_MAXSIZE: Final[int] = 8

#: Rate-limit for the "queue full, dropped a segment" warning and the
#: "publish failed" error -- item 88's own root cause was an I/O path
#: (implicitly) reachable from the streaming thread; a log line printed on
#: every single drop/failure under sustained contention would itself become
#: a source of overhead, and both are logged from the writer thread now
#: regardless (see ``_maybe_log_drop`` and ``run()``), so this bound also
#: keeps a 100%-failing disk from spamming stderr forever. One line per
#: minute is plenty to see either condition on a live soak.
_LOG_INTERVAL_S: Final[float] = 60.0

#: How long ``stop()``/``close()`` wait for the writer thread to drain before
#: giving up and abandoning it in the background (Round-2 review BLOCKER --
#: see the module docstring). Deliberately similar in magnitude to
#: ``GstPlayoutEngine.teardown_timeout_s``'s own 5s default so a wedged
#: caption tap and a wedged pipeline teardown add comparable, not
#: compounding, delay to the worker's overall bounded-teardown budget.
_DEFAULT_STOP_TIMEOUT_S: Final[float] = 5.0

DrainStatus = Literal["drained", "abandoned"]


def read_program_boundary(tap_dir: Path) -> tuple[str, int]:
    """Read the selected-audio writer's atomic program fence (legacy: no fence)."""
    try:
        payload = json.loads((tap_dir / "program-boundary.json").read_text())
    except FileNotFoundError:
        return "", 0
    token, first_segment = payload["token"], payload["first_segment"]
    if not isinstance(token, str) or not token or not isinstance(first_segment, int):
        raise ValueError("invalid caption program boundary")
    return token, first_segment


class SegmentWriterThread(threading.Thread):
    """Background daemon thread that owns every blocking write for the
    caption audio tap -- WAV encode, ``flush``, ``os.fsync``, and the atomic
    ``partial.replace(target)`` publish. Runs OFF the GStreamer streaming
    thread; see the module docstring for why that boundary is load-bearing.

    ``submit()`` is the only method the producer (the appsink's
    ``new-sample`` callback, via ``RollingWavSegmentWriter``) ever calls, and
    it never blocks: a full queue drops its OLDEST entry (never the newest,
    and never by waiting) to make room, then enqueues the new segment. If a
    concurrent consumer races the drop and refills the slot first, the NEW
    segment is dropped instead rather than retrying in a way that could
    block -- either way ``submit`` returns immediately.

    ``stop()`` is signalled via a ``threading.Event``, never by enqueueing a
    sentinel through the same bounded queue ``submit()`` uses -- a full queue
    would otherwise make even the stop signal itself block (Round-2 review
    BLOCKER; see the module docstring)."""

    def __init__(
        self,
        tap_dir: Path,
        sample_rate_hz: int,
        *,
        maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        fsync: Callable[[int], None] = os.fsync,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name="caption-audio-tap-writer", daemon=True)
        self.tap_dir = tap_dir
        self.sample_rate_hz = sample_rate_hz
        self._fsync = fsync
        self._monotonic = monotonic
        self._queue: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=maxsize)
        self._stop_event = threading.Event()

        self._drop_lock = threading.Lock()
        self._dropped_total = 0
        self._drop_log_pending = False
        self._last_drop_log_t = 0.0

        # Round-2 review BLOCKER: publish failures used to be invisible --
        # an unbounded ``publish_errors`` list nobody read, and ``_error``
        # was never set anywhere. A 100%-failing tap (e.g. a full disk) must
        # be VISIBLE without being made FATAL to air -- captions are a
        # side-channel, not the broadcast itself. ``consecutive_publish_
        # failures`` is a cheap health signal callers (e.g. a future engine-
        # side health surface) can poll; it resets to 0 on any successful
        # publish, so it reads "how long has this been broken right now",
        # not "how many failures ever". ``last_publish_error`` holds the most
        # recent failure's ``repr()`` (a string, not the exception object
        # itself, so nothing here can ever grow an unbounded object graph).
        self._failure_lock = threading.Lock()
        self.consecutive_publish_failures = 0
        self.last_publish_error: str | None = None
        self._last_failure_log_t = 0.0

    def submit(self, index: int, pcm: bytes) -> None:
        """Non-blocking hand-off of one finished segment's PCM bytes."""

        try:
            self._queue.put_nowait((index, pcm))
            return
        except queue.Full:
            pass
        dropped = True
        try:
            self._queue.get_nowait()  # drop the OLDEST pending segment
        except queue.Empty:
            dropped = False  # a consumer drained it first; no drop needed
        try:
            self._queue.put_nowait((index, pcm))
        except queue.Full:
            # A consumer refilled the freed slot before we could -- drop
            # THIS segment rather than ever retrying/blocking.
            dropped = True
        if dropped:
            # Count here (cheap, lock-only) but never PRINT here -- printing
            # is I/O, and keeping I/O off the streaming thread is this
            # module's whole point. The writer thread itself logs, rate-
            # limited, from ``_maybe_log_drop`` (called only from ``run()``).
            with self._drop_lock:
                self._dropped_total += 1
                self._drop_log_pending = True

    def _maybe_log_drop(self) -> None:
        """Writer-thread-only: print the rate-limited drop warning if one is
        pending. Never called from ``submit()`` (see there for why)."""
        with self._drop_lock:
            if not self._drop_log_pending:
                return
            now = self._monotonic()
            if now - self._last_drop_log_t < _LOG_INTERVAL_S:
                return
            self._last_drop_log_t = now
            self._drop_log_pending = False
            total = self._dropped_total
        print(
            f"WARN: caption audio tap writer queue full; dropped {total} segment(s) "
            "so far (rate-limited: at most one line per 60s)",
            file=sys.stderr,
            flush=True,
        )

    def run(self) -> None:
        # Poll with a bounded timeout rather than an unbounded ``get()`` so
        # the stop event is checked regularly even while idle, and so a
        # pending drop warning still gets logged (rate-limited) during a
        # quiet stretch. 0.5s adds, at most, that much latency to a
        # cooperative shutdown -- bounded and cheap.
        while True:
            self._maybe_log_drop()
            try:
                index, pcm = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            try:
                self._publish(index, pcm)
                with self._failure_lock:
                    self.consecutive_publish_failures = 0
            except Exception as exc:  # never let the writer thread die silently
                self._log_publish_failure(index, exc)
            if self._stop_event.is_set() and self._queue.empty():
                return

    def _log_publish_failure(self, index: int, exc: Exception) -> None:
        with self._failure_lock:
            self.consecutive_publish_failures += 1
            self.last_publish_error = repr(exc)
            count = self.consecutive_publish_failures
            now = self._monotonic()
            if now - self._last_failure_log_t < _LOG_INTERVAL_S:
                return
            self._last_failure_log_t = now
        print(
            f"ERROR: caption audio tap writer failed to publish segment {index}: {exc!r} "
            f"({count} consecutive failure(s); rate-limited: at most one line per 60s)",
            file=sys.stderr,
            flush=True,
        )

    def _publish(self, index: int, pcm: bytes) -> None:
        if index > _MAX_SEGMENT_INDEX:
            raise RuntimeError("caption audio tap exhausted its six-digit segment sequence")
        target = self.tap_dir / f"chunk-{index:06d}.wav"
        partial = target.with_suffix(".wav.partial")
        if target.exists() or partial.exists():
            raise RuntimeError(f"caption audio tap segment path already exists: {target}")
        raw_file: BinaryIO = partial.open("xb")
        try:
            wave_file = wave.open(raw_file, "wb")  # noqa: SIM115 - same lifecycle
            wave_file.setnchannels(1)
            wave_file.setsampwidth(2)
            wave_file.setframerate(self.sample_rate_hz)
            wave_file.writeframesraw(pcm)
            wave_file.close()
            raw_file.flush()
            self._fsync(raw_file.fileno())
            raw_file.close()
            partial.replace(target)
        except Exception:
            if not raw_file.closed:
                raw_file.close()
            partial.unlink(missing_ok=True)
            raise

    def stop(self, *, timeout: float | None = _DEFAULT_STOP_TIMEOUT_S) -> DrainStatus:
        """Signal the writer thread to drain and exit, bounded by ``timeout``.

        Returns ``"drained"`` if the thread exited within the bound (having
        flushed everything queued at the time ``stop()`` was called), or
        ``"abandoned"`` if it is still alive after the bound -- e.g. wedged
        inside a single slow/hung ``_publish`` call, which Python cannot
        interrupt from another thread. On ``"abandoned"``, the (daemon)
        thread is left running in the background rather than waited on
        further, so a stuck disk can never make THIS call block past
        ``timeout`` -- the caller must treat an ``"abandoned"`` result as
        "some segments may still be pending or lost", never as "drained"."""
        self._stop_event.set()
        self.join(timeout=timeout)
        if self.is_alive():
            print(
                f"WARN: caption audio tap writer thread did not stop within {timeout}s "
                "(likely wedged I/O) -- abandoning it in the background; any segment "
                "still queued or in flight may never publish",
                file=sys.stderr,
                flush=True,
            )
            return "abandoned"
        return "drained"


class RollingWavSegmentWriter:
    """Turn a stream of mono signed-16-bit PCM into atomically published WAVs.

    ``write_pcm_s16le`` never blocks on disk I/O: it only appends bytes to an
    in-memory buffer and, once a segment's worth has accumulated, hands the
    finished segment to a ``SegmentWriterThread`` for publishing. See the
    module docstring for why (item 88), and ``close()`` for why draining is
    bounded rather than unconditional (Round-2 review BLOCKER)."""

    def __init__(
        self,
        tap_dir: str | Path,
        *,
        segment_seconds: float = 5.0,
        sample_rate_hz: int = TAP_SAMPLE_RATE_HZ,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        fsync: Callable[[int], None] = os.fsync,
    ) -> None:
        if segment_seconds <= 0:
            raise ValueError("caption audio tap segment_seconds must be positive")
        if sample_rate_hz <= 0:
            raise ValueError("caption audio tap sample_rate_hz must be positive")
        self.tap_dir = Path(tap_dir).expanduser()
        self.tap_dir.mkdir(parents=True, exist_ok=True)
        self.segment_seconds = float(segment_seconds)
        self.sample_rate_hz = int(sample_rate_hz)
        frames = max(1, round(self.segment_seconds * self.sample_rate_hz))
        self._segment_bytes = frames * 2
        self._next_index = self._discover_next_index()
        self._buffer = bytearray()
        self._program_token: str | None = None
        self._closed = False
        self._lock = threading.Lock()  # guards ONLY the in-memory buffer/index bookkeeping
        self._writer_thread = SegmentWriterThread(
            self.tap_dir, self.sample_rate_hz, maxsize=queue_maxsize, fsync=fsync
        )
        self._writer_thread.start()

    @property
    def consecutive_publish_failures(self) -> int:
        """Health signal: how many publishes have failed in a row right now
        (0 once any publish succeeds). Never fatal to air on its own -- a
        caller may choose to surface this, but this module never acts on it
        beyond logging (see ``SegmentWriterThread``)."""
        return self._writer_thread.consecutive_publish_failures

    @property
    def last_publish_error(self) -> str | None:
        """The most recent publish failure's ``repr()``, or ``None``."""
        return self._writer_thread.last_publish_error

    def _discover_next_index(self) -> int:
        observed = -1
        for path in self.tap_dir.rglob("chunk-*.wav*"):
            match = _SEGMENT_RE.fullmatch(path.name)
            if match is not None and path.is_file():
                observed = max(observed, int(match.group(1)))
        return observed + 1

    def _submit_locked(self, segment: bytes) -> None:
        """Caller must hold ``self._lock``. Hands one finished segment to
        the writer thread -- non-blocking (see ``SegmentWriterThread.submit``).
        Raises if the six-digit segment sequence is exhausted (restored
        guard -- see ``_MAX_SEGMENT_INDEX``), consistent with the pre-item-88
        ``_open_segment``'s own behavior for the same condition."""
        if self._next_index > _MAX_SEGMENT_INDEX:
            raise RuntimeError("caption audio tap exhausted its six-digit segment sequence")
        index = self._next_index
        self._next_index += 1
        self._writer_thread.submit(index, segment)

    def begin_program(self, token: str) -> None:
        """Fence old queued WAVs and discard audio straddling a program change.

        The engine rejects queued samples below the switch running-time boundary.
        Indices keep increasing; delayed disk writes from the outgoing program
        remain below this fence. The token also rejects an already-mapped sample
        whose callback races this reset before taking the buffer lock.
        """
        with self._lock:
            self._buffer.clear()
            self._program_token = token
            first_segment = self._next_index
        target = self.tap_dir / "program-boundary.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"token": token, "first_segment": first_segment}))
        temporary.replace(target)

    def write_pcm_s16le(
        self, pcm: bytes | bytearray | memoryview, *, program_token: str | None = None
    ) -> None:
        """Append whole mono s16le samples, rotating at the configured duration.

        Non-blocking: this method only touches the in-memory buffer and the
        writer thread's non-blocking queue -- it never waits on disk I/O, so
        it is safe to call from a GStreamer streaming-thread callback."""

        body = bytes(pcm)
        if len(body) % 2:
            raise ValueError("caption audio tap accepts only whole 16-bit samples")
        if not body:
            return
        with self._lock:
            if program_token != self._program_token:
                return
            if self._closed:
                raise RuntimeError("caption audio tap writer is closed")
            self._buffer.extend(body)
            while len(self._buffer) >= self._segment_bytes:
                # Check the sequence guard BEFORE consuming from the buffer --
                # a caller that catches this ``RuntimeError`` and retries
                # should not find its audio already silently discarded.
                if self._next_index > _MAX_SEGMENT_INDEX:
                    raise RuntimeError("caption audio tap exhausted its six-digit segment sequence")
                segment = bytes(self._buffer[: self._segment_bytes])
                del self._buffer[: self._segment_bytes]
                self._submit_locked(segment)

    def close(self, *, timeout: float | None = _DEFAULT_STOP_TIMEOUT_S) -> DrainStatus:
        """Publish a non-empty trailing segment and make subsequent writes
        fail.

        Round-2 review BLOCKER: this is bounded by ``timeout``, NOT
        unconditional -- a wedged disk can leave the writer thread stuck
        inside a single blocking publish forever, and Python cannot
        interrupt a thread out of a blocking syscall. Returns ``"drained"``
        if the writer thread exited within the bound (everything queued at
        close time is on disk), or ``"abandoned"`` if it is still alive
        after the bound (the daemon thread is left running in the
        background; a trailing segment, or more, may never publish). Callers
        that need "segments really are on disk" must check the return value
        rather than assuming it from a bare successful return, unlike this
        method's pre-item-88 predecessor (which really was unconditional,
        because it did the I/O synchronously, on the caller's own thread)."""

        with self._lock:
            if self._closed:
                return "drained"
            self._closed = True
            if self._buffer:
                segment = bytes(self._buffer)
                self._buffer.clear()
                self._submit_locked(segment)
        return self._writer_thread.stop(timeout=timeout)

    def __enter__(self) -> RollingWavSegmentWriter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = ["TAP_SAMPLE_RATE_HZ", "RollingWavSegmentWriter", "SegmentWriterThread"]
