# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The one place a live source's readiness is observed and re-observed.

Two callers, one rule:

* the operator's explicit **Check source** action
  (``POST /api/staff/live/sources/{id}/probe``), and
* the **takeover gate** -- called by
  :class:`civiccast.egress.takeover_service.TakeoverService` immediately
  before it creates a takeover audit row or enqueues a route change.

Both run the same bounded ffprobe path (``civiccast.live.source_probe``),
resolve credentials the same way, redact the same way, and persist the same
observation. There is deliberately no second, looser code path for takeover:
the audit finding this module closes (ENG-003) existed precisely because the
API surface and the production takeover wiring had drifted into two different
answers about what "ready" meant.

Fail-closed is the design, not a fallback. Every uncertain outcome -- row
gone, endpoint changed under us, probe refused, credential unresolved, store
write failed -- returns a verification that refuses air and names the reason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from civiccast.live.readiness import (
    next_action_for,
    readiness_state,
    readiness_ttl_seconds,
)
from civiccast.live.secrets import SecretResolver, load_live_source_secret
from civiccast.live.source_endpoints import supports_credentials
from civiccast.live.source_probe import (
    DEFAULT_SOURCE_PROBE_TIMEOUT_SECONDS,
    ProbeObservation,
    observe_live_source,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from civiccast.live.models import LiveSourceResponse

__all__ = [
    "LiveSourceReadinessService",
    "TakeoverReadiness",
]


def _secret_ref_for(source: Any) -> str | None:
    """The credential handle to hand the engine, or ``None``.

    Guarded by the same capability rule the probe uses: a handle on a source
    type that cannot execute one is ignored here rather than passed downstream,
    so a legacy row can never make the engine believe it is authenticating.
    """
    handle = (getattr(source, "credentials_handle", None) or "").strip()
    if not handle or not supports_credentials(source.source_type):
        return None
    return handle


@dataclass(frozen=True)
class TakeoverReadiness:
    """The takeover gate's verdict about one selected ingest path."""

    ok: bool
    reason: str
    error_code: str | None = None
    reprobed: bool = False
    #: The source's credential HANDLE (never its secret), so the engine can
    #: open an authenticated SRT feed. ``None`` for every other shape.
    secret_ref: str | None = None


class LiveSourceReadinessService:
    """Probe a configured live source, persist the observation, gate takeover.

    ``store`` is a :class:`civiccast.live.store.LiveSourceStore` (typed
    loosely so this module does not import the store and create the same
    router/store circular-import problem the live router already works
    around). ``clock`` and ``probe`` are injectable so tests can be
    deterministic without patching module globals.
    """

    def __init__(
        self,
        store: Any,
        *,
        timeout_seconds: float = DEFAULT_SOURCE_PROBE_TIMEOUT_SECONDS,
        resolve_secret: SecretResolver | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        probe: Callable[..., ProbeObservation] | None = None,
    ) -> None:
        self._store = store
        self._timeout_seconds = timeout_seconds
        self._resolve_secret = (
            resolve_secret if resolve_secret is not None else load_live_source_secret
        )
        self._clock = clock
        self._probe = probe or observe_live_source

    # -- explicit operator probe ------------------------------------------

    def probe(self, live_source_id: str) -> tuple[LiveSourceResponse, ProbeObservation, datetime]:
        """Probe one source and persist the result.

        Raises ``LiveSourceNotFoundError`` (from the store) when the row is
        gone -- the router turns that into a 404. Every other outcome is a
        persisted observation, including failures: a failed check is
        information the operator needs on the screen, not an exception.
        """
        source = self._store.get(live_source_id)
        if source is None:
            from civiccast.live.store import LiveSourceNotFoundError

            raise LiveSourceNotFoundError(live_source_id)
        observation = self._observe(source)
        observed_at = self._clock()
        # ``row_version``/``endpoint_url`` are what was actually probed above,
        # read before the (possibly multi-second) probe ran. If an edit landed
        # in that window the store refuses the write rather than persisting a
        # verdict about an address that is no longer this row's -- see
        # ``LiveSourceProbeConflictError``.
        refreshed = self._store.record_probe_observation(
            live_source_id,
            ok=observation.ok,
            observed_at=observed_at,
            detail=observation.detail,
            error_code=observation.error_code,
            expected_row_version=source.row_version,
            expected_endpoint_url=source.endpoint_url,
        )
        return refreshed, observation, observed_at

    # -- takeover gate ------------------------------------------------------

    def verify_for_takeover(
        self,
        *,
        channel_id: str,
        path_id: str,
        endpoint_url: str,
    ) -> TakeoverReadiness:
        """Decide whether ``path_id`` may take air, re-probing when needed.

        ``path_id`` is the ingest plan's path id, which for an operator's own
        configured source is the ``live_source_id``
        (``civiccast.live.relay._source_path``). A path id that names no
        LiveSource row is a relay path, and this gate has no opinion about it
        -- relay health is the relay store's own heartbeat surface.

        The race this closes: the ingest plan the operator saw was built at
        request time, and the takeover arrives later. Between the two, the row
        can be edited, re-pointed at a different address, or deleted. So the
        row is re-read here, its endpoint compared against the one the plan
        actually offered, and a fresh bounded probe run whenever the stored
        observation is not currently ``ready``. Any mismatch fails closed.
        """
        source = self._store.get(path_id)
        if source is None:
            # Not a LiveSource path id. Either a relay path (fine, not ours to
            # judge) or a source deleted since the plan was built. The caller
            # distinguishes: it only asks about paths it took from the plan,
            # and a relay path is reported ok-with-no-opinion here.
            return TakeoverReadiness(
                ok=True,
                reason=(
                    f"Ingest path {path_id!r} is not a configured meeting source; its "
                    "readiness comes from the relay health surface."
                ),
            )

        if source.endpoint_url != endpoint_url:
            return TakeoverReadiness(
                ok=False,
                reason=(
                    f"{source.name} was changed while this takeover was being prepared "
                    "(its address is no longer the one that was checked). Reload the Live "
                    "Room, check the source, and take air again."
                ),
                error_code="source_changed_during_takeover",
            )

        ttl = readiness_ttl_seconds()
        now = self._clock()
        current = readiness_state(
            source.probe_state, source.probe_observed_at, ttl_seconds=ttl, now=now
        )
        if current == "ready":
            return TakeoverReadiness(
                ok=True,
                reason=f"{source.name} was confirmed delivering media within the last {ttl}s.",
                secret_ref=_secret_ref_for(source),
            )

        # never_probed / stale / failed all get one bounded fresh look before
        # air changes. This is the "perform or reuse a bounded fresh probe"
        # step: a within-TTL observation above is reused, everything else is
        # re-observed now rather than trusted.
        observation = self._observe(source)
        observed_at = self._clock()
        from civiccast.live.store import LiveSourceProbeConflictError

        try:
            # ``row_version``/``endpoint_url`` are the values read above,
            # BEFORE the (up to ``timeout_seconds``-long) probe ran. This is
            # the guard against the race this method exists to close: a PATCH
            # that repoints the row inside the probe window must not be
            # overwritten by a verdict this probe reached about the OLD
            # address. The store refuses the write instead of persisting it.
            refreshed = self._store.record_probe_observation(
                path_id,
                ok=observation.ok,
                observed_at=observed_at,
                detail=observation.detail,
                error_code=observation.error_code,
                expected_row_version=source.row_version,
                expected_endpoint_url=source.endpoint_url,
            )
        except LiveSourceProbeConflictError:
            # The row was edited while the probe was running. The edit's own
            # write (``LiveSourceStore.update``) already reset readiness to
            # ``never_probed`` when it changed anything probe-relevant; this
            # verdict must not clobber that with an observation about an
            # address that is no longer the row's. Fail closed and tell the
            # operator to look again, exactly as the pre-probe endpoint check
            # above does.
            return TakeoverReadiness(
                ok=False,
                reason=(
                    f"{source.name} was changed while it was being checked for takeover "
                    "(its address is no longer the one that was checked). Reload the Live "
                    "Room, check the source, and take air again."
                ),
                error_code="source_changed_during_takeover",
                reprobed=True,
            )
        except Exception:
            # The row vanished (or the write failed) between the read and the
            # write. Fail closed: nothing may change air on the strength of an
            # observation the station could not record.
            return TakeoverReadiness(
                ok=False,
                reason=(
                    f"CivicCast could not record a fresh check of {source.name} before "
                    "taking air. Reload the Live Room and try again."
                ),
                error_code="observation_not_recorded",
                reprobed=True,
            )

        if not observation.ok:
            return TakeoverReadiness(
                ok=False,
                reason=next_action_for(
                    "failed", source_name=refreshed.name, detail=observation.detail
                ),
                error_code=observation.error_code,
                reprobed=True,
            )
        return TakeoverReadiness(
            ok=True,
            reason=f"{refreshed.name} was re-checked just now and is delivering media.",
            reprobed=True,
            secret_ref=_secret_ref_for(refreshed),
        )

    # -- internals ----------------------------------------------------------

    def _observe(self, source: Any) -> ProbeObservation:
        return self._probe(
            source,
            timeout_seconds=self._timeout_seconds,
            resolve_secret=self._resolve_secret,
        )
