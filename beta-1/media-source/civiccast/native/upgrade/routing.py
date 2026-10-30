# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Upgrade-vs-fresh routing for the D3 install/upgrade engine (chain K/K2).

WHAT WENT WRONG (real hardware R7, 2026-08-01, request 0053b). The machine had
NO installed product -- 0 Add/Remove Programs entries, no ``CivicCastSupervisor``
service (``sc query`` -> 1060), no install directory. What it DID have was data
left behind on purpose by the previous uninstall: ``C:\\ProgramData\\CivicCast``
and the HKLM values under ``Software\\CivicCast\\Native``. The installer routed
that into the UPGRADE engine and logged ``old=1.0.0-rc15 new=1.0.0-rc15`` --
an "upgrade" from a version to itself, on a machine with nothing installed.
The engine then drained writers that did not exist and failed at the
pre-upgrade backup.

The routing gate lived in NSIS (``nsis-hooks-bootstrap.nsh``) and keyed on two
registry values::

    ${If} $R0 == "none"        ; InstalledVersion absent
      ${AndIf} $R2 == ""       ; DatabaseUrl absent
      ... skip the D3 engine

Both of those values SURVIVE uninstall by deliberate design (see
``native_uninstall.rs``'s ``NATIVE_D4_STATE_INVENTORY``: they are the credential
for, and the version stamp of, the PostgreSQL cluster that uninstall
deliberately preserves). So they are data-remnant signals, not
product-existence signals, and the gate could never fire on a machine that had
ever held a successful install.

THE SIGNAL THIS MODULE USES INSTEAD: the ``CivicCastSupervisor`` service being
registered in the Service Control Manager. Every other candidate was
considered and rejected on evidence:

* **ARP entry / registered uninstaller.** Tauri's generated install section
  writes the Add/Remove Programs entry (DisplayVersion included) and
  ``uninstall.exe`` BEFORE ``NSIS_HOOK_POSTINSTALL`` runs. By the time any
  routing decision executes, both are always present and always describe the
  version being installed RIGHT NOW. Proven live, Sandbox matrix row 1
  (2026-07-30): an ARP-keyed gate never fired at all.
* **``InstalledVersion`` / ``DatabaseUrl``.** Preserved across uninstall by
  design (inventory rows 2 and 4). This is exactly what R7 had.
* **``$INSTDIR\\CivicCast Native.exe`` / the install tree.** The installer has
  already written it by POSTINSTALL time.
* **``C:\\ProgramData\\CivicCast``.** Preserved across uninstall by design; it
  is the data root, which is the thing a fresh install must ADOPT.

The service is the only tracked D4 item that is both CREATED by a successful
install (``--civiccast-register-native-service``) and REMOVED by a successful
uninstall (``--civiccast-teardown-native-state``; inventory row 1). R7's own
evidence records both halves: ``sc.exe query CivicCastSupervisor`` returned
1060 (``ERROR_SERVICE_DOES_NOT_EXIST``) after uninstall while the registry
values remained. It is also the CAUSALLY correct signal: D3's drain seam reads
a running supervisor's D7 control pipe and its health gate SCM-starts the
service, so with no registered service those steps cannot mean anything even
if they somehow succeeded.

The same probe already gates the install-over-existing classification in
``NSIS_HOOK_PREINSTALL`` and the D3 drain's writers-active check
(``service_control._real_service_registered_probe``) -- this module reuses that
question rather than inventing a fourth product-existence convention.

WHERE THE DECISION LIVES. In Python, not NSIS. The hook file's own stated
contract is that "all D3 logic ... lives in tested Python
(civiccast.native.upgrade), never in NSIS script"; the routing gate was the
one piece that violated it, and it is the piece that broke. NSIS now passes
the signals it can read and branches on the engine's exit code.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from civiccast.native.supervisor.config import SERVICE_NAME
from civiccast.native.win_probes import SC_EXE

#: The literal ``--old-version`` value the NSIS hook passes when the
#: ``InstalledVersion`` marker is absent. Mirrors the hook's ``StrCpy $R0
#: "none"``; not a version string, so it can never collide with a real one.
NO_RECORDED_VERSION = "none"

#: ``sc query`` exits 0 when the service is registered in ANY run state and
#: 1060 (``ERROR_SERVICE_DOES_NOT_EXIST``) when it is not registered at all --
#: the same documented SCM contract
#: :func:`civiccast.native.upgrade.service_control._real_service_registered_probe`
#: and :func:`civiccast.native.win_probes._default_wsl_service_present` key on.
_ERROR_SERVICE_DOES_NOT_EXIST = 1060
_SERVICE_QUERY_TIMEOUT_SECONDS = 5.0

#: ``() -> True | False | None``. True: the product's service is registered
#: (a real installed product exists). False: Windows itself confirms it is not
#: registered. None: the question could not be answered.
InstalledProductProbe = Callable[[], bool | None]


class UpgradeRoute(StrEnum):
    """What this installer run should actually do about the database."""

    UPGRADE = "upgrade"
    """A real installed product exists and the version is changing: run the
    full D3 journaled sequence (drain, verified backup, tree/junction flip,
    migrate, health gate)."""

    FRESH_INSTALL = "fresh_install"
    """No installed product. The D3 engine is not applicable: there is nothing
    to drain, no junction to flip, no service to health-gate. Any preserved
    data root is ADOPTED as-is (D4 provisioning reuses an existing cluster and
    its credential by design) -- never deleted, never migrated by this
    engine."""

    SAME_VERSION_NO_OP = "same_version_no_op"
    """A real installed product exists and the recorded version equals the
    version being installed. There is no migration between a version and
    itself, so the migration engine must not run.

    <installer-path-audit MA-05> See REFUSED_DOWNGRADE below for the case
    this used to fall through to.

    Repair semantics were considered and NOT adopted: the codebase's repair
    path (D5, ``--civiccast-verify-native-install``) is not wired into the
    install chain at all (grep ``--civiccast-verify-native-install`` in
    ``nsis-hooks-bootstrap.nsh``: it appears in comments only), and the steps
    that DO repair a tree on this chain -- pack re-extraction, D2
    re-verification, D4 service/firewall re-registration -- already run before
    and after this engine regardless of the route. Inventing a repair
    invocation here would be new, unproven behavior on the install path. So
    this is an honest, loudly-logged no-op instead of a silent one."""

    REFUSED_DOWNGRADE = "refused_downgrade"
    """<installer-path-audit MA-05> The installed product is NEWER than the
    version this setup installs.

    ``decide_route`` used to have exactly one version comparison -- string
    equality -- and no ordering comparison anywhere, so this case routed to
    UPGRADE and drove ``alembic upgrade head`` toward an OLDER head. Alembic
    cannot locate the database's current revision in the older build's graph,
    so the migration fails, the engine rolls back, and (before BL-01) the
    rollback's own restore could not succeed either -- turning "the operator
    ran the wrong installer" into a halted station needing manual recovery.

    Refused BEFORE the interlock, the drain, the backup, or any mutation:
    nothing on the machine is touched."""


#: The version shapes this line ships: ``1.0.0-beta.3``, ``1.0.0-rc15``,
#: ``1.0.0``. Anything else is deliberately NOT ordered (see
#: :func:`is_downgrade`).
_VERSION_PATTERN = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre_label>[A-Za-z]+)\.?(?P<pre_number>\d+))?$"
)


def _version_sort_key(version: str) -> tuple[int, int, int, int, str, int] | None:
    """A comparable key for a version string, or None if it is not ours.

    Pre-release ordering follows semver: a version WITH a pre-release sorts
    BEFORE the same version without one (``1.0.0-rc15`` < ``1.0.0``), and two
    pre-releases of the same core sort by label then number.
    """

    match = _VERSION_PATTERN.match(version.strip())
    if match is None:
        return None
    pre_label = match.group("pre_label")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        1 if pre_label is None else 0,  # a release outranks any pre-release
        pre_label or "",
        int(match.group("pre_number") or 0),
    )


def is_downgrade(old_version: str, new_version: str) -> bool:
    """Is ``new_version`` strictly OLDER than the installed ``old_version``?

    <installer-path-audit MA-05> Fails OPEN by design: when either string is
    not one this product line ships (including
    :data:`NO_RECORDED_VERSION`), the ordering is unknown and this returns
    False, leaving the pre-existing behaviour untouched. Refusing an install
    because a version string could not be parsed would be a worse failure than
    the one this guard exists to prevent.
    """

    old_key = _version_sort_key(old_version)
    new_key = _version_sort_key(new_version)
    if old_key is None or new_key is None:
        return False
    return new_key < old_key


class RouteDecision:
    """A route plus the operator-facing sentence explaining it.

    Not a pydantic model: nothing here is persisted or parsed back, and the
    engine log wants the reason as text.
    """

    __slots__ = ("data_root_adopted", "reason", "route")

    def __init__(
        self, route: UpgradeRoute, reason: str, *, data_root_adopted: bool = False
    ) -> None:
        self.route = route
        self.reason = reason
        self.data_root_adopted = data_root_adopted

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"RouteDecision(route={self.route!r}, reason={self.reason!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RouteDecision):
            return NotImplemented
        return (
            self.route == other.route
            and self.reason == other.reason
            and self.data_root_adopted == other.data_root_adopted
        )

    def __hash__(self) -> int:
        return hash((self.route, self.reason, self.data_root_adopted))


def default_installed_product_probe(
    *,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    service_name: str = SERVICE_NAME,
) -> bool | None:
    """Is a real CivicCast (Native) product installed on this machine?

    Answered by SCM registration of ``service_name`` -- see this module's
    docstring for why every other candidate signal is unusable at
    POSTINSTALL time.

    Returns ``True`` (registered, in any run state), ``False`` (Windows
    confirms it is not registered), or ``None`` (the question could not be
    answered: a timeout, an ``OSError`` launching ``sc.exe``, or any other
    exit code). ``None`` is NOT silently treated as either answer here --
    :func:`decide_route` decides what an unanswerable probe means, and says
    so in its reason text.
    """

    try:
        completed = runner(
            [str(SC_EXE), "query", service_name],
            capture_output=True,
            timeout=_SERVICE_QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode == 0:
        return True
    if completed.returncode == _ERROR_SERVICE_DOES_NOT_EXIST:
        return False
    return None


def _describe_remnants(database_url: str, recorded_version: str, state_root: str | None) -> str:
    """The operator-facing sentence about what CivicCast data is already here.

    Empty string when there is none, which is how the caller decides whether
    an ADOPTION statement belongs in the reason at all -- promising to adopt
    and preserve data on a machine that has none reads as boilerplate and
    trains operators to skip the line that matters.
    """

    remnants: list[str] = []
    if recorded_version and recorded_version != NO_RECORDED_VERSION:
        remnants.append(f"a recorded InstalledVersion of {recorded_version}")
    if database_url:
        remnants.append("a registered database credential")
    if state_root is not None and Path(state_root).exists():
        remnants.append(f"an existing data root at {state_root}")
    if not remnants:
        return ""
    return (
        "Setup found "
        + ", ".join(remnants)
        + (
            "; that existing data is preserved and adopted by this installation "
            "as-is, never deleted."
        )
    )


def decide_route(
    *,
    old_version: str,
    new_version: str,
    database_url: str,
    installed_product: bool | None,
    state_root: str | None = None,
) -> RouteDecision:
    """Choose the route for this installer run. Pure -- no OS, no DB.

    ``installed_product`` is the tri-state answer from
    :func:`default_installed_product_probe`. ``old_version`` is the hook's
    ``--old-version`` (``"none"`` when the ``InstalledVersion`` marker is
    absent). ``database_url`` and ``state_root`` are read ONLY to describe the
    data remnants in the reason text -- they never select the route, which is
    the whole correction: R7 proved they are remnant signals, not
    product-existence signals.

    An UNANSWERABLE probe (``None``) routes to FRESH_INSTALL, fail-safe rather
    than fail-silent. The two outcomes are not symmetric: routing fresh when a
    product exists leaves a schema unmigrated, which the control plane's own
    startup ``check_schema_currency`` reports loudly at first boot; routing
    upgrade when no product exists is R7 -- draining absent writers, backing up
    through a cluster that may not be running, flipping a junction that does
    not exist, and terminating the install in a rollback dialog. The reason
    text names the ambiguity so it is never invisible in the log.
    """

    remnants = _describe_remnants(database_url, old_version, state_root)
    tail = f" {remnants}" if remnants else " No existing CivicCast data was found on this machine."

    if installed_product is None:
        return RouteDecision(
            UpgradeRoute.FRESH_INSTALL,
            "Could not determine whether a CivicCast (Native) product is installed "
            f"(the {SERVICE_NAME} service query gave no usable answer), so this run "
            "is treated as a FRESH INSTALL -- the safe direction; the install/upgrade "
            f"engine did not run.{tail}",
            data_root_adopted=bool(remnants),
        )

    if not installed_product:
        return RouteDecision(
            UpgradeRoute.FRESH_INSTALL,
            "No CivicCast (Native) product is installed on this machine (the "
            f"{SERVICE_NAME} service is not registered), so there is nothing to "
            "upgrade and the install/upgrade engine is not applicable and did not "
            f"run.{tail}",
            data_root_adopted=bool(remnants),
        )

    if old_version == new_version:
        return RouteDecision(
            UpgradeRoute.SAME_VERSION_NO_OP,
            f"version {new_version} is already the installed version; there is no "
            "migration between a version and itself, so the install/upgrade engine "
            "did not run. Nothing was drained, backed up, migrated, or changed in "
            "the database.",
        )

    # <installer-path-audit MA-05> THE DOWNGRADE GUARD.
    #
    # `decide_route` used to have exactly ONE version comparison -- the string
    # equality above -- and no ordering comparison anywhere. Running a beta.2
    # setup.exe over a beta.3 station therefore routed to UPGRADE and drove
    # `alembic upgrade head` toward an OLDER head: alembic cannot locate the
    # database's current revision in the older build's graph, so the migration
    # fails, the engine rolls back, and (before BL-01) the rollback restore
    # could not succeed either. Refuse before any of that, with a route of its
    # own so the installer can say what happened.
    if is_downgrade(old_version, new_version):
        return RouteDecision(
            UpgradeRoute.REFUSED_DOWNGRADE,
            f"a NEWER version of CivicCast (Native) is installed on this machine "
            f"({old_version}); this setup installs {new_version}. CivicCast does not "
            "support moving a station's database backwards, so the install/upgrade "
            "engine refused before draining, backing up, or migrating anything. "
            "Nothing was changed.",
        )

    return RouteDecision(
        UpgradeRoute.UPGRADE,
        f"an installed CivicCast (Native) product was found (the {SERVICE_NAME} "
        f"service is registered); upgrading {old_version} -> {new_version}.",
    )


__all__ = [
    "NO_RECORDED_VERSION",
    "InstalledProductProbe",
    "RouteDecision",
    "UpgradeRoute",
    "decide_route",
    "default_installed_product_probe",
    "is_downgrade",
]
