# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Pre-flight checklist contract evaluator.

Sprint 0.4 Slice 1 Commit 5. Implements the typed contract that the
v0.4 scope-lock (`docs/releases/v0.4-scope-lock.md` section 1, line
116 + line 127) and the design note (`docs/research/v04-slice1-
broadcast-spine-design.md` section "File Blast Radius" -> bullet
`civiccast/live/preflight.py`) call for.

Nine checks fire in canonical order:

    1. network              (probe-injected by caller)
    2. storage              (probe-injected by caller)
    3. ai_runtime           (stub; probe-injected; optional in Slice 1)
    4. live_source          (DB lookup: any LiveSource for the
                             session's channel_id)
    5. recording_target     (DB lookup: any non-rehearsal local
                             RecordingTarget exists)
    6. operator_confirm     (caller-supplied boolean)
    7. syndication          (provider posture: YouTube)
    8. internet_archive     (provider posture: Internet Archive)
    9. nas                  (provider posture: local NAS archive)

Every check carries a status (pass / fail / not_configured) and, when
the status is fail or not_configured, a machine-readable ``reason_code``
plus a human-readable ``message`` so the future staff router can
serialize the evaluation result and a future operator UI can show
actionable next steps without re-mapping enum values.

Readiness rule:

* Required checks (network, storage, live_source, recording_target,
  operator_confirm): MUST be ``pass``.
* AI runtime: ``pass`` OR ``not_configured`` is acceptable in Slice 1.
  A ``fail`` (probed and broken) blocks readiness.
* Publish surfaces (syndication, internet_archive, nas): never block
  readiness. They complete asynchronously AFTER the recording, so a
  station can lawfully go on air while they are unconfigured — but the
  operator is told which posture each one is in BEFORE the meeting,
  because discovering a simulated archive afterwards is discovering it
  too late.

These three used to be hard-coded ``not_configured`` with the message
"the underlying integration lands in a later rung." That text was written
at Sprint 0.4 and was still shipping at 1.0.0-rc17, long after the
integrations landed (``civiccast/archive/internet_archive.py``,
``civiccast/archive/local_nas.py``, ``civiccast/syndicate/youtube.py``).
They now report the station's real provider posture via
:func:`civiccast.platform.providers.describe_provider` — the same registry
the publish path itself resolves through, so the checklist and the publish
run cannot disagree (GauntletGate PE-2).

The evaluator is pure with respect to the inputs and the DB rows it
reads. It does not mutate state. The DB lookups for live_source +
recording_target are read-only; no router endpoint or finalization
side-effect lives in this module.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.live.models import (
    LiveSession,
    LiveSource,
    RecordingTarget,
)
from civiccast.live.recording_paths import (
    REHEARSAL_RECORDING_TARGET_ID,
    local_recording_path,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]
SourceProbe = Callable[[LiveSource], tuple[bool, str | None]]
# Bug B3 (field evidence, v0.4 beta candidate #17): ``network_reachable`` /
# ``storage_free_bytes`` were documented as "probe-injected by caller," but
# the only caller wired up (the operator's Run Meeting screen) never ran a
# probe -- it always submitted ``None``, so every pre-flight run showed
# "not probed" with no way to clear it (network.not_probed /
# storage.not_probed, forever). These two callables let the evaluator run
# its own probe when the caller doesn't supply a result, exactly like
# ``SourceProbe`` already does for ``live_source`` -- the check exists to
# answer the question, so it should ask it. A caller that already has a
# fresher probe result (or wants to force a specific outcome, e.g. the
# installer's private rehearsal) still wins: these only fire when the
# corresponding ``PreflightInputs`` field is ``None``.
NetworkProbe = Callable[[], "tuple[bool, str | None]"]
StorageProbe = Callable[[], "tuple[int | None, str | None]"]

# ---------------------------------------------------------------------------
# Status + check-name constants
# ---------------------------------------------------------------------------

PREFLIGHT_STATUS_PASS = "pass"  # noqa: S105 -- check-result enum value, not a credential
PREFLIGHT_STATUS_FAIL = "fail"
PREFLIGHT_STATUS_NOT_CONFIGURED = "not_configured"

_PREFLIGHT_STATUSES: tuple[str, ...] = (
    PREFLIGHT_STATUS_PASS,
    PREFLIGHT_STATUS_FAIL,
    PREFLIGHT_STATUS_NOT_CONFIGURED,
)

PREFLIGHT_CHECK_NETWORK = "network"
PREFLIGHT_CHECK_STORAGE = "storage"
PREFLIGHT_CHECK_AI_RUNTIME = "ai_runtime"
PREFLIGHT_CHECK_LIVE_SOURCE = "live_source"
PREFLIGHT_CHECK_RECORDING_TARGET = "recording_target"
PREFLIGHT_CHECK_OPERATOR_CONFIRM = "operator_confirm"
PREFLIGHT_CHECK_SYNDICATION = "syndication"
PREFLIGHT_CHECK_INTERNET_ARCHIVE = "internet_archive"
PREFLIGHT_CHECK_NAS = "nas"

# Canonical order; the evaluator emits results in this order so the
# operator UI does not have to sort.
_PREFLIGHT_CHECK_ORDER: tuple[str, ...] = (
    PREFLIGHT_CHECK_NETWORK,
    PREFLIGHT_CHECK_STORAGE,
    PREFLIGHT_CHECK_AI_RUNTIME,
    PREFLIGHT_CHECK_LIVE_SOURCE,
    PREFLIGHT_CHECK_RECORDING_TARGET,
    PREFLIGHT_CHECK_OPERATOR_CONFIRM,
    PREFLIGHT_CHECK_SYNDICATION,
    PREFLIGHT_CHECK_INTERNET_ARCHIVE,
    PREFLIGHT_CHECK_NAS,
)

# Required checks: each MUST be ``pass`` for the evaluation to be ``ready``.
_REQUIRED_CHECKS: frozenset[str] = frozenset(
    {
        PREFLIGHT_CHECK_NETWORK,
        PREFLIGHT_CHECK_STORAGE,
        PREFLIGHT_CHECK_LIVE_SOURCE,
        PREFLIGHT_CHECK_RECORDING_TARGET,
        PREFLIGHT_CHECK_OPERATOR_CONFIRM,
    }
)

# Publish-surface checks: report the station's provider posture. Never block
# readiness (they complete after the recording), but never claim a tier is
# handled when it is running on a simulation either.
_PUBLISH_SURFACE_CHECKS: tuple[str, ...] = (
    PREFLIGHT_CHECK_SYNDICATION,
    PREFLIGHT_CHECK_INTERNET_ARCHIVE,
    PREFLIGHT_CHECK_NAS,
)

# Check name -> provider registry kind, and the plain-English surface name the
# operator sees. Imported lazily in the evaluator to keep this module's import
# graph free of the provider adapters.
_PUBLISH_SURFACE_PROVIDER_KIND: dict[str, str] = {
    PREFLIGHT_CHECK_SYNDICATION: "youtube",
    PREFLIGHT_CHECK_INTERNET_ARCHIVE: "internet_archive",
    PREFLIGHT_CHECK_NAS: "local_nas",
}

_PUBLISH_SURFACE_LABEL: dict[str, str] = {
    PREFLIGHT_CHECK_SYNDICATION: "Syndication (YouTube)",
    PREFLIGHT_CHECK_INTERNET_ARCHIVE: "Internet Archive",
    PREFLIGHT_CHECK_NAS: "Local NAS archive",
}

# ---------------------------------------------------------------------------
# Reason codes (machine-readable; stable identifiers for UI mapping)
# ---------------------------------------------------------------------------

REASON_NETWORK_UNREACHABLE = "network.unreachable"
REASON_NETWORK_NOT_PROBED = "network.not_probed"
REASON_STORAGE_INSUFFICIENT = "storage.insufficient_free_space"
REASON_STORAGE_NOT_PROBED = "storage.not_probed"
REASON_AI_RUNTIME_NOT_READY = "ai_runtime.not_ready"
REASON_AI_RUNTIME_NOT_CONFIGURED = "ai_runtime.not_configured"
REASON_LIVE_SESSION_NOT_FOUND = "live_session.not_found"
REASON_NO_LIVE_SOURCE_FOR_CHANNEL = "live_source.none_configured_for_channel"
REASON_SELECTED_LIVE_SOURCE_INVALID = "live_source.selected_source_invalid"
REASON_LIVE_SOURCE_NOT_PROBED = "live_source.not_probed"
REASON_LIVE_SOURCE_UNAVAILABLE = "live_source.unavailable"
REASON_NO_RECORDING_TARGET = "recording_target.none_configured"
REASON_OPERATOR_NOT_CONFIRMED = "operator_confirm.not_confirmed"
REASON_PUBLISH_SURFACE_SIMULATED = "publish_surface.simulated"
REASON_PUBLISH_SURFACE_MISCONFIGURED = "publish_surface.misconfigured"


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


_DEFAULT_MIN_FREE_BYTES = 50 * (1024**3)  # 50 GiB


class PreflightInputs(BaseModel):
    """Caller-supplied probe results and operator-confirm flag.

    Treating probe results as inputs keeps the evaluator pure. A
    network/storage/AI probe is a side-effecting concern that lives in
    the staff API layer -- the evaluator only consumes the structured
    probe outcome, whether that outcome was supplied by the caller
    (e.g. an explicit rehearsal fixture) or filled in by the
    evaluator's own :data:`NetworkProbe` / :data:`StorageProbe`
    (bug B3: every real caller submitted ``None`` for both, so the
    evaluator now runs its own probe by default instead of depending
    on a caller that never probed).

    ``network_reachable`` / ``storage_free_bytes`` / ``ai_runtime_ready``
    default to ``None`` meaning "not yet known." For the required
    network + storage checks, ``None`` that a configured probe also
    could not resolve is treated as ``fail`` with a ``not_probed``
    reason code; for the optional AI runtime check, ``None`` is
    treated as ``not_configured``.
    """

    model_config = ConfigDict(extra="forbid")

    live_session_id: str = Field(min_length=1, max_length=64)
    live_source_id: str = Field(min_length=1, max_length=64)
    network_reachable: bool | None = None
    storage_free_bytes: int | None = Field(default=None, ge=0)
    storage_min_free_bytes: int = Field(default=_DEFAULT_MIN_FREE_BYTES, ge=0)
    ai_runtime_ready: bool | None = None
    operator_confirmed: bool = False


class PreflightCheckResult(BaseModel):
    """Single typed check outcome.

    ``reason_code`` is the machine-readable identifier the future
    operator UI maps to a "Next step" copy block. ``message`` is the
    human-readable fallback the API can surface verbatim while the
    UI is still building out per-reason mappings.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    status: str
    reason_code: str | None = None
    message: str | None = None


class PreflightEvaluation(BaseModel):
    """Full pre-flight evaluation result for a single live session.

    ``ready`` is derived from the checks per the readiness rule
    described in the module docstring. The future staff API will
    return this entire shape JSON-serialized; the operator UI will
    render ``checks`` in the order they appear (the evaluator emits
    them in canonical order; see :data:`_PREFLIGHT_CHECK_ORDER`).
    """

    model_config = ConfigDict(extra="forbid")

    live_session_id: str
    checks: list[PreflightCheckResult]
    ready: bool


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class PreflightEvaluator:
    """Evaluate the pre-flight checklist for a given live session.

    Constructor takes the same session-factory shape as
    :class:`civiccast.live.store.LiveSessionStore` so a caller can
    share an engine binding across stores. The evaluator opens one
    read-only session per evaluation; the session is closed before
    :meth:`evaluate` returns.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        source_probe: SourceProbe | None = None,
        network_probe: NetworkProbe | None = None,
        storage_probe: StorageProbe | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._source_probe = source_probe
        self._network_probe = network_probe
        self._storage_probe = storage_probe

    @property
    def source_probe_configured(self) -> bool:
        """Whether this evaluator can verify that media is actually arriving."""
        return self._source_probe is not None

    @property
    def network_probe_configured(self) -> bool:
        """Whether this evaluator can check network reachability itself."""
        return self._network_probe is not None

    @property
    def storage_probe_configured(self) -> bool:
        """Whether this evaluator can check free recording storage itself."""
        return self._storage_probe is not None

    def evaluate(
        self,
        inputs: PreflightInputs,
        *,
        source_probe_override: SourceProbe | None = None,
        network_probe_override: NetworkProbe | None = None,
        storage_probe_override: StorageProbe | None = None,
    ) -> PreflightEvaluation:
        source_probe = source_probe_override or self._source_probe
        network_probe = network_probe_override or self._network_probe
        storage_probe = storage_probe_override or self._storage_probe
        results_by_name: dict[str, PreflightCheckResult] = {}

        # Network -------------------------------------------------------------
        network_reachable = inputs.network_reachable
        network_probe_message: str | None = None
        if network_reachable is None and network_probe is not None:
            try:
                network_reachable, network_probe_message = network_probe()
            except Exception:
                network_reachable, network_probe_message = None, None

        if network_reachable is None:
            results_by_name[PREFLIGHT_CHECK_NETWORK] = PreflightCheckResult(
                name=PREFLIGHT_CHECK_NETWORK,
                status=PREFLIGHT_STATUS_FAIL,
                reason_code=REASON_NETWORK_NOT_PROBED,
                message=(
                    network_probe_message
                    or "CivicCast could not run its own network probe. Confirm the station "
                    "has a network connection, then select Run pre-flight again."
                ),
            )
        elif network_reachable:
            results_by_name[PREFLIGHT_CHECK_NETWORK] = PreflightCheckResult(
                name=PREFLIGHT_CHECK_NETWORK,
                status=PREFLIGHT_STATUS_PASS,
                message=network_probe_message,
            )
        else:
            results_by_name[PREFLIGHT_CHECK_NETWORK] = PreflightCheckResult(
                name=PREFLIGHT_CHECK_NETWORK,
                status=PREFLIGHT_STATUS_FAIL,
                reason_code=REASON_NETWORK_UNREACHABLE,
                message=(
                    network_probe_message
                    or "Network unreachable; check WAN connectivity and re-run pre-flight."
                ),
            )

        # Storage -------------------------------------------------------------
        storage_free_bytes = inputs.storage_free_bytes
        storage_probe_message: str | None = None
        if storage_free_bytes is None and storage_probe is not None:
            try:
                storage_free_bytes, storage_probe_message = storage_probe()
            except Exception:
                storage_free_bytes, storage_probe_message = None, None

        if storage_free_bytes is None:
            results_by_name[PREFLIGHT_CHECK_STORAGE] = PreflightCheckResult(
                name=PREFLIGHT_CHECK_STORAGE,
                status=PREFLIGHT_STATUS_FAIL,
                reason_code=REASON_STORAGE_NOT_PROBED,
                message=(
                    storage_probe_message
                    or "CivicCast could not run its own storage probe. Confirm the recording "
                    "drive is connected, then select Run pre-flight again."
                ),
            )
        elif storage_free_bytes >= inputs.storage_min_free_bytes:
            free_gib = storage_free_bytes / (1024**3)
            results_by_name[PREFLIGHT_CHECK_STORAGE] = PreflightCheckResult(
                name=PREFLIGHT_CHECK_STORAGE,
                status=PREFLIGHT_STATUS_PASS,
                message=f"{free_gib:.1f} GiB free.",
            )
        else:
            free_gib = storage_free_bytes / (1024**3)
            min_gib = inputs.storage_min_free_bytes / (1024**3)
            results_by_name[PREFLIGHT_CHECK_STORAGE] = PreflightCheckResult(
                name=PREFLIGHT_CHECK_STORAGE,
                status=PREFLIGHT_STATUS_FAIL,
                reason_code=REASON_STORAGE_INSUFFICIENT,
                message=(
                    f"Storage free {free_gib:.1f} GiB below required "
                    f"{min_gib:.1f} GiB; free up space or attach more storage."
                ),
            )

        # AI runtime (stub) ---------------------------------------------------
        if inputs.ai_runtime_ready is None:
            results_by_name[PREFLIGHT_CHECK_AI_RUNTIME] = PreflightCheckResult(
                name=PREFLIGHT_CHECK_AI_RUNTIME,
                status=PREFLIGHT_STATUS_NOT_CONFIGURED,
                reason_code=REASON_AI_RUNTIME_NOT_CONFIGURED,
                message="AI runtime not configured; optional for Slice 1.",
            )
        elif inputs.ai_runtime_ready:
            results_by_name[PREFLIGHT_CHECK_AI_RUNTIME] = PreflightCheckResult(
                name=PREFLIGHT_CHECK_AI_RUNTIME,
                status=PREFLIGHT_STATUS_PASS,
            )
        else:
            results_by_name[PREFLIGHT_CHECK_AI_RUNTIME] = PreflightCheckResult(
                name=PREFLIGHT_CHECK_AI_RUNTIME,
                status=PREFLIGHT_STATUS_FAIL,
                reason_code=REASON_AI_RUNTIME_NOT_READY,
                message="AI runtime probed but not ready; check the runtime adapter status.",
            )

        # Live source + recording target (DB-backed) --------------------------
        with self._session_factory() as session:
            live_session_row = session.execute(
                select(LiveSession).where(LiveSession.live_session_id == inputs.live_session_id)
            ).scalar_one_or_none()

            if live_session_row is None:
                results_by_name[PREFLIGHT_CHECK_LIVE_SOURCE] = PreflightCheckResult(
                    name=PREFLIGHT_CHECK_LIVE_SOURCE,
                    status=PREFLIGHT_STATUS_FAIL,
                    reason_code=REASON_LIVE_SESSION_NOT_FOUND,
                    message=(
                        f"LiveSession {inputs.live_session_id!r} not found; "
                        f"create the session before running pre-flight."
                    ),
                )
            else:
                source_row = session.execute(
                    select(LiveSource).where(
                        LiveSource.live_source_id == inputs.live_source_id,
                        LiveSource.channel_id == live_session_row.channel_id,
                    )
                ).scalar_one_or_none()
                if source_row is None:
                    channel_has_source = session.execute(
                        select(LiveSource.live_source_id)
                        .where(LiveSource.channel_id == live_session_row.channel_id)
                        .limit(1)
                    ).scalar_one_or_none()
                    reason_code = (
                        REASON_SELECTED_LIVE_SOURCE_INVALID
                        if channel_has_source is not None
                        else REASON_NO_LIVE_SOURCE_FOR_CHANNEL
                    )
                    results_by_name[PREFLIGHT_CHECK_LIVE_SOURCE] = PreflightCheckResult(
                        name=PREFLIGHT_CHECK_LIVE_SOURCE,
                        status=PREFLIGHT_STATUS_FAIL,
                        reason_code=reason_code,
                        message=(
                            f"Selected source {inputs.live_source_id!r} does not exist on "
                            f"channel {live_session_row.channel_id!r}; choose a source "
                            f"configured for this session and run pre-flight again."
                        ),
                    )
                elif source_probe is None:
                    results_by_name[PREFLIGHT_CHECK_LIVE_SOURCE] = PreflightCheckResult(
                        name=PREFLIGHT_CHECK_LIVE_SOURCE,
                        status=PREFLIGHT_STATUS_FAIL,
                        reason_code=REASON_LIVE_SOURCE_NOT_PROBED,
                        message=(
                            f"Source {source_row.live_source_id!r} is configured but has not "
                            "passed a server-side media probe; verify that CivicCast can "
                            "receive frames before going on air."
                        ),
                    )
                else:
                    try:
                        source_ready, source_message = source_probe(source_row)
                    except Exception:
                        source_ready, source_message = False, None
                    if source_ready:
                        results_by_name[PREFLIGHT_CHECK_LIVE_SOURCE] = PreflightCheckResult(
                            name=PREFLIGHT_CHECK_LIVE_SOURCE,
                            status=PREFLIGHT_STATUS_PASS,
                            message=source_message or "Server-side media probe passed.",
                        )
                    else:
                        results_by_name[PREFLIGHT_CHECK_LIVE_SOURCE] = PreflightCheckResult(
                            name=PREFLIGHT_CHECK_LIVE_SOURCE,
                            status=PREFLIGHT_STATUS_FAIL,
                            reason_code=REASON_LIVE_SOURCE_UNAVAILABLE,
                            message=(
                                source_message
                                or "Server-side media probe did not detect a usable source; "
                                "check the source endpoint and try again."
                            ),
                        )

            target_row = None
            target_rows = session.execute(
                select(RecordingTarget).order_by(
                    RecordingTarget.created_at.asc(),
                    RecordingTarget.recording_target_id.asc(),
                )
            ).scalars()
            for candidate in target_rows:
                if candidate.recording_target_id == REHEARSAL_RECORDING_TARGET_ID:
                    continue
                if local_recording_path(candidate.target_uri) is None:
                    continue
                target_row = candidate
                break
            if target_row is None:
                results_by_name[PREFLIGHT_CHECK_RECORDING_TARGET] = PreflightCheckResult(
                    name=PREFLIGHT_CHECK_RECORDING_TARGET,
                    status=PREFLIGHT_STATUS_FAIL,
                    reason_code=REASON_NO_RECORDING_TARGET,
                    message="No production local RecordingTarget configured; configure where CivicCast should save public recordings before going on air.",
                )
            else:
                results_by_name[PREFLIGHT_CHECK_RECORDING_TARGET] = PreflightCheckResult(
                    name=PREFLIGHT_CHECK_RECORDING_TARGET,
                    status=PREFLIGHT_STATUS_PASS,
                    message=f"Recording target {target_row.recording_target_id!r}.",
                )

        # Operator confirm ----------------------------------------------------
        if inputs.operator_confirmed:
            results_by_name[PREFLIGHT_CHECK_OPERATOR_CONFIRM] = PreflightCheckResult(
                name=PREFLIGHT_CHECK_OPERATOR_CONFIRM,
                status=PREFLIGHT_STATUS_PASS,
            )
        else:
            results_by_name[PREFLIGHT_CHECK_OPERATOR_CONFIRM] = PreflightCheckResult(
                name=PREFLIGHT_CHECK_OPERATOR_CONFIRM,
                status=PREFLIGHT_STATUS_FAIL,
                reason_code=REASON_OPERATOR_NOT_CONFIRMED,
                message="Operator must confirm pre-flight before the session can go on air.",
            )

        # Publish surfaces ---------------------------------------------------
        for surface in _PUBLISH_SURFACE_CHECKS:
            results_by_name[surface] = _evaluate_publish_surface(surface)

        # Emit in canonical order so the operator UI does not have to sort.
        ordered = [results_by_name[name] for name in _PREFLIGHT_CHECK_ORDER]
        ready = _compute_ready(ordered)
        return PreflightEvaluation(
            live_session_id=inputs.live_session_id,
            checks=ordered,
            ready=ready,
        )


def _evaluate_publish_surface(check_name: str) -> PreflightCheckResult:
    """Report what will actually happen to this tier after the broadcast.

    Three postures, and the operator needs to tell them apart BEFORE going on
    air (GauntletGate PE-2 / TW-1):

    * **real and usable** — ``pass``. The recording will genuinely reach it.
    * **real but unusable** — ``fail``. The station asked for a real publish
      and the credentials are missing or wrong, so the publish run will fail
      after the meeting. Does not block go-live; the recording is still made
      and the surface can be retried once fixed.
    * **simulated** (the shipped default) — ``not_configured``, said plainly:
      nothing is written anywhere.
    """

    from civiccast.platform.providers import describe_provider

    label = _PUBLISH_SURFACE_LABEL[check_name]
    posture = describe_provider(_PUBLISH_SURFACE_PROVIDER_KIND[check_name])

    if posture.simulated:
        return PreflightCheckResult(
            name=check_name,
            status=PREFLIGHT_STATUS_NOT_CONFIGURED,
            reason_code=REASON_PUBLISH_SURFACE_SIMULATED,
            message=(
                f"{label} is running in simulation - this meeting will NOT be "
                f"published there and nothing will be written. Configure the real "
                f"provider before relying on this surface for a public record."
            ),
        )
    if not posture.usable:
        return PreflightCheckResult(
            name=check_name,
            status=PREFLIGHT_STATUS_FAIL,
            reason_code=REASON_PUBLISH_SURFACE_MISCONFIGURED,
            message=(
                f"{label} is set to publish for real but is not usable: "
                f"{posture.error} The broadcast can still go ahead; this surface "
                f"will fail when the recording is published."
            ),
        )
    return PreflightCheckResult(
        name=check_name,
        status=PREFLIGHT_STATUS_PASS,
        message=f"{label} is configured; this meeting will be published there after the broadcast.",
    )


def _compute_ready(checks: list[PreflightCheckResult]) -> bool:
    """Apply the readiness rule documented at module top.

    Required checks must be ``pass``. AI runtime can be ``pass`` or
    ``not_configured`` but not ``fail``. The three publish surfaces never
    block in any status — they complete after the recording, so a station
    may lawfully go on air with them simulated or misconfigured. They are
    reported, not enforced.
    """
    required_passing = all(
        check.status == PREFLIGHT_STATUS_PASS for check in checks if check.name in _REQUIRED_CHECKS
    )
    if not required_passing:
        return False
    # AI runtime: must not be ``fail``. ``pass`` or ``not_configured`` OK.
    ai_check = next(
        (c for c in checks if c.name == PREFLIGHT_CHECK_AI_RUNTIME),
        None,
    )
    return not (ai_check is not None and ai_check.status == PREFLIGHT_STATUS_FAIL)
