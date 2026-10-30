# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Download and verify local AI models.

Task #57 (a) (disclosed in commit 52bc7253): the installer's acquisition
download experience (``acquisition_catalog.rs``'s ``local_ai_model`` catalog
component, task #56) stages ``gemma4:12b`` directly into Ollama's OWN
``OLLAMA_MODELS`` on-disk layout, at ``<install_root>\\packs\\local-ai-model\\
models\\...`` (``models/manifests/registry.ollama.ai/library/gemma4/12b`` +
``models/blobs/sha256-<digest>``, OCI distribution grammar -- colon on the
wire, dash on disk). Until this fix, :func:`download_release_models` shelled
out to ``ollama pull`` unconditionally, unaware anything had been staged
ahead of time.

Task #57 D1 (audit 2026-07-31): ``ollama pull`` is a thin CLIENT of the
ollama SERVER -- the SERVER's environment decides the model store, so
setting ``OLLAMA_MODELS`` around the pull subprocess (this module's previous
fix) configured the WRONG process and the staged pack was ignored (or, with
no server running at all, the pull failed outright). The authority is
``apps/installer/src-tauri/src/main.rs``'s ``NativeOllamaSelfTestServer``
(the installer's own production D2 self-test): it sets ``OLLAMA_HOST`` +
``OLLAMA_MODELS`` on ``ollama serve`` itself, binds a fresh loopback port,
polls ``/api/version`` against a 60s deadline, writes the server's output to
FILES (never captured pipes), and tears the process TREE down afterward.

This verb now does exactly that: when running as the installed embedded
interpreter it manages its OWN ephemeral ``ollama serve`` -- ``OLLAMA_MODELS``
pointed at the staged store, bound to a NON-default loopback port so a
user's own ollama on 11434 is neither used nor disturbed -- and drives every
per-model step against that server:

* a FULLY-staged model (:func:`check_staged_ollama_model` ``"staged"``) is
  verified as a no-op WITHOUT any network access: the manifest+blob walk is
  pure filesystem, and the staged store's serveability is proven by the
  ephemeral server listing the tag via loopback ``/api/tags`` -- no
  ``ollama pull`` runs at all (a pull always dials the registry even when
  local content is current, which an air-gapped station cannot do);
* a half-staged model (manifest present, referenced blob missing -- an
  interrupted/corrupt installer download) is reported, never silently
  trusted, and repaired by a network ``ollama pull`` routed through the
  ephemeral server (``OLLAMA_HOST`` on the pull CLIENT), so the re-download
  lands IN the staged store;
* an unstaged model is network-pulled the same way, into the staged store,
  so the runtime supervisor's ollama child (task #57 D2, which serves this
  SAME store) sees every provisioned tag.

The staged store is located exactly where the installer sources put it
(``install_layout.ollama_model_store_candidates``: the activation flow's
composed ``models\\ollama`` first -- ``native_activation.rs`` -- then the
acquisition flow's ``packs\\local-ai-model\\models`` --
``acquisition_catalog.rs``). A dev/CLI invocation (not the installed
``runtime\\`` interpreter, or no staged store present) keeps the legacy
behavior: plain ``ollama pull`` against whatever server the ambient
environment provides, with this process's environment NEVER mutated.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from civiccast.native.caption_tiers import (
    CAPTION_TIER_REGISTRY,
    FLOOR_TIER_ID,
    CaptionTierBindingError,
)
from civiccast.native.supervisor.install_layout import (
    ollama_model_store_candidates,
    resolve_install_layout,
)

ModelDownloadStatus = Literal["ok", "failed", "planned"]

WHISPER_MODEL_REPO = "Systran/faster-whisper-large-v3"
# Pinned to a known-good commit (audit item #27 — bandit B615, supply-chain
# integrity): an unpinned snapshot_download() silently follows whatever the
# repo owner pushes to `main` next, with no review on our side. Bump this
# deliberately when a model update is wanted, not implicitly on every pull.
WHISPER_MODEL_REVISION = "edaa852ec7e145841d8ffdb056a99866b5f0a478"
# Conservative single-default tag kept for back-compat callers (the <16GB summary
# default). The release pull provisions BOTH summary tags via ``SUMMARY_MODELS`` so
# the adaptive default (12B on >=16GB, e4b on smaller boxes) is present on first run
# regardless of detected RAM — see ``summary_provisioning_tags`` (S13 E2/T2/Q1).
SUMMARY_MODEL = "gemma4:e4b"
SUMMARY_MODELS = ("gemma4:12b", "gemma4:e4b")
TRANSLATION_MODEL = "translategemma:4b"

# ---------------------------------------------------------------------------
# Task #57 (a): consume what the installer's acquisition download experience
# already staged into Ollama's own OLLAMA_MODELS layout, before ever shelling
# out to `ollama pull`.
# ---------------------------------------------------------------------------

#: Matches `acquisition_catalog.rs`'s `DEFAULT_OLLAMA_REGISTRY_BASE_URL`'s
#: host (`native_packs::reviewed_ollama_model("gemma4-12b").registry` is
#: pinned to this exact string on the Rust side) -- the on-disk manifest
#: path's registry segment, never re-derived from the download URL.
OLLAMA_REGISTRY_HOST = "registry.ollama.ai"


def _split_ollama_tag(model: str) -> tuple[str, str]:
    """``"gemma4:12b"`` -> ``("gemma4", "12b")`` -- the OCI wire grammar
    (colon on the wire, path segments on disk) both installer flows use
    (`acquisition_catalog.rs` `reviewed_ollama_model`;
    `native_activation.rs` `validate_staged_runtime_layout`'s
    ``manifests/<registry>/library/<repo>/<tag>`` entries). A bare name
    defaults to ``latest``, matching ollama's own client behavior."""

    repository, _, tag = model.partition(":")
    return repository, tag or "latest"


@dataclass(frozen=True)
class StagedOllamaModelCheck:
    """The result of checking one ollama tag against a staged
    ``OLLAMA_MODELS`` directory. ``status`` is one of:

    * ``"not_staged"`` -- no manifest found: a network pull (through the
      ephemeral staged-store server on an installed station), nothing to
      report.
    * ``"staged"`` -- the manifest and every blob it references are present:
      the verifying no-op path (no network; serveability proven via the
      ephemeral server's loopback ``/api/tags``).
    * ``"half_staged"`` -- the manifest is present but at least one blob it
      references is missing (an interrupted/corrupt installer download) --
      reported via ``missing_blob_digests``, then repaired by a network pull
      through the ephemeral staged-store server, never a silent trust of
      incomplete content.
    """

    status: Literal["not_staged", "staged", "half_staged"]
    models_root: Path | None = None
    manifest_path: Path | None = None
    missing_blob_digests: tuple[str, ...] = ()


def _ollama_manifest_path(
    models_root: Path, *, repository: str, tag: str, registry: str = OLLAMA_REGISTRY_HOST
) -> Path:
    """Ollama's own on-disk manifest path convention (mirrored EXACTLY from
    `acquisition_catalog.rs`'s `local_ai_model_items`:
    ``manifests/<registry>/library/<repository>/<tag>`` -- never a second,
    invented layout)."""

    return models_root / "manifests" / registry / "library" / repository / tag


def _ollama_blob_path(models_root: Path, sha256_hex: str) -> Path:
    """Ollama's own on-disk blob path convention (mirrored from
    `acquisition_catalog.rs`'s `ollama_blob_item`: ``blobs/sha256-<digest>``
    -- dash, not colon, exactly what Ollama's own store uses on Windows)."""

    return models_root / "blobs" / f"sha256-{sha256_hex}"


def _manifest_referenced_digests(manifest: object) -> list[str]:
    """Every content-addressed digest (``sha256:<hex>``) an Ollama manifest
    references -- the config blob plus every layer -- per the OCI
    distribution manifest grammar. Malformed/unexpected shapes yield an empty
    list rather than raising: a manifest this module cannot parse is treated
    the same as one that doesn't verify (see :func:`check_staged_ollama_model`),
    never trusted."""

    if not isinstance(manifest, dict):
        return []
    digests: list[str] = []
    config = manifest.get("config")
    if isinstance(config, dict) and isinstance(config.get("digest"), str):
        digests.append(config["digest"])
    layers = manifest.get("layers")
    if isinstance(layers, list):
        for layer in layers:
            if isinstance(layer, dict) and isinstance(layer.get("digest"), str):
                digests.append(layer["digest"])
    return digests


def check_staged_ollama_model(
    models_root: Path, *, repository: str, tag: str, registry: str = OLLAMA_REGISTRY_HOST
) -> StagedOllamaModelCheck:
    """Classify what the installer staged (if anything) for one Ollama
    ``repository:tag`` under ``models_root`` -- see
    :class:`StagedOllamaModelCheck` for the three outcomes. Pure filesystem
    read, no network access, and never raises for a missing/malformed
    manifest (that is exactly the ``"not_staged"``/``"half_staged"`` cases
    this exists to classify, not an error in this function)."""

    manifest_path = _ollama_manifest_path(
        models_root, repository=repository, tag=tag, registry=registry
    )
    if not manifest_path.is_file():
        return StagedOllamaModelCheck(status="not_staged", models_root=models_root)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return StagedOllamaModelCheck(status="not_staged", models_root=models_root)

    missing: list[str] = []
    for digest in _manifest_referenced_digests(manifest):
        if not digest.startswith("sha256:"):
            continue
        hex_digest = digest.split(":", 1)[1]
        if not _ollama_blob_path(models_root, hex_digest).is_file():
            missing.append(digest)

    if missing:
        return StagedOllamaModelCheck(
            status="half_staged",
            models_root=models_root,
            manifest_path=manifest_path,
            missing_blob_digests=tuple(missing),
        )
    return StagedOllamaModelCheck(
        status="staged", models_root=models_root, manifest_path=manifest_path
    )


def _installed_ollama_models_root(python_executable: str | Path | None = None) -> Path | None:
    """The staged, product-owned ``OLLAMA_MODELS`` store on an INSTALLED
    station, or ``None`` for a dev/CLI invocation.

    Discovered the SAME way the running product derives its install root
    from the embedded interpreter (``Path(sys.executable)``, parent named
    ``"runtime"`` -> grandparent is the install root -- the shape
    ``station_runtime.station_environment_for_python`` and
    ``install_layout.resolve_install_root`` both rely on), then checked
    against the two installer staging conventions IN PREFERENCE ORDER via
    ``install_layout.ollama_model_store_candidates`` (single source of
    truth, never re-derived here): the activation flow's composed
    ``models\\ollama`` (all three reviewed tags -- ``native_activation.rs``),
    then the acquisition flow's ``packs\\local-ai-model\\models``
    (gemma4:12b -- ``acquisition_catalog.rs``). A store counts only when its
    ``manifests\\`` subtree exists; otherwise ``None`` -- callers fall
    through to the legacy plain ``ollama pull`` path."""

    embedded_python = Path(python_executable if python_executable is not None else sys.executable)
    if embedded_python.parent.name.casefold() != "runtime":
        return None
    layout = resolve_install_layout(executable=embedded_python)
    for candidate in ollama_model_store_candidates(layout):
        if (candidate / "manifests").is_dir():
            return candidate
    return None


def _staged_ollama_check_for(model: str, models_root: Path) -> StagedOllamaModelCheck:
    """Classify ``model``'s (e.g. ``"gemma4:12b"``) staged content under
    ``models_root`` -- pure filesystem, identity derived from the tag itself
    (:func:`_split_ollama_tag`; both installer flows pin the
    ``registry.ollama.ai`` registry segment, so the on-disk manifest path is
    fully determined by the tag)."""

    repository, tag = _split_ollama_tag(model)
    return check_staged_ollama_model(models_root, repository=repository, tag=tag)


def summary_provisioning_tags(
    system_ram_total_gb: int | None = None, *, has_gpu: bool = True
) -> tuple[str, ...]:
    """The summary runtime tags the release provisioning must install.

    Driven by the SAME ``detect_summary_model_default`` the first-run seed uses, so the
    provisioning plan can never advertise a default it does not fetch (S13 E2/T2/Q1).
    When ``system_ram_total_gb`` is ``None`` (the default), BOTH summary tags are
    returned so the adaptive default is present regardless of RAM — including the
    air-gapped path. When a specific RAM size is given, the plan still includes the tag
    of ``detect_summary_model_default(ram, has_gpu=has_gpu)`` (asserted by the
    provisioning-plan test) plus the conservative e4b fallback.

    ``has_gpu`` defaults ``True`` here -- the OPPOSITE of the production runtime
    default (:func:`~civiccast.ai_models.models.detect_summary_model_default`
    defaults ``has_gpu=False``, CPU-only, because a wrong *runtime* default can pick
    a model that fails to complete). Provisioning is asymmetric: staging an extra
    model nobody ends up using just costs disk space, while under-provisioning
    strands a genuinely GPU-equipped high-RAM box without 12B pre-staged (a
    first-selection network pull instead of an instant local swap). No caller in
    this codebase narrows provisioning by a specific RAM figure today (the real CLI
    path always leaves ``system_ram_total_gb=None`` and stages both tags
    unconditionally); this default only matters if a future caller narrows it, and
    conservative-stage-more is the safer failure mode for that case.
    """

    from civiccast.ai_models.catalog import catalog_tier
    from civiccast.ai_models.models import detect_summary_model_default

    if system_ram_total_gb is None:
        return SUMMARY_MODELS
    default_tag = catalog_tier(
        detect_summary_model_default(system_ram_total_gb, has_gpu=has_gpu)
    ).model_id
    fallback_tag = catalog_tier("gemma4-e4b-ollama").model_id
    # Preserve 12B-before-e4b ordering; de-dupe when the default already IS e4b.
    ordered = [default_tag] + [tag for tag in (fallback_tag,) if tag != default_tag]
    return tuple(ordered)


@dataclass(frozen=True)
class ModelDownloadItem:
    """One model download result."""

    id: str
    runtime: str
    source: str
    status: ModelDownloadStatus
    local_path: str | None
    operator_action: str


@dataclass(frozen=True)
class ModelDownloadReport:
    """Result of the local model bootstrap."""

    status: ModelDownloadStatus
    items: tuple[ModelDownloadItem, ...]


def _run_ollama_pull(model: str) -> None:
    ollama = shutil.which("ollama")
    if ollama is None:
        raise RuntimeError("ollama executable was not found on PATH")
    subprocess.run([ollama, "pull", model], check=True)  # noqa: S603 - fixed executable path.


def _download_whisper_model(cache_dir: Path | None) -> str:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError(
            "huggingface_hub is required to download whisper-large-v3; install the "
            "captions-runtime extra before release proof."
        ) from exc

    local_dir = cache_dir / "faster-whisper-large-v3" if cache_dir is not None else None
    return str(
        snapshot_download(  # nosec B615 - revision is pinned above.
            repo_id=WHISPER_MODEL_REPO,
            revision=WHISPER_MODEL_REVISION,
            local_dir=str(local_dir) if local_dir is not None else None,
        )
    )


def _download_floor_caption_model(cache_dir: Path | None) -> str:
    """Download the mandatory CPU-only caption floor tier (owner ruling, 2026-07-30).

    Sources repo/revision from :data:`civiccast.native.caption_tiers.CAPTION_TIER_REGISTRY`
    -- the single pinned source of truth also used by the caption pack builder
    (``scripts/build_native_caption_pack.py``) and the pack verifier
    (``civiccast.installer.native_packs.verify_caption_pack_tiers``) -- rather than a
    second hand-copied literal, so the three can never drift apart.
    """
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError(
            "huggingface_hub is required to download the caption floor tier; install "
            "the captions-runtime extra before release proof."
        ) from exc

    spec = CAPTION_TIER_REGISTRY[FLOOR_TIER_ID].require_bound()
    # require_bound() raises unless both of these are set, but it returns the
    # same CaptionTierSpec whose fields are still declared str | None, so the
    # guarantee is invisible to a type checker. Re-stating it here narrows the
    # types AND keeps the failure mode identical -- an unbound tier raises
    # CaptionTierBindingError either way, never reaches the network.
    repo_id = spec.model_repository
    revision = spec.model_revision
    if repo_id is None or revision is None:  # pragma: no cover - require_bound guarantees this
        raise CaptionTierBindingError(
            f"caption tier {spec.tier_id!r} passed require_bound() without a "
            "pinned repository/revision"
        )
    local_dir = cache_dir / spec.model_directory if cache_dir is not None else None
    return str(
        snapshot_download(  # nosec B615 - revision is pinned via caption_tiers.py.
            repo_id=repo_id,
            revision=revision,
            local_dir=str(local_dir) if local_dir is not None else None,
        )
    )


# ---------------------------------------------------------------------------
# Task #57 D1: the ephemeral staged-store `ollama serve` (server-side
# OLLAMA_MODELS -- mirrors main.rs's NativeOllamaSelfTestServer exactly).
# ---------------------------------------------------------------------------

#: Mirrors ``NativeOllamaSelfTestServer::wait_until_ready``'s 60-second
#: readiness deadline and 250ms poll cadence.
OLLAMA_SERVE_READY_BUDGET_SECONDS = 60.0
_OLLAMA_SERVE_POLL_SECONDS = 0.25
_OLLAMA_SERVE_STOP_DEADLINE_SECONDS = 10.0
_OLLAMA_HTTP_TIMEOUT_SECONDS = 5.0


def _find_ollama_executable() -> str:
    """The ollama binary to run: the installed station's reviewed binary
    (``install_layout.ollama_exe_path`` -- ``dependencies\\ollama\\
    ollama.exe``, ``native_activation.rs``'s staged runtime layout) when
    running as the embedded interpreter, else whatever is on PATH (the
    dev/CI convention :func:`_run_ollama_pull` has always used)."""

    embedded_python = Path(sys.executable)
    if embedded_python.parent.name.casefold() == "runtime":
        staged = resolve_install_layout(executable=embedded_python).ollama_exe_path
        if staged.is_file():
            return str(staged)
    ollama = shutil.which("ollama")
    if ollama is None:
        raise RuntimeError("ollama executable was not found on PATH")
    return ollama


def _free_loopback_port() -> int:
    """Reserve a fresh ephemeral loopback port (bind ``127.0.0.1:0`` and
    read it back -- the same trick ``NativeOllamaSelfTestServer::start``
    uses), so the ephemeral server NEVER lands on ollama's default 11434: a
    user's own ollama there is neither used nor disturbed."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _http_get_ok(url: str, timeout_seconds: float = _OLLAMA_HTTP_TIMEOUT_SECONDS) -> bool:
    """Bounded loopback GET; True iff HTTP 200. Never raises."""

    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as resp:  # noqa: S310  # nosec B310 - loopback probe; callers build the URL as f"http://{host}"
            return int(getattr(resp, "status", 0) or 0) == 200
    except Exception:
        return False


def _tail_of(path: Path, limit: int = 8 * 1024) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace").strip()


def _wait_for_ollama_serve_ready(
    proc: subprocess.Popen[bytes] | Any,
    base_url: str,
    *,
    stderr_path: Path,
    budget_seconds: float = OLLAMA_SERVE_READY_BUDGET_SECONDS,
    http_ok: Callable[[str], bool] = _http_get_ok,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Bounded readiness: poll ``GET /api/version`` on the ephemeral port
    until success, the process exits early, or the budget runs out -- the
    exact gate ``NativeOllamaSelfTestServer::wait_until_ready`` applies.
    Failure raises with the server's stderr tail (file-backed output --
    never captured pipes on a process that spawns descendants)."""

    deadline = clock() + budget_seconds
    while True:
        exit_code = proc.poll()
        if exit_code is not None:
            raise RuntimeError(
                "model-download step ollama-serve: the ephemeral `ollama serve` for the "
                f"staged store exited before readiness (exit {exit_code}): "
                f"{_tail_of(stderr_path)}"
            )
        if http_ok(f"{base_url}/api/version"):
            return
        if clock() >= deadline:
            raise RuntimeError(
                "model-download step ollama-serve: the ephemeral `ollama serve` for the "
                f"staged store did not become ready within {budget_seconds:.0f}s: "
                f"{_tail_of(stderr_path)}"
            )
        sleep(_OLLAMA_SERVE_POLL_SECONDS)


def _stop_ollama_serve_tree(proc: subprocess.Popen[bytes] | Any) -> None:
    """Tear the ephemeral server DOWN as a process TREE, bounded -- mirrors
    ``main.rs``'s ``terminate_native_ollama_process_tree`` (``taskkill /T /F``
    first on Windows, since ``TerminateProcess`` on the parent would orphan
    any runner descendants; plain terminate->kill elsewhere), with bounded
    waits at every step so a wedged server can never hang the installer."""

    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        taskkill = str(
            Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "taskkill.exe"
        )
        with contextlib.suppress(Exception):
            subprocess.run(  # noqa: S603
                [taskkill, "/T", "/F", "/PID", str(proc.pid)],
                check=False,
                timeout=_OLLAMA_SERVE_STOP_DEADLINE_SECONDS,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    else:
        with contextlib.suppress(Exception):
            proc.terminate()
    try:
        proc.wait(timeout=_OLLAMA_SERVE_STOP_DEADLINE_SECONDS)
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=_OLLAMA_SERVE_STOP_DEADLINE_SECONDS)


@contextlib.contextmanager
def _ephemeral_staged_ollama_server(models_root: Path) -> Iterator[str]:
    """Run ``ollama serve`` with ``OLLAMA_MODELS=models_root`` in the
    SERVER's environment (the whole D1 fix -- the server decides the store)
    on a fresh non-default loopback port, yielding the server's base URL.

    Environment/launch shape mirrors ``NativeOllamaSelfTestServer::start``:
    ``OLLAMA_HOST`` + ``OLLAMA_MODELS`` + offline hardening
    (``OLLAMA_NO_CLOUD``, ``NO_PROXY``) plus its one-shot economy knobs
    (``OLLAMA_KEEP_ALIVE=0``, ``OLLAMA_MAX_LOADED_MODELS=1``,
    ``OLLAMA_NUM_PARALLEL=1`` -- appropriate here exactly as there: this
    server exists for one verify/pull pass, not runtime serving); output
    goes to temp FILES (never ``capture_output`` pipes -- a serve that
    spawns descendants deadlocks a pipe reader); teardown is the bounded
    process-tree kill. This process's own environment is NEVER mutated."""

    ollama = _find_ollama_executable()
    port = _free_loopback_port()
    host = f"127.0.0.1:{port}"
    base_url = f"http://{host}"
    env = {
        **os.environ,
        "OLLAMA_HOST": host,
        "OLLAMA_MODELS": str(models_root),
        "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_KEEP_ALIVE": "0",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_NUM_PARALLEL": "1",
        "NO_PROXY": "127.0.0.1,localhost",
    }
    log_dir = Path(tempfile.mkdtemp(prefix="civiccast-model-download-ollama-"))
    stdout_path = log_dir / "serve.stdout.log"
    stderr_path = log_dir / "serve.stderr.log"
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        proc = subprocess.Popen(  # noqa: S603
            [ollama, "serve"],
            cwd=str(Path(ollama).parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        try:
            _wait_for_ollama_serve_ready(proc, base_url, stderr_path=stderr_path)
            yield base_url
        finally:
            _stop_ollama_serve_tree(proc)
    with contextlib.suppress(OSError):
        stdout_path.unlink()
        stderr_path.unlink()
        log_dir.rmdir()


def _run_ollama_pull_via(model: str, base_url: str) -> None:
    """``ollama pull`` as a CLIENT of the ephemeral staged-store server:
    ``OLLAMA_HOST`` on the pull subprocess targets that server, so the
    download lands in the STAGED store, not a profile store. Progress output
    inherits the console; this process's environment is never mutated."""

    ollama = _find_ollama_executable()
    env = {**os.environ, "OLLAMA_HOST": base_url}
    subprocess.run([ollama, "pull", model], check=True, env=env)  # noqa: S603


def _ollama_tags(base_url: str) -> frozenset[str]:
    """The model tags the server actually exposes (``GET /api/tags``) --
    loopback only, the staged-store serveability proof for the verifying
    no-op path. Raises loudly on an unreachable/malformed response."""

    with urllib.request.urlopen(  # noqa: S310  # nosec B310 - base_url is built locally as f"http://{host}", loopback only
        f"{base_url}/api/tags", timeout=_OLLAMA_HTTP_TIMEOUT_SECONDS
    ) as resp:
        payload = json.loads(resp.read())
    names: set[str] = set()
    models = payload.get("models") if isinstance(payload, dict) else None
    if isinstance(models, list):
        for row in models:
            if isinstance(row, dict):
                for key in ("name", "model"):
                    value = row.get(key)
                    if isinstance(value, str) and value:
                        names.add(value)
    return frozenset(names)


def download_release_models(
    *,
    cache_dir: Path | None = None,
    dry_run: bool = False,
    system_ram_total_gb: int | None = None,
    has_gpu: bool = True,
    whisper_downloader: Callable[[Path | None], str] = _download_whisper_model,
    floor_caption_downloader: Callable[[Path | None], str] = _download_floor_caption_model,
    ollama_pull: Callable[[str], None] = _run_ollama_pull,
    ollama_models_root: Callable[[], Path | None] = _installed_ollama_models_root,
    staged_ollama_server: Callable[[Path], AbstractContextManager[str]] = (
        _ephemeral_staged_ollama_server
    ),
    ollama_pull_via: Callable[[str, str], None] = _run_ollama_pull_via,
    ollama_tags: Callable[[str], frozenset[str]] = _ollama_tags,
) -> ModelDownloadReport:
    """Download the real local AI models required by the current release.

    Provisions whisper-large-v3 (captions QUALITY tier, auto-selected only when
    measured hardware allows) AND the caption FLOOR tier (medium, the mandatory
    CPU-only baseline bound by ``OWNER-DECISION-caption-adaptive-tier.md`` on
    2026-07-30 -- see :mod:`civiccast.native.caption_tiers`), BOTH summary tags (so
    the adaptive 12B/e4b default is present regardless of detected RAM — S13
    E2/T2/Q1), and the Spanish translation model. ``system_ram_total_gb`` narrows
    the summary set to the detected box's default + e4b fallback; ``None`` (the
    default) provisions both. The floor tier is always provisioned unconditionally
    (mirroring the summary both-tags pattern) so a station is never missing its
    mandatory caption baseline regardless of detected hardware.

    Task #57 D1: when ``ollama_models_root()`` locates a staged
    ``OLLAMA_MODELS`` store (the installed station), the whole ollama pass
    runs against this verb's OWN ephemeral ``ollama serve``
    (``staged_ollama_server``: ``OLLAMA_MODELS`` in the SERVER's environment
    -- the process that actually decides the store -- on a non-default
    loopback port). Per model: a verified fully-staged tree
    (:func:`check_staged_ollama_model`) is a verifying NO-OP with no network
    access (pure filesystem walk + the server listing the tag via loopback
    ``/api/tags``); a half-staged tree (manifest present, referenced blob
    missing) is reported in ``operator_action`` and repaired by a network
    ``ollama_pull_via`` routed through the ephemeral server (landing in the
    staged store); an unstaged model is network-pulled the same way. A
    staged model the server does NOT list fails loud (a store ollama cannot
    read must never be reported as provisioned). When no staged store
    resolves (dev/CLI invocation), the legacy 1-argument ``ollama_pull``
    path runs unchanged, and this process's environment is never mutated in
    either mode.
    """

    planned = dry_run
    items: list[ModelDownloadItem] = []
    ollama_models = (
        *summary_provisioning_tags(system_ram_total_gb, has_gpu=has_gpu),
        TRANSLATION_MODEL,
    )
    floor_spec = CAPTION_TIER_REGISTRY[FLOOR_TIER_ID].require_bound()
    # require_bound() guarantees a non-None pinned repository; narrow the
    # Optional for the ModelDownloadItem(source=...) sites below (mypy).
    floor_repo = floor_spec.model_repository
    assert floor_repo is not None

    if planned:
        items.append(
            ModelDownloadItem(
                id="faster-whisper-large-v3",
                runtime="faster-whisper",
                source=WHISPER_MODEL_REPO,
                status="planned",
                local_path=None,
                operator_action="Would download whisper-large-v3 for faster-whisper INT8 captions.",
            )
        )
        items.append(
            ModelDownloadItem(
                id=floor_spec.model_directory,
                runtime="faster-whisper",
                source=floor_repo,
                status="planned",
                local_path=None,
                operator_action=(
                    "Would download the caption floor tier "
                    f"({floor_spec.model_repository}) for faster-whisper INT8 captions."
                ),
            )
        )
    else:
        path = whisper_downloader(cache_dir)
        items.append(
            ModelDownloadItem(
                id="faster-whisper-large-v3",
                runtime="faster-whisper",
                source=WHISPER_MODEL_REPO,
                status="ok",
                local_path=path,
                operator_action=(
                    "runtime=faster-whisper model=whisper-large-v3 compute=int8 "
                    f"source={WHISPER_MODEL_REPO}"
                ),
            )
        )
        floor_path = floor_caption_downloader(cache_dir)
        items.append(
            ModelDownloadItem(
                id=floor_spec.model_directory,
                runtime="faster-whisper",
                source=floor_repo,
                status="ok",
                local_path=floor_path,
                operator_action=(
                    f"runtime=faster-whisper model={floor_spec.model_directory} compute=int8 "
                    f"source={floor_spec.model_repository}"
                ),
            )
        )

    def _ollama_item(model: str, note: str) -> ModelDownloadItem:
        return ModelDownloadItem(
            id=model.replace(":", "-"),
            runtime="ollama",
            source=model,
            status="ok",
            local_path=None,
            operator_action=f"runtime=ollama model={model} {note}",
        )

    if planned:
        for model in ollama_models:
            items.append(
                ModelDownloadItem(
                    id=model.replace(":", "-"),
                    runtime="ollama",
                    source=model,
                    status="planned",
                    local_path=None,
                    operator_action=f"Would run `ollama pull {model}`.",
                )
            )
        return ModelDownloadReport(status="planned", items=tuple(items))

    models_root = ollama_models_root()
    if models_root is None:
        # Dev/CLI invocation (no staged store): the legacy client pull against
        # whatever server the ambient environment provides -- environment
        # never mutated (task #57 D1: env-wrapping the CLIENT was the defect).
        for model in ollama_models:
            ollama_pull(model)
            items.append(_ollama_item(model, "pulled=true"))
        return ModelDownloadReport(status="ok", items=tuple(items))

    # Installed station: one ephemeral staged-store server for the whole pass
    # (task #57 D1 -- OLLAMA_MODELS on the SERVER, non-default loopback port).
    with staged_ollama_server(models_root) as base_url:
        served = ollama_tags(base_url)
        for model in ollama_models:
            check = _staged_ollama_check_for(model, models_root)
            if check.status == "staged":
                if model not in served:
                    raise RuntimeError(
                        f"model-download step ollama-verify: {model} is fully staged at "
                        f"{models_root} but the ephemeral ollama server does not list it "
                        "via /api/tags -- the staged store is unreadable by ollama; "
                        "refusing to report it as provisioned"
                    )
                note = (
                    f"imported=true staged_root={models_root} "
                    "(verified installer-staged manifest+blobs; the staged store served "
                    "the tag via loopback /api/tags on this verb's ephemeral ollama "
                    "server -- verifying no-op, no network access)"
                )
            elif check.status == "half_staged":
                ollama_pull_via(model, base_url)
                note = (
                    "pulled=true staged_root_incomplete=true "
                    f"missing_blobs={','.join(check.missing_blob_digests)} "
                    "(installer-staged manifest was present but referenced blob(s) were "
                    "missing; repaired by a network pull routed through the ephemeral "
                    "staged-store server)"
                )
            else:
                ollama_pull_via(model, base_url)
                note = (
                    f"pulled=true staged_root={models_root} "
                    "(network pull routed through the ephemeral staged-store server, so "
                    "the download landed in the staged store)"
                )
            items.append(_ollama_item(model, note))

    return ModelDownloadReport(status="ok", items=tuple(items))
