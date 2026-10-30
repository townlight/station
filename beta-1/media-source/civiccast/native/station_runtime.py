# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Resolve one activated native station into its control-plane environment.

The native bootstrap promotes a complete, signed five-pack station into one
version root.  This module is the runtime half of that contract: the Windows
service derives the active root from its embedded Python executable, validates
the persisted station-set and pre-activation receipt, then launches the control
plane with captions and GStreamer enabled against the exact installed pack.

WP1 adaptive-tier note (owner architecture, settled): captions ship with a
MEDIUM floor tier; large-v3 is OPTIONAL. Startup therefore resolves whichever
verified caption tier is actually staged -- the floor tier at the acquisition
flow's staging location (``packs/captions-floor/models/faster-whisper-medium``)
and/or large-v3 inside its signed component -- routing the choice through
:func:`civiccast.native.caption_tier_selection.select_caption_tier` so the
selection is explicit and provable, never a silent substitution. A staged
tier that fails verification is a hard, fail-closed error exactly as the
original mandatory large-v3 gate was; only a tier that is genuinely absent
is skipped.

Two installed states are legitimately not startable yet and are typed so the
Windows service supervisor can degrade gracefully instead of crashing:

* :class:`NativeStationNotActivatedError` -- the installer's payload
  extraction lays only ``runtime/`` and ``packs/``; ``station-set.json`` and
  ``activation-self-test.json`` are written ONLY by the later
  acquisition/activation flow. An ABSENT ``station-set.json`` is therefore
  installed-but-not-yet-activated, not corruption.
* :class:`NativeStationCaptionsUnavailableError` -- an activated station
  with NO caption tier staged at all (captions unavailable/degraded).

A PRESENT-but-corrupt artifact of any kind stays the loud parent
:class:`NativeStationConfigurationError`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Final

from civiccast import _native_version
from civiccast.native.app_payload import WHISPER_MODEL_FILES
from civiccast.native.caption_tier_selection import (
    TIER_SELECTED_EVENT,
    TierSelectionDecision,
    TierSelectionError,
    TierSelectionResult,
    select_caption_tier,
)
from civiccast.native.caption_tiers import (
    CAPTION_TIER_REGISTRY,
    FLOOR_TIER_ID,
    LARGE_V3_TIER_ID,
    CaptionTierBindingError,
)
from civiccast.native.gstreamer_runtime import (
    GstreamerRuntimeError,
    installed_gstreamer_environment,
)
from civiccast.native.supervisor.install_layout import packaged_portal_dist_dirs

_LOG = logging.getLogger(__name__)

#: The FFmpeg concat egress engine value (must match
#: :data:`civiccast.egress.engine_select._DEFAULT` /
#: ``_FFMPEG_ALIASES``). Written into the returned child environment as
#: ``CIVICCAST_EGRESS_ENGINE`` when a corrupt GStreamer closure cannot be
#: repaired in place, so ``engine_select.build_encoder_strategy`` selects
#: ``ConcatEncoderStrategy`` and the channel keeps airing on FFmpeg instead of
#: going dark (the Codex P1 on PR #406). ``GstPlayoutStrategy`` is only ever
#: selected when the closure verified clean and the GStreamer keys were
#: injected.
_FFMPEG_EGRESS_ENGINE: Final[str] = "ffmpeg-concat"

#: The child-environment key that selects the egress encoder engine, read by
#: ``civiccast.egress.engine_select.selected_engine_name``. Named here so the
#: GStreamer default and the degraded FFmpeg switch write the SAME key.
EGRESS_ENGINE_ENV: Final[str] = "CIVICCAST_EGRESS_ENGINE"

#: Set in the returned child environment (alongside the FFmpeg engine switch)
#: to record WHY egress was degraded away from GStreamer. This module runs
#: pre-DB under LocalSystem and holds no SQLAlchemy ``Session``, so it cannot
#: raise the operator alert or drive channel health itself; it leaves this
#: breadcrumb for the control-plane seam that DOES hold a session
#: (``civiccast.egress.automation.build_channel_automation``), which reads it
#: to raise the loud operator alert and mark the channel DEGRADED. Absent means
#: "egress is not degraded" -- never an empty string.
EGRESS_DEGRADED_REASON_ENV: Final[str] = "CIVICCAST_EGRESS_DEGRADED_REASON"

#: ``(gstreamer_runtime_root) -> bool``. The degraded-mode tier-2 self-repair
#: seam: return ``True`` iff the installed GStreamer closure at
#: ``<gstreamer_runtime_root>/dependencies/gstreamer`` is (now) healthy and
#: GStreamer egress may run. Injectable so each tier is unit-testable; the
#: production default is :func:`reverify_gstreamer_closure`.
GstreamerRepairHook = Callable[[Path], bool]


def reverify_gstreamer_closure(gstreamer_runtime_root: Path) -> bool:
    """Default degraded-mode tier-2 self-repair: re-verify the installed
    GStreamer closure ONCE, in place, and report whether it is now healthy.

    This is the SAFE automatic repair that runs at station-environment build
    time. It recovers the genuinely transient corrupt-closure states -- an
    on-access AV scanner that quarantined a plugin DLL during boot and released
    it a moment later, a reparse point that resolved late -- by simply
    re-running the same validation (:func:`installed_gstreamer_environment`)
    that first raised. If the closure is intact on the second look, GStreamer
    egress runs normally.

    It deliberately does NOT invoke the installer's signed re-stage
    (``native_repair.rs`` / ``CivicCast Native.exe --civiccast-repair``). That
    machinery is the only thing that can rebuild genuinely missing bytes, but
    it stops the ``CivicCastSupervisor`` service and ``remove_dir_all``s
    ``<install_root>/runtime`` -- the exact tree whose ``python.exe`` is running
    this supervisor (its ``ServiceQuiescenceAuthority`` + the "service stopped,
    not restarted" note make this explicit). Running it in-process at boot
    would therefore take the whole station off air -- the dead-air outage this
    state machine exists to prevent. The destructive signed re-stage is exposed
    instead as the operator RECOVERY action
    (:mod:`civiccast.native.gstreamer_repair`), launched detached at an
    operator-chosen maintenance moment; once it has rebuilt the closure the
    NEXT supervisor start re-verifies it here and GStreamer egress
    auto-restores. Returns ``True`` iff the closure verifies clean now.
    """

    try:
        installed_gstreamer_environment(gstreamer_runtime_root)
    except GstreamerRuntimeError:
        return False
    return True


#: Owner decision (Scott Converse, 2026-08-07, ratified): the caption FLOOR
#: tier (``captions-floor``, ``medium`` / ``faster-whisper-medium``) is the
#: mandatory baseline for native station activation; ``captions-large-v3`` is
#: an optional quality add-on, verified when present and simply absent when
#: not. The Rust-side activation gate made this swap in
#: ``native_activation.rs``/``native_distribution.rs``'s own
#: ``REQUIRED_COMPONENTS``/``OPTIONAL_COMPONENTS`` (kept in lockstep with
#: this dict, including the exact staged root ``packs/captions-floor`` --
#: see ``_TIER_MODEL_ROOT_PREFIX[FLOOR_TIER_ID]`` above, whose ``.../models``
#: suffix that root is the parent of); this dict is the runtime-side half of
#: the same contract, so a floor-only station's ``station-set.json`` (which
#: never carries a ``captions-large-v3`` pack entry at all) validates instead
#: of being rejected as an incomplete "five-pack" set.
REQUIRED_COMPONENT_ROOTS: Final[dict[str, str]] = {
    "core": ".",
    "captions-floor": "packs/captions-floor",
    "captions-whistle": "packs/captions-whistle",
    "summary-gemma4-12b": "components/summary-gemma4-12b",
    "summary-gemma4-e4b": "components/summary-gemma4-e4b",
    "translation-translategemma-4b": "components/translation-translategemma-4b",
}
#: Verified against its own pinned root when a station's pack inventory
#: carries it, and simply absent when it does not -- never required, and
#: never a second, silently-accepted convention for where it lands.
OPTIONAL_COMPONENT_ROOTS: Final[dict[str, str]] = {
    "captions-large-v3": "components/captions-large-v3",
}

#: Where each caption tier's model directory physically lands, relative to
#: the station version root -- large-v3 inside the signed five-pack
#: station's own `captions-large-v3` component (unchanged, the original
#: convention this module has always used); the floor tier at the EXACT
#: location the installer's acquisition download experience stages it
#: (task #57's sibling item (a)/(b): `component_acquisition.rs`'s
#: `caption_floor_tier_destination`, `packs\captions-floor\models\
#: faster-whisper-medium`) -- never a second, invented convention for where
#: that tier lives on disk.
_TIER_MODEL_ROOT_PREFIX: Final[dict[str, str]] = {
    LARGE_V3_TIER_ID: "components/captions-large-v3/models",
    FLOOR_TIER_ID: "packs/captions-floor/models",
}


def caption_tier_model_relative_root(tier_id: str) -> str:
    """Where ``tier_id``'s model directory lives, relative to the station
    version root -- the SAME resolution large-v3's mandatory gate has always
    used (``<prefix>/<model_directory>``), generalized via
    :data:`civiccast.native.caption_tiers.CAPTION_TIER_REGISTRY` (the single
    pinned source of truth for ``model_directory``) instead of a single
    hard-coded literal.

    Raises :class:`NativeStationConfigurationError` for a tier this module
    has no known on-disk location for, or one that is not yet owner-bound
    (:meth:`~civiccast.native.caption_tiers.CaptionTierSpec.require_bound`)
    -- fail closed, never silently guess a path.
    """

    try:
        prefix = _TIER_MODEL_ROOT_PREFIX[tier_id]
    except KeyError as exc:
        raise NativeStationConfigurationError(
            f"No staged-model location is known for caption tier {tier_id!r}"
        ) from exc
    try:
        spec = CAPTION_TIER_REGISTRY[tier_id].require_bound()
    except KeyError as exc:
        raise NativeStationConfigurationError(f"Unknown caption tier: {tier_id!r}") from exc
    return f"{prefix}/{spec.model_directory}"


def caption_tier_search_roots(
    version_root: str | Path, *, acquisition_root: str | Path | None = None
) -> tuple[Path, ...]:
    """The BASE roots a caption tier's model directory may live under, in
    PREFERENCE order (chain H1).

    Both trees carry the identical relative layout
    (``packs\\captions-floor\\models\\...``), so a tier delivered by the
    elevated installer and one downloaded by the non-elevated first-run GUI
    are interchangeable:

    1. ``version_root`` -- what the ELEVATED installer staged. Always first,
       so nothing in a user-writable directory can shadow it.
    2. ``acquisition_root`` -- ``<PROGRAMDATA>\\CivicCast``, where the
       first-run acquisition flow downloads to
       (``main.rs::acquisition_download_root``). The installed GUI runs
       non-elevated from Program Files and cannot write under
       ``version_root`` at all; before chain H1 it tried, got PermissionDenied
       at 0 bytes, and the operator was told the drive was full.

    ``acquisition_root=None`` searches ONLY ``version_root``: this function
    never probes this machine's real ProgramData tree on its own initiative,
    so a caller that names one root gets exactly one root.
    """

    roots = [Path(version_root)]
    if acquisition_root is not None:
        roots.append(Path(acquisition_root))
    return tuple(roots)


def _tier_base_root(roots: tuple[Path, ...], relative_root: str) -> Path | None:
    """The first root in ``roots`` whose ``relative_root`` directory ENTRY
    exists (lexists semantics, matching :func:`_staged_caption_tier_ids`: a
    mis-pointed junction is "present here" and then fails loudly inside the
    verification walk, never quietly reclassified as absent)."""

    for base in roots:
        if os.path.lexists(base / Path(relative_root)):
            return base
    return None


CAPTION_MODEL_RELATIVE_ROOT: Final[str] = caption_tier_model_relative_root(LARGE_V3_TIER_ID)
#: The mandatory CPU-only floor tier's model root (owner-bound 2026-07-30,
#: `Systran/faster-whisper-medium`) -- see
#: :func:`validate_floor_caption_model_root`.
FLOOR_CAPTION_MODEL_RELATIVE_ROOT: Final[str] = caption_tier_model_relative_root(FLOOR_TIER_ID)
EXPECTED_RUNTIME_CONTRACT: Final[dict[str, object]] = {
    "caption_tap": "inline",
    "caption_tap_atomic": True,
    "caption_model_root": CAPTION_MODEL_RELATIVE_ROOT,
    "caption_runtime": "faster-whisper",
    "caption_device": "cpu",
    "caption_compute_type": "int8",
    "egress_engine": "gstreamer",
    "egress_embed_captions": True,
    "offline_only": True,
    "whistle_assets_root": "packs/captions-whistle",
}
#: Fields of a caption self-test receipt that do NOT vary by tier -- the
#: runtime/library identity and execution posture are identical regardless
#: of which model actually ran. The per-tier identity fields (``model``,
#: ``model_path``, ``model_bin_bytes``, ``model_bin_sha256``) are derived at
#: validation time from :data:`CAPTION_TIER_REGISTRY` by
#: :func:`_expected_caption_receipt` -- never hand-pinned as a second literal
#: dict here, and never one tier's identity imposed on another receipt (the
#: exact defect ``caption_tiers.py``'s own module docstring documents this
#: registry was built to fix; a receipt-validation gate that still hard-coded
#: large-v3's identity would silently reintroduce it for every floor-only
#: station).
_CAPTION_RECEIPT_TIER_INDEPENDENT_FIELDS: Final[dict[str, object]] = {
    "runtime": "faster-whisper 1.2.1",
    "ctranslate2": "4.8.1",
    "device": "cpu",
    "compute_type": "int8",
    "local_files_only": True,
    "result": "passed",
}


def _expected_caption_receipt(tier_id: str) -> dict[str, object]:
    """The complete pinned caption-inference identity a self-test receipt
    must carry to be accepted as proof that ``tier_id`` -- the tier this
    station actually resolved and verified on disk -- is the one that ran.

    Derives the per-tier fields from :data:`CAPTION_TIER_REGISTRY` (read at
    call time, not captured at import, exactly like
    :func:`caption_tier_model_relative_root` and
    :func:`validate_floor_caption_model_root` already do) so a receipt
    naming a tier that disagrees with the registry's pinned identity for
    ``tier_id`` -- wrong model, wrong revision, or a tampered
    ``model_bin_sha256`` -- still fails closed. Raises
    :class:`NativeStationConfigurationError` for a tier this module has no
    known identity for, or one that is not yet owner-bound -- fail closed,
    never silently validate against an empty inventory.
    """

    try:
        spec = CAPTION_TIER_REGISTRY[tier_id].require_bound()
    except (KeyError, CaptionTierBindingError) as exc:
        raise NativeStationConfigurationError(
            "Native station activation self-test receipt names an unrecognized "
            f"or unbound caption tier: {tier_id!r}"
        ) from exc
    try:
        model_bin_bytes, model_bin_sha256 = spec.files["model.bin"]
    except KeyError as exc:
        raise NativeStationConfigurationError(
            f"caption tier {tier_id!r} has no pinned model.bin identity to "
            "validate an activation self-test receipt against"
        ) from exc
    return {
        **_CAPTION_RECEIPT_TIER_INDEPENDENT_FIELDS,
        "model": f"{spec.model_repository}@{spec.model_revision}",
        "model_path": caption_tier_model_relative_root(tier_id),
        "model_bin_bytes": model_bin_bytes,
        "model_bin_sha256": model_bin_sha256,
    }


class NativeStationConfigurationError(RuntimeError):
    """The activated native station cannot safely start its control plane."""


class NativeStationNotActivatedError(NativeStationConfigurationError):
    """The native station is installed but not yet activated.

    Raised ONLY when ``station-set.json`` is ABSENT at the resolved station
    root -- the exact state a fresh install is in, because the installer's
    payload extraction lays only ``runtime/`` and ``packs/`` while
    ``station-set.json`` is written solely by the acquisition/activation
    flow (``native_activation.rs``'s staging promotion). The Windows service
    supervisor catches this name specifically and degrades gracefully
    instead of crashing. A PRESENT-but-corrupt/unreadable ``station-set.json``
    stays the loud parent :class:`NativeStationConfigurationError`.
    """


class NativeStationCaptionsUnavailableError(NativeStationNotActivatedError):
    """An activated station has NO caption tier staged at all.

    ``station-set.json`` validated, but neither the floor caption tier
    (``packs/captions-floor/...``) nor large-v3
    (``components/captions-large-v3/...``) is present on disk: captions are
    unavailable and the control plane must not start captionless (captions
    are mandatory product scope). Subclass of
    :class:`NativeStationNotActivatedError` so the supervisor's existing
    graceful-degrade handler covers it (acquisition has simply not finished
    or must be repaired), while remaining separately typed for status
    reporting. A caption tier that IS staged but fails verification never
    raises this -- that stays the loud parent
    :class:`NativeStationConfigurationError`.
    """


#: Sandbox run 16 layer-4 crash: the Windows service host's own
#: ``sys.executable`` is ``pythonservice.exe``, never ``python.exe`` --
#: accepted alongside the two names an interactive/CLI invocation uses.
_ACCEPTED_STATION_PYTHON_NAMES: Final[frozenset[str]] = frozenset(
    {"python.exe", "pythonw.exe", "pythonservice.exe"}
)

#: The post-tag upgrade engine's junction-flip convention
#: (:mod:`civiccast.native.upgrade.junction`) lays each version at
#: ``<install_root>/app/<version>/`` and re-points ``<install_root>/current``
#: at it -- a resolved station root reached that way has
#: ``root.parent.name == "app"``, and ONLY THEN must the version-root's own
#: directory name match the station-set's ``product_version`` (an extra
#: sanity check that the junction was not left pointed at the wrong tree). A
#: fresh install has no such wrapping at all: the installer's payload
#: extraction writes straight to ``<install_root>/runtime/`` -- the junction
#: layer is laid only by the FIRST post-tag upgrade -- so that direct shape's
#: install-root directory name is unconstrained and this check does not
#: apply to it.
_JUNCTION_VERSION_PARENT_NAME: Final[str] = "app"


def _read_json_object(path: Path, *, label: str) -> dict[str, object]:
    try:
        details = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise NativeStationConfigurationError(f"{label} is missing or unreadable: {path}") from exc
    attributes = int(getattr(details, "st_file_attributes", 0))
    if (
        not stat.S_ISREG(details.st_mode)
        or path.is_symlink()
        or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise NativeStationConfigurationError(f"{label} must be a regular non-reparse file")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NativeStationConfigurationError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise NativeStationConfigurationError(f"{label} must contain a JSON object")
    return value


def _lower_sha256(value: object) -> str | None:
    if (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    ):
        return value
    return None


def _validate_station_set(station: dict[str, object]) -> tuple[str, str]:
    version = station.get("product_version")
    index_sha256 = _lower_sha256(station.get("distribution_index_sha256"))
    if (
        station.get("schema_version") != 2
        or station.get("product") != "civiccast-native"
        or not isinstance(version, str)
        or not version
        or station.get("compatible_core") != version
        or index_sha256 is None
        or not isinstance(station.get("signing_key_id"), str)
        or not station["signing_key_id"]
    ):
        raise NativeStationConfigurationError("Native station-set identity is invalid")
    if station.get("runtime") != EXPECTED_RUNTIME_CONTRACT:
        raise NativeStationConfigurationError(
            "Native station runtime contract is not the accepted offline large-v3 contract"
        )
    packs = station.get("packs")
    if not isinstance(packs, list):
        raise NativeStationConfigurationError("Native station pack inventory is missing")
    observed: dict[str, str] = {}
    for item in packs:
        if not isinstance(item, dict):
            raise NativeStationConfigurationError("Native station pack inventory is malformed")
        component = item.get("component")
        root = item.get("root")
        if (
            not isinstance(component, str)
            or not isinstance(root, str)
            or component in observed
            or _lower_sha256(item.get("outer_sha256")) is None
        ):
            raise NativeStationConfigurationError("Native station pack identity is malformed")
        observed[component] = root
    required = {name: observed.pop(name, None) for name in REQUIRED_COMPONENT_ROOTS}
    optional_valid = all(
        OPTIONAL_COMPONENT_ROOTS.get(name) == component_root
        for name, component_root in observed.items()
    )
    if required != REQUIRED_COMPONENT_ROOTS or not optional_valid:
        # Name what is ACTUALLY wrong rather than a stale "five-pack" literal
        # (the two-pack amendment replaced the five-pack model; only the
        # caption tier requirement changed -- Summary and Translation stay
        # mandatory). A missing/mismatched required component and an
        # unexpected/invalid optional one are reported separately so the
        # operator/log reader can tell "not activated for this tier" apart
        # from "a pack was tampered with."
        missing_or_mismatched = sorted(
            name for name, root in REQUIRED_COMPONENT_ROOTS.items() if required.get(name) != root
        )
        invalid_optional = sorted(
            name for name, root in observed.items() if OPTIONAL_COMPONENT_ROOTS.get(name) != root
        )
        detail = "; ".join(
            part
            for part in (
                f"missing or mismatched required components: {', '.join(missing_or_mismatched)}"
                if missing_or_mismatched
                else "",
                f"unexpected or invalid optional components: {', '.join(invalid_optional)}"
                if invalid_optional
                else "",
            )
            if part
        )
        raise NativeStationConfigurationError(
            f"Native station pack inventory does not match the required component set ({detail})"
        )
    return version, index_sha256


def _validate_activation_receipt(
    receipt: dict[str, object],
    *,
    version: str,
    index_sha256: str,
    tier_id: str,
) -> None:
    """Fail-closed, tier-aware receipt check.

    ``tier_id`` is the caption tier THIS STATION actually resolved and
    verified on disk (:func:`_resolve_caption_tier`'s return value) -- never
    parsed out of the receipt's own self-report first. The receipt is then
    checked against exactly that tier's pinned identity
    (:func:`_expected_caption_receipt`), so a receipt naming a *different*
    tier (e.g. claiming large-v3 on a station where large-v3 is not staged)
    fails here even though it is internally well-formed, and a receipt whose
    hash was tampered with fails the same way large-v3's mandatory gate
    always did -- this generalizes that gate, it does not loosen it.
    """

    caption = receipt.get("caption_inference")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("product") != "civiccast-native"
        or receipt.get("product_version") != version
        or receipt.get("distribution_index_sha256") != index_sha256
        or not isinstance(caption, dict)
        or any(
            caption.get(key) != value for key, value in _expected_caption_receipt(tier_id).items()
        )
    ):
        raise NativeStationConfigurationError(
            "Native station activation self-test receipt does not match this distribution"
        )


def _validate_tier_model_root(
    version_root: Path,
    relative_root: str,
    files: dict[str, tuple[int, str]],
    *,
    label: str = "caption",
) -> tuple[Path, dict[str, dict[str, int | str]]]:
    """Locate and verify a caption tier's model directory under
    ``version_root``, fail-closed exactly like the original large-v3-only
    gate this generalizes: missing, escaped-root, missing-file, or
    tampered-bytes all raise :class:`NativeStationConfigurationError`.

    ``relative_root``/``files`` let the SAME verification walk serve both
    large-v3 (:func:`_validate_model_root`, ``label="caption"`` for
    byte-identical messages to before this was generalized) and the floor
    tier (:func:`validate_floor_caption_model_root`) -- never a second,
    parallel implementation of this check.
    """

    model_root = version_root / Path(relative_root)
    try:
        resolved_root = model_root.resolve(strict=True)
    except OSError as exc:
        raise NativeStationConfigurationError(
            f"Mandatory packaged {label} model is missing: {model_root}"
        ) from exc
    expected_root = version_root / Path(relative_root).parent
    try:
        resolved_root.relative_to(expected_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise NativeStationConfigurationError(
            f"Mandatory packaged {label} model escaped its signed component root"
        ) from exc
    hash_receipt: dict[str, dict[str, int | str]] = {}
    for name, (expected_bytes, expected_sha256) in files.items():
        path = resolved_root / name
        digest = hashlib.sha256()
        try:
            path_details = path.lstat()
        except OSError as exc:
            raise NativeStationConfigurationError(
                f"Mandatory packaged {label} model file is missing or unreadable: {name}"
            ) from exc
        path_attributes = int(getattr(path_details, "st_file_attributes", 0))
        if (
            not stat.S_ISREG(path_details.st_mode)
            or path.is_symlink()
            or path_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or path_details.st_size != expected_bytes
        ):
            raise NativeStationConfigurationError(
                f"Mandatory packaged {label} model file identity is invalid: {name}"
            )
        try:
            with path.open("rb") as model_file:
                details = os.fstat(model_file.fileno())
                attributes = int(getattr(details, "st_file_attributes", 0))
                if (
                    not stat.S_ISREG(details.st_mode)
                    or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or details.st_size != expected_bytes
                ):
                    raise NativeStationConfigurationError(
                        f"Mandatory packaged {label} model file identity is invalid: {name}"
                    )
                for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise NativeStationConfigurationError(
                f"Mandatory packaged {label} model file is missing or unreadable: {name}"
            ) from exc
        observed_sha256 = digest.hexdigest()
        if observed_sha256 != expected_sha256:
            raise NativeStationConfigurationError(
                f"Mandatory packaged {label} model file {name} SHA-256 mismatch: "
                f"expected {expected_sha256}, observed {observed_sha256}"
            )
        hash_receipt[name] = {"bytes": details.st_size, "sha256": observed_sha256}
    return resolved_root, hash_receipt


def _validate_model_root(
    version_root: Path, *, acquisition_root: str | Path | None = None
) -> tuple[Path, dict[str, dict[str, int | str]]]:
    """The CURRENT mandatory large-v3 gate -- unchanged behavior (byte-
    identical error text and path resolution to before this was
    generalized), now implemented via :func:`_validate_tier_model_root` so
    the floor tier can reuse the SAME verification walk rather than a
    parallel one. Still reads the module-level ``WHISPER_MODEL_FILES`` name
    (not a value captured at import time) so existing tests that monkeypatch
    ``station_runtime.WHISPER_MODEL_FILES`` keep working unchanged.
    """

    roots = caption_tier_search_roots(version_root, acquisition_root=acquisition_root)
    base = _tier_base_root(roots, CAPTION_MODEL_RELATIVE_ROOT) or roots[0]
    return _validate_tier_model_root(
        base, CAPTION_MODEL_RELATIVE_ROOT, WHISPER_MODEL_FILES, label="caption"
    )


def validate_floor_caption_model_root(
    version_root: Path, *, acquisition_root: str | Path | None = None
) -> tuple[Path, dict[str, dict[str, int | str]]]:
    """Locate and verify the mandatory CPU-only floor-tier caption model
    (``Systran/faster-whisper-medium``, owner-bound 2026-07-30 per
    ``OWNER-DECISION-caption-adaptive-tier.md``) at the EXACT on-disk
    location the installer's acquisition download experience stages it --
    ``packs/captions-floor/models/faster-whisper-medium`` -- via the SAME
    per-tier resolution large-v3's existing mandatory gate uses
    (:func:`caption_tier_model_relative_root`), never a parallel convention.

    Fail-closed exactly like the large-v3 gate: missing, corrupt, or
    tampered files raise :class:`NativeStationConfigurationError`. Reads
    :data:`civiccast.native.caption_tiers.CAPTION_TIER_REGISTRY` at call time
    (not a value captured at import time), so a test can monkeypatch
    ``station_runtime.CAPTION_TIER_REGISTRY`` with a small fixture file set
    the same way existing large-v3 tests monkeypatch ``WHISPER_MODEL_FILES``.
    """

    spec = CAPTION_TIER_REGISTRY[FLOOR_TIER_ID].require_bound()
    roots = caption_tier_search_roots(version_root, acquisition_root=acquisition_root)
    base = _tier_base_root(roots, FLOOR_CAPTION_MODEL_RELATIVE_ROOT) or roots[0]
    return _validate_tier_model_root(
        base, FLOOR_CAPTION_MODEL_RELATIVE_ROOT, spec.files, label="floor-tier caption"
    )


def _staged_caption_tier_ids(roots: tuple[Path, ...]) -> set[str]:
    """Which caption tiers are PRESENT (staged) under ``version_root``.

    Presence is judged on the model root's directory ENTRY (lstat
    semantics, :func:`os.path.lexists`), never on what a link resolves to:
    a mis-pointed or dangling junction/symlink at a tier's model root is
    therefore "present" here, and then fails loudly inside
    :func:`_validate_tier_model_root`'s existing fail-closed walk -- it is
    never quietly reclassified as an absent tier.
    """

    staged: set[str] = set()
    for tier_id in (FLOOR_TIER_ID, LARGE_V3_TIER_ID):
        if _tier_base_root(roots, caption_tier_model_relative_root(tier_id)) is not None:
            staged.add(tier_id)
    return staged


def _resolve_caption_tier(
    root: Path,
    *,
    acquisition_root: str | Path | None = None,
) -> tuple[str, dict[str, object], Path, dict[str, dict[str, int | str]]]:
    """Resolve, verify, and select the caption tier an activated station
    starts with -- the settled owner architecture: the MEDIUM floor tier is
    the shipping baseline, large-v3 is optional quality.

    * No tier staged at all -> :class:`NativeStationCaptionsUnavailableError`
      (typed, supervisor-degradable -- never a captionless start).
    * Floor staged (alone or with large-v3) -> routed through
      :func:`select_caption_tier` (the WP1 seam), requesting the highest
      staged tier; no fallback authorization is needed because the request
      is staged by construction, so a refusal can never be silent.
    * ONLY large-v3 staged -> the signed five-pack activation flow's own
      layout (that flow stages large-v3 inside the promoted version root;
      the floor tier's acquisition destination lives at the install root):
      selected directly with the same explicit selection event
      ``select_caption_tier`` would log. This is the one shape the seam's
      floor-is-mandatory precondition cannot express.
    * Whichever tier is selected is then FULLY verified by the existing
      fail-closed walk; a staged-but-invalid tier raises loudly and is
      never substituted.
    """

    roots = caption_tier_search_roots(root, acquisition_root=acquisition_root)
    staged = _staged_caption_tier_ids(roots)
    if not staged:
        searched = " or ".join(str(candidate) for candidate in roots)
        raise NativeStationCaptionsUnavailableError(
            "Native station is activated but no caption tier is staged: "
            f"expected {FLOOR_CAPTION_MODEL_RELATIVE_ROOT} (floor) and/or "
            f"{CAPTION_MODEL_RELATIVE_ROOT} (large-v3) under {searched}"
        )
    if FLOOR_TIER_ID in staged:
        requested = LARGE_V3_TIER_ID if LARGE_V3_TIER_ID in staged else FLOOR_TIER_ID
        selection = select_caption_tier(
            available_tier_ids=staged,
            floor_tier_id=FLOOR_TIER_ID,
            decision=TierSelectionDecision(
                requested_tier_id=requested,
                allow_floor_fallback=False,
                reason="capacity policy pending: highest staged tier preferred",
            ),
        )
        tier_id = selection.tier_id
        tier_event = selection.log_event
    else:
        tier_id = LARGE_V3_TIER_ID
        tier_event = {
            "event": TIER_SELECTED_EVENT,
            "tier": LARGE_V3_TIER_ID,
            "requested": LARGE_V3_TIER_ID,
            "fallback": False,
            "reason": "five-pack activation layout: only large-v3 is staged",
        }
    if tier_id == LARGE_V3_TIER_ID:
        model_root, model_hash_receipt = _validate_model_root(
            root, acquisition_root=acquisition_root
        )
    else:
        model_root, model_hash_receipt = validate_floor_caption_model_root(
            root, acquisition_root=acquisition_root
        )
    return tier_id, tier_event, model_root, model_hash_receipt


def resolve_caption_model_for_tier(
    root: str | Path,
    tier_id: str,
    *,
    acquisition_root: str | Path | None = None,
) -> tuple[Path, dict[str, dict[str, int | str]]]:
    """Resolve and verify the exact staged caption tier requested by config.

    Activation's default tier is a startup policy; the AI Models selection is
    the runtime policy. A configured tier may therefore differ from the
    activation default, but it must be staged and pass the same pinned
    inventory verification before faster-whisper can load it.
    """

    if tier_id not in (FLOOR_TIER_ID, LARGE_V3_TIER_ID):
        raise NativeStationConfigurationError(f"Unknown configured caption tier: {tier_id!r}")
    roots = caption_tier_search_roots(Path(root), acquisition_root=acquisition_root)
    if _tier_base_root(roots, caption_tier_model_relative_root(tier_id)) is None:
        raise NativeStationCaptionsUnavailableError(
            f"Configured caption tier {tier_id!r} is not staged under "
            + " or ".join(str(candidate) for candidate in roots)
        )
    if tier_id == LARGE_V3_TIER_ID:
        return _validate_model_root(Path(root), acquisition_root=acquisition_root)
    return validate_floor_caption_model_root(Path(root), acquisition_root=acquisition_root)


def packaged_portal_environment(*, package_file: str | Path | None = None) -> dict[str, str]:
    """The two front-door variables ``civiccast.app._mount_packaged_portals``
    reads, derived from the ``civiccast`` package's own location.

    Chain L (TESTER2 request-0050c): the compiled operator console and
    resident portal ship INSIDE the ``civiccast`` package (the
    ``native-app-payload`` pack carries
    ``payload/Lib/site-packages/civiccast/apps/<portal>/dist``, and
    ``native_pack_staging::pack_extraction_destination`` lands that payload at
    ``<INSTDIR>\\runtime``). They are therefore on disk from pack-staging time
    onward and are a property of the INTERPRETER, not of any later station
    state -- so this derivation needs no root, no station-set, and no
    activation. See
    :func:`civiccast.native.supervisor.install_layout.packaged_portal_dist_dirs`
    for why the package's own location, not root arithmetic, is the source of
    truth.

    Pure: existence is the CALLER's gate. On a real station the dists are
    required members of the civiccast wheel
    (``build_native_app_payload.assert_civiccast_wheel_layout``), so their
    absence means a broken install and is reported at ERROR by the one
    component that can actually check -- ``app._configured_static_dir``.
    """

    operator_dist, public_dist = packaged_portal_dist_dirs(package_file)
    return {
        "CIVICCAST_OPERATOR_CONSOLE_DIST": str(operator_dist),
        "CIVICCAST_PUBLIC_PORTAL_DIST": str(public_dist),
    }


def lan_only_station_environment() -> dict[str, str]:
    """The one variable that tells the control plane it is running on a
    LAN-only station (F-16, sandbox newcomer re-walk `dd7f835f`, 2026-08-01).

    ``civiccast.app.create_app`` reads it and does not serve ``/docs`` or
    ``/redoc``: FastAPI's built-in renderers for those pull their JavaScript,
    CSS and fonts from ``cdn.jsdelivr.net`` / ``fastapi.tiangolo.com`` /
    ``fonts.googleapis.com``, and a council-chamber station is frequently
    firewalled outbound and sometimes air-gapped, so both pages render blank
    there. ``/openapi.json`` is unaffected -- it has no external dependency.

    A PRODUCER OF ITS OWN, merged by both env builders, rather than a literal
    in each: this is the same coupling that produced chain L's defect, where a
    variable set on only one of the two paths meant an installed-but-not-yet-
    activated station silently lost a surface. A station is LAN-only from the
    moment it is installed, long before it is activated -- and the re-walk was
    in exactly that state when it found the CDN dependency.

    Deliberately NOT keyed off ``CIVICCAST_NATIVE_STATION``, which
    ``installer/service.py``'s ``_native_station_activated`` reads as an
    ACTIVATION claim and which the pre-activation path must keep withholding.
    """

    return {"CIVICCAST_LAN_ONLY_STATION": "1"}


def native_reported_version_environment() -> dict[str, str]:
    """The one variable that tells the control plane it is running as a
    native station and must report the NATIVE product line's own version
    over ``/health``/``/api/version`` (native-windows chain J, 2026-08-02),
    not the WSL product line's ``civiccast._version.__version__``.

    A PRODUCER OF ITS OWN, merged by both env builders below, rather than a
    literal in each -- the same coupling chain L's defect exploited for
    ``CIVICCAST_LAN_ONLY_STATION`` (see :func:`lan_only_station_environment`):
    a variable set on only one of the two paths means a station that is
    installed but not yet activated silently reports the wrong version.
    ``civiccast.app._reported_version`` reads it, falling back to
    ``civiccast._version.__version__`` when unset -- i.e. every non-native
    hosting context is completely unaffected.
    """

    return {"CIVICCAST_NATIVE_REPORTED_VERSION": _native_version.__version__}


#: The VRAM floor for selecting GPU caption inference — the SAME >= 8 GB
#: NVIDIA line `hardware_inventory.rs`'s deployment-tier ladder and
#: `civiccast.platform.hardware._tier_for` already draw. One ladder,
#: three consumers; never a second threshold.
_WHISPER_CUDA_MIN_VRAM_GB: Final[float] = 8.0

#: Owner review finding (2026-08-15): cuBLAS/cuDNN are ABSENT on ~95%+ of
#: NVIDIA-equipped machines today -- they are not part of the base station
#: install, only of a separate CUDA component pack that ships later. Gating
#: cuda selection on VRAM alone therefore guaranteed the
#: `FasterWhisperRuntime._model_instance` load-failure fallback fired on
#: nearly every capable machine (a silent degradation dressed up as a
#: success: the env would claim "cuda" and the runtime would quietly demote
#: to cpu on first model load). Capability now means VERIFIED PRESENCE, not
#: hope: both DLLs must actually be staged before cuda is selected.
_CUDA_REQUIRED_DLL_NAMES: Final[tuple[str, ...]] = ("cublas64_12.dll", "cudnn64_9.dll")


def cuda_bin_dir(root: str | Path) -> Path:
    """``<root>\\dependencies\\cuda\\bin`` -- the future CUDA component's
    staging location, mirroring the EXISTING ``dependencies\\ffmpeg\\bin``
    bridged-component convention
    (``civiccast.native.supervisor.install_layout.InstallLayout.ffmpeg_bin_dir``,
    itself pinned by ``native_activation.rs``'s ``validate_staged_runtime_layout``).
    Pure path arithmetic for ONE candidate root, like every other
    ``dependencies\\<tool>\\`` location in this codebase: existence is the
    CALLER's gate, never assumed here. Used both directly (once a caller
    already knows the winning root) and internally by
    :func:`resolve_cuda_bin_dir`'s multi-root search.
    """

    return Path(root) / "dependencies" / "cuda" / "bin"


def resolve_cuda_bin_dir(
    version_root: str | Path, *, acquisition_root: str | Path | None = None
) -> Path | None:
    """The FIRST of ``(version_root, acquisition_root)`` whose
    ``dependencies\\cuda\\bin`` carries BOTH required CUDA runtime DLLs, in
    the SAME preference order and for the SAME chain-H1 reason
    :func:`caption_tier_search_roots` already exists for: the elevated
    installer stages components under ``version_root``, but the non-elevated
    first-run GUI cannot write there at all and downloads optional
    components (a future ``native-cuda-runtime`` component included) to
    ``acquisition_root`` (``<PROGRAMDATA>\\CivicCast``) instead. Checking
    ``version_root`` first means anything the ELEVATED installer staged
    always shadows anything a user-writable directory holds -- the same
    precedence rationale :func:`caption_tier_search_roots` documents.

    ``acquisition_root=None`` searches ONLY ``version_root``, mirroring
    :func:`caption_tier_search_roots`'s own no-acquisition-root contract.
    Returns ``None`` when neither root has both DLLs.
    """

    for root in caption_tier_search_roots(version_root, acquisition_root=acquisition_root):
        bin_dir = cuda_bin_dir(root)
        if all((bin_dir / name).is_file() for name in _CUDA_REQUIRED_DLL_NAMES):
            return bin_dir
    return None


def cuda_runtime_libs_present(
    version_root: str | Path | None, *, acquisition_root: str | Path | None = None
) -> bool:
    """Whether BOTH required CUDA runtime DLLs (cuBLAS, cuDNN) are staged at
    EITHER :func:`cuda_bin_dir` of ``version_root`` or of
    ``acquisition_root`` (search order: :func:`resolve_cuda_bin_dir`).

    ``version_root=None`` -- no root known to this caller -- answers
    ``False`` without even considering ``acquisition_root``: the same
    fail-closed posture as every other presence check in this module. This
    checks ONLY the component-pack locations; an operator who installed the
    CUDA toolkit system-wide still has the ``CIVICCAST_WHISPER_DEVICE=cuda``
    override documented on :func:`resolve_whisper_device` as their escape
    hatch.
    """

    if version_root is None:
        return False
    return resolve_cuda_bin_dir(version_root, acquisition_root=acquisition_root) is not None


def _probe_nvidia_vram_gb() -> float | None:
    """Best NVIDIA adapter's total VRAM in GiB, or ``None`` when no NVIDIA
    GPU is reachable (no device, no driver, no pynvml, NVML init failure).
    Same fail-closed NVML posture as ``civiccast.platform.hardware._probe_gpu``
    -- every failure mode returns ``None``, never raises."""

    try:
        import pynvml  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        pynvml.nvmlInit()
    except Exception:
        return None
    try:
        count = pynvml.nvmlDeviceGetCount()
        best: float | None = None
        for index in range(count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            vram_gb = mem.total / 1024**3
            if best is None or vram_gb > best:
                best = vram_gb
        return best
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            pynvml.nvmlShutdown()


def whisper_device_capability(
    install_root: str | Path | None = None,
    *,
    acquisition_root: str | Path | None = None,
) -> tuple[bool, bool]:
    """``(capable_gpu, libs_present)`` -- the two inputs
    :func:`resolve_whisper_device` gates cuda selection on, exposed as a
    PUBLIC pair for callers that need to explain a cpu selection (the
    installer's ``caption-device`` health check, owner review "option D")
    without reaching into this module's private probe or threshold. Reads
    the SAME ``_probe_nvidia_vram_gb`` and ``_WHISPER_CUDA_MIN_VRAM_GB``
    :func:`resolve_whisper_device` itself reads -- one decision, one
    explanation, never a second copy that can drift from the first.
    ``acquisition_root`` is the SAME chain-H1 second root
    :func:`resolve_cuda_bin_dir` searches -- a caller that knows the
    caption-tier acquisition root already knows this one; they are the same
    ``<PROGRAMDATA>\\CivicCast`` value.

    ``libs_present`` is only meaningful (and only computed) when
    ``capable_gpu`` is true -- an incapable machine is reported as having no
    libs regardless of what happens to be staged, exactly like
    :func:`resolve_whisper_device`'s own short-circuit.
    """

    vram_gb = _probe_nvidia_vram_gb()
    capable_gpu = vram_gb is not None and vram_gb >= _WHISPER_CUDA_MIN_VRAM_GB
    libs_present = capable_gpu and cuda_runtime_libs_present(
        install_root, acquisition_root=acquisition_root
    )
    return capable_gpu, libs_present


def resolve_whisper_device(
    install_root: str | Path | None = None,
    *,
    acquisition_root: str | Path | None = None,
) -> tuple[str, str]:
    """The ``(device, compute_type)`` pair the caption runtime should run on.

    OWNER RULING (2026-08-15): a station whose hardware can run the caption
    engine on GPU gets GPU — "it's memory more than anything". Owner review
    (option B, same day) sharpened "can run" to mean VERIFIED PRESENCE, not
    hope: cuBLAS/cuDNN are absent on ~95%+ of NVIDIA machines today (they
    ship in a separate CUDA component pack later), so VRAM alone was not
    capability -- it was a coin flip on whether the runtime's own cuda-load
    fallback (see below) would immediately fire. Decision order:

    1. An explicit ``CIVICCAST_WHISPER_DEVICE`` in the SUPERVISOR's own
       environment always wins, in EITHER direction and even WITHOUT the
       component pack's DLLs present (the operator's machine-level escape
       hatch -- they may have installed the CUDA toolkit system-wide, which
       this module has no way to see); ``CIVICCAST_WHISPER_COMPUTE_TYPE``
       rides along, defaulting sensibly per device.
    2. Otherwise: best NVIDIA adapter VRAM >= ``_WHISPER_CUDA_MIN_VRAM_GB``
       AND both required CUDA runtime DLLs staged at either
       ``install_root`` or ``acquisition_root``
       (:func:`resolve_cuda_bin_dir`) selects ``("cuda", "float16")``.
       ``acquisition_root`` is the SAME chain-H1 second root
       :func:`caption_tier_search_roots` already searches for captions: the
       elevated installer stages a future ``native-cuda-runtime`` component
       under ``install_root``, but the non-elevated first-run GUI cannot
       write there and downloads it to ``acquisition_root``
       (``<PROGRAMDATA>\\CivicCast``) instead -- a GUI-downloaded component
       would be invisible to this gate without it.
       ``install_root=None`` -- no root known to this caller -- means "no
       libs", same as any other absence, regardless of ``acquisition_root``.
    3. Anything else — no NVIDIA GPU, probe failure, pynvml absent, capable
       GPU but libs not staged in either root — fails closed to
       ``("cpu", "int8")``, the pack contract's validated baseline.

    A wrong ``cuda`` here degrades instead of killing captions: the runtime
    side (``FasterWhisperRuntime._model_instance``) falls back to cpu/int8
    with a logged warning when the CUDA backend cannot load. That fallback
    stays in place as a second line of defense (e.g. the override forces
    cuda without libs), but is no longer the ONLY defense -- the presence
    gate above means a capable machine with no component pack installed
    lands on cpu/int8 the first time, never via a caught load failure. Known,
    documented proxy limitation: VRAM cannot see tensor-core generation — a
    pre-tensor-core card with enough VRAM AND libs present will still be
    selected and may caption slower than the CPU would (the GTX 1660 Ti
    measured 30/30 deadline misses on GPU); the operator override above is
    the remedy until a measured-capability probe exists.
    """

    override = os.environ.get("CIVICCAST_WHISPER_DEVICE", "").strip()
    if override:
        compute = os.environ.get("CIVICCAST_WHISPER_COMPUTE_TYPE", "").strip()
        if not compute:
            compute = "float16" if override.startswith("cuda") else "int8"
        return override, compute
    vram_gb = _probe_nvidia_vram_gb()
    if (
        vram_gb is not None
        and vram_gb >= _WHISPER_CUDA_MIN_VRAM_GB
        and cuda_runtime_libs_present(install_root, acquisition_root=acquisition_root)
    ):
        return "cuda", "float16"
    return "cpu", "int8"


def pre_activation_control_plane_environment() -> dict[str, str]:
    """The child environment for a station that is installed but NOT YET
    activated -- everything that is already true at that point, and nothing
    that is not.

    Chain L defect (TESTER2 request-0050c: install PASS, service RUNNING,
    /health 200, ``/operator/`` 404). The supervisor caught
    :class:`NativeStationNotActivatedError` and started the control plane with
    an EMPTY env, because the ONLY producer of the child env was
    :func:`load_native_station_environment` -- which fails closed on an absent
    ``station-set.json`` long before it ever reaches the front door. That
    coupled two independent things: captions/GStreamer/model wiring, which
    genuinely requires an activated station, and the front door plus the setup
    handoff, which do not.

    What is in here, and why it is activation-independent:

    * the packaged portal dists -- delivered by the ``native-app-payload``
      pack (see :func:`packaged_portal_environment`); without them the
      operator console 404s and first-run setup cannot even be REACHED.

    The installer-handoff setup nonce that used to live here was retired
    2026-08-29 (owner decision): the control plane binds ``127.0.0.1`` only,
    so first setup is unreachable from the network by construction and the
    nonce was a redundant gate that produced repeated field failures. First
    setup is now admitted by loopback alone
    (``civiccast.installer.router._require_local_setup_request``).

    What is deliberately NOT in here: ``CIVICCAST_NATIVE_STATION`` and
    ``CIVICCAST_NATIVE_STATION_MANIFEST``. ``installer/service.py``'s
    ``_native_station_activated`` reads exactly those two to decide whether
    setup has finished, and it must keep failing CLOSED here -- serving the
    console a not-yet-activated station needs must never be mistaken for a
    claim that activation happened.
    """

    return {
        **packaged_portal_environment(),
        **lan_only_station_environment(),
        **native_reported_version_environment(),
    }


def _resolve_gstreamer_egress_environment(
    gstreamer_runtime_root: Path,
    environment: dict[str, str],
    *,
    repair_hook: GstreamerRepairHook,
) -> dict[str, str]:
    """The GStreamer degraded-mode + self-repair state machine (owner ruling:
    dead air is the cardinal sin, never acceptable).

    Called only when the closure DIRECTORY is present
    (``<runtime>/dependencies/gstreamer``). Resolves egress in three tiers and
    returns the child environment the control plane should run with:

    1. **DETECT.** ``installed_gstreamer_environment`` validates the closure.
       It raises :class:`GstreamerRuntimeError` for a PARTIAL / reparse-point /
       AV-quarantined / interrupted-install closure -- "present but broken",
       a non-malicious, recoverable state, distinct from "absent". K2 (PR #404)
       left this call unwrapped, so that exception propagated out through
       ``station_environment_for_python`` into the supervisor's dependency
       wiring (whose ``try/except`` only catches the unrelated
       ``NativeStationNotActivatedError``) and crashed the WHOLE supervisor --
       all streaming, not just GStreamer egress. It is caught here and NEVER
       propagates: the supervisor must not crash on any tier of this path.
    2. **SELF-REPAIR ONCE.** ``repair_hook`` (default
       :func:`reverify_gstreamer_closure`) re-verifies the closure in place a
       single time -- recovering a transient AV lock without taking the channel
       off air. If it now reports healthy, GStreamer runs normally: the keys
       are injected and this returns the GStreamer-enabled environment. (The
       destructive signed re-stage that can rebuild genuinely missing bytes is
       the operator RECOVERY action, not an automatic boot step -- see
       :func:`reverify_gstreamer_closure`.)
    3. **FALL BACK TO FFmpeg.** If repair does not restore the closure, egress
       switches to the FFmpeg concat engine
       (``CIVICCAST_EGRESS_ENGINE=ffmpeg-concat``) so
       ``engine_select.build_encoder_strategy`` selects ``ConcatEncoderStrategy``
       and the CHANNEL KEEPS AIRING on FFmpeg (the Codex P1 on PR #406). A
       loud ERROR is logged, and :data:`EGRESS_DEGRADED_REASON_ENV` is set so
       the control-plane seam that holds a DB session
       (``egress.automation.build_channel_automation``) raises the operator
       alert and marks the channel DEGRADED -- this module runs pre-DB under
       LocalSystem and cannot do either itself.

    A closure that verifies clean on the FIRST look never enters tiers 2-3: it
    returns the GStreamer-enabled environment unchanged in shape from before
    this state machine existed.
    """

    try:
        return installed_gstreamer_environment(gstreamer_runtime_root, base_environment=environment)
    except GstreamerRuntimeError as exc:
        detected = exc

    # Tier 2: self-repair once, in place. A True result means the closure is
    # healthy now -- re-derive and inject the GStreamer environment and run
    # GStreamer normally.
    repaired = False
    try:
        repaired = repair_hook(gstreamer_runtime_root)
    except Exception:  # a repair hook must never turn a degrade into a crash
        _LOG.exception(
            "GStreamer closure self-repair at %s raised; treating as unrepaired "
            "and falling back to the FFmpeg egress engine.",
            gstreamer_runtime_root,
        )
    if repaired:
        try:
            environment = installed_gstreamer_environment(
                gstreamer_runtime_root, base_environment=environment
            )
        except GstreamerRuntimeError as exc:
            # Reported healthy but re-validation still fails: do not trust it --
            # fall through to the FFmpeg switch rather than inject a bad env.
            _LOG.error(
                "GStreamer closure at %s reported repaired but still failed "
                "validation (%s); falling back to the FFmpeg egress engine.",
                gstreamer_runtime_root,
                exc,
            )
        else:
            _LOG.warning(
                "installed GStreamer closure at %s was corrupt (%s) but self-repair "
                "restored it; running GStreamer egress normally.",
                gstreamer_runtime_root,
                detected,
            )
            return environment

    # Tier 3: self-repair did not restore the closure. Switch egress to the
    # FFmpeg concat engine so the channel keeps airing, and leave the degraded
    # breadcrumb for the session-holding control-plane seam.
    reason = (
        f"installed GStreamer closure at {gstreamer_runtime_root} is corrupt or partial "
        f"({detected}) and in-place self-repair did not restore it"
    )
    environment[EGRESS_ENGINE_ENV] = _FFMPEG_EGRESS_ENGINE
    environment[EGRESS_DEGRADED_REASON_ENV] = reason
    _LOG.error(
        "%s; degrading egress to the FFmpeg concat engine "
        "(CIVICCAST_EGRESS_ENGINE=%s) so the channel keeps airing. Run the "
        "operator 'repair GStreamer runtime & restore full egress' recovery "
        "action to re-stage the signed closure and restore GStreamer egress.",
        reason,
        _FFMPEG_EGRESS_ENGINE,
    )
    return environment


def load_native_station_environment(
    version_root: str | Path,
    *,
    program_data_root: str | Path | None = None,
    gstreamer_repair_hook: GstreamerRepairHook = reverify_gstreamer_closure,
) -> dict[str, str]:
    """Validate an activated station and return the exact child environment.

    ``gstreamer_repair_hook`` is the degraded-mode tier-2 self-repair seam
    (:data:`GstreamerRepairHook`), injectable for tests; production uses
    :func:`reverify_gstreamer_closure`.
    """

    try:
        root = Path(version_root).expanduser().resolve(strict=True)
    except OSError as exc:
        raise NativeStationConfigurationError(
            f"Native station version root is missing: {version_root}"
        ) from exc
    station_path = root / "station-set.json"
    if not os.path.lexists(station_path):
        # A fresh install has runtime/ + packs/ and nothing else: the
        # station-set is written ONLY by the acquisition/activation flow.
        # Absence is the legitimate installed-but-not-yet-activated state;
        # anything present-but-unreadable falls through to the loud parent
        # error below.
        raise NativeStationNotActivatedError(
            f"Native station is installed but not yet activated: station-set.json is absent at {station_path}"
        )
    station = _read_json_object(station_path, label="Native station-set")
    version, index_sha256 = _validate_station_set(station)
    # Beta fix (Sandbox run 16 layer-4 crash): this equality is ONLY the
    # junction-layout's own sanity check (see _JUNCTION_VERSION_PARENT_NAME's
    # docstring) -- it never applies to the direct fresh-install shape, which
    # has no app/<version> wrapping to check in the first place. Do not
    # relax this beyond that: a version root reached through the junction
    # convention that is STILL misnamed is exactly the "pointed at the wrong
    # tree" corruption this guarded against, and must keep failing closed.
    if root.parent.name == _JUNCTION_VERSION_PARENT_NAME and root.name != version:
        raise NativeStationConfigurationError(
            "Native station version root does not match its station-set"
        )
    if program_data_root is None:
        civiccast_data_root = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "CivicCast"
    else:
        civiccast_data_root = Path(program_data_root)
    # Chain H1: `civiccast_data_root` is ALSO the first-run acquisition flow's
    # download root (`main.rs::acquisition_download_root` derives the same
    # `<PROGRAMDATA>\CivicCast`), so a caption tier the non-elevated GUI
    # downloaded is found here, and one the elevated installer staged is still
    # preferred from the version root.
    #
    # Resolved BEFORE the receipt is read: the receipt must be validated
    # against whichever tier this station actually staged and verified on
    # disk, never against a tier the receipt merely claims for itself. A
    # station with no caption tier staged at all still raises
    # NativeStationCaptionsUnavailableError here, exactly as before -- it
    # never gets far enough to read a receipt whose caption identity it has
    # no staged tier to check.
    tier_id, tier_event, model_root, model_hash_receipt = _resolve_caption_tier(
        root, acquisition_root=civiccast_data_root
    )
    # Field evidence (candidate 4eca729, 2026-08-29): `activation-self-test.json`
    # is written EXACTLY ONCE, by the elevated installer, at `root` -- and
    # describes whichever caption tier was staged at THAT moment (almost
    # always the mandatory floor tier alone; `captions-large-v3` is an
    # OPTIONAL component the non-elevated post-install GUI can acquire LATER,
    # into `civiccast_data_root` only, chain H1). The tier resolution above
    # already prefers the highest tier actually staged on disk, searching
    # BOTH roots -- so once an operator acquires large-v3 post-activation,
    # this function would otherwise keep reading the STALE floor-tier receipt
    # at `root` and validating it against the large-v3 identity, which can
    # never match (`NativeStationConfigurationError: ... receipt does not
    # match this distribution`, crash-looping an already-working station).
    #
    # The receipt is therefore read from the SAME root the resolved tier's
    # model was actually found in -- the identical two-root search
    # (`_tier_base_root` over `caption_tier_search_roots`) `_resolve_caption_tier`
    # already used to find the model itself, so the two can never point at
    # different stations. `main.rs`'s post-acquisition addendum writer
    # (`finalize_captions_large_acquisition`) is the ONE thing that populates
    # `civiccast_data_root/activation-self-test.json`, proving that specific
    # tier with a real inference self-test before this function will ever
    # trust it. A floor-only station is completely unaffected: its tier's
    # base root is always `root`, exactly as before.
    roots = caption_tier_search_roots(root, acquisition_root=civiccast_data_root)
    receipt_root = _tier_base_root(roots, caption_tier_model_relative_root(tier_id)) or root
    # Field failure (DESKTOP-2BR3SJR, UPGRADE-18-REPORT.md, 2026-08-30): on an
    # uninstall-old -> reinstall-new upgrade, `components/captions-large-v3`
    # under ProgramData SURVIVES uninstall (operator data is preserved by
    # design), so the tier resolution above legitimately picks large-v3 with
    # `receipt_root = civiccast_data_root` -- but the NEW install never wrote
    # an addendum receipt there (`finalize_captions_large_acquisition` runs
    # only when the GUI itself downloads the tier), and the elevated installer
    # writes its receipt only at `root`. Model present, receipt absent, on
    # EVERY start: raising here crash-looped the whole station. The product
    # non-negotiable is a working station, so an ORPHANED optional tier
    # degrades to the mandatory floor tier (whose receipt the fresh install
    # DID write at `root`) instead of raising -- the pre-4eca729 selection,
    # taken only when the resolved tier cannot be proven. A large-v3 tier
    # WITH its valid addendum receipt is still preferred and validated
    # exactly as before (the 4eca729 fix is untouched), and a station with
    # no provable tier at all still fails closed.
    try:
        receipt = _read_json_object(
            receipt_root / "activation-self-test.json",
            label="Native station activation self-test receipt",
        )
        _validate_activation_receipt(
            receipt, version=version, index_sha256=index_sha256, tier_id=tier_id
        )
    except NativeStationConfigurationError:
        if (
            tier_id == FLOOR_TIER_ID
            or _tier_base_root(roots, FLOOR_CAPTION_MODEL_RELATIVE_ROOT) is None
        ):
            # Either the floor tier itself is what failed to prove, or there
            # is no floor tier staged to fall back to: fail closed exactly as
            # before -- never start a station whose only tier is unproven.
            raise
        orphaned_tier_id = tier_id
        orphaned_dir = receipt_root / Path(caption_tier_model_relative_root(orphaned_tier_id))
        # The fallback must itself be fully proven: same verification walk and
        # same receipt validation the floor tier has always had. If THIS
        # raises, the station has no provable tier and stays loudly down.
        model_root, model_hash_receipt = validate_floor_caption_model_root(
            root, acquisition_root=civiccast_data_root
        )
        floor_receipt_root = _tier_base_root(roots, FLOOR_CAPTION_MODEL_RELATIVE_ROOT) or root
        receipt = _read_json_object(
            floor_receipt_root / "activation-self-test.json",
            label="Native station activation self-test receipt",
        )
        _validate_activation_receipt(
            receipt, version=version, index_sha256=index_sha256, tier_id=FLOOR_TIER_ID
        )
        tier_id = FLOOR_TIER_ID
        tier_event = {
            "event": TIER_SELECTED_EVENT,
            "tier": FLOOR_TIER_ID,
            "requested": orphaned_tier_id,
            "fallback": True,
            "reason": (
                f"caption tier {orphaned_tier_id} is staged but has no valid "
                "activation self-test receipt at its base root (orphaned by an "
                "uninstall/reinstall upgrade); degraded to the proven floor tier"
            ),
        }
        _LOG.warning(
            "Caption tier %s at %s is staged but has no valid activation "
            "self-test receipt at %s -- it was likely preserved from a previous "
            "install by an uninstall/reinstall upgrade. Starting on the proven "
            "floor caption tier instead; the higher caption tier will be "
            "re-validated/re-acquired from the operator console.",
            orphaned_tier_id,
            orphaned_dir,
            receipt_root / "activation-self-test.json",
        )
    tap_root = civiccast_data_root / "data" / "caption-tap"
    # Item 91: the ONE explicit off switch for the live caption audio tap
    # leg. Read from `os.environ` here -- this function runs inside the
    # supervisor process that is about to spawn the control-plane child
    # (`civiccast/native/supervisor/service.py`'s
    # `env = {**os.environ, **spec.env}` applies `spec.env` LAST, so
    # `spec.env` is the only place an override can ever win) -- i.e. the
    # SERVICE environment: whatever the operator set in the Windows
    # service's registry `Environment` (REG_MULTI_SZ), inherited by this
    # process at spawn. Before this fix the two lines this replaces were
    # unconditional: `CIVICCAST_CAPTION_TAP` was always forced to `"inline"`
    # and `CIVICCAST_CAPTION_TAP_DIR` was always injected, so an operator's
    # `CIVICCAST_CAPTION_TAP=off` in the service environment could never
    # reach the child -- `spec.env` always overwrote it back to "inline"
    # with a real tap dir. `build_audio_tap_plan`
    # (`civiccast/captions/tap.py`) only ever consults
    # `CIVICCAST_CAPTION_TAP_DIR`, so as long as ANY dir was set the egress
    # tee was built regardless of the mode string.
    #
    # Any value other than the literal `"off"` (unset, `"inline"`, or
    # anything else) keeps today's behavior byte-for-byte: forced inline
    # mode, tap dir injected. Only `"off"` changes anything, and it changes
    # exactly two things: the mode string passed through as `"off"` instead
    # of being overwritten, and the tap dir set to the EMPTY STRING rather
    # than merely omitted from this dict.
    #
    # Review round 2 caught the omission-only version of this as a real
    # defect: `env = {**os.environ, **spec.env}` in
    # `civiccast/native/supervisor/service.py` means an inherited
    # `CIVICCAST_CAPTION_TAP_DIR` already present in `os.environ` (a stray
    # leftover from a previous configuration; there should never be one, but
    # this switch does not get to trust that) is NOT removed merely because
    # `spec.env` (this function's return value) omits the key -- omitting a
    # key from `spec.env` only means `os.environ`'s own value for it wins the
    # merge unopposed. The only way `spec.env` can neutralize an inherited
    # value is to set the key itself, to something `build_audio_tap_plan`
    # treats as absent: its `.strip()` check on
    # `CIVICCAST_CAPTION_TAP_DIR` (`civiccast/captions/tap.py`) treats `""`
    # exactly like unset, so setting it to `""` here always wins the merge
    # (this dict is `spec.env`, applied last) and always leaves the plan
    # builder with nothing to read, regardless of what `os.environ` inherited.
    #
    # `civiccast.app`'s app-factory only calls `build_tap_worker` when
    # `CaptionTapWorkerSettings.from_env().mode == "inline"` (`civiccast/app.py`
    # around the "Live caption tap" comment), so passing `"off"` through also
    # means the in-app tap worker thread is never started -- it does not need
    # to idle against a missing directory, because it is never constructed
    # in the first place. `CaptionTapWorkerSettings.from_env()`
    # (`civiccast/captions/tap_worker.py`) already treats `mode == "off"` with
    # an empty (or absent) `CIVICCAST_CAPTION_TAP_DIR` as fully valid (the
    # fail-fast dir check is gated on `mode != TAP_MODE_OFF`, and its own
    # `root_raw` read is `.strip()`ped the same way), so the external-process
    # entrypoint (`python -m civiccast.captions.tap_worker`) is likewise safe
    # run against this same environment.
    caption_tap_disabled = os.environ.get("CIVICCAST_CAPTION_TAP", "").strip().lower() == "off"
    caption_tap_environment: dict[str, str] = (
        {"CIVICCAST_CAPTION_TAP": "off", "CIVICCAST_CAPTION_TAP_DIR": ""}
        if caption_tap_disabled
        else {
            "CIVICCAST_CAPTION_TAP": "inline",
            "CIVICCAST_CAPTION_TAP_DIR": str(tap_root),
        }
    )
    # The front door. `civiccast/app.py`'s `_mount_packaged_portals` serves
    # /operator/ and / ONLY when these are set, and nothing on a native station
    # ever set them (only the WSL `headless-bootstrap.ps1` did) -- so the
    # control plane came up answering /health and 404ing both of the surfaces
    # the product is actually reached through. Emitted UNCONDITIONALLY: on a
    # real station the dists are required members of the civiccast wheel
    # (`build_native_app_payload.assert_civiccast_wheel_layout`), so their
    # absence means a broken install and must be reported by the one component
    # that can actually check -- `_configured_static_dir`, which now logs at
    # ERROR level and still degrades gracefully.
    #
    # Chain L: derived from the `civiccast` package's OWN location rather than
    # by arithmetic on `root` -- the same source of truth the interpreter that
    # will serve them actually uses, and the one derivation the PRE-activation
    # path (`pre_activation_control_plane_environment`) shares, so the two
    # cannot drift apart. See `packaged_portal_environment`.
    whisper_device, whisper_compute = resolve_whisper_device(
        root, acquisition_root=civiccast_data_root
    )
    environment = {
        "CIVICCAST_NATIVE_STATION": "1",
        "CIVICCAST_NATIVE_STATION_ROOT": str(root),
        "CIVICCAST_NATIVE_STATION_MANIFEST": str(station_path),
        **caption_tap_environment,
        "CIVICCAST_CAPTION_TAP_ATOMIC": "1",
        "CIVICCAST_CAPTION_RUNTIME": "faster-whisper",
        "CIVICCAST_CAPTION_TIER": tier_id,
        "CIVICCAST_CAPTION_TIER_EVENT": json.dumps(
            tier_event, sort_keys=True, separators=(",", ":")
        ),
        "CIVICCAST_CAPTION_MODEL_HASH_RECEIPT": json.dumps(
            model_hash_receipt, sort_keys=True, separators=(",", ":")
        ),
        # This root comes from the validated signed runtime contract and is
        # independent of Whisper's selected model directory.
        "CIVICCAST_WHISTLE_ROOT": str(
            root / Path(str(EXPECTED_RUNTIME_CONTRACT["whistle_assets_root"]))
        ),
        "CIVICCAST_WHISPER_MODEL_PATH": str(model_root),
        "CIVICCAST_WHISPER_DEVICE": whisper_device,
        "CIVICCAST_WHISPER_COMPUTE_TYPE": whisper_compute,
        EGRESS_ENGINE_ENV: "gstreamer",
        "CIVICCAST_EGRESS_EMBED_CAPTIONS": "1",
        **packaged_portal_environment(),
        **lan_only_station_environment(),
        **native_reported_version_environment(),
        "PATH": os.environ.get("PATH", ""),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    if whisper_device == "cuda":
        # The CUDA runtime DLLs are only on the stock LocalSystem PATH once
        # they are on THIS PATH: they are never installed to a system
        # directory, only staged at cuda_bin_dir() of whichever root
        # resolve_whisper_device's presence gate actually found them under
        # (elevated `root`, or chain H1's non-elevated `civiccast_data_root`
        # -- the same acquisition root already threaded through above for
        # captions). Same prepend shape as installed_gstreamer_environment
        # below -- inherited PATH extended, never replaced.
        #
        # resolve_cuda_bin_dir returning None here means the OVERRIDE forced
        # cuda without either root having the DLLs staged (an operator
        # relying on a system-wide CUDA install) -- fall back to root's own
        # cuda_bin_dir as the best-effort location, matching the single-root
        # PATH behavior this gate replaces; a non-existent directory on PATH
        # is harmless.
        cuda_bin = resolve_cuda_bin_dir(root, acquisition_root=civiccast_data_root) or cuda_bin_dir(
            root
        )
        inherited_path = environment.get("PATH", "")
        environment["PATH"] = (
            f"{cuda_bin}{os.pathsep}{inherited_path}" if inherited_path else str(cuda_bin)
        )
        # PATH ALONE is not sufficient (TESTER4, RTX 5070 Ti: both DLLs
        # staged and on PATH, load still failed) -- Windows' loader since
        # Python 3.8 ignores PATH for a dependent DLL's own resolution and
        # requires `os.add_dll_directory` instead (the exact problem
        # `gstreamer_runtime.installed_gstreamer_environment`/
        # `bootstrap_installed_gstreamer_runtime` already solved for the
        # staged GStreamer DLLs). This variable is that fix's OTHER half:
        # `civiccast.captions.runtime.FasterWhisperRuntime` reads it and
        # calls `os.add_dll_directory` on it before a cuda model load. PATH
        # stays set too, for any non-Python consumer that still resolves
        # DLLs off PATH.
        environment["CIVICCAST_CUDA_BIN_DIR"] = str(cuda_bin)
    # GStreamer is not a fourth top-level `dependencies/<tool>` pack like
    # ffmpeg/ollama/cuda above -- `native_pack_staging.rs` stages no
    # separate GStreamer-only component at all (see
    # `test_station_activation_uses_the_embedded_app_payload_tree_not_a_third_component`
    # in tests/native/test_gstreamer_runtime.py). Instead
    # `scripts/build_native_app_payload_pack.py`'s
    # `_compose_payload_with_closure` copies the closure INTO the
    # native-app-payload pack itself, at `dependencies/gstreamer` relative to
    # THAT pack's own root, and `native_pack_staging::
    # pack_extraction_destination` bridges the whole app-payload component to
    # `<root>/runtime` -- the SAME directory that holds `python.exe` (see
    # `station_environment_for_python`). The closure therefore lands at
    # `<root>/runtime/dependencies/gstreamer`, never `<root>/dependencies/
    # gstreamer`: proven by the installed-product CI smoke
    # (`.github/workflows/native-beta-candidate-artifacts.yml` invokes
    # `installed_gstreamer_smoke` with `--version-root "$installRoot/runtime"`,
    # not `$installRoot`), and `installed_gstreamer_environment` itself
    # already expects its `version_root` argument to directly contain
    # `dependencies/gstreamer` -- this is the caller-side root the earlier
    # gate got wrong, not that function's own path arithmetic.
    gstreamer_runtime_root = root / "runtime"
    if (gstreamer_runtime_root / "dependencies" / "gstreamer").is_dir():
        environment = _resolve_gstreamer_egress_environment(
            gstreamer_runtime_root,
            environment,
            repair_hook=gstreamer_repair_hook,
        )
    # The installer-handoff setup nonce that used to be injected here was
    # retired 2026-08-29 (owner decision): the control plane binds
    # 127.0.0.1 only, so first setup is unreachable from the network by
    # construction and the nonce was a redundant, failure-prone gate. First
    # setup is now admitted by loopback alone
    # (civiccast.installer.router._require_local_setup_request).
    return environment


def station_environment_for_python(
    python_path: str | Path,
    *,
    program_data_root: str | Path | None = None,
    gstreamer_repair_hook: GstreamerRepairHook = reverify_gstreamer_closure,
) -> dict[str, str]:
    """Derive the promoted station root from the embedded Python's own path.

    Accepts BOTH station layouts (Sandbox run 16 layer-4 crash: the fresh
    install has no junction layer at all, and the running service host's
    ``sys.executable`` is never plain ``python.exe``):

    * the post-tag upgrade engine's junction layout --
      ``<version-root>/runtime/python.exe`` (``version-root.name`` must equal
      the station-set's ``product_version``, enforced by
      :func:`load_native_station_environment`);
    * the direct, fresh-install layout the installer's payload extraction
      actually produces -- ``<install_root>/runtime/python.exe``, with no
      version-named wrapping at all.

    Either shape's Python may be named ``python.exe``, ``pythonw.exe``, or
    ``pythonservice.exe`` (the Windows service host's own executable).
    """

    try:
        python = Path(python_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise NativeStationConfigurationError(
            f"Native embedded Python is missing: {python_path}"
        ) from exc
    if (
        python.name.casefold() not in _ACCEPTED_STATION_PYTHON_NAMES
        or python.parent.name != "runtime"
    ):
        raise NativeStationConfigurationError(
            "Native service Python must be one of python.exe/pythonw.exe/"
            "pythonservice.exe at <version-root>/runtime/ (the post-upgrade "
            "junction layout) or at <install_root>/runtime/ (the direct, "
            "fresh-install layout)"
        )
    return load_native_station_environment(
        python.parent.parent,
        program_data_root=program_data_root,
        gstreamer_repair_hook=gstreamer_repair_hook,
    )


__all__ = [
    "EGRESS_DEGRADED_REASON_ENV",
    "EGRESS_ENGINE_ENV",
    "GstreamerRepairHook",
    "NativeStationCaptionsUnavailableError",
    "NativeStationConfigurationError",
    "NativeStationNotActivatedError",
    "TierSelectionDecision",
    "TierSelectionError",
    "TierSelectionResult",
    "caption_tier_model_relative_root",
    "caption_tier_search_roots",
    "cuda_bin_dir",
    "cuda_runtime_libs_present",
    "lan_only_station_environment",
    "load_native_station_environment",
    "native_reported_version_environment",
    "packaged_portal_environment",
    "pre_activation_control_plane_environment",
    "resolve_cuda_bin_dir",
    "resolve_whisper_device",
    "reverify_gstreamer_closure",
    "select_caption_tier",
    "station_environment_for_python",
    "validate_floor_caption_model_root",
    "whisper_device_capability",
]
