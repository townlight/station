# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""S8-5 system-resource sampler (spec §3.8/§6.6).

A periodic tick persists a ``SystemResourceSample`` and feeds threshold breaches
into the alert hub via ``record_alert_condition``. Resource thresholds default to
``warning`` severity; db-unreachable / service-down default to ``critical`` (the
severity lives on the seeded rule, not here). Conditions fire on breach and
resolve cleanly when the breach clears, so a persistent low-disk condition pages
once — not every 60s tick.

This module owns the derive + persist + fire/resolve logic (fully unit-tested
against the real store). The platform probing that builds a ``SystemResourceSample``
(psutil CPU/RAM/GPU, storage free/writable, db ping, service-up, clock skew) is
wired separately where the daemon tick runs.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from civiccast.alerting.models import SystemResourceSample
from civiccast.alerting.store import (
    append_resource_sample,
    get_alert_events,
    record_alert_condition,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from civiccast.alerting.models import AlertConditionKind

_GIB = 1024**3

# Single-host resource conditions share this resource_ref so dedupe is per-box.
_RESOURCE_REF = "host"
# The condition kinds this sampler owns — used to scope resolve to our own events.
_RESOURCE_KINDS: tuple[str, ...] = ("disk-low", "clock-skew", "db-unreachable", "service-down")


@dataclass
class ResourceThresholds:
    """Operator-tunable breach thresholds for the resource sampler."""

    min_media_free_gb: float = 20.0
    min_backup_free_gb: float = 20.0
    max_clock_skew_seconds: float = 5.0


def derive_resource_conditions(
    sample: SystemResourceSample,
    thresholds: ResourceThresholds,
) -> list[tuple[AlertConditionKind, str]]:
    """Return the (condition, summary) pairs currently breached by *sample*.

    Pure: no I/O. A None metric (unsamplable on this platform) never breaches.
    """
    conditions: list[tuple[AlertConditionKind, str]] = []

    low_volumes: list[str] = []
    if (
        sample.media_volume_free_gb is not None
        and sample.media_volume_free_gb < thresholds.min_media_free_gb
    ):
        low_volumes.append(
            f"media {sample.media_volume_free_gb:.1f} GB (min {thresholds.min_media_free_gb:g})"
        )
    if (
        sample.backup_volume_free_gb is not None
        and sample.backup_volume_free_gb < thresholds.min_backup_free_gb
    ):
        low_volumes.append(
            f"backup {sample.backup_volume_free_gb:.1f} GB (min {thresholds.min_backup_free_gb:g})"
        )
    if low_volumes:
        conditions.append(("disk-low", "Low free space: " + ", ".join(low_volumes)))

    if (
        sample.clock_skew_seconds is not None
        and abs(sample.clock_skew_seconds) > thresholds.max_clock_skew_seconds
    ):
        conditions.append(
            (
                "clock-skew",
                f"Host clock skew {sample.clock_skew_seconds:+.1f}s "
                f"(max {thresholds.max_clock_skew_seconds:g}s)",
            )
        )

    if not sample.db_reachable:
        conditions.append(("db-unreachable", "Database is not reachable from the host."))

    if not sample.service_running:
        conditions.append(("service-down", "The CivicCast egress service is not running."))

    return conditions


def sample_and_record(
    session: Session,
    sample: SystemResourceSample,
    thresholds: ResourceThresholds | None = None,
    *,
    now: datetime | None = None,
) -> SystemResourceSample:
    """Persist *sample*, fire breached conditions, and resolve cleared ones."""
    thresholds = thresholds or ResourceThresholds()
    observed = now if now is not None else sample.sampled_at

    persisted = append_resource_sample(session, sample)

    breached = dict(derive_resource_conditions(sample, thresholds))
    for kind, summary in breached.items():
        record_alert_condition(
            session,
            kind=kind,
            resource_ref=_RESOURCE_REF,
            source_section="S8",
            summary=summary,
            observed_at=observed,
        )

    # Resolve any of OUR firing resource conditions that are no longer breached.
    for event in get_alert_events(session, state="firing"):
        if (
            event.resource_ref == _RESOURCE_REF
            and event.condition in _RESOURCE_KINDS
            and event.condition not in breached
        ):
            record_alert_condition(
                session,
                kind=event.condition,
                resource_ref=_RESOURCE_REF,
                source_section="S8",
                summary=f"{event.condition} cleared",
                observed_at=observed,
                resolved=True,
            )

    return persisted


# ---------------------------------------------------------------------------
# Platform probing (real metrics -> SystemResourceSample)
# ---------------------------------------------------------------------------


@dataclass
class ResourceProbes:
    """Injectable probe callables; each maps to one SystemResourceSample field.

    A probe returning None (or raising) yields a None metric — "not samplable on
    this platform" — never a fabricated value. ``db_reachable`` and
    ``service_running`` are required (the daemon wires real checks) so the sampler
    never claims the DB/service are up without actually checking.
    """

    cpu_percent: Callable[[], float | None]
    ram: Callable[[], tuple[float | None, float | None]]  # (used_gb, total_gb)
    gpu: Callable[[], tuple[float | None, float | None]]  # (percent, vram_used_gb)
    media_free_gb: Callable[[], float | None]
    backup_free_gb: Callable[[], float | None]
    backup_writable: Callable[[], bool]
    db_reachable: Callable[[], bool]
    service_running: Callable[[], bool]
    clock_skew_seconds: Callable[[], float | None]


def _safe[T](fn: Callable[[], T], default: T) -> T:
    try:
        return fn()
    except Exception:
        return default


def build_resource_sample(probes: ResourceProbes, *, now: datetime) -> SystemResourceSample:
    """Assemble a SystemResourceSample from *probes*, fail-safe per field.

    A crashing probe degrades to a safe value: metrics -> None, ``db_reachable``
    -> False (fail-closed: unknown reachability is treated as down, not up),
    ``backup_volume_writable``/``service_running`` -> False as well.
    """
    empty_metric_pair: tuple[float | None, float | None] = (None, None)
    ram_used, ram_total = _safe(probes.ram, empty_metric_pair)
    gpu_pct, gpu_vram = _safe(probes.gpu, empty_metric_pair)
    return SystemResourceSample(
        sampled_at=now,
        cpu_percent=_safe(probes.cpu_percent, None),
        ram_used_gb=ram_used,
        ram_total_gb=ram_total,
        gpu_percent=gpu_pct,
        gpu_vram_used_gb=gpu_vram,
        media_volume_free_gb=_safe(probes.media_free_gb, None),
        backup_volume_free_gb=_safe(probes.backup_free_gb, None),
        backup_volume_writable=_safe(probes.backup_writable, False),
        db_reachable=_safe(probes.db_reachable, False),
        service_running=_safe(probes.service_running, False),
        clock_skew_seconds=_safe(probes.clock_skew_seconds, None),
    )


def _free_gb(path: Path | None) -> Callable[[], float | None]:
    def probe() -> float | None:
        if path is None:
            return None
        return shutil.disk_usage(path).free / _GIB

    return probe


def _writable(path: Path | None) -> Callable[[], bool]:
    def probe() -> bool:
        if path is None:
            return True
        marker = path / ".civiccast-write-probe"
        try:
            marker.write_text("ok", encoding="utf-8")
            marker.unlink()
            return True
        except OSError:
            return False

    return probe


def default_resource_probes(
    *,
    db_reachable: Callable[[], bool],
    service_running: Callable[[], bool],
    media_path: Path | None = None,
    backup_path: Path | None = None,
    clock_skew_seconds: Callable[[], float | None] = lambda: None,
) -> ResourceProbes:
    """Real-platform probes (psutil CPU/RAM + disk free/writability).

    ``db_reachable`` and ``service_running`` are required — the daemon supplies a
    real DB ping and service-unit check (we never assume they're up). GPU live
    utilisation is None by default (optional; not sampled on headless boxes).
    Clock skew defaults to None (an NTP/host comparison is wired by the daemon
    when a time source is configured).
    """
    import psutil

    def cpu() -> float | None:
        return float(psutil.cpu_percent(interval=None))

    def ram() -> tuple[float | None, float | None]:
        vm = psutil.virtual_memory()
        return (vm.used / _GIB, vm.total / _GIB)

    return ResourceProbes(
        cpu_percent=cpu,
        ram=ram,
        gpu=lambda: (None, None),
        media_free_gb=_free_gb(media_path),
        backup_free_gb=_free_gb(backup_path),
        backup_writable=_writable(backup_path),
        db_reachable=db_reachable,
        service_running=service_running,
        clock_skew_seconds=clock_skew_seconds,
    )
