# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Isolated Whistle live transcription with recoverable Whisper fallback."""

from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import json
import logging
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol, cast

from civiccast.captions.models import AudioChunk, CaptionHypothesis, CaptionWord, CustomVocabulary
from civiccast.captions.runtime import CaptionRuntime

_LOG = logging.getLogger(__name__)
WEIGHTS_SHA256 = "b6e02f048568ac5d01a2042556c658061e699acbc0aa2a1439f52f3d461dffeb"
LIBRARY_SHA256 = "de2e2c39cd311fbd9971fad4736abc329ed970653674c203e149c4ef27fd1c62"
_FALLBACK_RETRY_COOLDOWN_SECONDS = 30.0
_WHISTLE_REQUEST_TIMEOUT_SECONDS = 30.0
_WHISPER_REQUEST_TIMEOUT_SECONDS = 120.0
_SLOW_REQUEST_WARNING_SECONDS = 10.0
_RECOVERY_INITIAL_COOLDOWN_SECONDS = 30.0
_RECOVERY_MAX_COOLDOWN_SECONDS = 300.0
_STDERR_RING_BYTES = 16 * 1024
_STDERR_LOG_TAIL_CHARS = 2048


@dataclass(slots=True)
class _ChannelRecovery:
    next_attempt_at: float
    retries: int = 0
    latest_chunk: AudioChunk | None = None
    latest_vocabulary: CustomVocabulary | None = None


def _recovery_retry_delay(retries: int) -> float:
    """After the initial 30s cooldown, use 60/120/240s and cap at five minutes."""

    exponent = min(max(retries - 1, 0), 3)
    # The bounded nonnegative int exponent makes int power return int; mypy's
    # general int-power result is Any. Float cooldown arithmetic returns float.
    return min(  # type: ignore[no-any-return]
        _RECOVERY_INITIAL_COOLDOWN_SECONDS * 2 * (2**exponent),
        _RECOVERY_MAX_COOLDOWN_SECONDS,
    )


def _speech_request_timeout(engine: str) -> float:
    if engine == "whistle":
        return _WHISTLE_REQUEST_TIMEOUT_SECONDS
    try:
        timeout = float(
            os.environ.get(
                "CIVICCAST_WHISPER_FALLBACK_TIMEOUT_SECONDS", str(_WHISPER_REQUEST_TIMEOUT_SECONDS)
            )
        )
    except ValueError as exc:
        raise ValueError(
            "Whisper fallback timeout must be finite and between 1 and 600 seconds"
        ) from exc
    if not math.isfinite(timeout) or not 1 <= timeout <= 600:
        raise ValueError("Whisper fallback timeout must be finite and between 1 and 600 seconds")
    return timeout


class _SpeechWorker(Protocol):
    def request(self, chunk: AudioChunk, vocabulary: CustomVocabulary | None) -> object: ...

    def close(self) -> None: ...


class _PreparedCaptionRuntime(CaptionRuntime, Protocol):
    def on_cuda(self) -> bool: ...

    def prepare(self) -> None: ...


class _CloseableCaptionRuntime(_PreparedCaptionRuntime, Protocol):
    def live_provider_status(self, channel: str) -> tuple[str, int | None]: ...

    def close(self) -> None: ...


class _SpeechProcess:
    """Fixed child protocol; assign a suspended child before native loading."""

    def __init__(
        self,
        engine: str,
        weights: Path,
        library: Path,
        fallback_config: Mapping[str, object],
        timeout: float | None = None,
    ) -> None:
        if os.name != "nt":
            raise RuntimeError("Whistle's pinned native engine requires Windows")
        import uuid

        import psutil
        import pywintypes
        import win32api
        import win32job
        import win32process
        import win32security

        self._engine = engine
        self._timeout = timeout if timeout is not None else _speech_request_timeout(engine)
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._responses: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
        self._sequence = 0
        self._process: subprocess.Popen[str] | None = None
        self._job: Any | None = None
        self._stderr: IO[str] | None = None
        self._stderr_tail = bytearray()
        self._stderr_tail_lock = threading.Lock()
        self._stderr_thread: threading.Thread | None = None
        self._closed = False
        try:
            token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), 8)
            try:
                sid = win32security.ConvertSidToStringSid(
                    win32security.GetTokenInformation(token, win32security.TokenUser)[0]
                )
            finally:
                token.Close()
            security = pywintypes.SECURITY_ATTRIBUTES()
            security.bInheritHandle = False
            security.SECURITY_DESCRIPTOR = (
                win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                    f"D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;{sid})", 1
                )
            )
            self._job = win32job.CreateJobObject(security, "CivicCastSpeech-" + uuid.uuid4().hex)
            limits = win32job.QueryInformationJobObject(
                self._job, win32job.JobObjectExtendedLimitInformation
            )
            flags = (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
            )
            limits["BasicLimitInformation"]["LimitFlags"] = flags
            cap = (1 if engine == "whistle" else 4) << 30
            limits["ProcessMemoryLimit"] = cap
            limits["JobMemoryLimit"] = cap
            win32job.SetInformationJobObject(
                self._job, win32job.JobObjectExtendedLimitInformation, limits
            )
            readback = win32job.QueryInformationJobObject(
                self._job, win32job.JobObjectExtendedLimitInformation
            )
            if (
                readback["ProcessMemoryLimit"] != cap
                or readback["JobMemoryLimit"] != cap
                or readback["BasicLimitInformation"]["LimitFlags"] != flags
            ):
                raise RuntimeError("speech child memory limits did not read back")
            # pywin32 311 does not expose CPU rate control. Use the two-field
            # Windows structure directly, retaining typed handles on 64-bit Windows.
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            for name in ("SetInformationJobObject", "QueryInformationJobObject"):
                fn = getattr(kernel, name)
                fn.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong] + (
                    [ctypes.c_void_p] if name.startswith("Query") else []
                )
                fn.restype = ctypes.c_int
            cpu = (ctypes.c_ulong * 2)(5, 2500)
            if not kernel.SetInformationJobObject(
                int(self._job), 15, ctypes.byref(cpu), ctypes.sizeof(cpu)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            read_cpu = (ctypes.c_ulong * 2)()
            if not kernel.QueryInformationJobObject(
                int(self._job), 15, ctypes.byref(read_cpu), ctypes.sizeof(read_cpu), None
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if list(read_cpu) != [5, 2500]:
                raise RuntimeError("speech child CPU cap did not read back")
            env = dict(
                os.environ,
                NEEDLE_TELEMETRY="0",
                DO_NOT_TRACK="1",
                HF_HUB_OFFLINE="1",
                OMP_NUM_THREADS="1",
            )
            # Ensure a source candidate and an installed payload each use their own code.
            env["PYTHONPATH"] = (
                str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get("PYTHONPATH", "")
            )
            argv = [
                sys.executable,
                "-m",
                "civiccast.captions.whistle_worker",
                engine,
                str(weights),
                str(library),
                json.dumps(fallback_config),
            ]
            process = subprocess.Popen(  # noqa: S603 -- fixed module/arguments, no shell or user command
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf8",
                env=env,
                stderr=subprocess.PIPE,
                creationflags=0x4 | 0x08000000 | 0x4000,
            )
            self._process = process
            self._stderr = cast(IO[str], process.stderr)
            process_handle = win32api.OpenProcess(0x501, False, process.pid)
            try:
                win32job.AssignProcessToJobObject(self._job, process_handle)
                if not win32job.IsProcessInJob(process_handle, self._job):
                    raise RuntimeError("speech child job membership unproven")
                threads = psutil.Process(process.pid).threads()
                if len(threads) != 1:
                    raise RuntimeError("suspended child has an unexpected thread count")
                thread_handle = win32api.OpenThread(2, False, threads[0].id)
                try:
                    win32process.ResumeThread(thread_handle)
                finally:
                    thread_handle.Close()
            finally:
                process_handle.Close()
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, daemon=True, name="caption-child-stderr"
            )
            self._stderr_thread.start()
            threading.Thread(target=self._read, daemon=True, name="caption-child-response").start()
            ready = self._receive(30)
            if ready.get("ready") != engine:
                raise RuntimeError(f"speech child failed startup: {ready}")
            _LOG.info(
                "Caption child ready: engine=%s pid=%s cap_bytes=%s device=%s",
                engine,
                process.pid,
                cap,
                ready.get("device"),
            )
        except BaseException:
            self.close()
            raise

    def _read(self) -> None:
        try:
            process = cast(subprocess.Popen[str], self._process)
            stdout = cast(IO[str], process.stdout)
            while not self._closed:
                line = stdout.readline(2 << 20)
                if not line:
                    self._responses.put({"error": "speech child exited"})
                    return
                self._responses.put(json.loads(line))
        except Exception as exc:
            if not self._closed:
                self._responses.put({"error": str(exc)})

    def _drain_stderr(self) -> None:
        stream = self._stderr
        if stream is None:
            return
        reader = getattr(getattr(stream, "buffer", None), "read1", stream.read)
        try:
            while True:
                raw = reader(4096)
                if not raw:
                    return
                data = raw.encode("utf8", "replace") if isinstance(raw, str) else raw
                with self._stderr_tail_lock:
                    self._stderr_tail.extend(data)
                    del self._stderr_tail[:-_STDERR_RING_BYTES]
        except (OSError, ValueError):
            return

    def _receive(
        self,
        timeout: float,
        *,
        deadline_at: float | None = None,
        slow_warning_at: float | None = None,
        request_context: tuple[str, str, int] | None = None,
        request_started_at: float | None = None,
    ) -> dict[str, object]:
        deadline = deadline_at if deadline_at is not None else time.monotonic() + timeout
        warned = False
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                raise TimeoutError("isolated speech child missed its deadline")
            wait_for = remaining
            if slow_warning_at is not None and not warned:
                wait_for = min(wait_for, max(0.001, slow_warning_at - now))
            try:
                result = self._responses.get(timeout=wait_for)
                if slow_warning_at is not None and not warned:
                    now = time.monotonic()
                    if now >= slow_warning_at:
                        self._log_slow_request(
                            request_context,
                            "response",
                            now - (request_started_at if request_started_at is not None else now),
                        )
                break
            except queue.Empty as exc:
                now = time.monotonic()
                if slow_warning_at is not None and not warned and now >= slow_warning_at:
                    warned = True
                    self._log_slow_request(
                        request_context,
                        "response",
                        now - (request_started_at if request_started_at is not None else now),
                    )
                if now >= deadline:
                    raise TimeoutError("isolated speech child missed its deadline") from exc
        if "error" in result:
            raise RuntimeError(result["error"])
        return result

    def _log_slow_request(
        self,
        context: tuple[str, str, int] | None,
        phase: str,
        elapsed: float,
    ) -> None:
        if context is None:
            _LOG.warning("Whistle request is slow: phase=%s elapsed_s=%.3f", phase, elapsed)
            return
        channel, chunk_id, request_id = context
        _LOG.warning(
            "Whistle request is slow: channel=%s request_id=%s chunk=%s phase=%s elapsed_s=%.3f",
            channel,
            request_id,
            chunk_id,
            phase,
            elapsed,
        )

    def _record_child_failure(
        self,
        context: tuple[str, str, int],
        phase: str,
        started_at: float,
        error: BaseException,
    ) -> None:
        process = self._process
        exit_status: int | None = None
        pid = getattr(process, "pid", None)
        if process is not None:
            with contextlib.suppress(Exception):
                exit_status = process.poll()
        cpu_user: float | None = None
        cpu_system: float | None = None
        private_bytes: int | None = None
        if pid is not None:
            with contextlib.suppress(Exception):
                import psutil

                child = psutil.Process(pid)
                cpu = child.cpu_times()
                memory = child.memory_info()
                cpu_user = round(float(cpu.user), 3)
                cpu_system = round(float(cpu.system), 3)
                private_value = getattr(memory, "private", None)
                if private_value is not None:
                    private_bytes = int(private_value)
        stderr_tail = ""
        stderr_lock = getattr(self, "_stderr_tail_lock", None)
        if stderr_lock is not None:
            with stderr_lock:
                stderr_tail = bytes(
                    getattr(self, "_stderr_tail", bytearray())[-_STDERR_LOG_TAIL_CHARS:]
                ).decode("utf8", "replace")
        channel, chunk_id, request_id = context
        _LOG.error(
            "Speech child request failed: engine=%s channel=%s request_id=%s chunk=%s elapsed_s=%.3f "
            "phase=%s exception=%s error=%s exit_status=%s cpu_user_s=%s cpu_system_s=%s "
            "private_bytes=%s stderr_tail=%s",
            getattr(self, "_engine", "unknown"),
            channel,
            request_id,
            chunk_id,
            max(0.0, time.monotonic() - started_at),
            phase,
            type(error).__name__,
            _sanitize_diagnostic_text(str(error), 512),
            exit_status,
            cpu_user,
            cpu_system,
            private_bytes,
            _sanitize_diagnostic_text(stderr_tail, _STDERR_LOG_TAIL_CHARS),
        )

    def request(self, chunk: AudioChunk, vocabulary: CustomVocabulary | None) -> object:
        with self._lock:
            if self._closed:
                raise RuntimeError("speech child is closed")
            self._sequence += 1
            started_at = time.monotonic()
            deadline = started_at + self._timeout
            warning_at = started_at + _SLOW_REQUEST_WARNING_SECONDS
            channel = chunk.chunk_id.rsplit("-tap-", 1)[0]
            context = (channel, chunk.chunk_id, self._sequence)
            phase = "input"
            request: dict[str, object] = {
                "id": self._sequence,
                "chunk": chunk.model_dump(exclude={"pcm_s16le"}),
                "pcm": base64.b64encode(chunk.pcm_s16le).decode("ascii"),
                "vocabulary": vocabulary.model_dump() if vocabulary else None,
            }
            try:
                sent, errors = threading.Event(), []

                def send() -> None:
                    try:
                        process = cast(subprocess.Popen[str], self._process)
                        stdin = cast(IO[str], process.stdin)
                        stdin.write(json.dumps(request) + "\n")
                        stdin.flush()
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        sent.set()

                threading.Thread(target=send, daemon=True, name="caption-child-input").start()
                warned = False
                while not sent.is_set():
                    now = time.monotonic()
                    remaining = deadline - now
                    if remaining <= 0:
                        raise TimeoutError("isolated speech child missed its input deadline")
                    wait_for = remaining
                    if getattr(self, "_engine", "whistle") == "whistle" and not warned:
                        wait_for = min(wait_for, max(0.001, warning_at - now))
                    if sent.wait(wait_for):
                        break
                    now = time.monotonic()
                    if (
                        getattr(self, "_engine", "whistle") == "whistle"
                        and not warned
                        and now >= warning_at
                    ):
                        warned = True
                        self._log_slow_request(context, phase, now - started_at)
                if errors:
                    raise errors[0]
                phase = "response"
                result = self._receive(
                    max(0.001, deadline - time.monotonic()),
                    deadline_at=deadline,
                    slow_warning_at=(
                        warning_at if getattr(self, "_engine", "whistle") == "whistle" else None
                    ),
                    request_context=context,
                    request_started_at=started_at,
                )
                if result.get("id") != self._sequence:
                    raise RuntimeError("speech child response identity mismatch")
                return result["result"]
            except BaseException as exc:
                self._record_child_failure(context, phase, started_at, exc)
                self.close()
                raise

    def close(self) -> None:
        # Independent of the request lock: shutdown must interrupt a stuck call.
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            job, self._job = self._job, None
            try:
                if job is not None:
                    import win32job

                    try:
                        win32job.TerminateJobObject(job, 1)
                    finally:
                        job.Close()
            finally:
                try:
                    if self._process is not None:
                        if self._process.poll() is None:
                            self._process.kill()
                        self._process.wait(timeout=3)
                        for pipe in (self._process.stdin, self._process.stdout):
                            if pipe:
                                pipe.close()
                finally:
                    stderr = getattr(self, "_stderr", None)
                    stderr_thread = getattr(self, "_stderr_thread", None)
                    if (
                        stderr_thread is not None
                        and stderr_thread is not threading.current_thread()
                    ):
                        stderr_thread.join(timeout=0.2)
                    if stderr is not None:
                        stderr.close()


def _sanitize_diagnostic_text(value: str, limit: int) -> str:
    """Keep bounded child diagnostics useful without exposing paths or controls."""

    text = "".join(char for char in value if char in "\t\n\r" or char.isprintable())
    text = re.sub(r"(?i)[A-Z]:[\\/][^\s\"']+", "<path>", text)
    text = re.sub(r"\\\\[^\s\"']+", "<path>", text)
    return re.sub(r"\s+", " ", text).strip()[-limit:]


class WhistleRuntime:
    """Per-channel Whistle isolation with recoverable Whisper fallback.

    Failed primary results are discarded. Recovery probes run in the background,
    share the station-wide primary lock, and never publish their probe transcript.
    """

    channel_workers = 3
    device = "cpu"
    compute_type = "cactus-quants"
    num_workers = 3
    caption_backlog_grace_seconds = int(
        (channel_workers * 2 + 1) * _WHISTLE_REQUEST_TIMEOUT_SECONDS
    )

    def __init__(
        self,
        *,
        weights: Path,
        library: Path,
        fallback_config: Mapping[str, object],
        worker_factory: Callable[[str], _SpeechWorker] | None = None,
    ) -> None:
        self.weights, self.library = Path(weights), Path(library)
        self._factory = worker_factory or (
            lambda engine: _SpeechProcess(engine, self.weights, self.library, fallback_config)
        )
        self._primaries: dict[str, _SpeechWorker] = {}
        self._fallback: _SpeechWorker | None = None
        self._recoveries: dict[str, _ChannelRecovery] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.RLock()
        self._fallback_lock = threading.Lock()
        self._primary_lock = threading.Lock()
        self._recovery_cancel = threading.Event()
        self._recovery_token = 0
        self._recovery_active_token: int | None = None
        self._recovery_channel: str | None = None
        self._recovery_worker: _SpeechWorker | None = None
        self._recovery_thread: threading.Thread | None = None
        self._closed = False
        self._primary_error: str | None = None
        self._fallback_retry_after = 0.0

    def on_cuda(self) -> bool:
        return False  # Whistle itself is always CPU, regardless of fallback.

    def live_provider_status(self, channel: str) -> tuple[str, int | None]:
        """Return the selected engine and recovery countdown without blocking."""

        # Fallback construction holds this guard while Whisper starts. Status
        # reporting must not make the caption scan wait on that initialization.
        if not self._guard.acquire(blocking=False):
            return "unknown", None
        try:
            if self._closed:
                return "unknown", None
            recovery = self._recoveries.get(channel)
            if recovery is not None:
                if self._fallback is None:
                    retry_in = self._fallback_retry_after - time.monotonic()
                    return "unknown", math.ceil(retry_in) if retry_in > 0 else None
                if self._recovery_channel == channel:
                    return "whistle-recovering", None
                retry_in = max(0.0, recovery.next_attempt_at - time.monotonic())
                return "whistle-recovery-cooldown", math.ceil(retry_in)
            if self._primary_error:
                if self._fallback is not None:
                    return "whisper-fallback", None
                retry_in = self._fallback_retry_after - time.monotonic()
                return "unknown", math.ceil(retry_in) if retry_in > 0 else None
            return "whistle-primary", None
        finally:
            self._guard.release()

    def prepare(self) -> None:
        for path, digest in ((self.weights, WEIGHTS_SHA256), (self.library, LIBRARY_SHA256)):
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                self._primary_error = f"Pinned Whistle asset missing or changed: {path}"
                _LOG.warning("%s; live captions will use Whisper", self._primary_error)
                return

    def _fallback_worker(self) -> _SpeechWorker:
        with self._guard:
            if self._closed:
                raise RuntimeError("caption runtime has been closed")
            if self._fallback is None:
                self._fallback = self._factory("whisper")
            return self._fallback

    def _fallback_request(
        self, channel: str, chunk: AudioChunk, vocabulary: CustomVocabulary | None
    ) -> object:
        with self._fallback_lock:
            with self._guard:
                if self._closed:
                    raise RuntimeError("caption runtime has been closed")
                fallback_missing = self._fallback is None
                retry_after = self._fallback_retry_after
            if fallback_missing and time.monotonic() < retry_after:
                raise RuntimeError("Whisper fallback retry cooldown is active")

            try:
                worker = self._fallback_worker()
            except Exception as startup_error:
                return self._retry_fallback_once(
                    channel, chunk, vocabulary, "startup", startup_error
                )

            try:
                result = worker.request(chunk, vocabulary)
            except Exception as request_error:
                return self._retry_fallback_once(
                    channel, chunk, vocabulary, "request", request_error
                )

            with self._guard:
                if self._closed:
                    raise RuntimeError("caption runtime has been closed")
                self._fallback_retry_after = 0.0
            return result

    def _retry_fallback_once(
        self,
        channel: str,
        chunk: AudioChunk,
        vocabulary: CustomVocabulary | None,
        phase: str,
        error: Exception,
    ) -> object:
        self._log_fallback_failure(channel, chunk, phase, error)
        self._discard_fallback()
        with self._guard:
            if self._closed:
                raise RuntimeError("caption runtime has been closed") from error

        try:
            replacement = self._fallback_worker()
        except Exception as replacement_error:
            self._cooldown_after_replacement_failure(
                channel, chunk, "replacement-startup", replacement_error
            )
            raise

        try:
            result = replacement.request(chunk, vocabulary)
        except Exception as replacement_error:
            self._cooldown_after_replacement_failure(
                channel, chunk, "immediate-replay", replacement_error
            )
            raise

        with self._guard:
            if self._closed:
                raise RuntimeError("caption runtime has been closed")
            self._fallback_retry_after = 0.0
        return result

    def _cooldown_after_replacement_failure(
        self, channel: str, chunk: AudioChunk, phase: str, error: Exception
    ) -> None:
        self._log_fallback_failure(channel, chunk, phase, error)
        self._discard_fallback()
        with self._guard:
            if self._closed:
                raise RuntimeError("caption runtime has been closed") from error
            self._fallback_retry_after = time.monotonic() + _FALLBACK_RETRY_COOLDOWN_SECONDS

    def _discard_fallback(self) -> None:
        with self._guard:
            worker = self._fallback
            self._fallback = None
        if worker is not None:
            worker.close()

    @staticmethod
    def _log_fallback_failure(
        channel: str, chunk: AudioChunk, phase: str, error: Exception
    ) -> None:
        _LOG.error(
            "Whisper fallback failed: channel=%s chunk=%s phase=%s exception=%s message=%s",
            channel,
            chunk.chunk_id,
            phase,
            type(error).__name__,
            _sanitize_diagnostic_text(str(error), 512),
        )

    def _start_recovery_if_due(
        self,
        channel: str,
        chunk: AudioChunk,
        vocabulary: CustomVocabulary | None,
    ) -> None:
        with self._guard:
            if self._closed or self._primary_error:
                return
            recovery = self._recoveries.get(channel)
            if recovery is None:
                return
            recovery.latest_chunk = chunk
            recovery.latest_vocabulary = vocabulary
            if (
                self._recovery_active_token is not None
                or time.monotonic() < recovery.next_attempt_at
            ):
                return
            self._recovery_token += 1
            token = self._recovery_token
            self._recovery_active_token = token
            self._recovery_channel = channel
            worker = threading.Thread(
                target=self._run_recovery_probe,
                args=(channel, token),
                daemon=True,
                name="civiccast-whistle-recovery",
            )
            self._recovery_thread = worker
        try:
            worker.start()
        except RuntimeError as exc:
            with self._guard:
                recovery = self._recoveries.get(channel)
                if recovery is not None and self._recovery_active_token == token:
                    recovery.retries += 1
                    recovery.next_attempt_at = time.monotonic() + _recovery_retry_delay(
                        recovery.retries
                    )
                    self._recovery_active_token = None
                    self._recovery_channel = None
            _LOG.error("Whistle recovery worker could not start: channel=%s error=%s", channel, exc)

    def _run_recovery_probe(self, channel: str, token: int) -> None:
        replacement: _SpeechWorker | None = None
        probe_chunk: AudioChunk | None = None
        try:
            with self._primary_lock:
                with self._guard:
                    recovery = self._recoveries.get(channel)
                    if (
                        self._closed
                        or self._recovery_cancel.is_set()
                        or self._recovery_active_token != token
                        or recovery is None
                    ):
                        return
                    probe_chunk = recovery.latest_chunk
                    vocabulary = recovery.latest_vocabulary
                if probe_chunk is None:
                    return

                replacement = self._factory("whistle")
                with self._guard:
                    if (
                        self._closed
                        or self._recovery_cancel.is_set()
                        or self._recovery_active_token != token
                    ):
                        should_close = True
                    else:
                        self._recovery_worker = replacement
                        should_close = False
                if should_close:
                    return

                started = time.monotonic()
                payload = replacement.request(probe_chunk, vocabulary)
                _hypotheses(probe_chunk, payload)
                with self._guard:
                    if (
                        self._closed
                        or self._recovery_cancel.is_set()
                        or self._recovery_active_token != token
                    ):
                        promoted = False
                    else:
                        self._primaries[channel] = replacement
                        self._recoveries.pop(channel, None)
                        self._recovery_worker = None
                        promoted = True
                if promoted:
                    _LOG.info(
                        "Whistle recovery succeeded: channel=%s probe_chunk=%s seconds=%.3f; "
                        "next chunk uses primary",
                        channel,
                        probe_chunk.chunk_id,
                        time.monotonic() - started,
                    )
                    replacement = None
        except Exception as exc:
            with self._guard:
                recovery = self._recoveries.get(channel)
                if (
                    not self._closed
                    and recovery is not None
                    and self._recovery_active_token == token
                ):
                    recovery.retries += 1
                    retry_seconds = _recovery_retry_delay(recovery.retries)
                    recovery.next_attempt_at = time.monotonic() + retry_seconds
                else:
                    retry_seconds = None
            _LOG.warning(
                "Whistle recovery failed: channel=%s probe_chunk=%s retry_in_seconds=%s "
                "exception=%s error=%s",
                channel,
                probe_chunk.chunk_id if probe_chunk is not None else "unknown",
                retry_seconds,
                type(exc).__name__,
                _sanitize_diagnostic_text(str(exc), 512),
            )
        finally:
            if replacement is not None:
                try:
                    replacement.close()
                except Exception:
                    _LOG.exception("Failed Whistle recovery child cleanup failed")
            with self._guard:
                if self._recovery_active_token == token:
                    self._recovery_active_token = None
                    self._recovery_channel = None
                    self._recovery_worker = None

    def _primary_request(
        self, channel: str, chunk: AudioChunk, vocabulary: CustomVocabulary | None
    ) -> object:
        # Serialize station-wide native recognition after the concurrent
        # three-station run exceeded the request deadline and lost queued audio.
        # This does not require transcript agreement before captions can air.
        with self._primary_lock:
            with self._guard:
                if self._is_closed():
                    raise RuntimeError("caption runtime has been closed")
                primary = self._primaries.get(channel)
            if primary is None:
                created = self._factory("whistle")
                with self._guard:
                    if self._closed:
                        created.close()
                        raise RuntimeError("caption runtime has been closed")
                    self._primaries[channel] = primary = created
                    _LOG.info(
                        "Whistle primary active: channel=%s pid=%s",
                        channel,
                        getattr(getattr(created, "_process", None), "pid", "n/a"),
                    )
            started = time.monotonic()
            try:
                result = primary.request(chunk, vocabulary)
            except Exception:
                _LOG.warning(
                    "Whistle inference failed: channel=%s seconds=%.3f",
                    channel,
                    time.monotonic() - started,
                )
                raise
            _LOG.info(
                "Whistle inference completed: channel=%s chunk=%s seconds=%.3f",
                channel,
                chunk.chunk_id,
                time.monotonic() - started,
            )
            return result

    def transcribe(
        self,
        chunks: Iterable[AudioChunk],
        vocabulary: CustomVocabulary | None = None,
    ) -> Iterator[CaptionHypothesis]:
        for chunk in chunks:
            if (
                chunk.sample_rate_hz != 16000
                or not 0 < len(chunk.pcm_s16le) <= 960000
                or len(chunk.pcm_s16le) % 2
            ):
                raise ValueError("Whistle requires <=30 seconds of mono 16 kHz s16le PCM")
            channel = chunk.chunk_id.rsplit("-tap-", 1)[0]
            with self._guard:
                if self._closed:
                    raise RuntimeError("caption runtime has been closed")
                lock = self._locks.setdefault(channel, threading.Lock())
                if len(self._locks) > self.channel_workers:
                    raise RuntimeError("Whistle live runtime supports at most three channels")
            with lock:
                with self._guard:
                    if self._closed:
                        raise RuntimeError("caption runtime has been closed")
                    primary_error = self._primary_error
                    recovering = channel in self._recoveries
                use_fallback = primary_error is not None or recovering
                if recovering:
                    self._start_recovery_if_due(channel, chunk, vocabulary)

                if not use_fallback:
                    try:
                        payload = self._primary_request(channel, chunk, vocabulary)
                        results = _hypotheses(chunk, payload)
                    except Exception as exc:
                        with self._guard:
                            if self._closed:
                                raise RuntimeError("caption runtime has been closed") from exc
                            recovery = self._recoveries.get(channel)
                            if recovery is None:
                                recovery = _ChannelRecovery(
                                    next_attempt_at=(
                                        time.monotonic() + _RECOVERY_INITIAL_COOLDOWN_SECONDS
                                    ),
                                    latest_chunk=chunk,
                                    latest_vocabulary=vocabulary,
                                )
                                self._recoveries[channel] = recovery
                            else:
                                recovery.latest_chunk = chunk
                                recovery.latest_vocabulary = vocabulary
                            worker = self._primaries.pop(channel, None)
                        _LOG.warning(
                            "Whistle failed for %s; switching to Whisper: %s",
                            channel,
                            _sanitize_diagnostic_text(str(exc), 512),
                        )
                        if worker is not None:
                            worker.close()
                        use_fallback = True

                if use_fallback:
                    with self._guard:
                        if self._closed:
                            raise RuntimeError("caption runtime has been closed")
                    if primary_error is None:
                        self._start_recovery_if_due(channel, chunk, vocabulary)
                    payload = self._fallback_request(channel, chunk, vocabulary)
                    fallback_hypotheses = cast(list[dict[str, object]], payload)
                    results = [
                        CaptionHypothesis.model_validate(result) for result in fallback_hypotheses
                    ]
                with self._guard:
                    if self._closed:
                        raise RuntimeError("caption runtime has been closed")
                    yield from results

    def _is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._guard:
            if self._closed:
                return
            self._closed = True
            self._recovery_cancel.set()
            workers = list(self._primaries.values())
            if self._fallback is not None:
                workers.append(self._fallback)
            if self._recovery_worker is not None:
                workers.append(self._recovery_worker)
            self._primaries.clear()
            self._fallback = None
            self._recoveries.clear()
            self._recovery_worker = None
            self._recovery_active_token = None
            self._recovery_channel = None
        for worker in workers:
            try:
                worker.close()
            except Exception:
                _LOG.exception("Speech child cleanup failed; continuing remaining child cleanup")


class MixedCaptionRuntime:
    """Keep ordinary Whisper channels on their existing live runtime."""

    channel_workers = 3
    num_workers = 3
    caption_backlog_grace_seconds = WhistleRuntime.caption_backlog_grace_seconds
    device = "mixed"
    compute_type = "mixed"

    def __init__(
        self,
        whistle: _CloseableCaptionRuntime,
        whisper: _PreparedCaptionRuntime,
        whistle_channels: Collection[str],
    ) -> None:
        if not whistle_channels or len(whistle_channels) > 3:
            raise ValueError("Select between one and three Whistle channels")
        self._whistle = whistle
        self._whisper = whisper
        self._whistle_channels = frozenset(whistle_channels)
        self._whisper_lock = threading.Lock()
        self._closed = False

    def on_cuda(self) -> bool:
        return self._whisper.on_cuda()

    def live_provider_status(self, channel: str) -> tuple[str, int | None]:
        if channel in self._whistle_channels:
            return self._whistle.live_provider_status(channel)
        return "whisper-primary", None

    def prepare(self) -> None:
        if self._closed:
            raise RuntimeError("caption runtime has been closed")
        self._whisper.prepare()
        self._whistle.prepare()
        _LOG.info(
            "Mixed live captions: Whistle channels=%s; other channels use existing Whisper",
            sorted(self._whistle_channels),
        )

    def transcribe(
        self,
        chunks: Iterable[AudioChunk],
        vocabulary: CustomVocabulary | None = None,
    ) -> Iterator[CaptionHypothesis]:
        if self._closed:
            raise RuntimeError("caption runtime has been closed")
        for chunk in chunks:
            channel = chunk.chunk_id.rsplit("-tap-", 1)[0]
            if channel in self._whistle_channels:
                yield from self._whistle.transcribe([chunk], vocabulary)
            elif self._whisper.on_cuda():
                yield from self._whisper.transcribe([chunk], vocabulary)
            else:
                # Preserve the tap's original single-call CPU safety profile.
                with self._whisper_lock:
                    yield from self._whisper.transcribe([chunk], vocabulary)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._whistle.close()


def _hypotheses(chunk: AudioChunk, payload: object) -> list[CaptionHypothesis]:
    typed_payload = cast(Mapping[str, object], payload)
    text = cast(str, typed_payload.get("text", ""))
    if not text.strip():
        return []
    raw_words = cast(list[Mapping[str, object]], typed_payload.get("words", []))
    words: list[CaptionWord] = []
    duration = len(chunk.pcm_s16le) / 32000
    for raw in raw_words:
        start, end = float(cast(float, raw["start"])), float(cast(float, raw["end"]))
        if not 0 <= start <= end <= duration + 0.08:
            raise ValueError("Whistle word timestamp is outside its audio window")
        raw_text = cast(str, raw["word"])
        words.append(
            CaptionWord(
                text=raw_text.strip(),
                start_seconds=chunk.start_seconds + start,
                end_seconds=chunk.start_seconds + end,
                confidence=cast(float, raw.get("probability", 1)),
            )
        )
    return [
        CaptionHypothesis(
            source_id=chunk.chunk_id,
            start_seconds=words[0].start_seconds if words else chunk.start_seconds,
            end_seconds=max(words[-1].end_seconds, words[0].start_seconds + 0.001)
            if words
            else chunk.end_seconds,
            text=text,
            confidence=sum(w.confidence for w in words) / len(words) if words else 1.0,
            words=words,
            audio_window_start_seconds=chunk.start_seconds,
            audio_window_end_seconds=chunk.end_seconds,
        )
    ]
