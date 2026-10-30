# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Local station setup state for first-admin handoff.

This file intentionally owns only the small bootstrap identity that lets the
installer hand a browser to the operator console. Durable civic records still
belong in the database-backed module stores.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

from pydantic import BaseModel, ConfigDict, Field

from civiccast.ai_models.models import detect_summary_model_default
from civiccast.auth.models import (
    OperatorIdentity,
    OperatorRole,
    StaffAccountCreateRequest,
    StaffAccountResponse,
    StaffAccountUpdateRequest,
    StaffLoginRequest,
    StaffLoginResponse,
    StaffPasswordResetRequest,
    StaffSessionResponse,
)
from civiccast.auth.roles import ALL_OPERATOR_ROLES
from civiccast.cable.channel import default_channel_profiles
from civiccast.installer.models import (
    LIVE_CAPTIONS_DEFAULT,
    FirstAdminSetupRequest,
    FirstAdminSetupResponse,
    RecoveryKit,
    StationAuthResponse,
    StationChannelProfile,
    StationDashboardReadyState,
    StationLoginRequest,
    StationOperationMode,
    StationProfile,
    StationRecoveryRequest,
    StationRuntimePaths,
    StationSetupState,
    StationStorageLocations,
)

if TYPE_CHECKING:
    from civiccast.installer.commissioning import (
        ChannelCommissioningSetup,
        CommissioningCheckReport,
        CommissioningProofRun,
        CommissioningReport,
        CommissioningState,
    )

_LOG = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_PASSWORD_ITERATIONS = 210_000
_RECOVERY_CODE_COUNT = 8
_DEFAULT_ROLES = ["setup_admin", "publish_operator", "support_admin", "viewer"]

# OWNER DECISION 2026-08-29, extended 2026-08-30 (field evidence, candidate
# #17 board-meeting test, then the owner's own two-browser repro): the
# station used to keep exactly one operator-console token, so ANY new token
# issuance silently invalidated every other already-open browser's session.
# The 08-29 fix made recovery APPEND but left ordinary password login
# REPLACING the whole list -- and the owner then watched browser B's
# console die minutes after browser A signed in with the correct password,
# while the sign-in card promised the opposite ("without touching any other
# browser or device already signed in"). Knowing the correct password is
# not evidence that other live sessions are compromised either, so BOTH
# login and recovery now append a fresh token to `operator_console.tokens`,
# a bounded list of concurrently valid sessions (covered by
# test_station_login_and_recovery_keep_other_sessions_signed_in). The cap
# below only bounds unbounded growth from repeated logins/recoveries that
# are never explicitly signed out -- oldest-issued entries are evicted
# first; it is not a security control.
_MAX_OPERATOR_SESSIONS = 20
_OPERATOR_SESSION_TTL = 12 * 60 * 60
_STATE_MUTATION_LOCK = threading.RLock()


def _serialize_state_mutation[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Keep read-modify-write station-state operations from clobbering each other."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with _STATE_MUTATION_LOCK:
            return function(*args, **kwargs)

    return wrapped


# The station's seed channels MUST match the real playout/egress channel
# lineup. A manually-maintained second list here (a numbered channel set)
# once diverged from the real public/education/government channels and severed
# schedule -> commit-to-air. Derive the seed from the single source of truth
# (civiccast.cable.channel) so they can never drift again; specific channel
# numbers vary city to city and are never named in seed data, cards, or docs —
# only the PEG channel types (public, education, government).
_CHANNEL_PURPOSES: dict[str, str] = {
    "public": "Public meetings, civic boards, and community access.",
    "education": "School board, campus, student, and athletics programming.",
    "government": "Council, board, and commission meetings and official notices.",
}
_DEFAULT_CHANNEL_PROFILES = [
    StationChannelProfile(
        channel_id=profile.channel_id,
        display_name=profile.branding.display_name,
        purpose=_CHANNEL_PURPOSES.get(profile.channel_id, profile.branding.display_name),
    )
    for profile in default_channel_profiles()
]
_FALLBACK_DEFAULT_CHANNEL_ID = "government"


def _normalized_default_channel_id(persisted: object) -> str:
    """Return a real channel id, healing a station provisioned before the
    single-vocabulary fix.

    Stations commissioned by an earlier build persisted a numbered channel id
    (e.g. a ``gov-ch*`` style id) that no longer exists in the real
    playout/egress lineup. The old fallback only fired when the key was
    *absent*, so a present-but-retired value stayed stuck forever. Normalize
    any value that is not one of the station's real channels.
    """
    known = {profile.channel_id for profile in _DEFAULT_CHANNEL_PROFILES}
    candidate = str(persisted).strip() if persisted else ""
    return candidate if candidate in known else _FALLBACK_DEFAULT_CHANNEL_ID


class StationSetupAlreadyCompleteError(RuntimeError):
    """Raised when first-admin setup is attempted after completion."""


class StationAuthError(RuntimeError):
    """Raised when local station admin sign-in or recovery fails."""


class StationAccountNotFoundError(RuntimeError):
    """Raised when a requested staff account or session does not exist."""


class StationSetupNotCompleteError(RuntimeError):
    """Raised when a recovery-kit action needs completed first-admin setup."""


# ---------------------------------------------------------------------------
# S13 first-run adaptive-model seed (S3 §3: lives in station-state JSON, not a DB
# table). The seed records the adaptive default computed from detected RAM at
# commissioning; the operator override slot mirrors slice-1's override-else-default.
# The durable runtime *selection* still lives in the 0053 DB store; this is the
# first-run seed the console falls back to when no DB selection exists (S13 §6.1).
# ---------------------------------------------------------------------------

# The features whose first-run default is seeded into station-state. Only summary's
# default is adaptive (RAM-dependent); captions/translation have a single local
# default and do not need a seed slot today.
_SEEDED_AI_FEATURES = ("summary",)


class SummaryModelSeed(BaseModel):
    """The first-run summary-model seed: adaptive default + operator override slot."""

    model_config = ConfigDict(extra="forbid")

    adaptive_default_key: str = Field(min_length=1, max_length=120)
    operator_override_key: str | None = Field(default=None, max_length=120)
    detected_ram_gb: int = Field(ge=0)
    seeded_at: datetime

    @property
    def effective_key(self) -> str:
        """Operator override if set, else the adaptive default (slice-1 rule)."""
        return self.operator_override_key or self.adaptive_default_key


class AiModelSeed(BaseModel):
    """The per-feature first-run seed block persisted under ``ai_models``."""

    model_config = ConfigDict(extra="forbid")

    summary: SummaryModelSeed


def seed_ai_model_default(*, system_ram_total_gb: int, has_gpu: bool = False) -> AiModelSeed:
    """Compute and persist the adaptive summary default into station-state.

    Idempotent on the override: re-seeding refreshes the adaptive default / detected
    RAM but preserves any operator override the wizard already recorded. The detected
    RAM should be the integer floor of the probed value (e.g. ``int(probe().ram.total_gb)``)
    so a 15.9 GB box rounds DOWN to 15 -> e4b, never up to a 12B it cannot run.

    ``has_gpu`` (default False, i.e. CPU-only) gates 12B the same way the runtime
    default does (:func:`~civiccast.ai_models.models.detect_summary_model_default`):
    field evidence on a 32GB CPU-only reference station showed 12B taking 366s to
    complete a summary once and then failing twice more, while e4b completed every
    attempt. A commissioning caller that cannot detect the GPU should pass the
    conservative default (False) rather than omit it -- omitting it here would
    silently reintroduce the RAM-only rule this signature exists to retire.
    """

    raw = _load_raw_state()
    existing = raw.get("ai_models")
    prior_override: str | None = None
    if isinstance(existing, dict):
        prior_summary = existing.get("summary")
        if isinstance(prior_summary, dict):
            prior_override = prior_summary.get("operator_override_key")

    summary = SummaryModelSeed(
        adaptive_default_key=detect_summary_model_default(system_ram_total_gb, has_gpu=has_gpu),
        operator_override_key=prior_override,
        detected_ram_gb=system_ram_total_gb,
        seeded_at=datetime.now(UTC),
    )
    seed = AiModelSeed(summary=summary)
    raw["ai_models"] = _ai_seed_to_state(seed)
    _save_raw_state(raw)
    return seed


def read_ai_model_seed() -> AiModelSeed | None:
    """Return the persisted first-run AI-model seed, or None before commissioning."""

    raw = _load_raw_state()
    block = raw.get("ai_models")
    if not isinstance(block, dict):
        return None
    try:
        return AiModelSeed.model_validate(block)
    except Exception:
        return None


def read_station_timezone() -> str | None:
    """Return the persisted station timezone, or None before commissioning.

    The first-admin setup wizard persists the operator's chosen IANA zone (or the
    ``"local"`` sentinel default) onto the station profile at commissioning time
    (M3). The running service is the actual consumer -- schedules, as-run logs,
    and program guides are wall-clock in this zone -- so it reads the value
    straight from station-state rather than requiring the installer to also copy
    it into the service's process environment as a second, driftable source of
    truth. Deliberately reads just this one field (not the full
    :func:`_profile_from_state` validation) so a timezone lookup never fails
    because an unrelated profile field is missing or malformed.
    """

    raw = _load_raw_state()
    station = raw.get("station")
    if not isinstance(station, dict):
        return None
    value = station.get("station_timezone")
    if not value:
        return None
    return str(value)


def resolve_station_timezone() -> str:
    """Effective station timezone: env override > persisted profile > default.

    This is the canonical S1 precedence loader for the timezone field.
    ``app.py``'s ``_station_tz`` delegates to this function rather than
    re-implementing the ``CIVICCAST_STATION_TZ`` / persisted-profile /
    default chain inline, so the precedence rule lives in exactly one
    place. Returns the raw zone name (or the ``"local"`` sentinel) — the
    caller resolves it to a ``tzinfo``.
    """

    env_value = os.environ.get("CIVICCAST_STATION_TZ")
    if env_value:
        return env_value
    persisted = read_station_timezone()
    if persisted:
        return persisted
    return "local"


def read_live_captions_enabled() -> bool | None:
    """The persisted live-caption switch, or ``None`` before commissioning.

    Deliberately reads just this one field rather than validating the whole
    profile (same reason as :func:`read_station_timezone`): whether the live
    caption tap runs must never depend on some unrelated profile field being
    well-formed.
    """

    raw = _load_raw_state()
    station = raw.get("station")
    if not isinstance(station, dict):
        return None
    value = station.get("live_captions_enabled")
    if value is None:
        return None
    return bool(value)


def resolve_live_captions_enabled() -> bool:
    """Whether the LIVE caption tap may run: env override > persisted >
    ``LIVE_CAPTIONS_DEFAULT`` (off in beta.5).

    Precedence is deliberately asymmetric, and only in the SAFE direction:
    ``CIVICCAST_CAPTION_TAP=off`` in the environment turns live captioning off
    no matter what is persisted, but no environment value can turn it back on
    against an operator who switched it off. An activated native station sets
    ``CIVICCAST_CAPTION_TAP=inline`` (and injects
    ``CIVICCAST_CAPTION_TAP_DIR``) UNLESS the service environment already
    carries ``CIVICCAST_CAPTION_TAP=off`` (item 91:
    ``civiccast.native.station_runtime.load_native_station_environment``
    passes that value through instead of overwriting it), so the reverse
    precedence -- letting a persisted "on" win over an explicit env "off" --
    would make the operator's switch unreachable on exactly the deployments
    that need it, and the asymmetric-safe direction chosen here still applies
    when both are in play.

    Defaults to OFF for beta.5 (temporary, owner decision 2026-09-09): with
    live captions on, the CEA-708 caption embed leg (``cccombiner`` between
    the encoder and the mux, ``civiccast/egress/gst/engine.py``
    ``_build_caption_embed``) held video for 25-30 s and then released a
    burst of ~900 frames every 1-2 minutes on every channel in the
    2026-09-09 sandbox soak, twice outlasting the 10 s stall watchdog. An
    absent key -- a fresh
    install, or a station commissioned before the switch existed -- therefore
    reads as off; an explicitly persisted ``true`` (an operator who turned
    them on) is honoured as before. The root-cause fix (beta.5.1) is what
    flips this default back.
    """

    if os.environ.get("CIVICCAST_CAPTION_TAP", "").strip().lower() == "off":
        return False
    persisted = read_live_captions_enabled()
    if persisted is None:
        return LIVE_CAPTIONS_DEFAULT
    return persisted


#: Set once the first time ``resolve_live_captions_enabled_or_default`` swallows
#: an exception, so a station whose ``station-state.json`` is permanently
#: corrupt/locked logs ONE WARNING per process instead of one per channel
#: start/reload, per safe-to-air computation, or per worker scan (the caption
#: feed polls every 2 s) for as long as the station runs -- matching the
#: "announced once" shape of ``CaptionTapWorker._disabled_announced``.
_live_captions_read_failure_announced = False


def resolve_live_captions_enabled_or_default() -> bool:
    """``resolve_live_captions_enabled()``, but never raises.

    ``_load_raw_state`` only suppresses ``FileNotFoundError`` and
    ``json.JSONDecodeError``. A byte that is not valid UTF-8 (``read_text``'s
    strict decode) or a Windows sharing violation while another process holds
    ``station-state.json`` (``PermissionError``/``OSError``) raise straight
    through the plain resolver. Every runtime reader of the switch -- the
    egress strategy deciding whether to build the embed leg, the safe-to-air
    banner, and the ``is_enabled`` callbacks of the caption tap, feed and proof
    workers -- is a best-effort accessibility path that must never take
    playout, the on-air banner, or a worker's ``run_once`` down over it. They
    all read through this helper so a read failure lands on the SAME value
    everywhere: the shipped default (``LIVE_CAPTIONS_DEFAULT``), i.e. "the
    switch could not be read", rather than on either operator choice. The
    failure is logged once per process, not once per call.
    """

    global _live_captions_read_failure_announced
    try:
        return resolve_live_captions_enabled()
    except Exception:
        if not _live_captions_read_failure_announced:
            _LOG.warning(
                "could not read the live-captions station-profile switch "
                "(station-state.json unreadable or locked); using the shipped "
                "default (%s) rather than blocking playout, the on-air banner, or "
                "the caption workers on an unrelated accessibility-feature switch. "
                "Every reader keeps using that default until the file becomes "
                "readable again; this warning is logged once per process.",
                "enabled" if LIVE_CAPTIONS_DEFAULT else "disabled",
                exc_info=True,
            )
            _live_captions_read_failure_announced = True
        return LIVE_CAPTIONS_DEFAULT


def resolve_station_display_name() -> str:
    """Effective station display name: env override > persisted profile > default.

    ``CIVICCAST_STATION_NAME`` is a distinct concept from
    ``CIVICCAST_STATION_ID`` (a separate, unrelated slug/id used by the
    trafficking log identifiers) -- this function governs the human-facing
    station name only.
    """

    env_value = os.environ.get("CIVICCAST_STATION_NAME")
    if env_value:
        return env_value
    raw = _load_raw_state()
    station = raw.get("station")
    if isinstance(station, dict):
        name = station.get("station_name")
        if isinstance(name, str) and name.strip():
            return name
    return "CivicCast Station"


def resolve_station_storage_locations() -> StationStorageLocations:
    """Effective storage roots: per-field env override > persisted profile > default.

    Individual env overrides (``CIVICCAST_STATION_MEDIA_LIBRARY``,
    ``CIVICCAST_STATION_RECORDINGS``, ``CIVICCAST_STATION_BACKUPS``) exist
    for advanced/offline scenarios; they are read here rather than
    scattered across callers. NOTE: this loader is deliberately scoped to
    the ``StationProfile.storage_locations`` identity fields set at
    first-admin setup. It does NOT govern ``CIVICCAST_UPLOAD_DIR`` /
    ``CIVICCAST_NAS_ARCHIVE_PATH`` -- those are a separate, more deeply
    cross-cutting env-driven contract (native supervisor child-process env
    inheritance, schedule/vod path resolution) that this slice
    intentionally does not fold in; see the S1/S3 PR description.
    """

    raw = _load_raw_state()
    station = raw.get("station")
    persisted = (
        _storage_locations_from_state(station.get("storage_locations"))
        if isinstance(station, dict)
        else _default_storage_locations()
    )
    return StationStorageLocations(
        media_library=os.environ.get("CIVICCAST_STATION_MEDIA_LIBRARY") or persisted.media_library,
        recordings=os.environ.get("CIVICCAST_STATION_RECORDINGS") or persisted.recordings,
        backups=os.environ.get("CIVICCAST_STATION_BACKUPS") or persisted.backups,
    )


class StationProfileUpdateRequest(BaseModel):
    """Mutable subset of ``StationProfile`` an operator can edit post-setup.

    Identity (``admin_username``), recovery-kit, and role fields are not
    editable through this surface -- they have their own dedicated flows
    (recovery, role management). Every field is optional so a PUT can
    change just one of them.
    """

    model_config = ConfigDict(extra="forbid")

    station_name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    station_timezone: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    public_base_url: Annotated[str, Field(min_length=1, max_length=240)] | None = None
    default_channel_id: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    storage_locations: StationStorageLocations | None = None
    live_captions_enabled: bool | None = None


def update_station_profile_fields(update: StationProfileUpdateRequest) -> StationProfile:
    """Validate and persist an operator edit to the mutable profile subset.

    Raises :class:`StationSetupNotCompleteError` if first-admin setup has
    not completed yet (there is no profile to edit). Returns the full,
    round-tripped ``StationProfile`` after the edit.
    """

    raw = _load_raw_state()
    station = raw.get("station")
    if not isinstance(station, dict) or not raw.get("setup_complete"):
        raise StationSetupNotCompleteError(
            "First-admin setup is not complete, so there is no station profile to edit."
        )

    if update.station_name is not None:
        station["station_name"] = update.station_name.strip()
    if update.station_timezone is not None:
        station["station_timezone"] = update.station_timezone.strip()
    if update.public_base_url is not None:
        station["public_base_url"] = update.public_base_url.strip()
    if update.default_channel_id is not None:
        station["default_channel_id"] = update.default_channel_id.strip()
    if update.storage_locations is not None:
        station["storage_locations"] = update.storage_locations.model_dump()
    if update.live_captions_enabled is not None:
        station["live_captions_enabled"] = bool(update.live_captions_enabled)

    raw["station"] = station
    _save_raw_state(raw)

    profile = _profile_from_state(raw)
    if profile is None:  # pragma: no cover - defensive; validated above
        raise StationSetupNotCompleteError("Station profile could not be re-read after update.")
    return profile


def read_commissioning_state() -> CommissioningState:
    """Return the persisted S3 commissioning progress (resumable across restarts).

    Mirrors the ``ai_models`` seed pattern exactly (S3 §3: commissioning
    state rides station-state JSON under its own namespaced top-level key,
    no DB table, no ``_SCHEMA_VERSION`` bump — that constant is not a
    migration gate in this file). Absent/malformed sub-blocks fail closed
    to ``None`` per field rather than raising, so a partially-written state
    file never blocks the wizard from resuming what it *can* read.
    """

    from civiccast.installer.commissioning import (
        ChannelCommissioningSetup,
        CommissioningCheckReport,
        CommissioningProofRun,
        CommissioningReport,
        CommissioningState,
    )

    raw = _load_raw_state()
    block = raw.get("commissioning")
    if not isinstance(block, dict):
        return CommissioningState()

    def _parse(model: type[BaseModel], key: str) -> Any:
        value = block.get(key)
        if not isinstance(value, dict):
            return None
        try:
            return model.model_validate(value)
        except Exception:
            return None

    return CommissioningState(
        first_run_checks=_parse(CommissioningCheckReport, "first_run_checks"),
        channel_setup=_parse(ChannelCommissioningSetup, "channel_setup"),
        proof_run=_parse(CommissioningProofRun, "proof_run"),
        report=_parse(CommissioningReport, "report"),
    )


def _save_commissioning_state(state: CommissioningState) -> None:
    raw = _load_raw_state()
    raw["commissioning"] = json.loads(state.model_dump_json())
    _save_raw_state(raw)


def save_commissioning_checks(report: CommissioningCheckReport) -> CommissioningState:
    """Persist the Screen 8 first-run cable check results."""

    state = read_commissioning_state()
    updated = state.model_copy(update={"first_run_checks": report})
    _save_commissioning_state(updated)
    return updated


def save_channel_commissioning_setup(setup: ChannelCommissioningSetup) -> CommissioningState:
    """Persist the Screen 9 channel setup choices."""

    state = read_commissioning_state()
    updated = state.model_copy(update={"channel_setup": setup})
    _save_commissioning_state(updated)
    return updated


def save_commissioning_proof_run(run: CommissioningProofRun) -> CommissioningState:
    """Persist the Screen 10 output-proof result."""

    state = read_commissioning_state()
    updated = state.model_copy(update={"proof_run": run})
    _save_commissioning_state(updated)
    return updated


def save_commissioning_report(report: CommissioningReport) -> CommissioningState:
    """Persist the Screen 11 final commissioning report."""

    state = read_commissioning_state()
    updated = state.model_copy(update={"report": report})
    _save_commissioning_state(updated)
    return updated


def reset_commissioning_state() -> None:
    """Clear all persisted commissioning progress (used by tests and re-commissioning)."""

    raw = _load_raw_state()
    raw.pop("commissioning", None)
    _save_raw_state(raw)


def set_ai_model_override(feature: str, model_key: str | None) -> AiModelSeed:
    """Record (or clear) the operator's first-run override for ``feature``.

    The wizard calls this when the operator overrides the adaptive default. Passing
    ``None`` clears the override and falls back to the adaptive default. Requires the
    seed to exist (``seed_ai_model_default`` must have run at commissioning).
    """

    if feature not in _SEEDED_AI_FEATURES:
        raise ValueError(
            f"Unknown or non-seeded AI feature {feature!r}; "
            f"seeded features: {', '.join(_SEEDED_AI_FEATURES)}."
        )
    seed = read_ai_model_seed()
    if seed is None:
        raise ValueError("AI model default is not seeded yet; run first-run commissioning first.")

    summary = seed.summary.model_copy(update={"operator_override_key": model_key})
    updated = AiModelSeed(summary=summary)
    raw = _load_raw_state()
    raw["ai_models"] = _ai_seed_to_state(updated)
    _save_raw_state(raw)
    return updated


def _ai_seed_to_state(seed: AiModelSeed) -> dict[str, Any]:
    """Serialize the seed to the plain JSON shape stored in station-state."""

    return {
        "summary": {
            "adaptive_default_key": seed.summary.adaptive_default_key,
            "operator_override_key": seed.summary.operator_override_key,
            "detected_ram_gb": seed.summary.detected_ram_gb,
            "seeded_at": seed.summary.seeded_at.isoformat(),
        }
    }


def station_state_path() -> Path:
    """Return the local bootstrap state path."""

    configured = os.getenv("CIVICCAST_STATION_STATE_PATH")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return root / "CivicCast" / "station-state.json"
    root = Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return root / "civiccast" / "station-state.json"


def read_station_setup_state(*, operator_console_url: str) -> StationSetupState:
    """Return setup state without secret-bearing fields."""

    raw = _load_raw_state()
    profile = _profile_from_state(raw)
    if profile is None:
        return StationSetupState(
            status="not_started",
            setup_complete=False,
            operator_console_url=operator_console_url,
            next_step="Create the first admin account and save the recovery kit.",
        )
    recovery = raw.get("recovery", {})
    recovery_kit_id = str(recovery.get("kit_id") or profile.recovery_kit_id)
    acknowledged = bool(recovery.get("acknowledged"))
    next_step = (
        "Open System Health and confirm the station is ready for a private rehearsal."
        if acknowledged
        else "Confirm the recovery kit is saved or printed before the first public meeting."
    )
    return StationSetupState(
        status="complete",
        setup_complete=True,
        station_name=profile.station_name,
        profile=profile,
        recovery_kit_created=True,
        recovery_kit_id=recovery_kit_id,
        recovery_kit_acknowledged=acknowledged,
        operator_console_url=operator_console_url,
        next_step=next_step,
    )


@_serialize_state_mutation
def complete_first_admin_setup(
    request: FirstAdminSetupRequest,
    *,
    operator_console_url: str,
) -> FirstAdminSetupResponse:
    """Persist first-admin setup and return the one-time recovery kit."""

    existing = _load_raw_state()
    if existing.get("setup_complete") and os.getenv("CIVICCAST_ALLOW_FIRST_ADMIN_RESET") != "1":
        raise StationSetupAlreadyCompleteError(
            "First-admin setup is already complete. Use the recovery flow or set "
            "CIVICCAST_ALLOW_FIRST_ADMIN_RESET=1 for an intentional local reset."
        )

    generated_at = datetime.now(UTC)
    kit_id = "rk_" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:18]
    recovery_codes = [_new_recovery_code() for _ in range(_RECOVERY_CODE_COUNT)]
    operator_token, token_entry = _new_operator_token_entry(source="setup")
    password_salt = secrets.token_hex(16)

    profile = StationProfile(
        station_name=request.station_name.strip(),
        admin_display_name=request.admin_display_name.strip(),
        admin_username=request.admin_username.strip(),
        default_channel_id=request.default_channel_id.strip(),
        public_base_url=request.public_base_url.strip() if request.public_base_url else None,
        station_timezone=request.station_timezone.strip(),
        storage_locations=request.storage_locations or _default_storage_locations(),
        runtime_paths=_station_runtime_paths(),
        channel_count=request.channel_count,
        channel_profiles=_build_channel_profiles(request.channel_count),
        sample_content_enabled=request.sample_content_enabled,
        initial_schedule_enabled=request.initial_schedule_enabled,
        default_roles=list(_DEFAULT_ROLES),
        operation_mode=request.operation_mode,
        dashboard_ready_state="not_ready",
        recovery_kit_id=kit_id,
        recovery_kit_generated_at=generated_at,
    )
    token_entry["operator_id"] = profile.admin_username
    recovery_kit = RecoveryKit(
        kit_id=kit_id,
        generated_at=generated_at,
        station_name=profile.station_name,
        admin_username=profile.admin_username,
        recovery_codes=recovery_codes,
        instructions=[
            "Print or save this kit before the first public meeting.",
            "Keep it somewhere separate from the CivicCast computer.",
            (
                "Routine sign-in: use 'Admin sign-in' with the username above and "
                "the admin password you just chose. The operator console's print/"
                "save actions include that password on the copy YOU save, so write "
                "it down now if you have not already -- CivicCast never stores it "
                "in a readable form and cannot show it again after this screen."
            ),
            (
                "Recovery codes are for emergencies ONLY -- use one only if the "
                "admin password is truly lost. Each code works once, immediately "
                "sets a new admin password, and should not be spent on routine "
                "sign-in."
            ),
            "If this kit is exposed, rotate the admin password and generate a new kit.",
        ],
        excludes=[
            "staff bearer token values",
            "provider secret values",
            "private keys",
            "resident email addresses",
            "database passwords",
        ],
    )

    raw_state = {
        "schema_version": _SCHEMA_VERSION,
        "setup_complete": True,
        "station": {
            "station_name": profile.station_name,
            "admin_display_name": profile.admin_display_name,
            "admin_username": profile.admin_username,
            "default_channel_id": profile.default_channel_id,
            "public_base_url": profile.public_base_url,
            "station_timezone": profile.station_timezone,
            "storage_locations": profile.storage_locations.model_dump(),
            "channel_count": profile.channel_count,
            "channel_profiles": [channel.model_dump() for channel in profile.channel_profiles],
            "sample_content_enabled": profile.sample_content_enabled,
            "initial_schedule_enabled": profile.initial_schedule_enabled,
            # Persisted explicitly at commissioning so a fresh install carries
            # the beta.5 default (off) as a stored value, and a later default
            # flip cannot silently change a station that already exists.
            "live_captions_enabled": profile.live_captions_enabled,
            "default_roles": profile.default_roles,
            "operation_mode": profile.operation_mode,
            "dashboard_ready_state": profile.dashboard_ready_state,
            "recovery_kit_id": profile.recovery_kit_id,
            "recovery_kit_generated_at": profile.recovery_kit_generated_at.isoformat(),
        },
        "admin": {
            "username": profile.admin_username,
            "display_name": profile.admin_display_name,
            "password_salt": password_salt,
            "password_hash": _hash_secret(request.admin_password, salt=password_salt),
            "password_iterations": _PASSWORD_ITERATIONS,
        },
        "recovery": {
            "kit_id": kit_id,
            "generated_at": generated_at.isoformat(),
            "destination": request.recovery_kit_destination.strip(),
            "acknowledged": False,
            "acknowledged_at": None,
            "code_hashes": [_hash_secret(code, salt=kit_id) for code in recovery_codes],
        },
        "operator_console": {
            "tokens": [token_entry],
        },
    }
    _save_raw_state(raw_state)

    return FirstAdminSetupResponse(
        status="complete",
        profile=profile,
        recovery_kit=recovery_kit,
        operator_console_url=operator_console_url,
        operator_console_token=operator_token,
        next_step="Open System Health, confirm readiness, then run a private rehearsal.",
    )


@_serialize_state_mutation
def acknowledge_recovery_kit(*, operator_console_url: str) -> StationSetupState:
    """Record that the operator saved or printed the one-time recovery kit."""

    raw = _load_raw_state()
    if not raw.get("setup_complete"):
        raise StationSetupNotCompleteError(
            "First-admin setup is not complete, so there is no recovery kit to confirm."
        )
    recovery = raw.setdefault("recovery", {})
    recovery["acknowledged"] = True
    recovery["acknowledged_at"] = datetime.now(UTC).isoformat()
    _save_raw_state(raw)
    return read_station_setup_state(operator_console_url=operator_console_url)


@_serialize_state_mutation
def verify_station_operator_token(token: str) -> OperatorIdentity | None:
    """Return a station-local operator identity when a session matches.

    Checks every concurrently valid session in ``operator_console.tokens``,
    not just the most recently issued one -- see ``_MAX_OPERATOR_SESSIONS``
    for why more than one can be valid at once. Legacy sessions acquire an
    expiry on first successful use; all newly issued sessions carry a fixed
    12-hour expiry.
    """

    raw = _load_raw_state()
    admin = raw.get("admin")
    console = raw.get("operator_console")
    if not isinstance(console, dict):
        return None
    entries = _operator_token_entries(console)
    for entry in entries:
        if not hmac.compare_digest(
            _hash_token(token, salt=entry["token_salt"]), entry["token_hash"]
        ):
            continue
        if _ensure_session_metadata(raw, console, entries, entry):
            _save_raw_state(raw)
        if _session_is_expired(entry):
            return None
        account_id = entry.get("account_id", "first-admin")
        session_id = str(entry.get("session_id") or _session_id_for_entry(entry))
        if account_id == "first-admin":
            if not isinstance(admin, dict):
                return None
            username = str(admin.get("username") or "first-admin")
            display_name = str(admin.get("display_name") or username)
            return OperatorIdentity(
                operator_id=username,
                operator_display_name=display_name,
                token_id=session_id,
                scopes=("admin",),
            )
        account = _find_staff_account(raw, str(account_id))
        if account is None or not account.get("enabled", True):
            return None
        roles = _account_roles(account)
        if not roles:
            return None
        return OperatorIdentity(
            operator_id=str(account["operator_id"]),
            operator_display_name=str(account["display_name"]),
            token_id=session_id,
            scopes=tuple(roles),
        )
    return None


def staff_login_credentials_correct(request: StaffLoginRequest) -> bool:
    """Non-mutating credential check for the login rate-limit exact-match bypass."""

    raw = _load_raw_state()
    account = _find_staff_account_by_username(raw, request.username)
    return account is not None and _verify_staff_password(account, request.password)


def login_staff_account(request: StaffLoginRequest) -> StaffLoginResponse:
    """Verify a local staff password and issue one bounded, expiring session."""

    with _STATE_MUTATION_LOCK:
        raw = _load_raw_state()
        account = _find_staff_account_by_username(raw, request.username)
        if account is None or not _verify_staff_password(account, request.password):
            raise StationAuthError("Invalid staff username or password.")
        if not account.get("enabled", True):
            raise StationAuthError("Invalid staff username or password.")
        roles = _account_roles(account)
        if not roles:
            raise StationAuthError("Invalid staff username or password.")
        token, entry = _issue_operator_token(
            raw,
            source="staff-login",
            account_id=str(account["operator_id"]),
            operator_id=str(account["operator_id"]),
            scopes=tuple(roles),
        )
        _save_raw_state(raw)
        return StaffLoginResponse(
            access_token=token,
            operator_id=str(account["operator_id"]),
            operator_display_name=str(account["display_name"]),
            roles=tuple(roles),
            issued_at=str(entry["issued_at"]),
            expires_at=str(entry["expires_at"]),
        )


def list_staff_accounts() -> list[StaffAccountResponse]:
    """Return first-admin and staff metadata, without password material."""

    raw = _load_raw_state()
    result: list[StaffAccountResponse] = []
    admin = raw.get("admin")
    if isinstance(admin, dict):
        username = str(admin.get("username") or "first-admin")
        result.append(
            StaffAccountResponse(
                operator_id="first-admin",
                username=username,
                display_name=str(admin.get("display_name") or username),
                roles=tuple(ALL_OPERATOR_ROLES),
                enabled=True,
                account_type="first_admin",
                created_at=(
                    str(raw.get("recovery", {}).get("generated_at"))
                    if isinstance(raw.get("recovery"), dict)
                    else None
                ),
                immutable=True,
            )
        )
    accounts = raw.get("staff_accounts")
    if isinstance(accounts, list):
        result.extend(
            _staff_account_response(account)
            for account in accounts
            if isinstance(account, dict) and isinstance(account.get("operator_id"), str)
        )
    return result


def create_staff_account(request: StaffAccountCreateRequest) -> StaffAccountResponse:
    """Create one staff account in portable station state."""

    with _STATE_MUTATION_LOCK:
        raw = _load_raw_state()
        username = request.username.strip().casefold()
        display_name = request.display_name.strip()
        if not username or not display_name:
            raise StationAuthError("A staff username and display name are required.")
        admin = raw.get("admin")
        if isinstance(admin, dict) and username == str(admin.get("username", "")).casefold():
            raise StationAuthError("That username is reserved for the first administrator.")
        if _find_staff_account_by_username(raw, username) is not None:
            raise StationAuthError("A staff account already uses that username.")
        now = datetime.now(UTC).isoformat()
        salt = secrets.token_hex(16)
        account: dict[str, Any] = {
            "operator_id": "staff_" + secrets.token_urlsafe(12),
            "username": username,
            "display_name": display_name,
            "password_salt": salt,
            "password_hash": _hash_secret(request.password, salt=salt),
            "password_iterations": _PASSWORD_ITERATIONS,
            "roles": sorted(set(request.roles)),
            "enabled": True,
            "created_at": now,
            "updated_at": now,
        }
        raw.setdefault("staff_accounts", []).append(account)
        _save_raw_state(raw)
        return _staff_account_response(account)


def update_staff_account(
    operator_id: str,
    request: StaffAccountUpdateRequest,
) -> StaffAccountResponse:
    """Update one staff record and revoke sessions after access changes."""

    if operator_id == "first-admin":
        raise StationAuthError("The first administrator is managed through the recovery flow.")
    with _STATE_MUTATION_LOCK:
        raw = _load_raw_state()
        account = _find_staff_account(raw, operator_id)
        if account is None:
            raise StationAccountNotFoundError("Staff account was not found.")
        current_roles = set(_account_roles(account))
        new_roles = set(request.roles) if request.roles is not None else current_roles
        new_enabled = (
            request.enabled if request.enabled is not None else bool(account.get("enabled", True))
        )
        if (
            account.get("enabled", True)
            and "setup_admin" in current_roles
            and (not new_enabled or "setup_admin" not in new_roles)
        ):
            _ensure_active_administrator_remains(raw, account)
        roles_changed = new_roles != current_roles
        enabled_changed = new_enabled != bool(account.get("enabled", True))
        if request.display_name is not None:
            display_name = request.display_name.strip()
            if not display_name:
                raise StationAuthError("A staff display name is required.")
            account["display_name"] = display_name
        account["roles"] = sorted(new_roles)
        account["enabled"] = new_enabled
        account["updated_at"] = datetime.now(UTC).isoformat()
        if roles_changed or enabled_changed:
            _revoke_staff_account_sessions(raw, operator_id)
        _save_raw_state(raw)
        return _staff_account_response(account)


def reset_staff_account_password(
    operator_id: str,
    request: StaffPasswordResetRequest,
) -> None:
    """Replace a staff password and revoke all of that account's sessions."""

    with _STATE_MUTATION_LOCK:
        raw = _load_raw_state()
        account = _find_staff_account(raw, operator_id)
        if account is None:
            raise StationAccountNotFoundError("Staff account was not found.")
        salt = secrets.token_hex(16)
        account["password_salt"] = salt
        account["password_hash"] = _hash_secret(request.new_password, salt=salt)
        account["password_iterations"] = _PASSWORD_ITERATIONS
        account["password_rotated_at"] = datetime.now(UTC).isoformat()
        account["updated_at"] = account["password_rotated_at"]
        _revoke_staff_account_sessions(raw, operator_id)
        _save_raw_state(raw)


def list_operator_sessions(*, current_token: str | None = None) -> list[StaffSessionResponse]:
    """List live and expired station sessions without exposing token material."""

    with _STATE_MUTATION_LOCK:
        raw = _load_raw_state()
        console = raw.get("operator_console")
        if not isinstance(console, dict):
            return []
        entries = _operator_token_entries(console)
        dirty = False
        for entry in entries:
            if _ensure_session_metadata(raw, console, entries, entry):
                dirty = True
        if console.get("tokens") is not entries:
            console["tokens"] = entries
            dirty = True
        if dirty:
            raw["operator_console"] = console
            _save_raw_state(raw)
        result: list[StaffSessionResponse] = []
        for entry in entries:
            operator_id = str(entry.get("operator_id") or "")
            account_id = str(entry.get("account_id") or "first-admin")
            if account_id == "first-admin":
                admin = raw.get("admin")
                if not isinstance(admin, dict):
                    continue
                operator_id = str(admin.get("username") or "first-admin")
                display_name = str(admin.get("display_name") or operator_id)
                roles = tuple(ALL_OPERATOR_ROLES)
            else:
                account = _find_staff_account(raw, account_id)
                if account is None:
                    continue
                operator_id = str(account["operator_id"])
                display_name = str(account["display_name"])
                roles = tuple(_account_roles(account))
            token_matches = bool(
                current_token
                and hmac.compare_digest(
                    _hash_token(current_token, salt=entry["token_salt"]),
                    entry["token_hash"],
                )
            )
            result.append(
                StaffSessionResponse(
                    session_id=str(entry["session_id"]),
                    operator_id=operator_id,
                    operator_display_name=display_name,
                    roles=roles,
                    source=str(entry.get("source") or "legacy"),
                    issued_at=str(entry["issued_at"]),
                    expires_at=str(entry["expires_at"]),
                    expired=_session_is_expired(entry),
                    current=token_matches,
                )
            )
        return result


def revoke_operator_session_by_id(session_id: str) -> None:
    """Revoke one station session selected by its public opaque id."""

    with _STATE_MUTATION_LOCK:
        raw = _load_raw_state()
        console = raw.get("operator_console")
        if not isinstance(console, dict):
            raise StationAccountNotFoundError("Staff session was not found.")
        entries = _operator_token_entries(console)
        for entry in entries:
            _ensure_session_metadata(raw, console, entries, entry)
        remaining = [entry for entry in entries if entry.get("session_id") != session_id]
        if len(remaining) == len(entries):
            raise StationAccountNotFoundError("Staff session was not found.")
        raw["operator_console"] = {
            "tokens": remaining,
            "rotated_at": datetime.now(UTC).isoformat(),
        }
        _save_raw_state(raw)


@_serialize_state_mutation
def login_station_admin(
    request: StationLoginRequest,
    *,
    operator_console_url: str,
) -> StationAuthResponse:
    """Verify local first-admin credentials and return a fresh console token."""

    raw = _load_raw_state()
    profile = _profile_from_state(raw)
    if profile is None or not _verify_admin_password(
        raw,
        username=request.admin_username,
        password=request.admin_password,
    ):
        raise StationAuthError("Invalid admin username or password.")
    # APPEND, never replace (OWNER DECISION 2026-08-30, see
    # _MAX_OPERATOR_SESSIONS above): a routine password sign-in on one
    # browser must not silently sign out every other browser or device
    # already signed in -- which is exactly what the sign-in card promises.
    operator_token, _entry = _issue_operator_token(raw, source="login")
    _save_raw_state(raw)
    return StationAuthResponse(
        status="authenticated",
        profile=profile,
        operator_console_url=operator_console_url,
        operator_console_token=operator_token,
        next_step="Open System Health and confirm the station is ready to broadcast.",
    )


@_serialize_state_mutation
def recover_station_admin(
    request: StationRecoveryRequest,
    *,
    operator_console_url: str,
) -> StationAuthResponse:
    """Consume one recovery code, reset the first-admin password, and return a token."""

    raw = _load_raw_state()
    profile = _profile_from_state(raw)
    if profile is None or request.admin_username.strip() != profile.admin_username:
        raise StationAuthError("Invalid recovery code or admin username.")
    if not _consume_recovery_code(raw, request.recovery_code):
        raise StationAuthError("Invalid recovery code or admin username.")
    _replace_admin_password(raw, request.new_admin_password)
    # APPEND rather than replace: a forgotten password is not evidence that
    # any other already-open session is compromised, so recovering does not
    # get to silently sign out the admin's own other tabs/devices (see the
    # OWNER DECISION comment on _MAX_OPERATOR_SESSIONS above).
    operator_token, _entry = _issue_operator_token(raw, source="recovery")
    _save_raw_state(raw)
    return StationAuthResponse(
        status="recovered",
        profile=profile,
        operator_console_url=operator_console_url,
        operator_console_token=operator_token,
        next_step="Sign in with the new password and print a fresh recovery kit when rotation is available.",
    )


def login_credentials_correct(request: StationLoginRequest) -> bool:
    """Non-mutating peek: would this password currently be accepted?

    OWNER DECISION 2026-08-30 (audit finding #4, day-one-lockout fix): lets
    ``civiccast.installer.router._enforce_setup_rate_limit`` give the setup
    login path the same correct-credential bypass the staff-auth pattern
    already has via ``civiccast.auth.tokens.token_matches_exactly`` -- a
    caller who actually knows the password must never be turned away by a
    budget someone else's (or their own earlier) wrong guesses saturated.
    Reads state fresh and performs no mutation and issues no token, so
    calling it does not consume or interfere with the real, token-issuing
    :func:`login_station_admin` call the handler makes afterward.
    """

    raw = _load_raw_state()
    profile = _profile_from_state(raw)
    if profile is None:
        return False
    return _verify_admin_password(
        raw,
        username=request.admin_username,
        password=request.admin_password,
    )


def recovery_code_correct(request: StationRecoveryRequest) -> bool:
    """Non-mutating peek: would this recovery code currently be accepted?

    Same OWNER DECISION as :func:`login_credentials_correct`, for the
    ``/api/setup/recover`` path. Loads its own throwaway copy of station
    state and never calls :func:`_save_raw_state`, so this peek does not
    consume the one-time recovery code -- the handler's real
    :func:`recover_station_admin` call still performs the consuming
    verification and persists that result.
    """

    raw = _load_raw_state()
    profile = _profile_from_state(raw)
    if profile is None or request.admin_username.strip() != profile.admin_username:
        return False
    return _consume_recovery_code(raw, request.recovery_code)


@_serialize_state_mutation
def revoke_other_operator_sessions(token: str) -> int:
    """Sign out every operator-console session except the caller's own.

    CRITICAL audit finding: login/recovery deliberately APPEND to
    ``operator_console.tokens`` (see the OWNER DECISION on
    ``_MAX_OPERATOR_SESSIONS`` above) so a routine sign-in never fratricides
    another already-open browser. But that fix left no way for an operator to
    deliberately end a lost/stolen laptop's session -- it would otherwise
    stay valid until 20 more logins evict it (months, at a single-admin
    station) or a destructive full station reset. This gives the operator a
    real "sign out other sessions" action: it keeps exactly the session that
    is calling it and revokes every other one, in one step.

    ``token`` is the caller's own already-verified bearer token (the plain
    value, not a hash) -- callers must resolve it from the same
    ``Authorization`` header that ``require_any_role`` already authenticated
    for this request. Raises :class:`StationAuthError` when ``token`` does
    not match any current operator-console session, so a caller
    authenticated some other way (an env-configured or DB-issued staff
    token) can never blindly wipe every local admin browser session by
    calling this.

    Returns the number of sessions revoked (0 when the caller's session was
    already the only one).
    """

    raw = _load_raw_state()
    console = raw.get("operator_console")
    if not isinstance(console, dict):
        raise StationAuthError("No operator-console session exists to revoke.")
    entries = _operator_token_entries(console)
    caller_entry: dict[str, Any] | None = None
    for entry in entries:
        if hmac.compare_digest(_hash_token(token, salt=entry["token_salt"]), entry["token_hash"]):
            caller_entry = entry
            break
    if caller_entry is None:
        raise StationAuthError("Invalid staff bearer token.")
    revoked_count = len(entries) - 1
    raw["operator_console"] = {
        "tokens": [caller_entry],
        "rotated_at": datetime.now(UTC).isoformat(),
    }
    _save_raw_state(raw)
    return revoked_count


@_serialize_state_mutation
def revoke_operator_session(token: str) -> None:
    """Sign out exactly the operator-console session that is calling.

    The operator console's "Sign out" control (2026-09-09 beta.5 clean-machine
    walkthrough: there was no sign-out at all -- the only way to end a
    browser's session was to wait for 20 other sign-ins to evict it or use
    "Sign out other sessions" from a *different* browser). This is the
    complement of :func:`revoke_other_operator_sessions`: it removes the
    caller's own entry from ``operator_console.tokens`` and leaves every
    other signed-in browser untouched.

    ``token`` is the caller's own already-verified bearer token (the plain
    value, not a hash). Raises :class:`StationAuthError` when it does not
    match any current operator-console session, so a caller authenticated
    some other way (an env-configured or DB-issued staff token) is reported
    back to the route, which then decides how to end *that* kind of session.
    """

    raw = _load_raw_state()
    console = raw.get("operator_console")
    if not isinstance(console, dict):
        raise StationAuthError("No operator-console session exists to revoke.")
    entries = _operator_token_entries(console)
    remaining = [
        entry
        for entry in entries
        if not hmac.compare_digest(
            _hash_token(token, salt=entry["token_salt"]), entry["token_hash"]
        )
    ]
    if len(remaining) == len(entries):
        raise StationAuthError("Invalid staff bearer token.")
    raw["operator_console"] = {
        "tokens": remaining,
        "rotated_at": datetime.now(UTC).isoformat(),
    }
    _save_raw_state(raw)


@_serialize_state_mutation
def regenerate_recovery_kit() -> RecoveryKit:
    """Mint a fresh 8-code recovery kit for an already-authenticated admin.

    CRITICAL audit finding: the first-setup recovery kit is shown exactly
    once. If the browser dies before the operator saves or prints it, the
    codes are gone forever -- and a later lost password becomes a permanent
    lockout with no escape except the destructive, undocumented
    ``CIVICCAST_ALLOW_FIRST_ADMIN_RESET`` full station wipe. This gives a
    signed-in admin (enforced by the router's auth dependency, NOT by this
    function -- it performs no credential check of its own) a real "I still
    have my password but lost/never-saved my codes" path: mint 8 new
    plaintext codes, replace the stored hashes so every old code stops
    working immediately, and return the new codes once. This is
    deliberately not reachable while signed out -- it must never become a
    second lockout-bypass alongside ``CIVICCAST_ALLOW_FIRST_ADMIN_RESET``.

    Resets the acknowledge-the-kit gate (``recovery.acknowledged``) back to
    ``False`` so the operator is walked through the same one-time save/print
    discipline the first-setup kit uses, rather than silently trusting that
    an old acknowledgment still applies to codes the operator has not seen.
    """

    raw = _load_raw_state()
    profile = _profile_from_state(raw)
    if profile is None or not raw.get("setup_complete"):
        raise StationSetupNotCompleteError(
            "First-admin setup is not complete, so there is no recovery kit to regenerate."
        )
    generated_at = datetime.now(UTC)
    kit_id = "rk_" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:18]
    recovery_codes = [_new_recovery_code() for _ in range(_RECOVERY_CODE_COUNT)]
    existing_recovery = raw.get("recovery")
    destination = (
        str(existing_recovery.get("destination") or "")
        if isinstance(existing_recovery, dict)
        else ""
    )
    raw["recovery"] = {
        "kit_id": kit_id,
        "generated_at": generated_at.isoformat(),
        "destination": destination,
        "acknowledged": False,
        "acknowledged_at": None,
        "code_hashes": [_hash_secret(code, salt=kit_id) for code in recovery_codes],
    }
    station = raw.get("station")
    if isinstance(station, dict):
        station["recovery_kit_id"] = kit_id
        station["recovery_kit_generated_at"] = generated_at.isoformat()
    _save_raw_state(raw)

    return RecoveryKit(
        kit_id=kit_id,
        generated_at=generated_at,
        station_name=profile.station_name,
        admin_username=profile.admin_username,
        recovery_codes=recovery_codes,
        instructions=[
            "This kit REPLACES every recovery code issued before it -- the old "
            "codes no longer work.",
            "Print or save this kit now. It is shown only once and CivicCast "
            "cannot show it again after this screen.",
            "Keep it somewhere separate from the CivicCast computer.",
            (
                "Recovery codes are for emergencies ONLY -- use one only if the "
                "admin password is truly lost. Each code works once, immediately "
                "sets a new admin password, and should not be spent on routine "
                "sign-in."
            ),
            "If this kit is exposed, rotate the admin password and regenerate a new kit.",
        ],
        excludes=[
            "staff bearer token values",
            "provider secret values",
            "private keys",
            "resident email addresses",
            "database passwords",
        ],
    )


def _load_raw_state() -> dict[str, Any]:
    path = station_state_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return cast(dict[str, Any], payload)


def _save_raw_state(payload: dict[str, Any]) -> None:
    path = station_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    _restrict_state_file(tmp_path)
    tmp_path.replace(path)
    _restrict_state_file(path)


def _profile_from_state(raw: dict[str, Any]) -> StationProfile | None:
    station = raw.get("station")
    if not isinstance(station, dict):
        return None
    try:
        return StationProfile(
            station_name=str(station["station_name"]),
            admin_display_name=str(station["admin_display_name"]),
            admin_username=str(station["admin_username"]),
            default_channel_id=_normalized_default_channel_id(station.get("default_channel_id")),
            public_base_url=(
                str(station["public_base_url"])
                if station.get("public_base_url") is not None
                else None
            ),
            station_timezone=str(station.get("station_timezone") or "local"),
            storage_locations=_storage_locations_from_state(station.get("storage_locations")),
            runtime_paths=_station_runtime_paths(),
            channel_count=int(station.get("channel_count") or 3),
            channel_profiles=_channel_profiles_from_state(
                station.get("channel_profiles"),
                channel_count=int(station.get("channel_count") or 3),
            ),
            sample_content_enabled=bool(station.get("sample_content_enabled", True)),
            initial_schedule_enabled=bool(station.get("initial_schedule_enabled", True)),
            # An absent key (a station commissioned before this switch
            # existed, or one that never touched it) reads as the beta.5
            # default -- OFF -- for the reason on ``LIVE_CAPTIONS_DEFAULT``;
            # an explicitly persisted value is honoured either way.
            live_captions_enabled=bool(station.get("live_captions_enabled", LIVE_CAPTIONS_DEFAULT)),
            default_roles=[
                str(role)
                for role in station.get("default_roles", _DEFAULT_ROLES)
                if str(role).strip()
            ],
            operation_mode=_operation_mode_from_state(station.get("operation_mode")),
            dashboard_ready_state=_dashboard_ready_state_from_state(
                station.get("dashboard_ready_state")
            ),
            recovery_kit_id=str(station["recovery_kit_id"]),
            recovery_kit_generated_at=datetime.fromisoformat(
                str(station["recovery_kit_generated_at"])
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _default_storage_root() -> Path:
    configured = os.getenv("CIVICCAST_STATION_STORAGE_ROOT")
    if configured:
        return Path(configured).expanduser()
    if os.getenv("CIVICCAST_NATIVE_STATION") == "1":
        from civiccast.native.supervisor.install_layout import resolve_install_layout

        return resolve_install_layout().civiccast_data_root / "data"
    if os.name == "nt":
        return Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "CivicCast"
    return Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "civiccast"


def _station_runtime_paths() -> StationRuntimePaths | None:
    """Report the same install, database and media paths the service uses."""

    if os.getenv("CIVICCAST_NATIVE_STATION") != "1":
        return None
    from civiccast.native.supervisor.children import default_upload_dir
    from civiccast.native.supervisor.install_layout import resolve_install_layout

    layout = resolve_install_layout()
    upload_dir = os.environ.get("CIVICCAST_UPLOAD_DIR") or default_upload_dir(
        program_data_root=str(layout.program_data_root)
    )
    return StationRuntimePaths(
        installation_root=str(layout.install_root),
        database_data=str(layout.postgres_data_dir),
        media_uploads=str(Path(upload_dir)),
    )


def _default_storage_locations() -> StationStorageLocations:
    root = _default_storage_root()
    return StationStorageLocations(
        media_library=str(root / "media"),
        recordings=str(root / "recordings"),
        backups=str(root / "backups"),
    )


def _build_channel_profiles(channel_count: int) -> list[StationChannelProfile]:
    profiles = list(_DEFAULT_CHANNEL_PROFILES[:channel_count])
    for index in range(len(profiles) + 1, channel_count + 1):
        profiles.append(
            StationChannelProfile(
                channel_id=f"local-ch{index:02d}",
                display_name=f"Local Channel {index}",
                purpose="Additional local playout channel reserved during first-run setup.",
            )
        )
    return profiles


def _storage_locations_from_state(value: object) -> StationStorageLocations:
    if isinstance(value, dict):
        try:
            return StationStorageLocations(
                media_library=str(value["media_library"]),
                recordings=str(value["recordings"]),
                backups=str(value["backups"]),
            )
        except (KeyError, TypeError, ValueError):
            pass
    return _default_storage_locations()


def _channel_profiles_from_state(
    value: object,
    *,
    channel_count: int,
) -> list[StationChannelProfile]:
    if isinstance(value, list):
        profiles: list[StationChannelProfile] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            try:
                profiles.append(
                    StationChannelProfile(
                        channel_id=str(item["channel_id"]),
                        display_name=str(item["display_name"]),
                        purpose=str(item["purpose"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        if profiles:
            return profiles
    return _build_channel_profiles(channel_count)


def _operation_mode_from_state(value: object) -> StationOperationMode:
    if value == "on_air":
        return "on_air"
    return "test"


def _dashboard_ready_state_from_state(value: object) -> StationDashboardReadyState:
    if value == "ready":
        return "ready"
    return "not_ready"


def _find_staff_account(raw: dict[str, Any], operator_id: str) -> dict[str, Any] | None:
    accounts = raw.get("staff_accounts")
    if not isinstance(accounts, list):
        return None
    return next(
        (
            account
            for account in accounts
            if isinstance(account, dict) and account.get("operator_id") == operator_id
        ),
        None,
    )


def _find_staff_account_by_username(raw: dict[str, Any], username: str) -> dict[str, Any] | None:
    normalized = username.strip().casefold()
    if not normalized:
        return None
    accounts = raw.get("staff_accounts")
    if not isinstance(accounts, list):
        return None
    return next(
        (
            account
            for account in accounts
            if isinstance(account, dict)
            and str(account.get("username", "")).casefold() == normalized
        ),
        None,
    )


def _account_roles(account: dict[str, Any]) -> list[OperatorRole]:
    values = account.get("roles")
    if not isinstance(values, list):
        return []
    known = set(ALL_OPERATOR_ROLES)
    return sorted({value for value in values if isinstance(value, str) and value in known})


def _staff_account_response(account: dict[str, Any]) -> StaffAccountResponse:
    return StaffAccountResponse(
        operator_id=str(account["operator_id"]),
        username=str(account["username"]),
        display_name=str(account["display_name"]),
        roles=tuple(_account_roles(account)),
        enabled=bool(account.get("enabled", True)),
        account_type="staff",
        created_at=str(account.get("created_at")) if account.get("created_at") else None,
        updated_at=str(account.get("updated_at")) if account.get("updated_at") else None,
    )


def _verify_staff_password(account: dict[str, Any], password: str) -> bool:
    salt = account.get("password_salt")
    expected = account.get("password_hash")
    if not isinstance(salt, str) or not isinstance(expected, str):
        return False
    return hmac.compare_digest(_hash_secret(password, salt=salt), expected)


def _ensure_active_administrator_remains(
    raw: dict[str, Any],
    account_being_changed: dict[str, Any],
) -> None:
    admin = raw.get("admin")
    active_admins = int(
        isinstance(admin, dict)
        and all(
            isinstance(admin.get(name), str) and admin.get(name)
            for name in ("username", "password_salt", "password_hash")
        )
    )
    accounts = raw.get("staff_accounts")
    if isinstance(accounts, list):
        active_admins += sum(
            1
            for account in accounts
            if isinstance(account, dict)
            and account is not account_being_changed
            and account.get("enabled", True)
            and "setup_admin" in _account_roles(account)
        )
    if active_admins <= 1:
        raise StationAuthError("At least one active setup administrator must remain.")


def _revoke_staff_account_sessions(raw: dict[str, Any], operator_id: str) -> None:
    console = raw.get("operator_console")
    if not isinstance(console, dict):
        return
    entries = _operator_token_entries(console)
    remaining = [entry for entry in entries if entry.get("account_id") != operator_id]
    if len(remaining) != len(entries):
        raw["operator_console"] = {
            "tokens": remaining,
            "rotated_at": datetime.now(UTC).isoformat(),
        }


def _ensure_session_metadata(
    raw: dict[str, Any],
    console: dict[str, Any],
    entries: list[dict[str, Any]],
    entry: dict[str, Any],
) -> bool:
    """Add bounded expiry and stable IDs when an old session is first used."""

    changed = False
    now = datetime.now(UTC)
    if not isinstance(entry.get("session_id"), str) or not entry["session_id"]:
        entry["session_id"] = "sess_" + secrets.token_urlsafe(12)
        changed = True
    if not isinstance(entry.get("account_id"), str):
        entry["account_id"] = "first-admin"
        changed = True
    if not isinstance(entry.get("issued_at"), str):
        entry["issued_at"] = now.isoformat()
        changed = True
    if not isinstance(entry.get("expires_at"), str):
        try:
            issued = datetime.fromisoformat(str(entry["issued_at"]).replace("Z", "+00:00"))
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            issued = now
        entry["expires_at"] = (issued + timedelta(seconds=_OPERATOR_SESSION_TTL)).isoformat()
        changed = True
    if changed:
        console["tokens"] = entries
        raw["operator_console"] = console
    return changed


def _session_is_expired(entry: dict[str, Any]) -> bool:
    value = entry.get("expires_at")
    if not isinstance(value, str):
        return True
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= datetime.now(UTC)


def _session_id_for_entry(entry: dict[str, Any]) -> str:
    session_id = entry.get("session_id")
    if isinstance(session_id, str) and session_id:
        return session_id
    return "legacy-" + str(entry.get("token_hash", ""))[:16]


def _hash_secret(secret: str, *, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        salt.encode("utf-8"),
        _PASSWORD_ITERATIONS,
    )
    return digest.hex()


def _hash_token(token: str, *, salt: str) -> str:
    """Hash high-entropy setup tokens without adding per-request PBKDF2 cost."""

    digest = hmac.new(salt.encode("utf-8"), token.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


def _verify_admin_password(raw: dict[str, Any], *, username: str, password: str) -> bool:
    admin = raw.get("admin")
    if not isinstance(admin, dict):
        return False
    stored_username = admin.get("username")
    salt = admin.get("password_salt")
    expected = admin.get("password_hash")
    if not all(isinstance(value, str) for value in [stored_username, salt, expected]):
        return False
    if str(stored_username) != username.strip():
        return False
    observed = _hash_secret(password, salt=str(salt))
    return hmac.compare_digest(observed, str(expected))


def _consume_recovery_code(raw: dict[str, Any], recovery_code: str) -> bool:
    recovery = raw.get("recovery")
    if not isinstance(recovery, dict):
        return False
    kit_id = recovery.get("kit_id")
    code_hashes = recovery.get("code_hashes")
    if not isinstance(kit_id, str) or not isinstance(code_hashes, list):
        return False
    observed = _hash_secret(recovery_code.strip(), salt=kit_id)
    remaining: list[str] = []
    consumed = False
    for stored in code_hashes:
        if not isinstance(stored, str):
            continue
        if not consumed and hmac.compare_digest(observed, stored):
            consumed = True
            continue
        remaining.append(stored)
    if consumed:
        recovery["code_hashes"] = remaining
        recovery["last_recovered_at"] = datetime.now(UTC).isoformat()
    return consumed


def _replace_admin_password(raw: dict[str, Any], new_password: str) -> None:
    admin = raw.get("admin")
    if not isinstance(admin, dict):
        raise StationAuthError("Station admin state is missing.")
    password_salt = secrets.token_hex(16)
    admin["password_salt"] = password_salt
    admin["password_hash"] = _hash_secret(new_password, salt=password_salt)
    admin["password_iterations"] = _PASSWORD_ITERATIONS
    admin["password_rotated_at"] = datetime.now(UTC).isoformat()


def _new_operator_token_entry(
    *,
    source: str,
    account_id: str = "first-admin",
    operator_id: str | None = None,
    scopes: tuple[str, ...] = ("admin",),
) -> tuple[str, dict[str, Any]]:
    """Mint one fresh operator token and its storable (salted-hash) entry."""

    operator_token = "ccst_" + secrets.token_urlsafe(32)
    token_salt = secrets.token_hex(16)
    issued_at = datetime.now(UTC)
    entry = {
        "token_salt": token_salt,
        "token_hash": _hash_token(operator_token, salt=token_salt),
        "session_id": "sess_" + secrets.token_urlsafe(12),
        "account_id": account_id,
        "operator_id": operator_id,
        "scopes": list(scopes),
        "issued_at": issued_at.isoformat(),
        "expires_at": (issued_at + timedelta(seconds=_OPERATOR_SESSION_TTL)).isoformat(),
        "source": source,
    }
    return operator_token, entry


def _operator_token_entries(console: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the console's valid-token entries, healing the pre-multi-session shape.

    A station commissioned before this fix persisted a single
    ``token_salt``/``token_hash`` pair directly on ``operator_console``
    instead of a ``tokens`` list. Read that legacy shape as a one-entry list
    so an already-issued token from an older build keeps working across the
    upgrade, until the next login/recovery migrates it to the list shape.
    """

    tokens = console.get("tokens")
    if isinstance(tokens, list):
        return [
            entry
            for entry in tokens
            if isinstance(entry, dict)
            and isinstance(entry.get("token_salt"), str)
            and isinstance(entry.get("token_hash"), str)
        ]
    legacy_salt = console.get("token_salt")
    legacy_hash = console.get("token_hash")
    if isinstance(legacy_salt, str) and isinstance(legacy_hash, str):
        return [{"token_salt": legacy_salt, "token_hash": legacy_hash}]
    return []


def _issue_operator_token(
    raw: dict[str, Any],
    *,
    source: str,
    account_id: str = "first-admin",
    operator_id: str | None = None,
    scopes: tuple[str, ...] = ("admin",),
) -> tuple[str, dict[str, Any]]:
    """Mint a fresh operator token and append it to the session list.

    Every issuance path (setup, ordinary login, recovery) keeps all other
    currently-valid sessions alive and just adds this one, bounded to
    ``_MAX_OPERATOR_SESSIONS`` by dropping the oldest-issued entries first so
    the state file cannot grow without limit (OWNER DECISION 2026-08-30 on
    ``_MAX_OPERATOR_SESSIONS``: no sign-in path gets to silently revoke
    another browser's session).
    """

    console = raw.get("operator_console")
    existing = _operator_token_entries(console) if isinstance(console, dict) else []
    if operator_id is None and account_id == "first-admin":
        admin = raw.get("admin")
        if isinstance(admin, dict):
            operator_id = str(admin.get("username") or "first-admin")
    operator_token, entry = _new_operator_token_entry(
        source=source,
        account_id=account_id,
        operator_id=operator_id,
        scopes=scopes,
    )
    tokens = [*existing, entry][-_MAX_OPERATOR_SESSIONS:]
    raw["operator_console"] = {
        "tokens": tokens,
        "rotated_at": datetime.now(UTC).isoformat(),
    }
    return operator_token, entry


def _restrict_state_file(path: Path, *, service_can_replace: bool = True) -> None:
    """Restrict local station bootstrap state after writing secret hashes."""

    if os.name == "nt":
        from civiccast.certs.authority import _restrict_windows_private_key_acl

        # Login saves sessions by atomically replacing this file. Recovery runs
        # as an administrator, so the service needs DELETE in addition to R/W
        # on the replacement state; private keys and recovery backups do not.
        _restrict_windows_private_key_acl(path, service_can_replace=service_can_replace)
        return
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _new_recovery_code() -> str:
    return "CC-" + secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12].upper()
