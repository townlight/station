# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Hardware probe — CPU, RAM, disk, GPU, VRAM, OS context.

Mirrored from AgentSuiteLocal's /api/hardware pattern (spec §5.4) with
the GPU + VRAM extension required by the tier decision tree (spec §7.7).
The probe returns a typed `HardwareProbe` model that the `civiccast doctor`
CLI and the `/api/hardware` FastAPI endpoint both consume — same data,
two surfaces.

GPU detection is NVIDIA-only via NVML (per ADR 0005). AMD ROCm and Apple
Metal probing are post-1.0 work; on systems without an NVIDIA driver the
probe returns `gpu = None` rather than failing.
"""

from __future__ import annotations

import contextlib
import platform as _platform
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import psutil
from pydantic import BaseModel, Field

Tier = Literal["tier-0", "tier-1", "tier-1-plus", "tier-2"]
OSKind = Literal["wsl2", "linux", "macos", "windows", "unknown"]


class CPUInfo(BaseModel):
    """CPU summary."""

    cores_physical: int = Field(..., description="Number of physical CPU cores.")
    cores_logical: int = Field(..., description="Number of logical CPU cores (with SMT).")
    brand: str = Field(..., description="CPU brand string, best-effort.")


class RAMInfo(BaseModel):
    """System memory summary in gigabytes."""

    total_gb: float = Field(..., description="Total system RAM in GB.")
    available_gb: float = Field(..., description="Available RAM in GB.")


class DiskInfo(BaseModel):
    """Working-storage filesystem summary in gigabytes."""

    path: str = Field(
        ...,
        description=(
            "Filesystem probed. The local probe reports the exact path (defaults to the "
            "user's home directory); the public GET /api/hardware surface reports only "
            "the volume anchor, so it never discloses the OS account name."
        ),
    )
    total_gb: int = Field(..., description="Total filesystem size in GB.")
    free_gb: int = Field(..., description="Free filesystem space in GB.")


class GPUInfo(BaseModel):
    """NVIDIA GPU summary. None when no NVIDIA GPU is detected."""

    name: str = Field(..., description="GPU model name as reported by NVML.")
    vram_total_gb: float = Field(..., description="Total VRAM in GB.")
    vram_free_gb: float = Field(..., description="Free VRAM in GB at probe time.")
    driver_version: str = Field(..., description="NVIDIA driver version.")
    cuda_version: str | None = Field(
        default=None,
        description="CUDA runtime version exposed via NVML, if available.",
    )


class OSContext(BaseModel):
    """Operating system context — relevant for deployment-target classification."""

    kind: OSKind = Field(
        ...,
        description=(
            "OS classification: windows (native Windows — the primary deployment "
            "target per ADR 0021, which superseded ADR 0003's WSL2-primary "
            "decision), linux (native Linux, a supported deployment target), "
            "macos (Apple, a supported deployment target), wsl2 (Linux running "
            "inside a WSL2 guest — informational only; the WSL2 installer lane "
            "ADR 0003 described was retired under the owner's 'no linux' decision "
            "and is not a deployment target), or unknown."
        ),
    )
    system: str = Field(..., description="platform.system() value.")
    release: str = Field(..., description="platform.release() value.")
    machine: str = Field(..., description="platform.machine() value (architecture).")
    hostname: str = Field(..., description="Network hostname for human identification.")


class HardwareProbe(BaseModel):
    """Full hardware probe result.

    Returned by `civiccast.platform.hardware.probe()`, served as JSON by
    the `/api/hardware` endpoint, and rendered in human-readable form by
    `civiccast doctor`.
    """

    cpu: CPUInfo
    ram: RAMInfo
    disk: DiskInfo
    gpu: GPUInfo | None = Field(
        default=None,
        description="NVIDIA GPU info, or None if no NVIDIA GPU is detected.",
    )
    os: OSContext
    recommended_tier: Tier = Field(
        ...,
        description=(
            "Recommended CivicCast deployment tier per spec §7.7 decision tree. "
            "tier-0: no GPU or VRAM<8GB, batch-only. "
            "tier-1: VRAM 8-16GB, streaming with hot-swap. "
            "tier-1-plus: VRAM 16-24GB, all AI loaded simultaneously. "
            "tier-2: VRAM>=24GB, multi-stream / consortium."
        ),
    )
    civiccast_version: str = Field(..., description="CivicCast version that produced this probe.")


# ---------------------------------------------------------------------------
# Probe implementation
# ---------------------------------------------------------------------------


def probe(disk_path: Path | None = None) -> HardwareProbe:
    """Probe local hardware and return a typed `HardwareProbe`.

    `disk_path` selects which mount the disk summary reports against.
    Defaults to the user's home directory, matching AgentSuiteLocal's
    pattern. Pass an explicit path when probing storage other than home
    (e.g., a NAS volume).
    """
    from civiccast._version import __version__

    return HardwareProbe(
        cpu=_probe_cpu(),
        ram=_probe_ram(),
        disk=_probe_disk(disk_path or Path.home()),
        gpu=_probe_gpu(),
        os=_probe_os(),
        recommended_tier=_tier_for(_probe_gpu()),
        civiccast_version=__version__,
    )


def public_hardware_probe(disk_path: Path | None = None) -> HardwareProbe:
    """Probe for the UNAUTHENTICATED ``GET /api/hardware`` surface.

    That endpoint is public by documented design so the installer can size a
    deployment before a station or any staff token exists
    (docs/ops/staff-route-protection.md). The probe's disk path defaults to
    ``Path.home()``, so returning it verbatim also disclosed the operating
    system account name to any caller (GauntletGate W-3). The endpoint's job is
    to answer *how big is this machine*, never *whose machine is it*.

    The path is reduced to its filesystem anchor -- ``C:\\`` on Windows, ``/``
    on POSIX -- which still names the volume the free-space figures describe
    while carrying no directory or account information. ``probe()`` is
    unchanged: ``civiccast doctor`` runs on the box, for the person who owns
    it, and wants the real path.
    """

    full = probe(disk_path)
    return full.model_copy(
        update={"disk": full.disk.model_copy(update={"path": str(Path(full.disk.path).anchor)})}
    )


def _probe_cpu() -> CPUInfo:
    physical = psutil.cpu_count(logical=False) or psutil.cpu_count() or 1
    logical = psutil.cpu_count(logical=True) or physical
    return CPUInfo(cores_physical=physical, cores_logical=logical, brand=_cpu_brand())


def _probe_ram() -> RAMInfo:
    mem = psutil.virtual_memory()
    return RAMInfo(
        total_gb=round(mem.total / 1024**3, 1),
        available_gb=round(mem.available / 1024**3, 1),
    )


def _probe_disk(path: Path) -> DiskInfo:
    """Report free/total space for the volume that would hold ``path``.

    ``shutil.disk_usage`` requires the path to exist -- it raises
    ``FileNotFoundError`` otherwise, rather than answering for the volume the
    path would live on. The default caller (``probe()``) passes
    ``Path.home()``, which is normally guaranteed to exist, but not always:
    a freshly provisioned account before first login, or a hermetic test
    environment that redirects ``HOME``/``USERPROFILE`` onto a directory it
    never creates. Walk up to the nearest existing ancestor to measure, while
    still reporting the originally requested ``path`` verbatim -- callers
    (``civiccast doctor``, the operator-authenticated probe) want to see
    where they asked to measure, not where the measurement actually landed.
    """
    usage_path = path
    while not usage_path.exists():
        parent = usage_path.parent
        if parent == usage_path:
            break
        usage_path = parent
    usage = shutil.disk_usage(usage_path)
    return DiskInfo(
        path=str(path),
        total_gb=round(usage.total / 1024**3),
        free_gb=round(usage.free / 1024**3),
    )


def _probe_gpu() -> GPUInfo | None:
    """Return NVIDIA GPU info, or None if no NVIDIA GPU is reachable.

    Uses NVML via nvidia-ml-py (imported as `pynvml`). Catches every NVML
    failure mode — driver missing, library missing, no GPU present, WSL2
    GPU passthrough not configured — and returns None rather than raising.
    The doctor CLI's output makes the no-GPU case explicit so operators
    understand what they're seeing.
    """
    try:
        import pynvml  # type: ignore[import-untyped]
    except ImportError:
        return None

    try:
        pynvml.nvmlInit()
    except Exception:
        return None

    try:
        if pynvml.nvmlDeviceGetCount() == 0:
            return None
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        driver = pynvml.nvmlSystemGetDriverVersion()
        if isinstance(driver, bytes):
            driver = driver.decode("utf-8", errors="replace")
        cuda_int: int | None
        try:
            cuda_int = pynvml.nvmlSystemGetCudaDriverVersion()
        except Exception:
            cuda_int = None
        cuda_str = f"{cuda_int // 1000}.{(cuda_int % 1000) // 10}" if cuda_int is not None else None
        return GPUInfo(
            name=name,
            vram_total_gb=round(mem.total / 1024**3, 1),
            vram_free_gb=round(mem.free / 1024**3, 1),
            driver_version=driver,
            cuda_version=cuda_str,
        )
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            pynvml.nvmlShutdown()


def _probe_os() -> OSContext:
    system = _platform.system()
    release = _platform.release()
    return OSContext(
        kind=_classify_os(system, release),
        system=system,
        release=release,
        machine=_platform.machine(),
        hostname=_platform.node() or "unknown",
    )


def _classify_os(system: str, release: str) -> OSKind:
    """Classify the OS into one of the values the deployment story cares about."""
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        if _is_wsl2(release):
            return "wsl2"
        return "linux"
    return "unknown"


def _is_wsl2(release: str) -> bool:
    """Detect WSL2 via the kernel release string and /proc/version contents.

    WSL2 kernels include "microsoft" or "WSL" in the release string. Some
    distros normalize the case differently; `/proc/version` is the
    authoritative cross-distro check.

    NOTE: inside a Linux container (Docker, Podman, k8s pod) `/proc/version`
    reports the host kernel, not the container's. A container running on
    Docker Desktop atop Windows-WSL2 would falsely match "microsoft" and
    misreport itself as WSL2 even though the container is plain Linux.
    The container detection below skips the /proc/version fallback when
    we're clearly inside a container.
    """
    if "microsoft" in release.lower() or "wsl" in release.lower():
        return True
    if _in_linux_container():
        return False
    try:
        proc_version = Path("/proc/version").read_text(errors="replace").lower()
        return "microsoft" in proc_version or "wsl" in proc_version
    except OSError:
        return False


def _in_linux_container() -> bool:
    """Best-effort detection that we're running inside a Linux container.

    Used to suppress the WSL2 `/proc/version` fallback (the host kernel info
    leaks through into containers, which would otherwise lie about the
    container's actual OS).
    """
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(errors="replace")
    except OSError:
        return False
    return any(marker in cgroup for marker in ("docker", "containerd", "kubepods", "podman"))


def _cpu_brand() -> str:
    """Best-effort human-readable CPU brand string.

    Resolution order:

    1. **Linux** — read ``/proc/cpuinfo`` ``model name``. Authoritative when
       present and the most operator-readable form.
    2. **Windows** — call ``wmic cpu get name`` to fetch the friendly model
       name (e.g., ``AMD Ryzen 7 7800X3D 8-Core Processor``). Falls through
       to ``platform.processor()`` if WMIC is unavailable or fails.
    3. **macOS / fallback** — ``platform.processor()`` plus the architecture
       suffix.

    On Windows specifically, ``platform.processor()`` returns the raw form
    ``"AMD64 Family 25 Model 80 Stepping 0, AuthenticAMD"`` — accurate but
    not what an operator wants to see. The WMIC call adds a subprocess but
    runs once at probe time and returns a cached result.
    """
    # Linux first (authoritative when /proc/cpuinfo is available).
    with contextlib.suppress(OSError):
        cpuinfo = Path("/proc/cpuinfo").read_text(errors="replace")
        for line in cpuinfo.splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()

    # Windows: prefer WMIC for the friendly name.
    if _platform.system() == "Windows":
        wmic_brand = _wmic_cpu_brand()
        if wmic_brand:
            return wmic_brand

    proc = _platform.processor()
    if proc:
        return f"{proc} ({_platform.machine()})"
    return f"{_platform.machine()} (brand unavailable)"


def _wmic_cpu_brand() -> str | None:
    """Return the friendly CPU brand name on Windows via WMIC, or ``None``.

    Tries ``wmic cpu get name /value`` (parseable key=value form). Returns
    None if WMIC is missing, returns non-zero, or produces no Name line —
    the caller falls back to ``platform.processor()`` in that case.
    """
    wmic = shutil.which("wmic")
    if wmic is None:
        return None

    try:
        completed = subprocess.run(  # noqa: S603 — argv is fully literal
            [wmic, "cpu", "get", "name", "/value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None

    if completed.returncode != 0:
        return None

    for line in completed.stdout.splitlines():
        line = line.strip()
        if line.lower().startswith("name="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    return None


def _tier_for(gpu: GPUInfo | None) -> Tier:
    """Recommend a deployment tier from the GPU/VRAM probe per spec §7.7."""
    if gpu is None or gpu.vram_total_gb < 8:
        return "tier-0"
    if gpu.vram_total_gb < 16:
        return "tier-1"
    if gpu.vram_total_gb < 24:
        return "tier-1-plus"
    return "tier-2"
