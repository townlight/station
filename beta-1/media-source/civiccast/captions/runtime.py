# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Runtime adapter boundary for CivicCast caption engines."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import wave
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from math import exp, isfinite
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from civiccast.captions.models import AudioChunk, CaptionHypothesis, CaptionWord, CustomVocabulary
from civiccast.native.caption_tiers import (
    CAPTION_TIER_REGISTRY,
    LARGE_V3_TIER_ID,
    CaptionTierBindingError,
    CaptionTierSpec,
)

logger = logging.getLogger(__name__)

#: Kept-alive `os.add_dll_directory` handles, keyed by the directory string
#: already registered -- makes :func:`_ensure_cuda_dll_directory` idempotent
#: per directory (a repeat call for the same directory is a no-op) and, per
#: the stdlib's own contract for `os.add_dll_directory`, keeps each returned
#: `_AddedDllDirectory` handle referenced for the life of the process:
#: letting it get garbage-collected removes the search path it added. Same
#: kept-alive-handle shape as `civiccast.native.gstreamer_runtime`'s own
#: module-level `_DLL_HANDLES`, which solved the identical problem for the
#: staged GStreamer DLLs.
_CUDA_DLL_DIRECTORY_HANDLES: dict[str, object] = {}


def _ensure_cuda_dll_directory() -> None:
    """Register the staged CUDA runtime DLL directory with the Windows
    loader before a cuda-device model load.

    TESTER4 (RTX 5070 Ti), real hardware: with both required DLLs staged
    AND on PATH, faster-whisper's CUDA backend still failed to load them.
    Consistent with a documented Windows/CPython behavior: since Python
    3.8, the loader no longer searches PATH to resolve a DLL's own
    dependent DLLs -- only directories added via `os.add_dll_directory`
    (or a handful of fixed system locations) are searched for THAT. PATH
    alone (`CIVICCAST_WHISPER_DEVICE=cuda`'s PATH prepend in
    `civiccast.native.station_runtime.load_native_station_environment`) is
    therefore necessary for non-Python consumers but not sufficient for
    this one -- the exact problem
    `civiccast.native.gstreamer_runtime.bootstrap_installed_gstreamer_runtime`
    already solved for the staged GStreamer DLLs, via the same fix mirrored
    here: `os.add_dll_directory` with a kept-alive handle.

    Reads `CIVICCAST_CUDA_BIN_DIR` (set by `load_native_station_environment`
    only when cuda was actually selected, alongside the PATH prepend) rather
    than deriving a path itself -- one producer, never a second copy of the
    resolution logic. A no-op when: the variable is unset (cpu selected, or
    an environment station_runtime never touched); the staged directory does
    not exist; this is not Windows; or `os.add_dll_directory` is unavailable
    (any non-Windows CPython). Never raises -- a missing/stale directory here
    is reported by the existing cuda-load-failure fallback in
    `FasterWhisperRuntime._model_instance`, not by this helper.

    Guard order is DELIBERATE: the two platform-independent checks (env var
    set, directory exists) run FIRST, and the two Windows-only checks
    (`os.name`, `os.add_dll_directory`) run LAST. A platform-only guard
    placed first would short-circuit every other check behind it, so a test
    for "no-op when the env var is unset" or "no-op when the directory is
    missing" could pass on a real Windows host for the WRONG reason -- it
    never got far enough to exercise the check it claims to test -- while
    silently masking the same test failing everywhere else. Checking the
    OS-independent conditions first means those tests exercise the same code
    path on every CI runner, Windows or not.
    """

    cuda_bin_dir = os.environ.get("CIVICCAST_CUDA_BIN_DIR", "").strip()
    if not cuda_bin_dir or cuda_bin_dir in _CUDA_DLL_DIRECTORY_HANDLES:
        return
    if not Path(cuda_bin_dir).is_dir():
        return
    if os.name != "nt":
        return
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is None:
        return
    _CUDA_DLL_DIRECTORY_HANDLES[cuda_bin_dir] = add_dll_directory(cuda_bin_dir)


#: Environment variable through which an activated native station DECLARES
#: which caption tier it selected. Written by
#: ``civiccast.native.station_runtime.station_environment`` next to
#: ``CIVICCAST_WHISPER_MODEL_PATH``, so on a real station the tier backing a
#: packaged model path is always explicit -- never inferred, per
#: ``OWNER-DECISION-caption-adaptive-tier.md`` ("tier selection must be
#: explicit, logged, provable").
CAPTION_TIER_ENV_VAR = "CIVICCAST_CAPTION_TIER"

#: CTranslate2 ``intra_threads`` FLOOR for the LIVE caption tap on CPU.
#:
#: The live tap shares its box with playout, and playout is the product. The
#: batch/VOD default of ``0`` ("every core") produced the measured field
#: failure this constant exists to prevent: on tester DESKTOP-VBMA6O5 with
#: three channels ON_AIR and no CUDA, the control plane burned ~247% of a core
#: transcribing audio the tap's own overload handling then discarded, while
#: the three GStreamer playout workers were repeatedly killed by their
#: 10-second no-output stall watchdog.
#:
#: This is the FLOOR the live tap's ``cpu_threads`` never goes below; the
#: actual default a live runtime is built with is
#: :func:`default_live_tap_cpu_threads`, which now scales (capped) with core
#: count instead of always being exactly this value.
#: ``CIVICCAST_WHISPER_CPU_THREADS`` still overrides either one on a station
#: the operator has personally sized, but NOT to "every core": for the LIVE
#: tap this variable is CLAMPED, not honoured verbatim -- ``0`` falls back
#: to the default (:func:`_resolved_whisper_cpu_threads_env`) and anything
#: above :data:`LIVE_TAP_CPU_THREADS_CEILING` is capped at the ceiling, both
#: logged at WARNING. ``0`` ("every core") is restored only for the
#: batch/VOD runtime (``live=False``), which keeps the original fail-fast,
#: no-ceiling behavior.
LIVE_TAP_CPU_THREADS = 1

#: Ceiling on :func:`default_live_tap_cpu_threads` -- item 79 (sandbox
#: candidate 3b, MEASURED: 10 "Caption tap overload" events with a cluster of
#: GStreamer worker stalls inside them, the same root cause as the tester's
#: beta.4 soak). Paired with
#: :func:`civiccast.captions.tap_worker.default_max_channel_workers` retaining
#: a flat 1 on CPU, the CPU live tap's whole steady-state ASR budget stays at
#: most this many CTranslate2 intra-op threads, on any box, however large.
LIVE_TAP_CPU_THREADS_CEILING = 2

#: How many CPUs one point of the live tap's ``cpu_threads`` ceiling is
#: allowed to assume. See :func:`default_live_tap_cpu_threads`.
_CPUS_PER_LIVE_TAP_CPU_THREAD = 8

#: Env override for the live tap's default ``cpu_threads``, checked ahead of
#: :func:`default_live_tap_cpu_threads`'s core-count formula (but still
#: beneath ``CIVICCAST_WHISPER_CPU_THREADS``, the pre-existing override that
#: applies to both the live and batch/VOD runtimes -- see
#: :class:`FasterWhisperRuntime`). Scoped to the live tap deliberately: a
#: batch/VOD pass must not have its thread count changed out from under it
#: by a variable set to protect playout, a station that is never on air
#: while it runs. The native capacity proof
#: (``scripts/prove_native_caption_capacity.py``) is the opposite case, not
#: an exception to this one: it deliberately measures the LIVE tap and
#: passes its own ``--cpu-threads``/``--beam-size`` (both defaulted to the
#: real live sizing, item 79) straight to the constructor, so this
#: live-only variable never reaches it either way.
CAPTION_TAP_CPU_THREADS_ENV_VAR = "CIVICCAST_CAPTION_TAP_CPU_THREADS"

#: A three-channel station produces three new audio segments every segment
#: interval.  A CUDA-backed live runtime therefore needs one CTranslate2
#: worker per channel to keep those calls in flight together; serializing the
#: three calls makes even a healthy 2.75-second GPU inference take about 8.25
#: seconds of wall time against a 5-second segment cadence.  CPU live captions
#: deliberately stay at one worker so playout keeps the machine.
LIVE_TAP_CUDA_NUM_WORKERS = 3

#: Sample rate at which a LIVE chunk's raw PCM is handed to faster-whisper
#: directly, instead of being written to a temporary WAV and decoded back.
#:
#: The temporary-WAV round-trip buys exactly one thing: a call to
#: ``faster_whisper.audio.decode_audio``, which opens the file with PyAV,
#: resamples it to 16 kHz mono s16le, converts the samples back with
#: ``astype(np.float32) / 32768.0`` and then runs an explicit ``gc.collect()``
#: per call (the library's own comment: "this slows down loading the audio a
#: little bit"). MEASURED, U33, three-channel reference station (RTX 5070 Ti,
#: ``medium``/float16, 10 s windows advancing 5 s, 39-window corpus): that
#: stage is 49.7 ms per window with one caller in flight and 98.3 ms per
#: window with the live tap's three channel threads in flight -- 1.98x
#: inflation, 1.94 s -> 3.83 s across the same windows, because every byte of
#: it is Python/GIL work the channel threads contend for. It is the largest
#: *pure-CPU* stage in the live path: at three-way concurrency the other CPU
#: stages are VAD 40.1 ms, word alignment 28.4 ms, feature extraction 19.4 ms
#: and the WAV write 1.1 ms per window. Removing the round-trip measured
#: 1817.5 ms -> 1630.6 ms mean and 1879.9 ms -> 1609.4 ms median per window
#: across the three channels, with the emitted word spans identical to the
#: unmodified path on 24 of 24 windows (U33 b9, arm ``array``).
#:
#: A 16 kHz mono s16le chunk needs none of it. Its samples are already
#: little-endian signed 16-bit at this rate, so the conversion the decoder
#: would have performed is exactly what
#: :func:`_pcm_s16le_to_whisper_audio` does. Any other rate still needs the
#: resampler and keeps the temporary-WAV path, as does every batch/VOD chunk,
#: which has no real-time cadence to miss.
LIVE_TAP_PCM_PASSTHROUGH_RATE_HZ = 16_000


def default_live_tap_cpu_threads() -> int:
    """Default CTranslate2 ``intra_threads`` for the LIVE caption tap.

    Item 79: the flat floor of :data:`LIVE_TAP_CPU_THREADS` (``1``) is
    replaced with a core-count-aware default -- one thread per 8 CPUs, never
    below the floor and never above :data:`LIVE_TAP_CPU_THREADS_CEILING`.
    On the 8-core field station this is still exactly ``1``; a bigger box
    gets a little more headroom without approaching "every core" again,
    which is the exact shape of the original measured field failure (see
    :data:`LIVE_TAP_CPU_THREADS`'s docstring).

    Overridable end-to-end via :data:`CAPTION_TAP_CPU_THREADS_ENV_VAR`
    (``CIVICCAST_CAPTION_TAP_CPU_THREADS``).
    """

    return max(
        LIVE_TAP_CPU_THREADS,
        min(
            LIVE_TAP_CPU_THREADS_CEILING,
            (os.cpu_count() or 1) // _CPUS_PER_LIVE_TAP_CPU_THREAD,
        ),
    )


def _clamped_caption_tap_cpu_threads_env(default: int) -> int:
    """``CIVICCAST_CAPTION_TAP_CPU_THREADS``, CLAMPED rather than fatal.

    Every other ``cpu_threads``-shaped setting in this class fails fast via
    :func:`_env_int`, and should. This one is different in kind, for the same
    reason :func:`civiccast.captions.tap_worker._clamped_env_seconds` is:
    ``FasterWhisperRuntime(live=True)`` is constructed UNGUARDED during
    control-plane startup (``civiccast.app`` calls
    :func:`civiccast.ai_models.runtime.build_caption_runtime` directly, with
    no try/except around it), so raising here does not degrade captions --
    it takes an ACTIVATED STATION OFF AIR over a mistyped thread count for a
    feature that is explicitly best effort.

    ``0`` in particular ("every core") is refused rather than honoured: it
    would silently hand the live tap the exact batch/VOD sizing that
    produced the original DESKTOP-VBMA6O5 field failure this whole module
    exists to prevent.

    So: unparseable or non-positive values fall back to ``default`` (already
    guaranteed to be at least :data:`LIVE_TAP_CPU_THREADS`, i.e. never
    ``0``), logged at WARNING naming the variable, the rejected value, and
    what is being used instead -- visible rather than silent. A value ABOVE
    :data:`LIVE_TAP_CPU_THREADS_CEILING` is capped at the ceiling, also with
    a WARNING: this variable exists to bound the live tap, so a station
    asking for more than the ceiling is still refused, just not silently.
    """

    raw = os.environ.get(CAPTION_TAP_CPU_THREADS_ENV_VAR, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s must be an integer; got %r. Using %d so the live caption tap "
            "still starts -- captions are best effort and must never take a "
            "station off air.",
            CAPTION_TAP_CPU_THREADS_ENV_VAR,
            raw,
            default,
        )
        return default
    if value < 1:
        logger.warning(
            "%s must be at least 1; got %d. Using %d instead -- %s would give "
            "the live tap the batch/VOD sizing ('every core') that produced "
            "the original field failure this module exists to prevent.",
            CAPTION_TAP_CPU_THREADS_ENV_VAR,
            value,
            default,
            "0" if value == 0 else "a negative value",
        )
        return default
    if value > LIVE_TAP_CPU_THREADS_CEILING:
        logger.warning(
            "%s asked for %d CTranslate2 threads for the live caption tap; "
            "capping at %d (LIVE_TAP_CPU_THREADS_CEILING) -- the live tap "
            "shares its box with playout and must never approach 'every "
            "core' again.",
            CAPTION_TAP_CPU_THREADS_ENV_VAR,
            value,
            LIVE_TAP_CPU_THREADS_CEILING,
        )
        return LIVE_TAP_CPU_THREADS_CEILING
    return value


def _resolved_whisper_cpu_threads_env(default: int, *, live: bool) -> int:
    """``CIVICCAST_WHISPER_CPU_THREADS``: FAIL-FAST for batch, CLAMPED for live.

    For the BATCH/VOD runtime (``live=False``) this is exactly the old
    ``_env_int`` behaviour, unchanged: an unparseable or negative value
    raises immediately, and ``0`` ("every core") is honoured -- a
    finalization pass is allowed to use the machine. A batch/offline caller
    is never constructed unguarded at control-plane startup, so failing fast
    here is the same "say so loudly" posture every
    other CTranslate2 setting on this class keeps.

    For the LIVE tap (``live=True``) this is CLAMPED instead, for the same
    reason :func:`_clamped_caption_tap_cpu_threads_env` is:
    ``FasterWhisperRuntime(live=True)`` is constructed UNGUARDED during
    control-plane startup (``civiccast.app`` ->
    :func:`civiccast.ai_models.runtime.build_caption_runtime`, no try/except
    around it), so a mistyped value here must not raise and take an
    activated station off air. Three live-only corrections, each logged at
    WARNING:

    - unparseable or negative -> falls back to ``default``;
    - ``0`` ("every core") is refused even when this GENERIC variable (rather
      than the live-only :data:`CAPTION_TAP_CPU_THREADS_ENV_VAR`) is what set
      it -- an operator setting ``CIVICCAST_WHISPER_CPU_THREADS=0`` almost
      always means "let the batch/finalization pass use every core," and
      honouring that literally for the live tap too would silently hand it
      the exact sizing that produced the DESKTOP-VBMA6O5 field failure this
      module exists to prevent -- falls back to ``default``;
    - anything above :data:`LIVE_TAP_CPU_THREADS_CEILING` is capped at the
      ceiling rather than passed through uncapped.
    """

    raw = os.environ.get("CIVICCAST_WHISPER_CPU_THREADS", "").strip()
    if not raw:
        return default
    if not live:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(
                f"CIVICCAST_WHISPER_CPU_THREADS must be an integer; got {raw!r}."
            ) from exc
        if value < 0:
            raise ValueError(f"CIVICCAST_WHISPER_CPU_THREADS must be at least 0; got {value}.")
        return value
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "CIVICCAST_WHISPER_CPU_THREADS must be an integer; got %r. Using "
            "%d so caption transcription still starts -- captions are best "
            "effort and must never take a station off air.",
            raw,
            default,
        )
        return default
    if value < 0:
        logger.warning(
            "CIVICCAST_WHISPER_CPU_THREADS must be at least 0; got %d. Using %d instead.",
            value,
            default,
        )
        return default
    if value == 0:
        logger.warning(
            "CIVICCAST_WHISPER_CPU_THREADS=0 ('every core') is refused for "
            "the LIVE caption tap -- that is the exact sizing that produced "
            "the DESKTOP-VBMA6O5 field failure this module exists to "
            "prevent. Using %d instead; set CIVICCAST_CAPTION_TAP_CPU_THREADS "
            "if the live tap genuinely needs a different value.",
            default,
        )
        return default
    if value > LIVE_TAP_CPU_THREADS_CEILING:
        logger.warning(
            "CIVICCAST_WHISPER_CPU_THREADS asked for %d CTranslate2 threads "
            "for the LIVE caption tap; capping at %d "
            "(LIVE_TAP_CPU_THREADS_CEILING) -- the live tap shares its box "
            "with playout and must never approach 'every core' again. The "
            "batch/VOD runtime is unaffected by this cap.",
            value,
            LIVE_TAP_CPU_THREADS_CEILING,
        )
        return LIVE_TAP_CPU_THREADS_CEILING
    return value


#: Greedy decoding for the live tap on CPU: beam search costs roughly its
#: width in decoder passes, and the live tap has a hard real-time budget that
#: a VOD pass does not. Overridable with ``CIVICCAST_WHISPER_BEAM_SIZE``.
LIVE_TAP_CPU_BEAM_SIZE = 1

#: Files a tier's pinned inventory carries for PROVENANCE rather than for
#: inference: CTranslate2/faster-whisper never opens them, and an upstream
#: snapshot may legitimately omit them (the pinned ``medium`` snapshot does).
#: Excluded from the runtime's presence gate so this gate keeps checking
#: exactly what "can this model be loaded offline" depends on -- the same
#: five large-v3 files it has always checked.
_NON_INFERENCE_MODEL_FILES = frozenset({".gitattributes", "LICENSE", "LICENSE.md", "README.md"})


def _tier_required_files(spec: CaptionTierSpec) -> tuple[str, ...]:
    """The files ``spec``'s tier must have on disk to be loadable offline.

    Derived from that tier's OWN pinned inventory in
    :data:`civiccast.native.caption_tiers.CAPTION_TIER_REGISTRY` -- the single
    source of truth the pack builder and both installer-side verifiers already
    use -- never hand-transcribed here. Hand-transcribing it is precisely the
    defect this function replaces: a flat, large-v3-shaped literal that
    demanded ``preprocessor_config.json``/``vocabulary.json`` of every tier,
    so the ``medium`` floor tier (``vocabulary.txt``, no preprocessor config)
    could never load.
    """

    return tuple(sorted(set(spec.files) - _NON_INFERENCE_MODEL_FILES))


def caption_tier_required_files(tier_id: str) -> tuple[str, ...]:
    """:func:`_tier_required_files` for a tier id, failing closed on an
    unknown or not-yet-owner-bound tier rather than guessing an inventory."""

    try:
        spec = CAPTION_TIER_REGISTRY[tier_id]
    except KeyError as exc:
        raise FasterWhisperRuntimeUnavailableError(
            f"The activated native station declared an unknown caption tier: {tier_id!r}. "
            "CivicCast will not guess a model file inventory."
        ) from exc
    try:
        return _tier_required_files(spec.require_bound())
    except CaptionTierBindingError as exc:
        raise FasterWhisperRuntimeUnavailableError(
            f"Caption tier {tier_id!r} is not bound to a pinned model identity: {exc}"
        ) from exc


#: Backwards-compatible alias: the large-v3 tier's required files, derived
#: from its pinned inventory instead of being restated. Kept because callers
#: and proofs that are large-v3-specific by construction
#: (``scripts/prove_native_caption_capacity.py``) still name it. NEW code
#: must use :func:`caption_tier_required_files` -- a module-level constant
#: cannot express a per-tier inventory, which is how the large-v3 shape came
#: to be imposed on every tier in the first place.
REQUIRED_LOCAL_MODEL_FILES = _tier_required_files(CAPTION_TIER_REGISTRY[LARGE_V3_TIER_ID])


def _tier_for_model_directory(model_path: Path) -> str | None:
    """The tier whose pinned ``model_directory`` this path's basename IS, or
    ``None`` when the basename names no known tier.

    Exact match only: ``faster-whisper-medium`` is the floor tier and
    ``faster-whisper-large-v3`` is the quality tier. Anything else is
    unidentified, never "probably large-v3".
    """

    name = model_path.name
    for tier_id, spec in CAPTION_TIER_REGISTRY.items():
        if spec.model_directory == name:
            return tier_id
    return None


def _resolve_packaged_model_tier(
    model_path: Path, *, declared_tier: str | None = None
) -> tuple[str | None, tuple[str, ...]]:
    """Resolve ``(tier_id, required_files)`` for a packaged model directory.

    The station's DECLARED tier wins, and when the directory basename also
    names a known tier the two must agree: a station that says ``floor`` but
    points at ``faster-whisper-large-v3`` (or the reverse) is exactly the
    silent cross-tier swap the caption-integrity work exists to prevent, and
    fails closed and loudly here rather than being loaded.

    With no declared tier and an unrecognizable directory name the tier is
    unidentified; the caller then requires the directory to satisfy at least
    one KNOWN tier's inventory completely. That is not a weaker gate -- no
    directory passes without being a complete, recognizable caption model --
    it simply stops one tier's shape from standing in for all of them.
    """

    declared = declared_tier or os.environ.get(CAPTION_TIER_ENV_VAR, "").strip()
    on_disk = _tier_for_model_directory(model_path)
    if declared:
        # The DECLARATION is validated first: an unknown or unbound tier id is
        # reported as exactly that, rather than as a swap against whatever the
        # directory happens to be named.
        required = caption_tier_required_files(declared)
        if on_disk is not None and on_disk != declared:
            raise FasterWhisperRuntimeUnavailableError(
                f"The activated native station declared caption tier {declared!r} but its "
                f"packaged model path is tier {on_disk!r} ({model_path}). CivicCast will not "
                "load a caption model that was silently swapped for another tier."
            )
        return declared, required
    if on_disk is not None:
        return on_disk, caption_tier_required_files(on_disk)
    return None, ()


def _missing_packaged_model_files(model_path: Path, required: tuple[str, ...]) -> list[str]:
    return [name for name in required if not (model_path / name).is_file()]


class CaptionRuntime(Protocol):
    """Protocol implemented by concrete caption runtimes."""

    def transcribe(
        self,
        chunks: Iterable[AudioChunk],
        vocabulary: CustomVocabulary | None = None,
    ) -> Iterable[CaptionHypothesis]:
        """Yield caption hypotheses for the provided audio chunks."""


class FasterWhisperRuntimeUnavailableError(RuntimeError):
    """Raised when the optional faster-whisper runtime is requested but absent."""


class WhisperCppRuntimeUnavailableError(RuntimeError):
    """Raised when the verified native whisper.cpp caption pack is unavailable."""


class WhisperCppRuntime:
    """Offline large-v3 whisper.cpp/Vulkan adapter for native Windows stations.

    The executable and model are external caption-pack assets. Each chunk is
    passed to the pinned CLI without a shell, and the full JSON response is
    validated before it crosses the :class:`CaptionRuntime` boundary. A Vulkan
    backend is mandatory; silent CPU fallback would violate the measured
    three-channel capacity contract.
    """

    def __init__(
        self,
        *,
        executable: Path,
        model: Path,
        threads: int = 4,
        beam_size: int = 5,
        audio_context: int = 512,
        max_segment_chars: int = 42,
        language: str = "en",
        timeout_seconds: float = 60.0,
    ) -> None:
        self.executable = Path(executable).expanduser().resolve()
        self.model = Path(model).expanduser().resolve()
        missing = [str(path) for path in (self.executable, self.model) if not path.is_file()]
        if missing:
            raise WhisperCppRuntimeUnavailableError(
                "The verified native caption pack is missing required files: " + ", ".join(missing)
            )
        if "large-v3" not in self.model.name.lower():
            raise WhisperCppRuntimeUnavailableError(
                "The native caption pack model filename must identify large-v3; "
                f"got {self.model.name!r}."
            )
        if threads < 1:
            raise ValueError("whisper.cpp threads must be at least 1")
        if beam_size < 1:
            raise ValueError("whisper.cpp beam size must be at least 1")
        if audio_context < 1:
            raise ValueError("whisper.cpp audio context must be at least 1")
        if max_segment_chars < 1:
            raise ValueError("whisper.cpp maximum segment length must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("whisper.cpp timeout must be greater than zero")
        self.threads = threads
        self.beam_size = beam_size
        self.audio_context = audio_context
        self.max_segment_chars = max_segment_chars
        self.language = language
        self.timeout_seconds = timeout_seconds

    def transcribe(
        self,
        chunks: Iterable[AudioChunk],
        vocabulary: CustomVocabulary | None = None,
    ) -> Iterable[CaptionHypothesis]:
        initial_prompt = _build_initial_prompt(vocabulary)
        for chunk in chunks:
            yield from self._transcribe_chunk(chunk, initial_prompt=initial_prompt)

    def _transcribe_chunk(
        self,
        chunk: AudioChunk,
        *,
        initial_prompt: str | None,
    ) -> Iterable[CaptionHypothesis]:
        with TemporaryDirectory(prefix="civiccast-whispercpp-") as temp_dir:
            temp_root = Path(temp_dir)
            wav_path = temp_root / "chunk.wav"
            output_base = temp_root / "result"
            _write_pcm_chunk_wav(chunk, wav_path)
            command = [
                str(self.executable),
                "--model",
                str(self.model),
                "--file",
                str(wav_path),
                "--language",
                self.language,
                "--threads",
                str(self.threads),
                "--beam-size",
                str(self.beam_size),
                "--audio-ctx",
                str(self.audio_context),
                "--max-len",
                str(self.max_segment_chars),
                "--split-on-word",
                "--output-json-full",
                "--output-file",
                str(output_base),
            ]
            if initial_prompt:
                command.extend(["--prompt", initial_prompt])
            # The installer verifies both pack paths before activation; argv is
            # passed directly and never through a command shell.
            result = subprocess.run(  # noqa: S603
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-1200:]
                raise WhisperCppRuntimeUnavailableError(
                    "The native whisper.cpp caption runtime failed "
                    f"(exit {result.returncode}): {detail}"
                )
            if "using Vulkan" not in result.stderr or "backend" not in result.stderr:
                raise WhisperCppRuntimeUnavailableError(
                    "The native whisper.cpp caption runtime did not confirm a Vulkan "
                    "backend; refusing silent CPU fallback."
                )
            output_path = output_base.with_suffix(".json")
            if not output_path.is_file():
                raise WhisperCppRuntimeUnavailableError(
                    "The native whisper.cpp caption runtime produced no JSON result."
                )
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise WhisperCppRuntimeUnavailableError(
                    "The native whisper.cpp caption runtime produced invalid JSON."
                ) from exc
            _validate_whisper_cpp_large_v3_identity(payload)
            for index, segment in enumerate(payload.get("transcription", [])):
                hypothesis = _whisper_cpp_hypothesis(chunk, index, segment)
                if hypothesis is not None:
                    yield hypothesis


class FasterWhisperRuntime:
    """Lazy faster-whisper adapter for live and batch caption chunks.

    The optional dependency is imported only when audio is transcribed so the
    default CivicCast install remains lightweight. Install
    ``civiccast[captions-runtime]`` on hosts that should execute the model.
    """

    def __init__(
        self,
        model_size_or_path: str = "large-v3",
        *,
        device: str = "auto",
        compute_type: str = "int8",
        cpu_threads: int | None = None,
        num_workers: int | None = None,
        beam_size: int | None = None,
        language: str | None = None,
        task: str = "transcribe",
        vad_filter: bool = True,
        live: bool = False,
        packaged_model_path: str | None = None,
        packaged_model_tier: str | None = None,
    ) -> None:
        packaged_model = (
            packaged_model_path or os.environ.get("CIVICCAST_WHISPER_MODEL_PATH", "").strip()
        )
        declared_tier = packaged_model_tier or None
        self._local_files_only = False
        if os.environ.get("CIVICCAST_NATIVE_STATION", "").strip() == "1" and not packaged_model:
            raise FasterWhisperRuntimeUnavailableError(
                "The activated native station did not provide its verified packaged "
                "caption model path. CivicCast will not fall back to a first-use "
                "network download."
            )
        if packaged_model:
            model_path = Path(packaged_model).resolve()
            tier_id, required = _resolve_packaged_model_tier(
                model_path, declared_tier=declared_tier
            )
            if tier_id is not None:
                missing = _missing_packaged_model_files(model_path, required)
                tier_label = f" (caption tier {tier_id!r})"
            else:
                # Unidentified directory: it must still be a COMPLETE, known
                # caption model -- it just may be any tier's. Report the
                # closest candidate (fewest missing files) so the operator is
                # told what to fix, not merely that nothing matched.
                candidates = {
                    known: _missing_packaged_model_files(
                        model_path, caption_tier_required_files(known)
                    )
                    for known in CAPTION_TIER_REGISTRY
                }
                best = min(candidates, key=lambda known: len(candidates[known]))
                missing = candidates[best]
                tier_label = f" (no caption tier declared; closest tier {best!r})"
            if missing:
                raise FasterWhisperRuntimeUnavailableError(
                    "The packaged offline caption model is missing or incomplete at "
                    f"{model_path}{tier_label}; missing: {', '.join(missing)}. CivicCast "
                    "will not fall back to a first-use network download."
                )
            self.model_size_or_path = str(model_path)
            self._local_files_only = True
        else:
            self.model_size_or_path = model_size_or_path
        self.device = os.environ.get("CIVICCAST_WHISPER_DEVICE", "").strip() or device
        self.compute_type = (
            os.environ.get("CIVICCAST_WHISPER_COMPUTE_TYPE", "").strip() or compute_type
        )
        # ``live`` is the whole point of this distinction: a LIVE tap runtime
        # shares the box with playout, a batch/VOD runtime does not. It is a
        # constructor flag rather than a caller-side kwargs bundle because the
        # first version of this fix WAS a caller-side bundle -- in
        # ``build_tap_worker`` -- and the product never executed it: the app
        # pre-builds the runtime through
        # ``civiccast.ai_models.runtime.build_caption_runtime`` and injects it,
        # so ``build_tap_worker``'s own construction branch is dead in the
        # native service. The conservative values have to live where the
        # runtime is actually constructed.
        self._live = live
        # cpu_threads is CTranslate2's intra_threads and 0 means "every core".
        # That stays the batch/VOD default -- a finalization pass is allowed
        # to use the machine. For the LIVE tap, 0 is refused even via
        # CIVICCAST_WHISPER_CPU_THREADS (see
        # ``_resolved_whisper_cpu_threads_env`` below); the batch/VOD path
        # keeps the old fail-fast (raise) behaviour on a bad value.
        #
        # Precedence for the live tap: CIVICCAST_WHISPER_CPU_THREADS (below,
        # applies to both live and batch) > an explicit `cpu_threads`
        # constructor argument > CAPTION_TAP_CPU_THREADS_ENV_VAR
        # (CIVICCAST_CAPTION_TAP_CPU_THREADS, live-only) >
        # default_live_tap_cpu_threads()'s core-count formula (item 79).
        #
        # CLAMPED, not `_env_int` (fail-fast): this constructor runs UNGUARDED
        # during control-plane startup for the live tap (`civiccast.app` ->
        # `civiccast.ai_models.runtime.build_caption_runtime(live=True)`, no
        # try/except around it), so a mistyped or zero value here must not
        # take an activated station off air -- see
        # `_clamped_caption_tap_cpu_threads_env`.
        if live:
            default_cpu_threads = _clamped_caption_tap_cpu_threads_env(
                default_live_tap_cpu_threads()
            )
        else:
            default_cpu_threads = 0
        # FAIL-FAST for batch, CLAMPED for live -- see
        # `_resolved_whisper_cpu_threads_env`: this generic override is read
        # for the live tap just as unguarded as the tap-only one above, so
        # `live=True` clamps a bad or oversized value with a warning instead
        # of raising; `live=False` keeps the original `_env_int` (raise)
        # behaviour unchanged.
        self.cpu_threads = _resolved_whisper_cpu_threads_env(
            default_cpu_threads if cpu_threads is None else cpu_threads,
            live=live,
        )
        if live:
            # Logged ONCE, at tap start (this constructor runs once per live
            # runtime instance -- see `civiccast.ai_models.runtime.build_caption_runtime`
            # and `civiccast.captions.tap_worker.build_tap_worker`), so an
            # operator or a field report can see exactly what the live tap's
            # ASR was sized to without re-deriving it from core count and env
            # state by hand.
            logger.info(
                "Live caption tap ASR intra-op threads: cpu_threads=%d "
                "(cpu_count=%s, %s=%s, CIVICCAST_WHISPER_CPU_THREADS=%s)",
                self.cpu_threads,
                os.cpu_count(),
                CAPTION_TAP_CPU_THREADS_ENV_VAR,
                os.environ.get(CAPTION_TAP_CPU_THREADS_ENV_VAR, "<unset>"),
                os.environ.get("CIVICCAST_WHISPER_CPU_THREADS", "<unset>"),
            )
        default_num_workers = LIVE_TAP_CUDA_NUM_WORKERS if live and self.on_cuda() else 1
        self.num_workers = _env_int(
            "CIVICCAST_WHISPER_NUM_WORKERS",
            default_num_workers if num_workers is None else num_workers,
            minimum=1,
        )
        # Beam search costs roughly its beam width in decoder passes. Beam 5
        # stays the batch/VOD default; a LIVE tap has a hard real-time budget a
        # VOD pass does not, so it decodes greedily on every device.
        #
        # The device is deliberately NOT part of this decision any more. The
        # live tap's budget is set by the 5 s segment cadence, not by which
        # chip runs the decoder: on CUDA, beam 5 measured 0.589 s per chunk
        # against beam 1's 0.433 s, and a batch slower than the cadence lets a
        # second and third segment settle, trips the max-2 backlog gate, and
        # clears live captions. Greedy decoding on CUDA too is what keeps the
        # tap real-time. Batch/VOD still gets beam 5.
        if beam_size is None:
            beam_size = LIVE_TAP_CPU_BEAM_SIZE if live else 5
        # Scoped to the live tap deliberately: a batch/VOD pass and the native
        # capacity proof must not have their beam width changed out from under
        # them by a variable set to protect playout.
        self.beam_size = (
            _env_int("CIVICCAST_WHISPER_BEAM_SIZE", beam_size, minimum=1) if live else beam_size
        )
        self.language = language
        self.task = task
        self.vad_filter = vad_filter
        self._model: Any | None = None
        self._model_lock = threading.Lock()

    def on_cuda(self) -> bool:
        """Whether this runtime will actually decode on a GPU.

        ``self.device`` is a REQUEST, not an answer: its default is ``"auto"``,
        which is precisely the value that neither ``startswith("cuda")`` nor
        ``startswith("cpu")`` resolves, and which
        ``CIVICCAST_WHISPER_DEVICE`` never spells. Asking the environment
        instead would therefore have reported "not CUDA" for a GPU station and
        "not CUDA" for a CPU station alike -- a test that always passes and a
        distinction that never fires.

        ``auto`` is resolved by asking CTranslate2 how many CUDA devices it can
        see, which is the same question faster-whisper's own ``auto`` answers.
        Any failure (CTranslate2 absent, a driver that will not enumerate)
        resolves to CPU: the conservative answer is the safe one here, because
        the only thing this decides is whether to spend GPU-sized compute on a
        box that shares its CPU with playout.
        """

        device = str(self.device).strip().lower()
        if device.startswith("cuda"):
            return True
        if device.startswith("cpu"):
            return False
        try:
            import ctranslate2

            return int(ctranslate2.get_cuda_device_count()) > 0
        except Exception:
            return False

    def transcribe(
        self,
        chunks: Iterable[AudioChunk],
        vocabulary: CustomVocabulary | None = None,
    ) -> Iterable[CaptionHypothesis]:
        initial_prompt = _build_initial_prompt(vocabulary)
        for chunk in chunks:
            yield from self._transcribe_chunk(chunk, initial_prompt=initial_prompt)

    def prepare(self) -> None:
        """Resolve and load the model before a live multi-channel dispatch.

        The live tap uses this once work is pending so a requested CUDA runtime
        can complete its existing CUDA-to-CPU fallback before the tap chooses
        an executor size. Without this seam, the first three-channel scan could
        submit three calls and discover the fallback inside the first one,
        leaving two already-submitted calls contending on the CPU.
        """

        self._model_instance()

    def _model_instance(self) -> Any:
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    model_kwargs: dict[str, Any] = {
                        "device": self.device,
                        "compute_type": self.compute_type,
                    }
                    if self.cpu_threads:
                        model_kwargs["cpu_threads"] = self.cpu_threads
                    if self.num_workers != 1:
                        model_kwargs["num_workers"] = self.num_workers
                    if self._local_files_only:
                        model_kwargs["local_files_only"] = True
                    if str(model_kwargs["device"]).startswith("cuda"):
                        # Must happen BEFORE the load attempt below, not in
                        # the except block: Windows' loader needs the
                        # directory registered before CTranslate2's CUDA
                        # backend resolves cuBLAS/cuDNN, not after it has
                        # already failed to.
                        _ensure_cuda_dll_directory()
                    try:
                        self._model = _load_whisper_model_class()(
                            self.model_size_or_path,
                            **model_kwargs,
                        )
                    except Exception as exc:
                        # A GPU device selection must DEGRADE, never kill
                        # captions: CTranslate2's CUDA backend dynamically
                        # links cuBLAS/cuDNN, so `device="cuda"` on a station
                        # without those libraries (they are not yet in the
                        # pinned payload) raises at model load. Fall back to
                        # the pack contract's validated cpu/int8 baseline and
                        # say so loudly — captions arrive slower, not never.
                        # A cpu-device failure has no safer tier below it and
                        # re-raises untouched.
                        if not str(model_kwargs["device"]).startswith("cuda"):
                            raise
                        logger.warning(
                            "faster-whisper could not initialize on device=%s "
                            "(%s); falling back to cpu/int8 — captions will "
                            "run slower until the CUDA runtime is available",
                            model_kwargs["device"],
                            exc,
                        )
                        self.device = "cpu"
                        self.compute_type = "int8"
                        model_kwargs["device"] = "cpu"
                        model_kwargs["compute_type"] = "int8"
                        if self._live:
                            # CUDA live captions use three CTranslate2 workers
                            # so the station's three channels can meet the
                            # five-second segment cadence.  If CUDA loading
                            # fails, do not carry that GPU concurrency onto the
                            # CPU that is also running playout.
                            self.num_workers = 1
                            model_kwargs.pop("num_workers", None)
                        if self._live and "CIVICCAST_WHISPER_BEAM_SIZE" not in os.environ:
                            # A LIVE runtime already decodes greedily on every
                            # device, so the CPU fallback needs no beam change;
                            # this re-asserts the live-tap width in case a
                            # caller-supplied beam_size arrived from a
                            # GPU-sized capacity proof. An explicit operator
                            # override is left alone.
                            self.beam_size = LIVE_TAP_CPU_BEAM_SIZE
                        self._model = _load_whisper_model_class()(
                            self.model_size_or_path,
                            **model_kwargs,
                        )
        return self._model

    @contextmanager
    def _chunk_audio_source(self, chunk: AudioChunk) -> Iterator[Any]:
        """Yield the audio source to hand the model for one chunk.

        A LIVE chunk already at :data:`LIVE_TAP_PCM_PASSTHROUGH_RATE_HZ` is
        converted straight from its own samples -- see that constant's
        docstring for the measurement. Everything else is written to a
        temporary WAV for faster-whisper to decode, which is every batch/VOD
        chunk and any live chunk at another rate (the resampler is the whole
        point there). When there is a temporary directory, it lives exactly as
        long as the model call inside the ``with`` body.
        """

        if self._live and chunk.sample_rate_hz == LIVE_TAP_PCM_PASSTHROUGH_RATE_HZ:
            yield _pcm_s16le_to_whisper_audio(chunk.pcm_s16le)
            return

        with TemporaryDirectory(prefix="civiccast-caption-") as temp_dir:
            wav_path = Path(temp_dir) / "chunk.wav"
            _write_pcm_chunk_wav(chunk, wav_path)
            yield str(wav_path)

    def _transcribe_source(self, source: Any, *, initial_prompt: str | None) -> Iterable[Any]:
        """Hand one audio source to the model; return its segment iterator.

        The single place the live and batch/VOD paths state their model
        settings. ``source`` is whatever faster-whisper accepts: a filesystem
        path for the batch/VOD path and for a live chunk that is not at
        :data:`LIVE_TAP_PCM_PASSTHROUGH_RATE_HZ`, or a float32 PCM array for a
        live chunk that is (:func:`_pcm_s16le_to_whisper_audio`).
        """

        segments: Iterable[Any]
        segments, _info = self._model_instance().transcribe(
            source,
            beam_size=self.beam_size,
            language=self.language,
            task=self.task,
            vad_filter=self.vad_filter,
            initial_prompt=initial_prompt,
            **({"word_timestamps": True} if self._live else {}),
        )
        return segments

    def _transcribe_chunk(
        self,
        chunk: AudioChunk,
        *,
        initial_prompt: str | None,
    ) -> Iterable[CaptionHypothesis]:
        with self._chunk_audio_source(chunk) as source:
            segments = self._transcribe_source(source, initial_prompt=initial_prompt)

            live_segments: list[CaptionHypothesis] = []
            live_words: list[CaptionWord] = []
            for index, segment in enumerate(segments):
                text = str(getattr(segment, "text", "")).strip()
                if not text:
                    continue

                start_seconds = chunk.start_seconds + float(getattr(segment, "start", 0.0))
                end_seconds = chunk.start_seconds + float(getattr(segment, "end", 0.0))
                if not isfinite(start_seconds) or not isfinite(end_seconds):
                    continue
                if end_seconds <= chunk.start_seconds or start_seconds >= chunk.end_seconds:
                    continue
                start_seconds = max(start_seconds, chunk.start_seconds)
                end_seconds = min(end_seconds, chunk.end_seconds)
                if end_seconds <= start_seconds:
                    continue

                hypothesis = CaptionHypothesis(
                    source_id=_segment_source_id(chunk.chunk_id, index),
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    text=text,
                    confidence=_segment_confidence(segment),
                )
                if self._live:
                    live_segments.append(hypothesis)
                    for word in getattr(segment, "words", None) or []:
                        word_start = chunk.start_seconds + float(word.start)
                        word_end = chunk.start_seconds + float(word.end)
                        if not isfinite(word_start) or not isfinite(word_end):
                            continue
                        if word_end <= chunk.start_seconds or word_start >= chunk.end_seconds:
                            continue
                        word_start = max(word_start, chunk.start_seconds)
                        word_end = min(word_end, chunk.end_seconds)
                        if word_end <= word_start:
                            continue
                        live_words.append(
                            CaptionWord(
                                text=str(word.word),
                                start_seconds=word_start,
                                end_seconds=word_end,
                                confidence=float(word.probability),
                            )
                        )
                else:
                    yield hypothesis
            if live_segments:
                # Segment boundaries vary between overlapping ASR windows.
                # Confirm one window against another, not two fragments from
                # the same pass. Keep the actual speech envelope separately.
                # The model's text bound validates the combined result: an
                # oversized result fails explicitly, never silently truncates.
                yield CaptionHypothesis(
                    source_id=_segment_source_id(chunk.chunk_id, 0),
                    start_seconds=min(h.start_seconds for h in live_segments),
                    end_seconds=max(h.end_seconds for h in live_segments),
                    text=" ".join(h.text for h in live_segments),
                    confidence=min(h.confidence for h in live_segments),
                    audio_window_start_seconds=chunk.start_seconds,
                    audio_window_end_seconds=chunk.end_seconds,
                    words=live_words,
                )


def _load_whisper_model_class() -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise FasterWhisperRuntimeUnavailableError(
            "faster-whisper is not installed. Install CivicCast with "
            "`civiccast[captions-runtime]`, then confirm CUDA/cuDNN or CPU "
            "runtime compatibility before enabling live captions."
        ) from exc
    return WhisperModel


def _pcm_s16le_to_whisper_audio(pcm_s16le: bytes) -> Any:
    """Return the float32 [-1, 1] array faster-whisper's decoder would have.

    Byte-for-byte the arithmetic ``decode_audio`` performs once PyAV has
    decoded and resampled a file (``faster_whisper/audio.py``:
    ``audio.astype(np.float32) / 32768.0`` over little-endian signed 16-bit
    samples), for audio already mono and at
    :data:`LIVE_TAP_PCM_PASSTHROUGH_RATE_HZ`.

    ``numpy`` arrives with the optional caption runtime, not with CivicCast
    itself, so it is imported here rather than at module scope -- the same
    laziness :func:`_load_whisper_model_class` applies to faster-whisper.
    """

    import numpy as np

    return np.frombuffer(pcm_s16le, dtype="<i2").astype(np.float32) / 32768.0


def _write_pcm_chunk_wav(chunk: AudioChunk, output_path: Path) -> None:
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(chunk.sample_rate_hz)
        wav_file.writeframes(chunk.pcm_s16le)


def _build_initial_prompt(vocabulary: CustomVocabulary | None) -> str | None:
    if vocabulary is None:
        return None

    parts: list[str] = []
    if vocabulary.initial_prompt:
        parts.append(vocabulary.initial_prompt.strip())
    if vocabulary.terms:
        parts.append("Prefer these civic terms and names: " + "; ".join(vocabulary.terms) + ".")
    return " ".join(parts) or None


def _segment_source_id(chunk_id: str, index: int) -> str:
    return f"{chunk_id[:110]}-{index:04d}"


def _segment_confidence(segment: Any) -> float:
    avg_logprob = getattr(segment, "avg_logprob", None)
    no_speech_prob = getattr(segment, "no_speech_prob", None)

    if isinstance(avg_logprob, int | float):
        acoustic_confidence = max(0.0, min(1.0, exp(float(avg_logprob))))
    else:
        acoustic_confidence = 1.0

    if isinstance(no_speech_prob, int | float):
        speech_confidence = 1.0 - max(0.0, min(1.0, float(no_speech_prob)))
    else:
        speech_confidence = 1.0

    return round(max(0.0, min(1.0, acoustic_confidence * speech_confidence)), 4)


def _validate_whisper_cpp_large_v3_identity(payload: object) -> None:
    if not isinstance(payload, dict):
        raise WhisperCppRuntimeUnavailableError(
            "The native caption runtime did not report the required large-v3 identity."
        )
    model = payload.get("model")
    audio = model.get("audio") if isinstance(model, dict) else None
    if not (
        isinstance(model, dict)
        and model.get("type") == "large"
        and model.get("multilingual") is True
        and isinstance(audio, dict)
        and audio.get("state") == 1280
        and audio.get("layer") == 32
    ):
        raise WhisperCppRuntimeUnavailableError(
            "The native caption runtime failed the required large-v3 identity check."
        )


def _whisper_cpp_hypothesis(
    chunk: AudioChunk,
    index: int,
    segment: object,
) -> CaptionHypothesis | None:
    if not isinstance(segment, dict):
        return None
    text = str(segment.get("text", "")).strip()
    offsets = segment.get("offsets")
    if not text or not isinstance(offsets, dict):
        return None
    start_ms = offsets.get("from")
    end_ms = offsets.get("to")
    if not isinstance(start_ms, int | float) or not isinstance(end_ms, int | float):
        return None
    start_seconds = max(chunk.start_seconds, chunk.start_seconds + float(start_ms) / 1000)
    end_seconds = min(chunk.end_seconds, chunk.start_seconds + float(end_ms) / 1000)
    if end_seconds <= start_seconds:
        return None
    probabilities: list[float] = []
    tokens = segment.get("tokens")
    if isinstance(tokens, list):
        for token in tokens:
            if not isinstance(token, dict):
                continue
            token_text = str(token.get("text", ""))
            probability = token.get("p")
            if token_text.startswith("[_") or not isinstance(probability, int | float):
                continue
            probabilities.append(max(0.0, min(1.0, float(probability))))
    confidence = round(sum(probabilities) / len(probabilities), 4) if probabilities else 0.0
    return CaptionHypothesis(
        source_id=_segment_source_id(chunk.chunk_id, index),
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        text=text,
        confidence=confidence,
    )


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer; got {raw!r}.") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}; got {value}.")
    return value
