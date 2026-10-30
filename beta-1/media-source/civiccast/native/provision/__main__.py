# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""CLI entry point the native NSIS hook set invokes for live PostgreSQL
provisioning execution (WP2 provision-execution wiring;
``native_service_registration.rs``'s module doc's "DatabaseUrl" STOP section
names this as the missing caller). Mirrors
:mod:`civiccast.native.upgrade.__main__`'s CLI shape: all the logic lives in
the tested engine (:mod:`civiccast.native.provision`), never here. This
module's own job is narrow: generate the database credential, decide whether
a run is even needed (idempotency), assemble the plan/context, drive the
engine against its REAL seam bundle, and hand the resolved ``DatabaseUrl``
back to the Rust caller through a single stdout marker line -- never through
a log, a journal entry, an NSIS variable dump, or this process's own
diagnostic output.

NATS JetStream was removed from the product entirely (owner decision
2026-08-20, ADR 0023 "NATS removed -- in-process event bus", which
supersedes ADR 0001); this CLI no longer accepts NATS flags or resolves NATS
paths.

Invocation (``nsis-hooks-native.nsh``'s ``NSIS_HOOK_POSTINSTALL``)::

    python -m civiccast.native.provision \\
        --install-root "C:\\Program Files\\CivicCast (Native)" \\
        --owner-run-id <run-id> \\
        --pack-signing-key-id <id> --pack-public-key-base64 <base64> \\
        --pack-product-version <v> --pack-compatible-core <v> \\
        [--existing-database-url <value-or-empty>]

Every other flag (``--program-data-root``, ``--server-pack-path``,
``--initdb-path``, hosts/ports, database name/username, PostgreSQL major
version) has a derived or product default (:func:`resolve_provision_paths`)
and exists mainly so tests can override it without touching a real
filesystem layout.

Password handling (credential-sensitive, read this before changing anything
here):

* The password is generated ONLY on the fresh-provisioning path
  (:data:`ProvisionCliAction.RUN`) -- never on a no-op or fail-loud path,
  and never twice for the same cluster.
* It is placed into exactly ONE output surface this process controls: the
  single ``CIVICCAST_DATABASE_URL=...`` line printed to stdout
  (:func:`format_handoff_line`). Every other write this module performs
  (``sys.stderr.write`` progress lines) is static text or a phase name --
  never the password or a URL containing it. Grepping this file for
  ``print(`` finds exactly ONE call site: the DatabaseUrl handoff line. (The
  installer-handoff setup nonce that used to travel the same way was retired
  2026-08-29 -- owner decision -- once the control plane's loopback-only bind
  made it a redundant gate; see ``civiccast.installer.router.
  _require_local_setup_request``.)
* ``civiccast.native.provision.orchestrator`` never writes the password into
  a journal history entry, the recovery document, or a log line -- verified
  by inspection: every ``_persist``/``_halt`` call's ``detail`` string is
  static or built from ``PostgresClusterDecision``/``DatabaseDecision``
  text, which never reference ``context.database_password``.
* CORRECTED 2026-07-30 (municipal-shared-PC hardening fix; the previous
  revision of this bullet claimed the password's on-disk persistence in
  ``ProvisionJournal.context`` was a deliberate, load-bearing design decision
  "needed so a KILLED run resumed by a NEW process can still resolve the
  final ``DatabaseUrl`` on completion" -- an independently re-verified audit
  found the journal directory was NOT actually ACL'd as that claim assumed,
  and by-inspection re-checking of the resume path shows the claim's own
  JUSTIFICATION was also wrong: no resume path ever reads
  ``journal.context.database_password`` back. ``run_provision``'s resume
  branch (``orchestrator._drive_forward(existing, seams)``) drives the
  LOADED journal's phase forward using ``seams`` built in THIS invocation of
  ``main()`` from a FRESHLY generated password (line ~414 below), not the
  journal's persisted one; and this file's own final
  ``resolve_database_url(plan=plan, context=context)`` call (line ~427)
  uses that same fresh, never-reloaded ``context`` too -- never
  ``outcome.journal.context``. The persisted value was therefore dead data
  with a false justification.
  ``civiccast.native.provision.journal.write_journal`` now redacts
  ``context.database_password`` to a fixed, non-secret marker before
  writing the journal to disk (the in-memory object this process holds is
  untouched) and additionally hardens the state root directory's own DACL
  to SYSTEM + Administrators -- see that module's docstring.
* The Rust caller (``native_service_registration::run_native_provision``)
  captures this process's stdout/stderr WITHOUT ever printing it, parses the
  one handoff line, and writes the value straight to
  ``HKLM\\SOFTWARE\\CivicCast\\Native\\DatabaseUrl`` via the existing
  ``write_database_url`` (in-process function call, not a second CLI
  subprocess -- so the password never appears on a second process's argv
  either).
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from civiccast.native.pgdata_acl import PgDataAclError
from civiccast.native.provision.journal import (
    JournalError,
    journal_path,
    load_journal_with_ignored_keys,
)
from civiccast.native.provision.models import (
    ProvisionContext,
    ProvisionJournal,
    ProvisionPhase,
    ProvisionPlan,
    ProvisionSeams,
    resolve_database_url,
)
from civiccast.native.provision.orchestrator import (
    halt_adopt_foreign_cluster,
    halt_resume_credential_lost,
    run_provision,
    write_recovery_document,
)
from civiccast.native.provision.port_select import (
    DEFAULT_PORT_CANDIDATES,
    format_excluded_ranges_for_operator,
    resolve_provision_port,
)
from civiccast.native.provision.seams import (
    AdoptionForeignClusterError,
    build_default_seams_for,
    migrate_provisioned_schema,
    pg_ctl_path_for,
    psql_path_for,
    reset_cluster_credential,
    run_schema_migration_to_head,
)

# ---------------------------------------------------------------------------
# Exit codes -- the installer branches on a number, not stderr text (same
# contract as civiccast.native.upgrade.__main__'s _EXIT_CODES).
# ---------------------------------------------------------------------------

EXIT_SUCCESS = 0  #: freshly provisioned COMPLETE, or a clean no-op reuse
EXIT_PROVISIONING_FAILED = 10  #: engine halted at FAILED; recovery doc emitted
EXIT_REPAIR_NEEDED = 20  #: existing cluster, no registry value -- D5 territory
#: C1 fix (2026-07-31): the engine provisioned cluster/role/database
#: (COMPLETE) but the post-provision schema migration to alembic head
#: failed. A DISTINCT number in the same decade-spaced band as the codes
#: above (and as ``civiccast.native.upgrade.__main__``'s ``_EXIT_CODES``,
#: which also uses 30 for its own fourth outcome) so the installer can name
#: the exact failed step; the handoff line is NOT printed on this path, so
#: the Rust caller never writes a DatabaseUrl for a schema-less database.
EXIT_SCHEMA_MIGRATION_FAILED = 30
EXIT_UNEXPECTED = 40  #: programming/environment fault (e.g. a malformed key)
#: F6 fix (pgdata-DACL audit follow-up, 2026-08-01): migrate_provisioned_schema's
#: SECOND normalize_pgdata_acl call (inside _start_provisioned_cluster, run
#: again here to start the just-provisioned cluster back up for the schema
#: migration) can itself raise PgDataAclError. Before this fix that fell into
#: the same bare ``except Exception`` as an alembic failure below and was
#: misreported as EXIT_SCHEMA_MIGRATION_FAILED with the message "'alembic
#: upgrade head' did not complete" -- wrong step name, and a decision-relevant
#: one: exit 30 (like every code above 0) still suppresses the handoff line,
#: which is CORRECT here (a machine whose data-directory ACL could not be
#: normalized must not advertise a DatabaseUrl), but the next install then
#: reads "no registry value, cluster present" and lands on
#: FAIL_LOUD_CREDENTIAL_LOST (exit 20, documented above as NOT fixed by a
#: repair install) -- a confusing dead end for a defect that is actually a
#: filesystem/ACL permission problem, not a lost credential. Next unused value
#: in the decade-spaced band above (0/10/20/30/40) so the installer can name
#: this exact step distinctly from both.
EXIT_SCHEMA_ACL_NORMALIZATION_FAILED = 50


# ---------------------------------------------------------------------------
# Password generation
# ---------------------------------------------------------------------------

#: 256 bits of entropy -- generous for a machine-generated, never-typed
#: credential. ``secrets.token_urlsafe`` draws from the URL-safe base64
#: alphabet (A-Z a-z 0-9 - _), which is already shell-safe, registry-safe,
#: and contains no character ``build_database_url``'s ``quote(..., safe="")``
#: needs to escape awkwardly -- the provision models' own percent-encoding
#: still runs on top, this charset just avoids gratuitous escaping.
_DEFAULT_PASSWORD_ENTROPY_BYTES = 32
_MIN_PASSWORD_ENTROPY_BYTES = 16  # 128 bits -- refuse anything weaker


def generate_database_password(*, entropy_bytes: int = _DEFAULT_PASSWORD_ENTROPY_BYTES) -> str:
    """A cryptographically random password for the provisioned PostgreSQL
    role. Pure w.r.t. everything except the CSPRNG itself -- no I/O."""

    if entropy_bytes < _MIN_PASSWORD_ENTROPY_BYTES:
        raise ValueError(
            f"entropy_bytes must be at least {_MIN_PASSWORD_ENTROPY_BYTES} "
            f"(128 bits) for a database credential, got {entropy_bytes!r}"
        )
    return secrets.token_urlsafe(entropy_bytes)


# ---------------------------------------------------------------------------
# Handoff marker: the ONLY surface the resolved DatabaseUrl (and therefore
# the password) is ever printed to. A unique prefix on its own line so the
# Rust caller's parser cannot confuse it with any other stdout noise.
# ---------------------------------------------------------------------------

HANDOFF_MARKER_PREFIX = "CIVICCAST_DATABASE_URL="


def format_handoff_line(database_url: str) -> str:
    if "\n" in database_url or "\r" in database_url:
        raise ValueError("database_url must not contain newline characters")
    return f"{HANDOFF_MARKER_PREFIX}{database_url}"


def parse_handoff_line(captured_stdout: str) -> str | None:
    """Find the ONE handoff line among arbitrary captured stdout, mirrored by
    the Rust side's own parser (``native_service_registration::
    parse_provision_handoff``) so both ends agree on the exact same format
    without either importing the other."""

    for line in captured_stdout.splitlines():
        if line.startswith(HANDOFF_MARKER_PREFIX):
            return line[len(HANDOFF_MARKER_PREFIX) :]
    return None


# ---------------------------------------------------------------------------
# Idempotency: the no-op / fail-loud decision matrix (pure, no I/O)
# ---------------------------------------------------------------------------


class ProvisionCliAction(StrEnum):
    """What the CLI does before ever touching a real seam.

    | cluster exists | registry value present | journal state                        | action                     |
    |-----------------|-------------------------|----------------------------------------|----------------------------|
    | no               | (irrelevant)            | (irrelevant)                           | RUN (fresh provisioning)   |
    | yes              | yes                     | (irrelevant)                           | NOOP_REUSE_EXISTING        |
    | yes              | no                      | resumable (non-terminal, pre-credential) | RUN (resume via journal) |
    | yes              | no                      | credential lost (non-terminal, at/past POSTGRES_CLUSTER_READY) | FAIL_LOUD_CREDENTIAL_LOST |
    | yes              | no                      | none / terminal                        | ADOPT_EXISTING (N-15)      |

    N-15: the last row USED to be ``FAIL_LOUD_MISSING_REGISTRY`` -- but a
    surviving cluster with no registry value and no in-flight journal is the
    exact, expected state uninstall leaves behind: chain M / security fix F-02
    deletes the ``DatabaseUrl`` credential while PRESERVING the product data
    directory (so the credential lives nowhere and cannot be reconstructed),
    yet the installer already promises the operator that "your recorded data
    and database are preserved by uninstall and will be adopted by the new
    installation". Refusing here broke that promise on every
    uninstall-then-reinstall. ``ADOPT_EXISTING`` keeps it: the CLI
    re-establishes a FRESH credential on the SAME cluster (no initdb, no
    database drop -- station data preserved) via
    :func:`~civiccast.native.provision.seams.reset_cluster_credential`, whose
    own LIVE ownership check fails loud
    (:class:`~civiccast.native.provision.seams.AdoptionForeignClusterError` ->
    :func:`~civiccast.native.provision.orchestrator.halt_adopt_foreign_cluster`)
    on a cluster the product did not create -- so "never touch a foreign
    cluster" is preserved, moved to where it can be verified against the live
    cluster instead of guessed offline. ``FAIL_LOUD_MISSING_REGISTRY`` remains
    as the label that foreign-cluster refusal is reported under.

    On a re-install over an existing cluster, the registry's ``DatabaseUrl``
    is the SOURCE OF TRUTH (task instruction): reusing it means the CLI never
    regenerates a password it cannot re-derive, and never re-runs ``initdb``
    over a cluster the orchestrator's own idempotency logic would refuse to
    touch anyway.

    Task #55 (audit-lite FINDING-003): a cluster with NO registry value is
    NOT automatically "an unrelated, previously-successful cluster needing a
    repair install" -- it is exactly what an INTERRUPTED provisioning run
    also leaves behind (``initdb`` at ``POSTGRES_CLUSTER_READY`` succeeded,
    but the run was killed -- process kill, power loss, AV interference,
    a Sandbox timeout -- before ever reaching the end and handing off
    ``DatabaseUrl`` to the Rust caller). :func:`probe_resumable_journal`
    distinguishes the two: a present, non-terminal
    (:class:`~civiccast.native.provision.models.ProvisionPhase`) journal at
    this ``state_root`` is proof a run is genuinely IN PROGRESS, not that
    this is a foreign cluster -- that case now resumes via
    :func:`~civiccast.native.provision.orchestrator.run_provision`'s own
    idempotent journal-replay (each forward step re-detects and reuses
    already-landed state) instead of being misdirected to
    ``FAIL_LOUD_MISSING_REGISTRY``. A cluster with NO registry value AND no
    resumable journal is still not a case this CLI may guess its way out of
    -- it surfaces as a fail-loud condition requiring operator repair (D5's
    territory), never a silent regenerate-and-hope.

    Task #57 (disclosed in commit abdba55b): resuming is only SAFE while the
    journal is still BEFORE ``POSTGRES_CLUSTER_READY`` -- at or past that
    phase, ``initdb --pwfile`` already baked a real credential into the
    cluster, and the RUN branch would drive forward with a FRESHLY generated
    (therefore different, and never reconciled) password
    (:func:`probe_credential_lost_journal` detects this, distinct from
    :func:`probe_resumable_journal`). ``FAIL_LOUD_CREDENTIAL_LOST`` halts
    that case instead of silently misdirecting it into the RUN branch --
    see :func:`~civiccast.native.provision.orchestrator.
    halt_resume_credential_lost` for the honest, situation-specific recovery
    document it writes (never the generic "needs a repair install" message,
    which does not describe or fix this state).
    """

    RUN = "run"
    NOOP_REUSE_EXISTING = "noop_reuse_existing"
    ADOPT_EXISTING = "adopt_existing"
    FAIL_LOUD_MISSING_REGISTRY = "fail_loud_missing_registry"
    FAIL_LOUD_CREDENTIAL_LOST = "fail_loud_credential_lost"


def decide_provision_cli_action(
    *,
    cluster_exists: bool,
    existing_database_url: str | None,
    journal_resumable: bool = False,
    credential_lost: bool = False,
    reused_database_url_usable: bool | None = None,
) -> ProvisionCliAction:
    """Choose the provisioning action for the observed machine state.

    ``reused_database_url_usable`` (installer-path audit MA-32) is the
    outcome of actually TRYING the registry's ``DatabaseUrl`` against a
    listening server:

    * ``True``  -- a connection succeeded; the reused value is real.
    * ``None``  -- the question could not be asked, almost always because the
      cluster is not running yet (D4 provisioning runs before the service
      starts, so this is the ORDINARY case on a healthy reinstall). Behaviour
      is unchanged from before this parameter existed: reuse the value.
    * ``False`` -- the server IS listening and rejected the credential, or the
      database it names does not exist. THAT is the state the old code
      accepted as "already provisioned": ``bool(existing_database_url.strip())``
      with no connection attempt, so a ``%ProgramData%\\CivicCast`` restored
      from a different station, a hand-edited or partially-written
      ``DatabaseUrl``, or a cluster whose password was reset out of band all
      produced a no-op, an exit 0, and a control plane that could not
      authenticate to its own database. Adopt the surviving cluster instead
      and re-establish a credential on it (which never drops data --
      ``reset_cluster_credential``).

    The default is ``None`` so every existing caller and test keeps its
    previous meaning explicitly rather than by omission.
    """

    if not cluster_exists:
        return ProvisionCliAction.RUN
    has_registry_value = bool(existing_database_url and existing_database_url.strip())
    if has_registry_value:
        if reused_database_url_usable is False:
            return ProvisionCliAction.ADOPT_EXISTING
        return ProvisionCliAction.NOOP_REUSE_EXISTING
    if journal_resumable:
        return ProvisionCliAction.RUN
    if credential_lost:
        return ProvisionCliAction.FAIL_LOUD_CREDENTIAL_LOST
    # N-15: a cluster that EXISTS with NO registry value and NO in-flight
    # (resumable / credential-lost) journal is the exact state uninstall
    # leaves behind BY DESIGN -- chain M / security fix F-02 deletes the
    # DatabaseUrl credential while deliberately PRESERVING the product data
    # directory (station data). This used to FAIL_LOUD_MISSING_REGISTRY, which
    # aborted every uninstall-then-reinstall over preserved data; it now ADOPTS
    # the surviving cluster (re-establishing a fresh credential on it WITHOUT
    # re-initializing or dropping any data -- see reset_cluster_credential).
    # The "is this genuinely the product's own cluster, or a foreign one at
    # the same path?" question is NOT guessed offline here: it is answered by a
    # LIVE ownership check inside the adoption seam, which fails loud
    # (AdoptionForeignClusterError) rather than ever taking over foreign data.
    return ProvisionCliAction.ADOPT_EXISTING


#: <installer-path-audit BL-12> A placeholder, NOT a credential.
#:
#: ``migrate_provisioned_schema`` reads only the data directory, the
#: port/host, the state root and the owner run id off the ``ProvisionContext``
#: it is handed; the connection it migrates over comes from
#: ``--existing-database-url``, which already carries the real password. The
#: reuse path must therefore construct a context WITHOUT generating a
#: password -- creating one there would be a credential that run has no
#: business creating, on a path whose whole contract is "touch nothing".
#: Named rather than inlined so it is greppable and so the linter's
#: hardcoded-password rule is answered once, here, with the reason.
_MIGRATION_ONLY_UNUSED_PASSWORD = "not-a-credential-see-BL-12"  # noqa: S105  # nosec B105


def probe_reused_database_url(database_url: str | None) -> bool | None:
    """Is the registry's ``DatabaseUrl`` actually usable right now?

    Installer-path audit MA-32. Returns exactly the tri-state
    :func:`decide_provision_cli_action`'s ``reused_database_url_usable``
    documents, and never raises.

    The two-step shape is load-bearing and is why this is not simply a
    ``SELECT 1``. D4 provisioning runs BEFORE the supervisor service starts,
    so on an ordinary healthy reinstall the cluster is not listening at all --
    a bare connection attempt would fail on every machine and turn every
    reinstall into an adoption (which resets the credential). So:

    1. Open a plain TCP socket to the URL's host/port. Connection refused or
       an unparseable URL means the question cannot be asked -> ``None``, and
       the caller's behaviour is exactly what it was before this probe
       existed.
    2. Only when something IS listening there, try the credential. A refusal
       from a live server is a real, actionable answer -> ``False``.
    """

    if not database_url or not database_url.strip():
        return None
    try:
        from civiccast.db.url import normalize_database_url

        normalized = normalize_database_url(database_url.strip())
        parts = urlsplit(normalized)
        host = parts.hostname
        port = parts.port or 5432
    except Exception:
        return None
    if not host:
        return None

    try:
        with socket.create_connection((host, port), timeout=3):
            pass
    except OSError:
        # Nothing is listening (the normal pre-service-start case) -- the
        # credential question cannot be asked, so do not answer it.
        return None

    try:
        from sqlalchemy import create_engine, text

        from civiccast.db import connect_options

        engine = create_engine(normalized, poolclass=None, **connect_options(normalized))
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()
    except Exception:
        # A LIVE server refused this credential, or the database it names does
        # not exist. Either way the registry value is not the working value
        # this station needs.
        return False
    return True


def probe_cluster_exists(postgres_data_dir: str) -> bool:
    """A CLI-level pre-check, separate from (and cheaper than) the
    orchestrator's own :func:`~civiccast.native.provision.models.
    evaluate_postgres_cluster` -- this one only answers "does *anything*
    live here yet", to decide whether the CLI should even attempt a run at
    all, before any password is generated."""

    return (Path(postgres_data_dir) / "PG_VERSION").exists()


def probe_resumable_journal(state_root: str) -> bool:
    """Task #55 (audit-lite FINDING-003), narrowed by task #57: True iff
    ``state_root`` holds a valid, NON-TERMINAL provisioning journal whose
    phase is still BEFORE ``POSTGRES_CLUSTER_READY`` -- i.e. a PRIOR
    invocation of this CLI got partway through provisioning but was
    interrupted before ``initdb`` ever ran, so a freshly generated password
    is still safe to drive forward with.

    This is the CLI-level signal that distinguishes an interrupted
    mid-provisioning retry from a genuinely foreign PostgreSQL cluster living
    at the same data directory: only the former should resume via
    :func:`~civiccast.native.provision.orchestrator.run_provision`; a
    ``COMPLETE``/``FAILED`` terminal journal, or no journal at all, still
    leaves the caller to fall back to the registry-value check
    (:func:`decide_provision_cli_action`).

    Task #57 (disclosed in commit abdba55b): a non-terminal journal AT OR
    PAST ``POSTGRES_CLUSTER_READY`` used to also count as "resumable" here,
    but resuming that one regenerates a password the already-initialized
    cluster's REAL credential (set once, by ``initdb --pwfile``, at that
    phase) will reject. That case is no longer "resumable" at all -- see
    :func:`probe_credential_lost_journal`, which recognizes it and gives the
    caller an honest, distinct halt instead.

    Propagates :class:`~civiccast.native.provision.journal.JournalError`
    UNCHANGED for a present-but-corrupt journal -- a journal this engine
    cannot trust must never be silently treated as either resumable or not
    (house rule: "fail-loud on any unexpected state, never silently
    repair" -- :mod:`civiccast.native.provision.orchestrator`'s module
    docstring); the caller decides how to surface that fault.
    """

    from civiccast.native.provision.journal import load_journal

    journal = load_journal(state_root)
    return (
        journal is not None
        and not journal.phase.is_terminal
        and journal.phase.rank < ProvisionPhase.POSTGRES_CLUSTER_READY.rank
    )


def probe_credential_lost_journal(state_root: str) -> ProvisionJournal | None:
    """Task #57 (disclosed in commit abdba55b): the companion probe to
    :func:`probe_resumable_journal` for the ONE other non-terminal case that
    function no longer treats as safely resumable -- a journal AT OR PAST
    ``POSTGRES_CLUSTER_READY``. Returns the loaded journal (so the caller can
    hand it straight to :func:`~civiccast.native.provision.orchestrator.
    halt_resume_credential_lost` without a second disk read) when that
    condition holds, else ``None`` (no journal, a terminal journal, or a
    journal still safely resumable via :func:`probe_resumable_journal`).

    Propagates :class:`~civiccast.native.provision.journal.JournalError`
    UNCHANGED for a present-but-corrupt journal, exactly like
    :func:`probe_resumable_journal`.
    """

    from civiccast.native.provision.journal import load_journal

    journal = load_journal(state_root)
    if (
        journal is not None
        and not journal.phase.is_terminal
        and journal.phase.rank >= ProvisionPhase.POSTGRES_CLUSTER_READY.rank
    ):
        return journal
    return None


# ---------------------------------------------------------------------------
# Path derivation
# ---------------------------------------------------------------------------

_PROGRAM_DATA_SUBDIR = "CivicCast"


@dataclass(frozen=True)
class ProvisionPaths:
    program_data_root: str
    state_root: str
    postgres_data_dir: str
    postgres_config_path: str
    postgres_hba_path: str
    server_pack_path: str
    initdb_path: str


def resolve_provision_paths(
    *,
    install_root: str,
    program_data_root: str | None = None,
    server_pack_path: str | None = None,
    initdb_path: str | None = None,
) -> ProvisionPaths:
    """Derive every filesystem location a provisioning run needs from two
    roots: ``install_root`` (Program Files) and ``program_data_root``
    (ProgramData, default the ``PROGRAMDATA`` env var -- the SAME convention
    :func:`civiccast.native.supervisor.children.default_egress_work_dir` and
    :data:`civiccast.native.supervisor.service.DEFAULT_LOG_ROOT` already
    use). String-built, not ``pathlib.Path``, for the same reason
    ``default_egress_work_dir`` gives: the Windows-style backslash path must
    be identical regardless of the host OS running this pure module's tests.

    PostgreSQL data lives under the ``<program_data_root>\\CivicCast\\data``
    tree (alongside the existing egress work dir at ``...\\data\\egress``),
    so a single ACL boundary covers all of it. ``postgresql.conf``/
    ``pg_hba.conf`` MUST live INSIDE
    ``postgres_data_dir`` -- not a style choice: ``postgres_child_spec`` in
    :mod:`civiccast.native.supervisor.children` launches ``pg_ctl start -D
    <data_dir> -w`` with no ``-c config_file=`` override, so PostgreSQL only
    ever reads its default ``$PGDATA/postgresql.conf``/``$PGDATA/pg_hba.conf``
    locations; writing the provisioned config anywhere else would be
    silently ignored by the real server.

    ``server_pack_path``/``initdb_path`` default to a
    ``packs\\native-server-binaries\\`` convention under ``install_root`` --
    a CODER DECISION, not a spec-pinned value: no earlier work package
    established where an extracted server-binaries pack lands on disk (the
    existing component-pack acquisition flow, ``--civiccast-acquire-channel``
    / ``--civiccast-import-station``, is an operator/cache-root-driven step
    with no fixed staged-directory convention of its own yet -- see
    ``nsis-hooks-native.nsh``'s own comment on this). Documented here and in
    the WP2 evidence file so a later work package that establishes a
    different pack-staging convention has one place to change it; both
    parameters remain overridable for exactly that reason.
    """

    pd_root = (program_data_root or os.environ.get("PROGRAMDATA", r"C:\ProgramData")).rstrip("\\/")
    civiccast_root = f"{pd_root}\\{_PROGRAM_DATA_SUBDIR}"
    postgres_data_dir = f"{civiccast_root}\\data\\pgdata"

    install = install_root.rstrip("\\/")
    default_server_pack_path = f"{install}\\packs\\native-server-binaries.ccpack"
    default_initdb_path = f"{install}\\packs\\native-server-binaries\\payload\\bin\\initdb.exe"

    return ProvisionPaths(
        program_data_root=pd_root,
        state_root=f"{civiccast_root}\\provision",
        postgres_data_dir=postgres_data_dir,
        postgres_config_path=f"{postgres_data_dir}\\postgresql.conf",
        postgres_hba_path=f"{postgres_data_dir}\\pg_hba.conf",
        server_pack_path=server_pack_path or default_server_pack_path,
        initdb_path=initdb_path or default_initdb_path,
    )


# ---------------------------------------------------------------------------
# N-16 (fleet-tester candidate 99db2c6, soak/INSTALL-FAILED.md /
# soak/evidence-provision-failure/): adopted-journal staleness.
#
# Uninstall preserves ProgramData -- including this CLI's own provisioning
# journal -- by design (see ProvisionCliAction.ADOPT_EXISTING's N-15 doc).
# That is correct for the journal's DATA-lifecycle fields (nothing here ever
# re-derives ``postgres_data_dir``/``postgres_config_path``/
# ``postgres_hba_path``, all of which live under ``program_data_root`` and
# stay fixed regardless of where the product's OWN files are installed). It
# is WRONG for ``context.server_pack_path``: that is the one journal field
# derived from ``install_root`` (Program Files, or wherever ``/D=`` pointed a
# given run), and a later install to a DIFFERENT ``install_root`` over the
# SAME preserved ProgramData leaves the adopted journal naming a pack path
# under the OLD location -- which no longer exists once the candidate lives
# somewhere else. Nothing downstream re-validated that path against THIS
# run's install root before trusting it: a TERMINAL (COMPLETE/FAILED) journal
# short-circuits ``run_provision`` without ever touching a seam at all (see
# that function's own docstring -- "left alone on rerun"), and even a
# non-terminal one would drive the recorded (stale) path forward.
#
# Live-diagnosed (read-only, not reproduced against real files -- see
# soak/INSTALL-FAILED.md's own "no preserved data or candidate files were
# modified to test that inference" caveat) on candidate 99db2c6: a fresh
# install to ``C:\CivicCastHostStore\install`` after an uninstall that
# preserved ``C:\ProgramData\CivicCast`` adopted a 2026-08-16 journal whose
# ``server_pack_path`` still named
# ``C:\Program Files\CivicCast (Native)\packs\native-server-binaries.ccpack``
# -- a path Program Files no longer had (only a residual ``uninstall.exe``
# was left there), while the real candidate pack sat under the NEW install
# root instead. Provisioning returned rc 75.
# ---------------------------------------------------------------------------


def journal_stale_reason(existing: ProvisionJournal, *, paths: ProvisionPaths) -> str | None:
    """Return why the ADOPTED ``existing`` journal is stale relative to THIS
    run's ``paths``, or ``None`` if it is still trustworthy.

    Deliberately checks ONLY ``server_pack_path`` -- the sole journal
    ``context`` field :func:`resolve_provision_paths` derives from
    ``install_root`` (every sibling recorded artifact path --
    ``postgres_data_dir``/``postgres_config_path``/``postgres_hba_path``/
    ``state_root`` -- derives from ``program_data_root`` instead, which does
    not vary with where the product's own files are installed, so a mismatch
    there would mean something else entirely: a different ProgramData root,
    not a stale journal). Does NOT independently probe whether the
    RE-DERIVED (current-run) ``server_pack_path`` exists on disk -- that is
    :func:`~civiccast.native.provision.seams.build_default_seams_for`'s
    ``verify_pack`` seam's job, which already fails loud (and, after this
    same fix, always leaves an operator recovery document) when re-derivation
    still can't find a real pack. This function only answers "is the
    RECORDED path still describing THIS run", not "does a pack exist" --
    conflating the two would make every adoption test that fakes pack
    verification (no real ``.ccpack`` staged on the test host) look stale.
    """

    recorded = existing.context.server_pack_path
    expected = paths.server_pack_path
    if recorded != expected:
        return (
            f"the adopted journal's server_pack_path ({recorded!r}) does not match "
            f"this run's install root -- expected {expected!r}. This is the exact "
            "signature of an install to a DIFFERENT location (e.g. a custom /D= "
            "directory) over a ProgramData tree preserved from a PRIOR install at "
            "the OLD location."
        )
    return None


# ---------------------------------------------------------------------------
# Pack public key decoding (a PUBLIC key -- not credential-sensitive; safe to
# pass on argv, unlike the password)
# ---------------------------------------------------------------------------

_ED25519_PUBLIC_KEY_BYTES = 32


def decode_pack_public_key(b64_value: str) -> Ed25519PublicKey:
    import base64

    raw = base64.b64decode(b64_value, validate=True)
    if len(raw) != _ED25519_PUBLIC_KEY_BYTES:
        raise ValueError(
            f"pack public key must be exactly {_ED25519_PUBLIC_KEY_BYTES} bytes, got {len(raw)}"
        )
    return Ed25519PublicKey.from_public_bytes(raw)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m civiccast.native.provision",
        description=(
            "Run the journaled CivicCast (Native) PostgreSQL provisioning engine (spec D4)."
        ),
    )
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--owner-run-id", required=True)
    parser.add_argument("--pack-signing-key-id", required=True)
    parser.add_argument("--pack-public-key-base64", required=True)
    parser.add_argument("--pack-product-version", required=True)
    parser.add_argument("--pack-compatible-core", required=True)
    parser.add_argument(
        "--existing-database-url",
        default="",
        help="The CURRENT HKLM DatabaseUrl value (empty on a fresh install).",
    )
    parser.add_argument("--program-data-root", default=None)
    parser.add_argument("--server-pack-path", default=None)
    parser.add_argument("--initdb-path", default=None)
    parser.add_argument("--postgres-host", default="127.0.0.1")
    parser.add_argument("--postgres-port", type=int, default=5432)
    parser.add_argument("--database-name", default="civiccast")
    parser.add_argument("--database-username", default="civiccast_svc")
    parser.add_argument("--postgres-major-version", default="17")
    return parser


# ---------------------------------------------------------------------------
# Plan / context assembly (pure)
# ---------------------------------------------------------------------------


def build_plan_and_context(
    *, paths: ProvisionPaths, args: argparse.Namespace, database_password: str
) -> tuple[ProvisionPlan, ProvisionContext]:
    plan = ProvisionPlan(
        postgres_major_version=args.postgres_major_version,
        database_name=args.database_name,
        database_username=args.database_username,
        server_pack_product_version=args.pack_product_version,
        server_pack_compatible_core=args.pack_compatible_core,
        server_pack_signing_key_id=args.pack_signing_key_id,
    )
    context = ProvisionContext(
        postgres_host=args.postgres_host,
        postgres_port=args.postgres_port,
        postgres_data_dir=paths.postgres_data_dir,
        postgres_config_path=paths.postgres_config_path,
        postgres_hba_path=paths.postgres_hba_path,
        database_password=database_password,
        server_pack_path=paths.server_pack_path,
        state_root=paths.state_root,
        owner_run_id=args.owner_run_id,
    )
    return plan, context


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def _run_engine_and_finish(
    *,
    paths: ProvisionPaths,
    args: argparse.Namespace,
    plan: ProvisionPlan,
    context: ProvisionContext,
    # Was `object`, which made the run_provision call below an arg-type
    # error and, worse, meant every seams.* attribute access in this
    # function was unchecked. The only caller passes what
    # build_default_seams_for returns.
    seams: ProvisionSeams,
) -> int:
    """Drive the journaled provisioning engine to COMPLETE, migrate the schema
    to alembic head, and print the DatabaseUrl + setup-nonce handoff lines.

    Shared verbatim by the RUN (fresh install) and ADOPT_EXISTING (N-15
    reinstall-over-preserved-data) paths: both reach this point with a fresh
    ``database_password`` already established for ``context`` -- RUN via
    ``initdb --pwfile`` (baked when the cluster is created), ADOPT via
    :func:`~civiccast.native.provision.seams.reset_cluster_credential` (set on
    the surviving cluster before this runs). Because the ADOPT path's cluster
    already exists, the engine here DETECTS and REUSES it (no initdb) and
    DETECTS and REUSES its database (no CREATE) -- station data is preserved;
    the only difference from RUN is that no data was created, only adopted.
    """

    outcome = run_provision(plan, context, seams)
    sys.stderr.write(f"provision outcome: {outcome.phase.value}\n")
    if not outcome.ok:
        return EXIT_PROVISIONING_FAILED

    database_url = resolve_database_url(plan=plan, context=context)

    # C1 fix (2026-07-31): the engine created cluster/role/database but NO
    # tables -- on a first-ever install the NSIS chain's D3 upgrade engine
    # (the product's only other alembic runner) is skipped by design
    # ("fresh-install gate"), so this is the ONE place the fresh schema can
    # be brought to alembic head. Runs the same in-process migration
    # mechanism D3 uses (see run_schema_migration_to_head), idempotently,
    # against the real freshly-provisioned DatabaseUrl. Fail-loud with its
    # own step-identifying exit code; the handoff line is NOT printed on
    # failure, so no registry value is ever written for a schema-less DB.
    # (Idempotent, so the ADOPT path's already-migrated database is a safe
    # no-op here too.)
    try:
        migrate_provisioned_schema(
            context,
            pg_ctl_path=pg_ctl_path_for(paths.initdb_path),
            database_url=database_url,
            install_root=args.install_root,
        )
    except PgDataAclError as exc:
        # F6 (audit follow-up): migrate_provisioned_schema's own
        # normalize_pgdata_acl call (inside _start_provisioned_cluster,
        # re-starting the just-provisioned cluster for the migration) failed
        # before 'pg_ctl start' -- never even reached alembic. Caught
        # DISTINCTLY, ahead of the generic handler below, so this is never
        # misreported as a schema-migration/alembic failure: same
        # credential-redaction treatment (defensive here too, since
        # PgDataAclError's own message is static/path-only and does not
        # embed the URL or password), its own step-identifying exit code,
        # and the handoff line is still correctly suppressed -- a machine
        # whose pgdata ACL could not be normalized must not advertise a
        # DatabaseUrl.
        detail = str(exc).replace(database_url, "[database-url redacted]")
        detail = detail.replace(context.database_password, "[password redacted]")
        write_recovery_document(
            context.state_root,
            reason=(
                "the database was provisioned but its data directory's ACL could not be "
                f"normalized before restarting the cluster for schema migration: {detail}"
            ),
            attempting="schema_migration (pgdata ACL normalization)",
            next_steps=[
                "PostgreSQL cluster/role/database were successfully provisioned -- this "
                "failure is NOT a lost or foreign cluster. Only the data directory's ACL "
                "normalization before restarting for schema migration failed, so "
                "'alembic upgrade head' was never attempted.",
                f"Data directory: {context.postgres_data_dir}",
                "Check filesystem permissions on the data directory (the service account "
                "must be able to set its own DACL there) before retrying.",
                "This provisioning run will not be silently retried. Once the permission "
                "issue is resolved, run the installer again.",
                f"Preserve this journal for support: {journal_path(context.state_root)}",
            ],
        )
        sys.stderr.write(
            f"provision outcome: schema_acl_normalization_failed (the database was "
            f"provisioned but its data directory's ACL could not be normalized before "
            f"restarting the cluster for schema migration -- 'alembic upgrade head' was "
            f"never attempted: {detail})\n"
        )
        return EXIT_SCHEMA_ACL_NORMALIZATION_FAILED
    except Exception as exc:
        # Never let the generated credential reach stderr: SQLAlchemy/
        # alembic error text can embed the connection URL. Static prefix +
        # redacted detail only.
        detail = str(exc).replace(database_url, "[database-url redacted]")
        detail = detail.replace(context.database_password, "[password redacted]")
        write_recovery_document(
            context.state_root,
            reason=(
                f"the database was provisioned but 'alembic upgrade head' did not complete: {detail}"
            ),
            attempting="schema_migration",
            next_steps=[
                "PostgreSQL cluster/role/database were successfully provisioned -- this "
                "failure is NOT a lost or foreign cluster. Only the post-provision schema "
                "migration to alembic head failed.",
                f"Data directory: {context.postgres_data_dir}",
                "Read the failure detail recorded above; a common cause is the migration "
                "target already holding a schema at an unexpected revision -- do not hand-"
                "edit the database without a verified backup, escalate to support instead.",
                "This provisioning run will not be silently retried. Once the blocking "
                "condition is resolved, run the installer again (idempotent -- already-"
                "applied migrations are skipped).",
                f"Preserve this journal for support: {journal_path(context.state_root)}",
            ],
        )
        sys.stderr.write(
            f"provision outcome: schema_migration_failed (the database was provisioned "
            f"but 'alembic upgrade head' did not complete: {detail})\n"
        )
        return EXIT_SCHEMA_MIGRATION_FAILED
    sys.stderr.write("provision outcome: schema migrated to alembic head\n")

    print(format_handoff_line(database_url))
    return EXIT_SUCCESS


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    paths = resolve_provision_paths(
        install_root=args.install_root,
        program_data_root=args.program_data_root,
        server_pack_path=args.server_pack_path,
        initdb_path=args.initdb_path,
    )

    # N-16: an adopted journal (ProgramData preserved by uninstall) whose
    # recorded server_pack_path no longer matches THIS run's install root is
    # stale -- see journal_stale_reason's docstring. Checked FIRST, before
    # anything (including run_provision's own terminal-journal short-circuit)
    # ever trusts it, so a fresh run always re-derives every path from the
    # CURRENT install location rather than a prior one. Only the provisioning
    # bookkeeping file is reset here -- the preserved PostgreSQL data
    # directory (station data) is never touched by this.
    try:
        existing_journal, ignored = load_journal_with_ignored_keys(paths.state_root)
    except JournalError as exc:
        write_recovery_document(
            paths.state_root,
            reason=f"the provisioning journal at {paths.state_root!r} is corrupt/unparseable: {exc}",
            attempting="reading the adopted provisioning journal",
        )
        sys.stderr.write(
            "provision outcome: unexpected error reading the provisioning "
            f"journal at {paths.state_root!r}: {exc}\n"
        )
        return EXIT_UNEXPECTED
    if existing_journal is not None:
        # beta.5.1: an adopted journal may carry keys this version's models do
        # not declare (the August 2026 installers' context.nats_*; or a newer
        # installer's fields on a reinstall over preserved ProgramData).
        # The load above drops them and hands back their names, computed
        # from the same bytes it parsed (no second read of the file that
        # could escape as an unguarded OSError). Say which ones ONCE per run,
        # here, on this CLI's own stderr (this CLI configures no logging, so
        # load_journal's INFO line reaches nothing in production). Not per
        # load: the probes below and run_provision each load this same file
        # again. Where the line ends up: the installer's Rust wrapper
        # (run_native_provision, Command::output()) captures and deliberately
        # does not forward this stream, so the note is NOT yet in
        # install-progress.log; forwarding it is a follow-up tracked with the
        # runtime-ownership diagnosability PR.
        if ignored:
            sys.stderr.write(
                f"provision note: adopted provisioning journal at {paths.state_root!r} "
                f"carries {len(ignored)} field(s) this version does not declare; ignored "
                f"(legacy or newer-installer keys, not corruption): {', '.join(ignored)}\n"
            )
        stale_reason = journal_stale_reason(existing_journal, paths=paths)
        if stale_reason is not None:
            sys.stderr.write(
                f"provision outcome: adopted provisioning journal at {paths.state_root!r} "
                f"is STALE ({stale_reason}); resetting it so this run re-derives every path "
                "from the CURRENT install location. The preserved PostgreSQL data directory "
                "is NOT touched by this reset -- only the provisioning-run bookkeeping "
                "journal is cleared.\n"
            )
            journal_path(paths.state_root).unlink(missing_ok=True)

    cluster_exists = probe_cluster_exists(paths.postgres_data_dir)

    # Task #55 (audit-lite FINDING-003), narrowed by task #57: only consulted
    # in the ONE branch it can change (cluster exists, no registry value) --
    # never probed on a fresh install (cluster_exists=False, always RUN
    # regardless) or a registry-value reinstall (NOOP_REUSE_EXISTING, which
    # never touches the journal today either), so a stray/corrupt journal at
    # this state_root cannot regress either of those two already-correct
    # paths.
    journal_resumable = False
    credential_lost_journal: ProvisionJournal | None = None
    has_registry_value = bool(args.existing_database_url and args.existing_database_url.strip())
    if cluster_exists and not has_registry_value:
        try:
            journal_resumable = probe_resumable_journal(paths.state_root)
            if not journal_resumable:
                # Only the credential-lost probe needs a second read, and
                # only when the first one already found nothing safely
                # resumable -- a corrupt journal already raised above.
                credential_lost_journal = probe_credential_lost_journal(paths.state_root)
        except JournalError as exc:
            write_recovery_document(
                paths.state_root,
                reason=(
                    f"the provisioning journal at {paths.state_root!r} is corrupt/unparseable: {exc}"
                ),
                attempting="classifying the adopted provisioning journal (resumable vs credential-lost)",
            )
            sys.stderr.write(
                "provision outcome: unexpected error reading the provisioning "
                f"journal at {paths.state_root!r}: {exc}\n"
            )
            return EXIT_UNEXPECTED

    # <installer-path-audit MA-32> "Already provisioned" used to be the mere
    # PRESENCE of a registry string -- no connection attempt, no credential
    # check -- after which the installer printed "provisioning complete (or
    # already provisioned; no-op)" and exited 0 over a control plane that
    # could not authenticate to its own database. See
    # probe_reused_database_url for why this is a two-step probe rather than
    # a bare SELECT 1.
    reused_usable = probe_reused_database_url(args.existing_database_url)

    action = decide_provision_cli_action(
        cluster_exists=cluster_exists,
        existing_database_url=args.existing_database_url,
        journal_resumable=journal_resumable,
        credential_lost=credential_lost_journal is not None,
        reused_database_url_usable=reused_usable,
    )

    if reused_usable is False:
        sys.stderr.write(
            "provision note: the DatabaseUrl recorded in HKLM\\SOFTWARE\\CivicCast\\Native was "
            "rejected by the PostgreSQL server that is listening at its host/port (bad "
            "credential, or the database it names does not exist), so it is NOT reused. "
            "Adopting the surviving cluster and re-establishing a credential on it instead; "
            "no data is dropped.\n"
        )

    if action is ProvisionCliAction.NOOP_REUSE_EXISTING:
        # <installer-path-audit BL-12> THE SCHEMA STILL HAS TO BE BROUGHT TO
        # HEAD.
        #
        # This branch returned EXIT_SUCCESS here, having touched nothing. That
        # is correct for the CREDENTIAL and the CLUSTER -- both are reused
        # as-is, deliberately -- but it left the one path through this CLI
        # that never migrates. Combined with D3 routing FRESH_INSTALL whenever
        # the supervisor service is absent (which is exactly what an uninstall
        # leaves behind, while %ProgramData%\CivicCast and its cluster survive
        # BY DESIGN), a machine could take: D3 -> FRESH_INSTALL, no migration;
        # D4 provisioning -> NOOP_REUSE_EXISTING, no migration; the installer
        # stamps the new InstalledVersion anyway; and every future run then
        # reports SAME_VERSION_NO_OP, because InstalledVersion records WHICH
        # INSTALLER LAST RAN, not which schema the database is at. The machine
        # was permanently locked out of its own upgrade engine, with no
        # operator path back.
        #
        # The RUN and ADOPT_EXISTING paths below already migrate through
        # `migrate_provisioned_schema`; this reuses the same call, which is
        # idempotent (alembic skips applied revisions), so an
        # already-current database is a genuine no-op. Nothing about the
        # credential or the cluster's contents changes.
        # The reused cluster's OWN port, not a freshly selected one: this
        # cluster already exists and is already listening (or will be started)
        # on whatever the recorded DatabaseUrl names. `_pg_ctl_argv_start`
        # passes the context's port through to the postmaster, so selecting a
        # different one here would start the cluster somewhere the recorded
        # credential does not point.
        reused_port = args.postgres_port
        try:
            parsed_port = urlsplit(args.existing_database_url).port
            if parsed_port:
                reused_port = parsed_port
        except ValueError:
            pass
        args.postgres_port = reused_port
        _, noop_context = build_plan_and_context(
            paths=paths,
            args=args,
            # NOT a credential. `migrate_provisioned_schema` reads only the
            # data directory, the port/host, the state root and the owner run
            # id off this context; the connection it migrates over comes from
            # `--existing-database-url`, which already carries the real
            # password. Generating a fresh one here would be worse than
            # useless -- it would be a credential this run has no business
            # creating on a path whose whole contract is "touch nothing".
            database_password=_MIGRATION_ONLY_UNUSED_PASSWORD,
        )
        # <cross-version Gate A, run 33837269907, 2026-09-04> DO NOT
        # start/stop a cluster THIS PROCESS DOES NOT OWN.
        #
        # `migrate_provisioned_schema` is a start/migrate/stop sandwich: it
        # calls `normalize_pgdata_acl` and `pg_ctl start` on
        # `postgres_data_dir`, and `pg_ctl stop` in a `finally`. That is
        # correct on every path where D4 provisioning is the only thing
        # touching the cluster -- which is what BL-12 assumed when it added
        # this migration call to a branch that had previously touched nothing
        # (and is still what `pgdata_acl`'s own module docstring describes:
        # "NOT called on ... NOOP_REUSE_EXISTING").
        #
        # It is WRONG when the supervisor service is already running with its
        # own postmaster on that data directory, which is exactly the state
        # the UPGRADE route now leaves behind: D3 step 6's health gate
        # SCM-STARTS the service to read /health and, on a committed upgrade,
        # deliberately leaves it running (`stop_service` is wired only to the
        # D3 halt path). D4 then runs seconds later, and its `pg_ctl start`
        # collides with the live postmaster's `postmaster.pid` lock -- a
        # nonzero CLI exit, which the NSIS d4-provision step collapses to
        # installer exit 75 -> exit 116, with the `finally`'s `pg_ctl stop`
        # additionally yanking the running server's lock file out from under
        # it (Gate A evidence 4b30c99/20260904-062048Z: postgres logged
        # "performing immediate shutdown because data directory lock file is
        # invalid" and needed crash recovery on the next start).
        #
        # This never fired before 2026-09-04 only because every prior
        # cross-version run's D3 ROLLED BACK (evidence
        # gate-a-cross-version-33681670855: `route=UPGRADE engine_exit=10`)
        # and so never reached the health gate that starts the service.
        #
        # `reused_usable is True` is the precise, already-computed answer to
        # "is a server listening at the recorded URL right now AND does the
        # recorded credential work against it" (see
        # `probe_reused_database_url`). When it is, the schema migration is
        # run IN PLACE over that live connection -- no ACL write, no pg_ctl
        # at all -- which is both the safe thing and the honest one: this
        # branch's whole contract is "touch nothing". Every other case (the
        # server is not listening, or the probe could not answer) keeps the
        # start/migrate/stop sandwich, because then nothing else owns it.
        try:
            if reused_usable is True:
                run_schema_migration_to_head(
                    database_url=args.existing_database_url,
                    install_root=args.install_root,
                    state_root=noop_context.state_root,
                    owner_run_id=noop_context.owner_run_id,
                )
            else:
                migrate_provisioned_schema(
                    noop_context,
                    pg_ctl_path=pg_ctl_path_for(paths.initdb_path),
                    database_url=args.existing_database_url,
                    install_root=args.install_root,
                )
        except PgDataAclError as exc:
            write_recovery_document(
                paths.state_root,
                reason=f"the preserved cluster's data-directory ACL could not be normalized: {exc}",
                attempting=(
                    "starting the preserved cluster to bring its schema to alembic head "
                    "(reuse path)"
                ),
            )
            sys.stderr.write(
                "provision outcome: schema migration of the reused database could not start: "
                f"{exc}\n"
            )
            return EXIT_SCHEMA_ACL_NORMALIZATION_FAILED
        except Exception as exc:
            detail = str(exc).replace(args.existing_database_url, "[database-url redacted]")
            write_recovery_document(
                paths.state_root,
                reason=(
                    f"the reused database's schema could not be brought to alembic head: {detail}"
                ),
                attempting="'alembic upgrade head' against the reused database (reuse path)",
            )
            sys.stderr.write(
                "provision outcome: the existing database was reused but "
                f"'alembic upgrade head' did not complete: {detail}\n"
            )
            return EXIT_SCHEMA_MIGRATION_FAILED

        sys.stderr.write(
            "provision outcome: noop_reuse_existing (an existing PostgreSQL cluster and "
            "an existing DatabaseUrl registry value were both found; the credential and the "
            "cluster's data were not touched, and the schema was brought to alembic head"
            + (
                "; the recorded DatabaseUrl was verified against the live server, and the "
                "migration ran IN PLACE over that already-running server -- this step did "
                "not start, stop or re-ACL a cluster it does not own"
                if reused_usable is True
                else "; the server was not listening yet, so the recorded DatabaseUrl could "
                "not be verified before use, and this step started the cluster itself for "
                "the migration and stopped it again"
            )
            + ")\n"
        )
        return EXIT_SUCCESS

    if action is ProvisionCliAction.FAIL_LOUD_CREDENTIAL_LOST:
        assert credential_lost_journal is not None
        outcome = halt_resume_credential_lost(credential_lost_journal)
        sys.stderr.write(
            "provision outcome: fail_loud_credential_lost (a previous provisioning attempt "
            f"at {paths.state_root!r} reached phase {credential_lost_journal.phase.value!r} "
            "(at or past 'postgres_cluster_ready') but never completed; its PostgreSQL "
            "cluster is already initialized with a credential that was never persisted to "
            "HKLM\\SOFTWARE\\CivicCast\\Native\\DatabaseUrl and cannot be reconstructed -- "
            "resuming would only regenerate a DIFFERENT password the cluster will reject. "
            "This is NOT fixed by a repair install; see the recovery document at "
            f"{outcome.journal.recovery_document_path!r})\n"
        )
        return EXIT_REPAIR_NEEDED

    # action is RUN or ADOPT_EXISTING from here on: both need a decoded pack
    # key, a freshly generated password, and the real seam bundle.
    try:
        public_key = decode_pack_public_key(args.pack_public_key_base64)
    except Exception as exc:
        write_recovery_document(
            paths.state_root,
            reason=f"the server-binaries pack's embedded public key could not be decoded: {exc}",
            attempting="decoding the pack public key",
        )
        sys.stderr.write(f"provision outcome: unexpected error decoding pack public key: {exc}\n")
        return EXIT_UNEXPECTED

    # Real-world LPM deployment failure (2026-08-27, candidate 75cc13f, two
    # independent installer runs): pg_ctl start failed with "could not bind
    # IPv4 address 127.0.0.1: Permission denied" / "could not create any
    # TCP/IP sockets" because port 5432 sat inside a Windows-administered
    # excluded TCP port range (Hyper-V/WSL winnat reservations move at boot)
    # -- PROVISION-RECOVERY.md correctly named the pg_ctl diagnostic (PR #51)
    # but nothing survived it. Resolved BEFORE build_plan_and_context so the
    # chosen port -- whether the standard 5432 or a documented fallback --
    # flows into every downstream consumer through context.postgres_port: the
    # rendered postgresql.conf, the pg_ctl start/stop argv, and (via
    # resolve_database_url below) the DatabaseUrl this run hands back to the
    # Rust caller, which is the single source of truth every runtime consumer
    # (the supervisor's postgres child spec) must read the port from.
    port_selection = resolve_provision_port(
        host=args.postgres_host,
        preferred_port=args.postgres_port,
        candidates=DEFAULT_PORT_CANDIDATES,
    )
    if port_selection.outcome == "no_candidate_available":
        excluded_text = format_excluded_ranges_for_operator(port_selection.netsh_raw_output)
        write_recovery_document(
            paths.state_root,
            reason=(
                f"no usable PostgreSQL port on host {args.postgres_host!r}: {port_selection.detail}"
            ),
            attempting="port_selection",
            next_steps=[
                "Every candidate PostgreSQL port this installer tried failed a local "
                f"loopback bind test on {args.postgres_host!r}: {port_selection.detail}",
                "Ports tried, in the order attempted, and why each one was rejected:\n"
                + "\n".join(
                    f"   - port {attempt.port}: {attempt.outcome} -- {attempt.detail}"
                    for attempt in port_selection.attempts
                ),
                "This almost always means Windows has administratively EXCLUDED these "
                "ports from use (a Hyper-V/WSL 'winnat' dynamic port reservation made at "
                "boot -- these move across reboots) or security software is blocking the "
                "bind. Any program asking for one of these exact ports would fail "
                "identically; this is not specific to PostgreSQL.",
                f"Windows-excluded TCP port ranges observed at the time of this failure "
                f"('netsh int ipv4 show excludedportrange protocol=tcp'):\n{excluded_text}",
                "To reset the Hyper-V/WSL NAT port reservation table (run as "
                "Administrator, in order): 'net stop winnat' then 'net start winnat' -- "
                "this frequently frees a port that was excluded only because of a stale "
                "reservation, then retry the install.",
                "If a port is genuinely in use by another service (not Windows-excluded), "
                "stop that service or free the port before retrying.",
                "This provisioning run will not be silently retried. Once a port is free, "
                "run the installer again.",
                f"Preserve this journal for support: {journal_path(paths.state_root)}",
            ],
        )
        sys.stderr.write(
            "provision outcome: provisioning_failed (no usable PostgreSQL port found: "
            f"{port_selection.detail})\n"
        )
        return EXIT_PROVISIONING_FAILED
    assert port_selection.port is not None  # outcome == "selected" guarantees this
    if port_selection.port != args.postgres_port:
        sys.stderr.write(
            f"provision outcome: postgres port {args.postgres_port} was unavailable "
            f"(Windows-excluded range or bind refusal); selected fallback port "
            f"{port_selection.port} instead -- {port_selection.detail}\n"
        )
    args.postgres_port = port_selection.port

    database_password = generate_database_password()
    plan, context = build_plan_and_context(
        paths=paths, args=args, database_password=database_password
    )
    seams = build_default_seams_for(
        plan, context, public_key=public_key, initdb_path=paths.initdb_path
    )

    if action is ProvisionCliAction.ADOPT_EXISTING:
        # Verify the server-binaries pack BEFORE the credential reset executes
        # any of its binaries (pg_ctl/psql) -- the same "PACK_VERIFIED first,
        # before any binary runs" ordering the engine guarantees on a fresh
        # install (the pack is also D2-verified earlier in the NSIS chain, so
        # this is defense-in-depth). run_provision re-verifies idempotently
        # below; a failure here halts before the surviving cluster is touched.
        try:
            seams.verify_pack()
        except Exception as exc:
            detail = str(exc).replace(context.database_password, "[password redacted]")
            write_recovery_document(
                paths.state_root,
                reason=(
                    "the server-binaries pack could not be verified before adopting the "
                    f"surviving cluster (server_pack_path={context.server_pack_path!r}): {detail}"
                ),
                attempting="pack_verified (pre-adoption re-verification)",
                next_steps=[
                    "A surviving PostgreSQL data directory was found and this installer "
                    "attempted to ADOPT it (re-establish a fresh credential on it, station "
                    "data preserved) -- but its server-binaries pack could not be verified "
                    f"at {context.server_pack_path!r}, this run's CURRENT install root.",
                    "The surviving PostgreSQL data directory itself was NOT touched -- "
                    "this failure happened before any adoption action ran.",
                    "Obtain a correctly signed server-binaries pack at the path above "
                    "before retrying (a corrupt or partial install/copy is the most common "
                    "cause).",
                    "This provisioning run will not be silently retried. Once the pack is "
                    "verifiable, run the installer again.",
                    f"Preserve this journal for support: {journal_path(paths.state_root)}",
                ],
            )
            sys.stderr.write(
                "provision outcome: provisioning_failed (the server-binaries pack could not "
                f"be verified before adopting the surviving cluster: {detail})\n"
            )
            return EXIT_PROVISIONING_FAILED

        # N-15: re-establish a fresh credential ON the surviving, product-owned
        # cluster (chain M / F-02 deleted the old one; the data directory was
        # preserved). reset_cluster_credential fails loud
        # (AdoptionForeignClusterError) rather than ever adopting a cluster it
        # cannot prove the product created; a fresh scram credential is set on
        # ours, so every downstream engine/psql connection authenticates with
        # this run's password -- WITHOUT any initdb or database drop.
        try:
            adoption = reset_cluster_credential(
                context,
                plan,
                pg_ctl_path=pg_ctl_path_for(paths.initdb_path),
                psql_path=psql_path_for(paths.initdb_path),
            )
        except AdoptionForeignClusterError as exc:
            outcome = halt_adopt_foreign_cluster(plan, context, reason=str(exc))
            sys.stderr.write(
                "provision outcome: fail_loud_missing_registry (an existing PostgreSQL "
                f"cluster was found at {paths.postgres_data_dir!r} with no "
                "HKLM\\SOFTWARE\\CivicCast\\Native\\DatabaseUrl, but it could not be adopted "
                "because it is not a CivicCast-provisioned cluster this installer may take "
                f"over; see the recovery document at {outcome.journal.recovery_document_path!r})\n"
            )
            return EXIT_REPAIR_NEEDED
        except Exception as exc:
            # A real fault while re-establishing the credential (pg_ctl/psql
            # execution failure). Redact defensively even though the message is
            # static/path-only, and suppress the handoff line -- a cluster whose
            # credential could not be re-established must not advertise a
            # DatabaseUrl.
            detail = str(exc).replace(context.database_password, "[password redacted]")
            write_recovery_document(
                paths.state_root,
                reason=(
                    "could not re-establish the database credential on the surviving "
                    f"cluster at {paths.postgres_data_dir!r}: {detail}"
                ),
                attempting="postgres_cluster_ready (credential adoption)",
                next_steps=[
                    "A surviving PostgreSQL data directory was found and this installer "
                    "attempted to re-establish a fresh credential on it (station data "
                    "preserved; no initdb, no database drop) -- that attempt failed while "
                    "running pg_ctl/psql, not because the cluster is foreign.",
                    f"Data directory: {paths.postgres_data_dir}",
                    "Check that postgres could actually start against the just-written "
                    "loopback-trust config (port/host conflicts, permissions) and that the "
                    "pg_ctl/psql binaries in the server-binaries pack are present before "
                    "retrying.",
                    "This provisioning run will not be silently retried. Once the blocking "
                    "condition is resolved, run the installer again.",
                    f"Preserve this journal for support: {journal_path(paths.state_root)}",
                ],
            )
            sys.stderr.write(
                "provision outcome: provisioning_failed (could not re-establish the database "
                f"credential on the surviving cluster at {paths.postgres_data_dir!r}: {detail})\n"
            )
            return EXIT_PROVISIONING_FAILED

        sys.stderr.write(f"provision outcome: adopt_existing ({adoption.detail})\n")

        # The prior provision left a terminal COMPLETE journal at this
        # state_root (or uninstall removed it). Either way, run_provision must
        # drive a FRESH forward run over the now-credential-reset cluster
        # rather than short-circuiting on the stale terminal journal, so clear
        # it first. The journal is provisioning-run bookkeeping, never station
        # data (which lives in pgdata, untouched).
        journal_path(context.state_root).unlink(missing_ok=True)

    return _run_engine_and_finish(paths=paths, args=args, plan=plan, context=context, seams=seams)


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
