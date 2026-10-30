# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Normalize the PostgreSQL data directory's DACL before any INSTALL-TIME
``pg_ctl start`` (row-4b update-path blocker, Sandbox run 21, 2026-08-01).

THE DEFECT THIS EXISTS TO PREVENT
=================================
The documented update path -- uninstall (which preserves the cluster and the
HKLM ``DatabaseUrl`` by design) then reinstall -- died at the D3 upgrade
engine with, verbatim::

    FATAL: could not open file "pg_wal/000000010000000000000002": Permission denied

THE MECHANISM (verified from primary source and reproduced locally; see the
WP report for the full transcript):

1. ``C:\\ProgramData``'s default DACL carries ``CREATOR OWNER:(OI)(CI)(IO)(F)``
   (measured on a real Windows 11 box). Every file created underneath it
   therefore inherits ONE extra ACE naming *whoever created that individual
   file* -- not the product, not a fixed account.
2. D4 provisioning runs ``initdb`` as the elevated INSTALLING USER, so every
   file the fresh cluster is born with carries ``<installing user>:(I)(F)``.
   That is why a fresh install's own ``pg_ctl start`` works.
3. The supervisor service then runs postgres as **LocalSystem** for the life
   of the installation. Every file postgres creates from then on -- crucially
   the new WAL segments -- inherits ``NT AUTHORITY\\SYSTEM:(I)(F)`` instead.
   For the installing user those files are only ``BUILTIN\\Users:(I)(RX)``.
4. Uninstall removes the service but preserves the cluster; the reinstall's
   D3 engine calls :func:`civiccast.native.upgrade.pg_lifecycle.
   real_start_postgres` -> ``pg_ctl start``.
5. ``pg_ctl`` on Windows NEVER launches the postmaster with the caller's own
   token. ``src/common/restricted_token.c`` (PostgreSQL 17,
   ``CreateRestrictedProcess``) calls::

       CreateRestrictedToken(origToken, DISABLE_MAX_PRIVILEGE,
                             2, dropSids /* BUILTIN\\Administrators,
                                            BUILTIN\\Power Users */,
                             0, NULL, 0, NULL, &restrictedToken)

   ``SidsToDisable`` marks those two groups ``SE_GROUP_USE_FOR_DENY_ONLY``,
   so an ALLOW ACE for ``BUILTIN\\Administrators`` grants the postmaster
   NOTHING, and ``DISABLE_MAX_PRIVILEGE`` strips SeBackup/SeRestore/
   SeTakeOwnership as well. The token's own USER SID stays enabled (it is
   not in ``dropSids``, and no restricting SIDs are supplied).

So the install-time postmaster's effective access to a SYSTEM-created WAL
segment is exactly ``BUILTIN\\Users:(RX)`` -- read-only -- and opening it
``O_RDWR`` fails with ``ERROR_ACCESS_DENIED``. The cluster is unstartable by
the installer even though the installer is elevated.

THE FIX
=======
:func:`normalize_pgdata_acl` replaces the data directory's DACL with a
PROTECTED (inheritance-blocked) DACL naming exactly three principals, each
with an inheritable (``OI``/``CI``) full-control ACE, and lets Windows'
own ACL-propagation engine push that set onto every existing child object:

* ``SY`` -- ``NT AUTHORITY\\SYSTEM``: the identity the supervisor service (and
  therefore the production postmaster) runs as. Required.
* ``BA`` -- ``BUILTIN\\Administrators``: the identity the installer,
  uninstaller and any operator repair tool run as. Required for the product
  to be manageable and removable. Deliberately grants the postmaster nothing
  (deny-only in its restricted token, see above).
* the CALLING PROCESS's own user SID -- the identity the install-time
  postmaster actually runs as, and the only one of the three that survives
  ``CreateRestrictedToken`` with any file access. This is the ACE that fixes
  the defect.

SECURITY REASONING FOR WHAT THIS GRANTS (this cluster holds recorded
public-meeting data and the service's own database credential):

* It NARROWS access. The pre-fix DACL was whatever ``C:\\ProgramData``
  inherited down, which includes ``BUILTIN\\Users:(OI)(CI)(RX)`` (every local
  account could READ the cluster files) and ``BUILTIN\\Users:(CI)(WD,AD,...)``
  (every local account could ADD files inside the cluster). Both are removed
  here for the directory itself and for every child whose own DACL is still
  INHERITED (``SetNamedSecurityInfo``'s propagation, measured), and the ``P``
  flag stops them flowing back in on any future re-inheritance at the root.
  MEASURED LIMIT (see :func:`normalize_pgdata_acl`'s docstring and
  ``tests/native/test_pgdata_acl_win.py``): a child that already carries its
  OWN EXPLICIT ``BUILTIN\\Users`` ACE keeps it -- propagation replaces only
  the inherited portion of a child's DACL -- and propagation stops at a
  PROTECTED (``D:P``) child directory, which this call does not detect or
  report. Neither shape is known to occur on a cluster this codebase creates
  (every file postgres/initdb write is inherited-only, never explicitly
  ACL'd), so this is a documented edge, not an expected one. Nothing
  unprivileged ever needs pgdata: the supervisor runs as LocalSystem and
  every application component reaches the database over TCP as a database
  role (grepped: ``postgres_data_dir`` appears only in ``native/provision``,
  ``native/upgrade/pg_lifecycle`` and ``native/supervisor``).
* The one principal ADDED is the installing account, which is by
  construction a local administrator (the installer requires elevation) and
  therefore already had full control of these files via
  ``BUILTIN\\Administrators`` and could have taken ownership at will. Naming
  it explicitly grants no privilege it did not already hold; it only makes
  that access reachable from a token in which ``BUILTIN\\Administrators`` has
  been forced deny-only. It is not a privilege-escalation path.
* Same three-grantee, protected, SDDL shape the provisioning journal's own
  state root already uses (:func:`civiccast.native.provision.journal.
  _harden_state_root_acl`) and the same "SYSTEM + Administrators only"
  convention as ``native_service_registration.rs``'s
  ``SYSTEM_ADMIN_ONLY_SDDL`` -- one house definition of "product-owned
  Windows object", not a new one.

WHY ``SetNamedSecurityInfo`` AND NOT ``icacls``
===============================================
* It must repair EXISTING SYSTEM-created child files, not just the directory.
  ``SetNamedSecurityInfo`` runs Windows' inheritance-propagation engine over
  the subtree; ``win32security.SetFileSecurity`` (what ``journal.py`` uses
  for its single flat directory) does not propagate at all.
* It REPLACES the DACL rather than merging into it, so the result on the
  DIRECTORY ITSELF is exactly the three ACEs regardless of what was there
  before. ``icacls /inheritance:r /grant:r`` only strips INHERITED entries --
  measured locally: a stale EXPLICIT ``BUILTIN\\Users`` ACE survived that
  command and was still granting every local account read access afterwards.
  (This "replace" guarantee is about the target path's own descriptor; what
  reaches PROPAGATED children is the narrower, measured claim in the
  SECURITY REASONING section above.)
* No child process, so it cannot reproduce the install-time subprocess-hang
  defect class this codebase has already been bitten by twice (see
  :mod:`civiccast.native.pg_ctl_exec`), and no locale dependency (well-known
  SID aliases ``SY``/``BA``, never localized account NAMES like
  "Administrators", which do not resolve on a non-English Windows).

Windows-only; a no-op returning ``None`` on any other platform so
``import civiccast.native.pgdata_acl`` and the pure logic here stay testable
on Linux, per the house rule in :mod:`civiccast.native.win_probes`
(``pywin32`` is imported LAZILY for the same reason).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from civiccast.native.service_identity import SERVICE_SID

#: The DACL every install-time postgres data directory is normalized to.
#: ``D:P`` -- protected, so ``C:\ProgramData``'s ``BUILTIN\Users`` entries can
#: never re-inherit. ``OICI`` -- object+container inherit, so every existing
#: child (propagation) and every FUTURE file the LocalSystem service creates
#: carries the same three grantees. See the module docstring for why exactly
#: these three principals and no others.
PGDATA_DACL_SDDL_TEMPLATE = (
    "D:P(A;OICI;GA;;;SY)(A;OICI;GA;;;BA)(A;OICI;GA;;;{caller_sid})"
    f"(A;OICI;0x1301bf;;;{SERVICE_SID})"
)

#: A textual SID as ``ConvertSidToStringSid`` returns it. Validated before it
#: is ever spliced into :data:`PGDATA_DACL_SDDL_TEMPLATE` so a malformed or
#: hostile value can never smuggle extra ACEs into the descriptor.
_SID_RE = re.compile(r"^S-1-\d+(-\d+)+$")

#: Stable token prefixed onto every fault this module raises, so an installer
#: log line can be attributed to this step without parsing the prose (mirrors
#: the ``step d4-provision:`` breadcrumbs ``nsis-hooks-bootstrap.nsh``
#: matches on).
FAILURE_STEP = "pgdata-acl-normalize"

SidReader = Callable[[], str]
DaclApplier = Callable[[str, str], None]


class PgDataAclError(RuntimeError):
    """Fail-loud fault from :func:`normalize_pgdata_acl`.

    Deliberately NOT swallowed anywhere: if the data directory's ACL could
    not be normalized, the very next thing the caller does is ``pg_ctl
    start``, which will either fail with the opaque ``Permission denied``
    this module exists to prevent or -- worse -- succeed while leaving the
    recorded-data cluster readable by every local account. Both outcomes must
    stop the install with a message naming this step, not continue quietly.
    """


def pgdata_dacl_sddl(caller_sid: str) -> str:
    """Render :data:`PGDATA_DACL_SDDL_TEMPLATE` for ``caller_sid``.

    Pure (no Windows, no filesystem) so the exact descriptor this module
    applies is unit-testable on any host OS. Raises :class:`PgDataAclError`
    for anything that is not a textual SID.
    """

    if not _SID_RE.match(caller_sid or ""):
        raise PgDataAclError(
            f"{FAILURE_STEP}: refusing to build a security descriptor from "
            f"{caller_sid!r}, which is not a textual SID (expected 'S-1-...')"
        )
    return PGDATA_DACL_SDDL_TEMPLATE.format(caller_sid=caller_sid)


def _current_process_sid() -> str:
    """The calling process token's user SID as an ``S-1-...`` string.

    Read from the TOKEN, never from ``%USERNAME%``: the environment can be
    stale or spoofed, and an account NAME still has to be resolved (and can
    be ambiguous across a local/domain pair), whereas the token's user SID is
    exactly the principal ``pg_ctl``'s restricted postmaster will present.
    Same primitive :func:`civiccast.native.provision.journal.
    _current_process_sid_sddl` already uses.
    """

    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    sid, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
    return str(win32security.ConvertSidToStringSid(sid))


def _apply_protected_dacl(path: str, sddl: str) -> None:
    """Apply ``sddl``'s DACL to ``path`` as a PROTECTED DACL, propagating it
    to every child object.

    ``SetNamedSecurityInfo`` (not ``SetFileSecurity``) is what runs the
    inheritance-propagation engine over the subtree -- see the module
    docstring. Real Win32 call; unit-tested only through the injected seam in
    :func:`normalize_pgdata_acl`, proven live against a real cluster.
    """

    import win32security

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        sddl, win32security.SDDL_REVISION_1
    )
    win32security.SetNamedSecurityInfo(
        path,
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        descriptor.GetSecurityDescriptorDacl(),
        None,
    )


def normalize_pgdata_acl(
    data_dir: str | Path,
    *,
    sid_reader: SidReader | None = None,
    apply_dacl: DaclApplier | None = None,
) -> str | None:
    """Normalize ``data_dir``'s DACL (module docstring) and return the SDDL
    that was applied; ``None`` on a non-Windows host, where this is a no-op.

    Idempotent and safe to re-run: it SETS an absolute descriptor rather than
    merging deltas, so N calls leave exactly the same DACL as one call (proven
    locally by applying it twice and diffing the resulting descriptors).
    Called at every install-time ``pg_ctl start`` call site this codebase
    actually starts postgres from rather than once at cluster creation, for
    the same self-healing reason ``journal.write_journal`` re-hardens its
    state root on every write: a cluster provisioned by an OLDER build, or by
    a DIFFERENT administrator account, is repaired by the next install
    instead of needing a remediation tool an operator has to know about.
    NOT called on two paths, by design, because neither one starts postgres
    from a state this fix needs to reach: :mod:`civiccast.native.provision`'s
    ``NOOP_REUSE_EXISTING`` branch WHEN the recorded ``DatabaseUrl`` already
    authenticates against a LIVE server (BL-12 gave that branch a schema
    migration; when the supervisor service is already running its own
    postmaster on the cluster -- the state D3's health gate leaves behind on
    a committed upgrade -- the migration runs in place over that connection,
    so nothing here starts, stops or re-ACLs a cluster this process does not
    own; when nothing is listening, D4 does start the cluster itself and this
    normalization DOES run), and
    :func:`civiccast.native.upgrade.pg_lifecycle.wrap_schema_revision`'s
    no-op branch when the database is already reachable (the running
    supervisor service owns that postgres; this call never starts one).
    Correctness is unaffected on those paths -- nothing here needed
    normalizing, or a different code path already owns the running server --
    but be aware the DACL tightening this module exists for simply does not
    run there; it is not a second, silent enforcement point.

    ``sid_reader``/``apply_dacl`` are the injectable seams (production
    defaults: :func:`_current_process_sid` / :func:`_apply_protected_dacl`),
    so the decision logic is fully unit-testable without Windows.

    Every failure to write the DIRECTORY's DACL -- missing directory,
    unreadable token, refused DACL write -- raises :class:`PgDataAclError`.
    Never silent, never best-effort, about that one write: this function (and
    ``SetNamedSecurityInfo`` underneath it) reports success or failure for
    the ROOT call only. Windows' propagation onto existing children happens
    as a side effect of that single call and is not separately observed or
    reported here -- if propagation stops early (a protected child, the
    measured limit above), this function still returns normally, because
    from the Win32 API's own point of view the call it made succeeded. There
    is no per-child result to be silent or best-effort about; it simply
    is not visible at this layer.
    """

    if os.name != "nt":
        return None

    path = Path(data_dir)
    if not path.is_dir():
        raise PgDataAclError(
            f"{FAILURE_STEP}: postgres data directory {str(path)!r} does not exist "
            "(or is not a directory), so its ACL cannot be normalized"
        )

    read_sid = sid_reader if sid_reader is not None else _current_process_sid
    apply = apply_dacl if apply_dacl is not None else _apply_protected_dacl

    try:
        caller_sid = read_sid()
    except PgDataAclError:
        raise
    except Exception as exc:
        raise PgDataAclError(
            f"{FAILURE_STEP}: could not read the calling process's own user SID, so the "
            f"install-time postgres identity for {str(path)!r} is unknown: {exc}"
        ) from exc

    sddl = pgdata_dacl_sddl(caller_sid)

    try:
        apply(str(path), sddl)
    except Exception as exc:
        raise PgDataAclError(
            f"{FAILURE_STEP}: could not apply the normalized DACL {sddl!r} to the postgres "
            f"data directory {str(path)!r}: {exc}"
        ) from exc
    return sddl


__all__ = [
    "FAILURE_STEP",
    "PGDATA_DACL_SDDL_TEMPLATE",
    "PgDataAclError",
    "normalize_pgdata_acl",
    "pgdata_dacl_sddl",
]
