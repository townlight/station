# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""The 0.5.0 gate: restore a backup into a completely fresh database and PROVE it.

Proof, not vibes: alembic head matches the code's expected head, every
table's row count matches the backup manifest, a deterministic per-table
content checksum (ordered by PK) matches, and the app's own stores can open
and read the restored database (not just SQLAlchemy raw SQL).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from civiccast import schema_check
from civiccast.db import connect_options
from civiccast.db.url import normalize_database_url
from civiccast.dr.backup import (
    _parse_postgres_url,
    build_sqlite_engine,
    create_fresh_postgres_database,
    run_postgres_restore,
    snapshot_tables,
)
from civiccast.dr.models import (
    BackupManifest,
    RestoreDrillReport,
    RestoreTableResult,
    TableSnapshot,
)
from civiccast.egress.store import PostgresEgressStore
from civiccast.schedule.store import PostgresAssetStore


def _table_results(
    expected_tables: Iterable[TableSnapshot], actual_tables: list[TableSnapshot]
) -> list[RestoreTableResult]:
    """Row-count + checksum comparison shared by every engine's restore drill.

    ``expected_tables`` comes from the backup manifest (captured at backup
    time from the source database); ``actual_tables`` is a fresh
    :func:`civiccast.dr.backup.snapshot_tables` of the just-restored copy.
    A table missing from the restore, or present with a different row count
    or content checksum, is ``matched=False`` -- the thing this drill exists
    to catch.

    Symmetric on purpose: an UNEXPECTED extra table in the restored copy
    (leftover from a prior drill sharing the target database, a migration
    that ran twice, a restore pointed at the wrong artifact...) is exactly
    as real a piece of drift as a missing one, but iterating only
    ``expected_tables`` can never see it -- a for-loop over the manifest
    simply never visits a table the manifest doesn't know about. So after
    the expected-side loop this also walks every ``actual_tables`` entry NOT
    present in ``expected_tables`` and reports it ``matched=False`` with its
    ``expected_*`` fields left ``None`` -- there was nothing to expect it
    against, which is itself the finding.
    """

    expected_list = list(expected_tables)
    expected_names = {t.name for t in expected_list}
    actual_by_name = {t.name: t for t in actual_tables}
    results: list[RestoreTableResult] = []
    for expected in expected_list:
        actual = actual_by_name.get(expected.name)
        results.append(
            RestoreTableResult(
                name=expected.name,
                expected_row_count=expected.row_count,
                actual_row_count=actual.row_count if actual else None,
                expected_checksum=expected.checksum_sha256,
                actual_checksum=actual.checksum_sha256 if actual else None,
                matched=actual is not None
                and actual.row_count == expected.row_count
                and actual.checksum_sha256 == expected.checksum_sha256,
            )
        )
    for actual in actual_tables:
        if actual.name in expected_names:
            continue
        results.append(
            RestoreTableResult(
                name=actual.name,
                expected_row_count=None,
                actual_row_count=actual.row_count,
                expected_checksum=None,
                actual_checksum=actual.checksum_sha256,
                matched=False,
            )
        )
    return results


def run_sqlite_restore_drill(
    *, backup_dir: Path, manifest: BackupManifest, work_dir: Path
) -> RestoreDrillReport:
    """Restore ``manifest``'s SQLite artifact into a brand-new file and verify it.

    ``work_dir`` must not be (and is never touched as) the original database
    location — this proves the backup is independently restorable, not that
    the live file still exists.
    """

    started_at = datetime.now(UTC)
    errors: list[str] = []
    work_dir.mkdir(parents=True, exist_ok=True)
    restored_path = work_dir / f"restored-{manifest.backup_id}.sqlite3"
    shutil.copy2(backup_dir / manifest.db_artifact, restored_path)

    database_url = f"sqlite:///{restored_path}"
    db_revision = schema_check.read_db_revision(database_url)
    expected_head = schema_check.expected_migration_head()
    schema_ok = db_revision == expected_head

    engine = build_sqlite_engine(restored_path)
    table_results: list[RestoreTableResult] = []
    try:
        table_results = _table_results(manifest.tables, snapshot_tables(engine))

        app_store_reads: dict[str, int] = {}
        session_factory = sessionmaker(bind=engine, future=True)
        try:
            app_store_reads["assets"] = len(PostgresAssetStore(session_factory).list())
        except Exception as exc:
            errors.append(f"asset store read-through failed: {exc}")
        try:
            app_store_reads["egress_configs"] = len(
                PostgresEgressStore(session_factory).list_configs()
            )
        except Exception as exc:
            errors.append(f"egress store read-through failed: {exc}")
    finally:
        engine.dispose()

    return RestoreDrillReport(
        backup_id=manifest.backup_id,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        schema_ok=schema_ok,
        db_revision=db_revision,
        expected_head=expected_head,
        tables=table_results,
        app_store_reads=app_store_reads,
        errors=errors,
    )


def _pg_extension_names(engine: Engine) -> list[str]:
    """Installed extension names, e.g. ``["btree_gist", "plpgsql"]``."""

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT extname FROM pg_extension ORDER BY extname")).fetchall()
    return [row[0] for row in rows]


def _pg_sequence_states(
    engine: Engine, *, schema: str = "civiccast"
) -> list[tuple[str, int, bool]]:
    """(name, last_value, is_called) for every sequence in ``schema``.

    A same-name-but-different-value sequence is the failure mode a pure
    name comparison cannot see: ``last_value`` and ``is_called`` together
    are the two columns Postgres itself consults to answer the next
    ``nextval()`` call, so an identical name with a different pair is a
    sequence that will hand out different -- possibly already-used,
    colliding -- values than production would. ``pg_dump``/``pg_restore``
    DO carry sequence state (unlike cluster-global roles, which never
    survive a single-database dump at all -- see
    :func:`civiccast.dr.backup.run_postgres_globals_backup`), so a mismatch
    here is real restore drift, not an inherent boundary of the tool.
    Sequence names come from the catalog (``information_schema.sequences``),
    never from user input, before being interpolated into the per-sequence
    ``SELECT`` -- quoted, but still catalog-sourced identifiers rather than
    parameters, because Postgres has no bind-parameter syntax for table
    names.
    """

    with engine.connect() as conn:
        names = [
            row[0]
            for row in conn.execute(
                text(
                    "SELECT sequence_name FROM information_schema.sequences "
                    "WHERE sequence_schema = :schema ORDER BY sequence_name"
                ),
                {"schema": schema},
            ).fetchall()
        ]
        states: list[tuple[str, int, bool]] = []
        for name in names:
            row = conn.execute(
                text(f'SELECT last_value, is_called FROM "{schema}"."{name}"')  # noqa: S608 -- identifier from the catalog query above, quoted, not user input  # nosec B608
            ).fetchone()
            assert row is not None  # a sequence the catalog just listed always has a row
            states.append((name, row[0], row[1]))
    return states


def _pg_constraint_defs(engine: Engine, *, schema: str = "civiccast") -> list[tuple[str, str, str]]:
    """(table, constraint name, definition) for every constraint in ``schema``.

    Row/checksum equality (:func:`_table_results`) proves the DATA came back
    intact; it says nothing about the constraints that were supposed to keep
    protecting that data going forward. A restore that lost a CHECK or a
    FOREIGN KEY (a pg_restore ordering failure, a permission the restore
    role lacked...) still reads back byte-identical rows today and only
    breaks on the NEXT write, long after the drill declared victory.
    ``pg_get_constraintdef`` is the same function ``pg_dump`` itself calls
    to emit constraint DDL, so this compares against Postgres's own
    canonical rendering rather than a hand-rolled reconstruction that could
    drift from what pg_dump actually produces.
    """

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT conrelid::regclass::text AS rel, conname, pg_get_constraintdef(oid) "
                "FROM pg_constraint WHERE connamespace = CAST(:schema AS regnamespace) "
                "ORDER BY 1, 2"
            ),
            {"schema": schema},
        ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def _pg_index_defs(engine: Engine, *, schema: str = "civiccast") -> list[tuple[str, str, str]]:
    """(table, index name, definition) for every index in ``schema``.

    Same rationale as :func:`_pg_constraint_defs`: a lost or malformed index
    (a unique index enforcing a business rule, a partial index a hot query
    depends on) still reads back the same rows today and only shows up as a
    duplicate-key bug or a full-table-scan outage on the NEXT write or
    query -- exactly the kind of drift a checksum-only comparison cannot
    see. ``pg_indexes.indexdef`` is Postgres's own canonical rendering,
    again the same source ``pg_dump`` reads.
    """

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT tablename, indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = :schema ORDER BY 1, 2"
            ),
            {"schema": schema},
        ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def _pg_table_grants(
    engine: Engine, *, schema: str = "civiccast"
) -> list[tuple[str, str, str, str, bool]]:
    """(grantee, grantor, table, privilege, is_grantable) for every table ACL entry in ``schema``, PUBLIC included.

    Honest assumption this comparison makes: the drill connects as the SAME
    role that owns the production objects -- the single-role station
    deployment this program targets today. :func:`civiccast.dr.backup.run_postgres_restore`
    runs ``pg_restore --no-owner`` (see that function's own docstring), so a
    drill that connected as a DIFFERENT role than production would
    legitimately see different grants after restore, and this comparison
    would (correctly) report that as an error. That is not a false
    positive -- it is this drill surfacing the fact that a cross-role
    restore changes who can read the data, which is exactly the kind of
    silent-until-3am fact a disaster-recovery drill exists to catch. A
    station running a genuinely multi-role deployment needs to widen this
    comparison (or restore with role/grant replay); that is out of scope
    for the single-role station this drill targets today.
    """

    # aclexplode over relacl (with acldefault for never-touched ACLs), NOT
    # information_schema.role_table_grants (round-4 auditor finding: that
    # view omits PUBLIC grants entirely and this function previously dropped
    # grantor and grantability) -- consistent with every other ACL collector
    # in this module. Grantee oid 0 renders as PUBLIC.
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT COALESCE(gr.rolname, 'PUBLIC') AS grantee, "
                "granter.rolname AS grantor, c.relname AS table_name, "
                "acl.privilege_type, acl.is_grantable "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN LATERAL aclexplode("
                "COALESCE(c.relacl, acldefault('r', c.relowner))) AS acl "
                "LEFT JOIN pg_roles gr ON gr.oid = acl.grantee "
                "JOIN pg_roles granter ON granter.oid = acl.grantor "
                "WHERE n.nspname = :schema AND c.relkind = 'r' "
                "ORDER BY 1, 2, 3, 4"
            ),
            {"schema": schema},
        ).fetchall()
    return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]


def _pg_database_acl(engine: Engine, *, database: str) -> set[tuple[str, str, str, bool]]:
    """(grantee, grantor, privilege_type, is_grantable) -- every ACL entry on ``database``.

    Uses server-side ``aclexplode`` (the same function ``psql``'s own ``\\l+``
    and ``information_schema`` privilege views are built on) rather than
    hand-parsing the raw ``aclitem[]`` text -- ``COALESCE(datacl,
    acldefault('d', datdba))`` covers a database whose ACL has never been
    touched (``datacl IS NULL`` means "use the implicit default": CONNECT +
    TEMP to PUBLIC, everything to the owner), so a database with zero
    explicit grants still returns its real, effective privilege set instead
    of an empty one.

    Honest note (name-resolved comparison): ``grantee``/``grantor`` are
    resolved to role NAMES (``pg_roles.rolname``), matching every other
    comparison in this module -- two clusters where a same-NAMED role has a
    different underlying oid compare EQUAL here, which is correct for this
    program's single-station, name-is-identity deployment model. A grantee
    oid of ``0`` is ``aclexplode``'s own encoding for the pseudo-role
    ``PUBLIC`` (it never appears in ``pg_roles``) -- included here as the
    literal string ``'PUBLIC'`` rather than excluded, because PUBLIC grants
    (e.g. default CONNECT) are real privilege state that must match, even
    though :func:`_pg_relevant_roles` excludes PUBLIC from role-EXISTENCE
    checking (a pseudo-role that always exists can never be "missing").
    """

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT COALESCE(gte.rolname, 'PUBLIC') AS grantee, "
                "COALESCE(gtor.rolname, '') AS grantor, "
                "acl.privilege_type, acl.is_grantable "
                "FROM pg_database d "
                "CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) "
                "AS acl "
                "LEFT JOIN pg_roles gte ON gte.oid = acl.grantee "
                "LEFT JOIN pg_roles gtor ON gtor.oid = acl.grantor "
                "WHERE d.datname = :db"
            ),
            {"db": database},
        ).fetchall()
    return {(row[0], row[1], row[2], row[3]) for row in rows}


def _pg_schema_acl(engine: Engine, *, schema: str = "civiccast") -> set[tuple[str, str, str, bool]]:
    """(grantee, grantor, privilege_type, is_grantable) -- every ACL entry on ``schema``.

    Same ``aclexplode``/``acldefault`` mechanism and name-resolution honesty
    note as :func:`_pg_database_acl`; ``'n'`` is the ``acldefault`` object-type
    code for a schema (Postgres's own convention, matching ``pg_namespace``'s
    role in the catalog).
    """

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT COALESCE(gte.rolname, 'PUBLIC') AS grantee, "
                "COALESCE(gtor.rolname, '') AS grantor, "
                "acl.privilege_type, acl.is_grantable "
                "FROM pg_namespace n "
                "CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) "
                "AS acl "
                "LEFT JOIN pg_roles gte ON gte.oid = acl.grantee "
                "LEFT JOIN pg_roles gtor ON gtor.oid = acl.grantor "
                "WHERE n.nspname = :schema"
            ),
            {"schema": schema},
        ).fetchall()
    return {(row[0], row[1], row[2], row[3]) for row in rows}


def _pg_sequence_acls(
    engine: Engine, *, schema: str = "civiccast"
) -> set[tuple[str, str, str, str, bool]]:
    """(sequence, grantee, grantor, privilege_type, is_grantable) for every sequence in ``schema``.

    Same ``aclexplode``/``acldefault`` mechanism as :func:`_pg_database_acl`;
    ``'s'`` is the ``acldefault`` object-type code for a sequence.
    :func:`_pg_table_grants` already covers ordinary tables via
    ``information_schema.role_table_grants``, which does NOT include
    sequences (Postgres does not classify a sequence as a "table" in that
    view) -- this is the sequence-privilege equivalent, scoped to
    ``relkind = 'S'`` directly against ``pg_class``.
    """

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, COALESCE(gte.rolname, 'PUBLIC') AS grantee, "
                "COALESCE(gtor.rolname, '') AS grantor, "
                "acl.privilege_type, acl.is_grantable "
                "FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN LATERAL "
                "aclexplode(COALESCE(c.relacl, acldefault('s', c.relowner))) AS acl "
                "LEFT JOIN pg_roles gte ON gte.oid = acl.grantee "
                "LEFT JOIN pg_roles gtor ON gtor.oid = acl.grantor "
                "WHERE n.nspname = :schema AND c.relkind = 'S'"
            ),
            {"schema": schema},
        ).fetchall()
    return {(row[0], row[1], row[2], row[3], row[4]) for row in rows}


def _pg_default_acls(
    engine: Engine, *, schema: str = "civiccast", owners: set[str]
) -> set[tuple[str, str, str, str, str, bool]]:
    """(owner, defaclobjtype, grantee, grantor, privilege_type, is_grantable) default ACLs.

    ``pg_default_acl`` rows are ``ALTER DEFAULT PRIVILEGES FOR ROLE <owner>
    [IN SCHEMA <schema>] GRANT ...`` captures -- privileges that will apply
    to objects ``<owner>`` creates in the FUTURE, not to any object that
    exists today. Scoped, per the round-4 acceptance criterion, to this
    ``schema`` (``defaclnamespace`` resolves to ``schema``) OR to a
    cluster-wide default (``defaclnamespace = 0``, Postgres's own encoding
    for "no IN SCHEMA clause was given"), AND to ``owners`` -- the relevant
    owner roles already known to the caller (this module fetches every row
    matching the namespace filter, then filters to ``owners`` in Python,
    the same "fetch broad, filter narrow" pattern :func:`_pg_role_attributes`
    already uses, to stay agnostic of how a given SQLAlchemy/DBAPI adapts a
    Python list to a Postgres array bind parameter).
    """

    if not owners:
        return set()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT owner.rolname AS owner, da.defaclobjtype, "
                "COALESCE(gte.rolname, 'PUBLIC') AS grantee, "
                "COALESCE(gtor.rolname, '') AS grantor, "
                "acl.privilege_type, acl.is_grantable "
                "FROM pg_default_acl da "
                "JOIN pg_roles owner ON owner.oid = da.defaclrole "
                "LEFT JOIN pg_namespace n ON n.oid = da.defaclnamespace "
                "CROSS JOIN LATERAL aclexplode(da.defaclacl) AS acl "
                "LEFT JOIN pg_roles gte ON gte.oid = acl.grantee "
                "LEFT JOIN pg_roles gtor ON gtor.oid = acl.grantor "
                "WHERE n.nspname = :schema OR da.defaclnamespace = 0"
            ),
            {"schema": schema},
        ).fetchall()
    return {(row[0], row[1], row[2], row[3], row[4], row[5]) for row in rows if row[0] in owners}


def _pg_all_role_membership_edges(engine: Engine) -> set[tuple[str, str]]:
    """Every ``(member, role)`` edge in ``pg_auth_members``, name-resolved, CLUSTER-WIDE.

    Unlike :func:`_pg_role_memberships` (which restricts to edges where BOTH
    endpoints are already in a given role set -- the comparison this module
    runs between source and standby), this reads every edge on the cluster
    with no filter, because :func:`_close_roles_over_memberships` needs the
    full edge set to walk outward from a seed until a fixpoint.
    """

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT m.rolname AS member, r.rolname AS role FROM pg_auth_members am "
                "JOIN pg_roles m ON m.oid = am.member "
                "JOIN pg_roles r ON r.oid = am.roleid"
            )
        ).fetchall()
    return {(row[0], row[1]) for row in rows}


def _close_roles_over_memberships(seed: set[str], edges: set[tuple[str, str]]) -> set[str]:
    """Transitive closure of ``seed`` over every membership edge that touches it.

    Round-4 fix (CC-WS2-001, Critical, auditor-EXECUTED false-pass): the
    round-3 seed (table/sequence owners plus direct table grantees) misses a
    role that is ONLY a membership PARENT -- the auditor's own example, an
    ``app_owner`` role (seeded, because it owns tables) that inherits
    ``ops_admin`` (never seeded, because ``ops_admin`` itself owns nothing
    and holds no direct grant). :func:`_pg_role_memberships` only compares
    edges where BOTH endpoints are ALREADY in the role set it is given, so
    if ``ops_admin`` never makes it into that set, the edge is invisible to
    the comparison regardless of whether it changed.

    This repeatedly adds BOTH endpoints of any edge where AT LEAST ONE
    endpoint is already in the closed set, until a fixpoint -- not just a
    single pass over edges touching the seed, because a CHAIN (A member-of B
    member-of C) needs repeated passes to reach C once B joins on the first
    pass. Cluster role counts at this program's target scale (a single
    station) are small, so an ``O(edges * passes)`` loop over the full edge
    set is cheap.
    """

    closed = set(seed)
    changed = True
    while changed:
        changed = False
        for member, role in edges:
            if member in closed and role not in closed:
                closed.add(role)
                changed = True
            if role in closed and member not in closed:
                closed.add(member)
                changed = True
    return closed


def _pg_relevant_roles(engine: Engine, *, schema: str = "civiccast") -> set[str]:
    """Roles the drill expects ``globals.sql`` to have captured, TRANSITIVELY closed.

    Round-4 rewrite (CC-WS2-001, Critical): round-3 seeded only table
    owners plus direct table grantees. The full seed now is every role that
    OWNS the database (``pg_database.datdba``), the ``civiccast`` schema
    (``pg_namespace.nspowner``), any table, or any sequence; UNION every
    non-``PUBLIC`` grantee found in the database ACL
    (:func:`_pg_database_acl`), the schema ACL (:func:`_pg_schema_acl`),
    table grants (:func:`_pg_table_grants`), sequence ACLs
    (:func:`_pg_sequence_acls`), and default ACLs scoped to this schema and
    to that owner set (:func:`_pg_default_acls`). That seed is then run
    through :func:`_close_roles_over_memberships` -- the TRANSITIVE closure
    over every ``pg_auth_members`` edge touching it -- so a role reachable
    only by INHERITING a seeded role (or being inherited BY one) is included
    too. ``PUBLIC`` is a pseudo-role ``pg_dumpall`` never emits a ``CREATE
    ROLE``/``ALTER ROLE`` for -- it always exists -- so it is excluded from
    this EXISTENCE/attribute/membership check (it is NOT excluded from the
    ACL-value comparisons themselves, which include PUBLIC rows on purpose;
    see :func:`_pg_database_acl`'s docstring).

    Honest note: this is name-resolved, like every ACL/role helper in this
    module -- a role is "the same role" here iff it has the same
    ``rolname``, matching this drill's single-station, name-is-identity
    deployment model.
    """

    with engine.connect() as conn:
        database = conn.execute(text("SELECT current_database()")).scalar_one()

    table_owners = set(_pg_table_owners(engine, schema=schema).values())
    sequence_owners = set(_pg_sequence_owners(engine, schema=schema).values())
    database_owner = _pg_database_owner(engine, database=database)
    schema_owner = _pg_schema_owner(engine, schema=schema)

    owners = table_owners | sequence_owners
    if database_owner:
        owners.add(database_owner)
    if schema_owner:
        owners.add(schema_owner)

    grantees: set[str] = set()
    grantees.update(
        grantee
        for grantee, _grantor, _priv, _grantable in _pg_database_acl(engine, database=database)
    )
    grantees.update(
        grantee for grantee, _grantor, _priv, _grantable in _pg_schema_acl(engine, schema=schema)
    )
    grantees.update(
        grantee for grantee, _grantor, _table, _priv, _g in _pg_table_grants(engine, schema=schema)
    )
    grantees.update(
        grantee
        for _seq, grantee, _grantor, _priv, _grantable in _pg_sequence_acls(engine, schema=schema)
    )
    grantees.update(
        grantee
        for _owner, _objtype, grantee, _grantor, _priv, _grantable in _pg_default_acls(
            engine, schema=schema, owners=owners
        )
    )
    grantees.discard("PUBLIC")

    seed = owners | grantees
    edges = _pg_all_role_membership_edges(engine)
    return _close_roles_over_memberships(seed, edges)


def _bare_relation_name(rel: str) -> str:
    """Strip an optional schema qualifier/quoting from a catalog relation name.

    ``pg_constraint``'s ``conrelid::regclass::text`` (used by
    :func:`_pg_constraint_defs`) returns a SCHEMA-QUALIFIED name
    (``civiccast.sometable``) whenever ``civiccast`` is not on the
    connection's ``search_path`` -- true here, since this program never sets
    one. ``pg_indexes.tablename`` (used by :func:`_pg_index_defs`), by
    contrast, is always bare -- it is a plain catalog column, not a
    ``regclass`` cast. Both shapes need to resolve to the same scratch-table
    name in :func:`_canonicalize_defs`, so this strips a leading schema
    qualifier (and any quoting) when present and is a no-op otherwise.
    """

    return rel.rsplit(".", 1)[-1].strip('"')


_NOT_NULL_CONSTRAINT_PATTERN = re.compile(r"^\s*NOT NULL\s", re.IGNORECASE)


def _is_not_null_constraint_def(defn: str) -> bool:
    """True for a PG17 ``contype = 'n'`` constraint's deparse (``NOT NULL "col"``)."""

    return bool(_NOT_NULL_CONSTRAINT_PATTERN.match(defn))


_INDEX_ON_SCHEMA_PATTERN = re.compile(r'\bON\s+"?civiccast"?\.')
_INDEX_NAME_PATTERN = re.compile(
    r'^(CREATE(?:\s+UNIQUE)?\s+INDEX\s+)("?[A-Za-z0-9_]+"?)(\s+ON\s+)', re.IGNORECASE
)


def _rewrite_index_def_for_canon(defn: str, scratch_name: str) -> str:
    """Point an ``indexdef`` at the scratch schema and a collision-free name.

    ``pg_indexes.indexdef``'s shape is stable: ``CREATE [UNIQUE] INDEX <name>
    ON civiccast.<table> USING ...`` (Postgres's own canonical rendering, the
    same source ``pg_dump`` reads). The rewrite is deliberately conservative
    -- only the index-name token immediately after ``CREATE [UNIQUE] INDEX``
    and the single ``ON civiccast.`` occurrence are touched; the USING
    clause, column/expression list, and any WHERE predicate pass through
    completely untouched, because that untouched part is exactly what this
    function exists to run back through Postgres and canonicalize.
    """

    renamed = _INDEX_NAME_PATTERN.sub(rf'\1"{scratch_name}"\3', defn, count=1)
    return _INDEX_ON_SCHEMA_PATTERN.sub('ON "__dr_canon".', renamed, count=1)


def _scratch_name(kind: str, rel: str, name: str) -> str:
    """A deterministic, collision-free scratch identifier for a ``(kind, rel, name)`` key.

    Round-4 fix (CC-WS2-001, Critical, auditor-executed false-pass): the same
    key ALWAYS maps to the same scratch name, on both the source-def and the
    restored-def canonicalization for that key. That determinism is what
    makes comparing the RAW canonical readback (:func:`_canonicalize_defs`,
    no string-replace of any kind) correct: the scratch schema/name tokens
    are then byte-identical substrings on both sides of a matching key, so
    they cancel out in a plain ``==`` the same way a shared prefix cancels
    out of a diff, with no runtime rewrite needed to make them line up --
    and therefore nothing here can ever rewrite a predicate LITERAL that
    happens to collide with a scratch token, which is exactly how the prior
    (round-3) global ``str.replace`` reverse-mapping produced a false clean
    compare for two indexes whose WHERE predicates differed only in the
    literals ``'__dr_canon'`` vs ``'civiccast'``.
    """

    prefix = "__i_" if kind == "index" else "__c_"
    digest = hashlib.sha1(f"{rel}\x1f{name}".encode()).hexdigest()[:12]  # noqa: S324 -- not cryptographic, just a deterministic short identifier  # nosec B324
    return f"{prefix}{digest}"


_INDEX_HEADER_PATTERN = re.compile(
    r'^(CREATE(?:\s+UNIQUE)?\s+INDEX\s+)"?[A-Za-z0-9_]+"?(\s+ON\s+)"?[A-Za-z0-9_]+"?(\.)',
    re.IGNORECASE,
)


def _friendly_canonical_for_display(canonical_text: str, *, kind: str, real_name: str) -> str:
    """Best-effort, DISPLAY-ONLY rewrite of a RAW canonical definition.

    Never consulted for the equality comparison itself (that always uses the
    untouched raw canonical text from :func:`_canonicalize_defs`) -- this
    exists purely so an operator debugging a 3 a.m. restore sees
    ``civiccast.<real index name>`` instead of ``"__dr_canon"."__i_<hash>"``
    noise in the error message. Deliberately narrow: only the ``CREATE
    [UNIQUE] INDEX <name> ON <schema>.`` HEADER -- anchored at the start of
    the string via ``^`` -- is a candidate for replacement, via one bounded
    regex substitution, never a global ``str.replace`` over the whole
    definition (that global-replace shape is exactly last round's false-pass
    bug: it also rewrote scratch-token-shaped text inside WHERE predicate
    literals). If the header doesn't match this exact expected shape
    (defensive -- it always should, since :func:`_canonicalize_defs` just
    built it), the raw canonical text is shown unchanged instead: ugly but
    honest, never a guess.
    """

    if kind != "index":
        return canonical_text

    def _replace(match: re.Match[str]) -> str:
        return f'{match.group(1)}"{real_name}"{match.group(2)}"civiccast"{match.group(3)}'

    rewritten, count = _INDEX_HEADER_PATTERN.subn(_replace, canonical_text, count=1)
    return rewritten if count else canonical_text


def _canonicalize_defs(engine: Engine, defs: list[tuple[str, str, str]], *, kind: str) -> list[str]:
    """Canonicalize every (rel, name, def) entry through ONE server's own deparse.

    Round-4 fix (CC-WS2-001, Critical): returns the RAW canonical readback
    from Postgres -- ``pg_get_constraintdef``/``pg_get_indexdef`` -- with NO
    string replacement of any kind. Round-3's version applied an
    unrestricted ``str.replace`` of the scratch schema/name tokens over the
    WHOLE deparsed text before returning it, which the auditor's executed
    control broke: an index WHERE predicate containing the string literal
    ``'__dr_canon'`` on one side and ``'civiccast'`` on the other compared
    EQUAL, because the global replace rewrote the LITERAL, not just the
    identifier it was meant for. This version never does that rewrite in the
    comparison path at all -- see :func:`_scratch_name`'s docstring for why
    a deterministic per-key scratch name makes that rewrite unnecessary: the
    scratch tokens are already identical text on both sides of a matching
    key, so a plain ``==`` over the raw canonical text is correct by
    construction. (A DISPLAY-ONLY, narrowly-scoped reverse mapping still
    exists, but only in :func:`_friendly_canonical_for_display`, applied to
    a COPY after the comparison, for the error message -- never here.)

    Replaces the older text-stripping ``_normalize_ddl`` comparison (deleted
    in round 3): stripping casts/parens/whitespace by regex cannot tell a
    REAL semantic difference (different operator precedence, a negated
    predicate, a different cast TARGET type) from harmless deparse noise --
    both survive the same strip. This instead re-parses each definition
    through a live Postgres server (the restored/standby database itself --
    disposable, so mutating it here is safe) and compares Postgres's OWN
    canonical rendering, which is precisely the rendering that distinguishes
    those cases: two definitions that mean the same thing deparse
    identically no matter how they were originally parenthesized or
    cast-decorated; two definitions that mean DIFFERENT things deparse
    differently, full stop.

    Mechanism: a scratch schema ``__dr_canon`` is created in ``engine``'s
    database (dropped in a ``finally``, so nothing here persists). For every
    distinct source table referenced by ``defs``, ``CREATE TABLE
    "__dr_canon"."<table>" (LIKE "civiccast"."<table>" INCLUDING DEFAULTS)``
    -- a REGULAR table, deliberately not TEMP: a temporary table cannot carry
    a foreign key that references a permanent table, and FOREIGN KEY
    constraint defs reference ``civiccast.*`` targets verbatim (left
    unmodified -- those targets already exist, in the real ``civiccast``
    schema, in this same database). ``INCLUDING DEFAULTS`` copies columns
    (types, defaults) only; NOT NULL is always copied by plain ``LIKE``
    regardless of options, but no NAMED constraint (CHECK/PK/UNIQUE/FK/
    EXCLUDE) is -- those are added back one entry at a time below, under a
    disposable, DETERMINISTIC scratch name (:func:`_scratch_name`),
    specifically so Postgres's parser/deparser can be observed on them in
    isolation.

    Entries are processed ONE AT A TIME, in input order: create the scratch
    object under its deterministic name, read back its canonical form, then
    DROP it before moving to the next entry. This is what makes it safe for
    two entries in ``defs`` to share the same ``(rel, name)`` key (exactly
    what :func:`_def_lists_mismatch` does below: a source entry and a
    restored entry for the same key, processed in the SAME call to this
    function, get the identical deterministic scratch name) -- without the
    drop, the second entry's ``CREATE`` would collide with the first's still
    -present object of the same name in the same scratch schema.

    ``kind="constraint"``: each def is applied via ``ALTER TABLE ... ADD
    CONSTRAINT "<scratch>" <def>`` and read back via ``pg_get_constraintdef``
    -- the same function ``pg_dump`` itself calls -- then the constraint is
    dropped. A PG17 ``contype = 'n'`` (NOT NULL) def is recognized up front
    by its ``NOT NULL <col>`` shape and compared by raw-text equality
    instead -- NOT NULL defs are trivially stable text across a
    dump/restore round trip, so canonicalizing them adds no value, and (per
    this repo's own testing note) attempting to re-add a NOT NULL constraint
    that ``LIKE`` already implied for the column is exactly the kind of
    PG17-syntax edge case worth sidestepping rather than guessing at. Any
    OTHER ``ADD CONSTRAINT`` failure (a def this function did not
    anticipate) falls back to the same raw-text equality rather than
    raising -- a canonicalization bug must never turn into a drill crash; it
    degrades to the old (still correct, just less-lenient-to-noise)
    comparison for that one entry.

    ``kind="index"``: each def is rewritten (:func:`_rewrite_index_def_for_canon`)
    to target the scratch schema under the deterministic scratch name,
    executed, read back via ``pg_get_indexdef`` UNCHANGED, then dropped.
    """

    if kind not in ("constraint", "index"):
        raise ValueError(f"unknown canonicalization kind: {kind!r}")
    if not defs:
        return []

    canonical: list[str] = [""] * len(defs)
    tables = sorted({_bare_relation_name(rel) for rel, _name, _def in defs})

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text('DROP SCHEMA IF EXISTS "__dr_canon" CASCADE'))
        conn.execute(text('CREATE SCHEMA "__dr_canon"'))
        try:
            for table in tables:
                conn.execute(
                    text(
                        f'CREATE TABLE "__dr_canon"."{table}" '
                        f'(LIKE "civiccast"."{table}" INCLUDING DEFAULTS)'
                    )
                )

            for i, (rel, name, defn) in enumerate(defs):
                scratch_name = _scratch_name(kind, rel, name)
                if kind == "constraint":
                    if _is_not_null_constraint_def(defn):
                        canonical[i] = defn
                        continue
                    table = _bare_relation_name(rel)
                    try:
                        conn.execute(
                            text(
                                f'ALTER TABLE "__dr_canon"."{table}" '
                                f'ADD CONSTRAINT "{scratch_name}" {defn}'
                            )
                        )
                    except DBAPIError:
                        canonical[i] = defn
                        continue
                    try:
                        row = conn.execute(
                            text(
                                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                "WHERE conname = :cname "
                                "AND connamespace = CAST('__dr_canon' AS regnamespace)"
                            ),
                            {"cname": scratch_name},
                        ).fetchone()
                        assert row is not None  # the ADD CONSTRAINT above just committed it
                        canonical[i] = row[0]  # RAW readback -- no replace of any kind
                    finally:
                        conn.execute(
                            text(
                                f'ALTER TABLE "__dr_canon"."{table}" '
                                f'DROP CONSTRAINT "{scratch_name}"'
                            )
                        )
                else:
                    conn.execute(text(_rewrite_index_def_for_canon(defn, scratch_name)))
                    try:
                        row = conn.execute(
                            text(
                                "SELECT pg_get_indexdef(pg_index.indexrelid) "
                                "FROM pg_index JOIN pg_class "
                                "ON pg_class.oid = pg_index.indexrelid "
                                "WHERE pg_class.relname = :iname "
                                "AND pg_class.relnamespace = CAST('__dr_canon' AS regnamespace)"
                            ),
                            {"iname": scratch_name},
                        ).fetchone()
                        assert row is not None  # the CREATE INDEX above just committed it
                        canonical[i] = row[0]  # RAW readback -- no replace of any kind
                    finally:
                        conn.execute(text(f'DROP INDEX "__dr_canon"."{scratch_name}"'))
        finally:
            conn.execute(text('DROP SCHEMA IF EXISTS "__dr_canon" CASCADE'))
    return canonical


def _detect_ddl_kind(defn: str) -> str:
    """``"index"`` for a ``pg_indexes.indexdef`` string, ``"constraint"`` otherwise.

    ``pg_get_constraintdef`` output never begins with ``CREATE`` (it is
    ``CHECK (...)``, ``PRIMARY KEY (...)``, ``FOREIGN KEY (...) REFERENCES
    ...``, ``UNIQUE (...)``, ``EXCLUDE USING ... (...)``, or ``NOT NULL
    <col>``); ``pg_indexes.indexdef`` always begins with ``CREATE [UNIQUE]
    INDEX``. A single element of whichever list :func:`_def_lists_mismatch`
    was called with is enough to tell the two apart.
    """

    return "index" if defn.lstrip().upper().startswith("CREATE ") else "constraint"


def _def_lists_mismatch(
    source: list[tuple[str, str, str]],
    restored: list[tuple[str, str, str]],
    canon_engine: Engine,
) -> str | None:
    """Compare (rel, name, definition) lists; return a COMPACT diff or None.

    Keys on (rel, name) strictly, same as before. For common keys,
    definitions now compare via :func:`_canonicalize_defs` -- BOTH sides
    passed through the SAME server's own deparse (``canon_engine``, the
    restored/standby database), so source-server deparse quirks (a different
    Postgres minor version, a different original parenthesization) vanish
    identically on both sides, while any REAL difference (missing/renamed/
    retargeted constraints and indexes, a changed literal, operator, column,
    cast TARGET type, or list membership) survives canonicalization on both
    sides and is still reported.

    Round-4 fix (CC-WS2-001, Critical): the source-def and restored-def for
    EACH matched key are canonicalized in the SAME call to
    :func:`_canonicalize_defs` (source entries for all common keys, then
    restored entries for all common keys, in one ``defs`` list) so that a
    shared key gets the SAME deterministic scratch name
    (:func:`_scratch_name`) on both sides -- see that function's docstring
    for why this makes the raw-canonical-text equality below correct without
    any runtime string-replace. The comparison itself (``s_canon !=
    d_canon``) always uses the RAW, untouched canonical text; a
    presentation-friendly, reverse-mapped form
    (:func:`_friendly_canonical_for_display`) is built ONLY for the error
    message below, on a COPY, after the comparison has already happened --
    it can never influence which keys are reported as changed.

    Returns only the differing entries -- a station operator debugging a
    3 a.m. restore needs the six lines that differ, not two hundred matching
    ones.
    """

    src_by_key = {(rel, name): defn for rel, name, defn in source}
    dst_by_key = {(rel, name): defn for rel, name, defn in restored}
    only_source = sorted(set(src_by_key) - set(dst_by_key))
    only_restored = sorted(set(dst_by_key) - set(src_by_key))
    common_keys = sorted(set(src_by_key) & set(dst_by_key))

    changed: list[tuple[str, str]] = []
    src_canon_by_key: dict[tuple[str, str], str] = {}
    dst_canon_by_key: dict[tuple[str, str], str] = {}
    if common_keys:
        kind = _detect_ddl_kind(src_by_key[common_keys[0]])
        combined = [(rel, name, src_by_key[(rel, name)]) for rel, name in common_keys]
        combined += [(rel, name, dst_by_key[(rel, name)]) for rel, name in common_keys]
        canonical = _canonicalize_defs(canon_engine, combined, kind=kind)
        half = len(common_keys)
        src_canon_by_key = dict(zip(common_keys, canonical[:half], strict=True))
        dst_canon_by_key = dict(zip(common_keys, canonical[half:], strict=True))
        changed = sorted(
            key for key in common_keys if src_canon_by_key[key] != dst_canon_by_key[key]
        )

    if not (only_source or only_restored or changed):
        return None
    parts: list[str] = []
    if only_source:
        parts.append(f"missing from restore: {only_source!r}")
    if only_restored:
        parts.append(f"unexpected in restore: {only_restored!r}")
    if changed:
        # changed is only ever non-empty when common_keys was, so src_by_key
        # has an entry for changed[0]; recomputed (cheaply -- a lstrip +
        # startswith check) rather than trusting the `kind` local from the
        # branch above stays in scope, for clarity at the call site.
        display_kind = _detect_ddl_kind(src_by_key[changed[0]])
        parts.append(
            "definition changed: "
            + "; ".join(
                f"{key!r}: source="
                f"{_friendly_canonical_for_display(src_canon_by_key[key], kind=display_kind, real_name=key[1])!r} "
                f"restored="
                f"{_friendly_canonical_for_display(dst_canon_by_key[key], kind=display_kind, real_name=key[1])!r}"
                for key in changed
            )
        )
    return " | ".join(parts)


def _role_captured_in_globals(globals_sql: str, role: str) -> bool:
    """Whether ``role`` shows up as created or altered in a ``globals.sql`` capture.

    Accepts quoted or unquoted ``CREATE ROLE``/``ALTER ROLE`` spellings.
    ``ALTER ROLE`` (without ``CREATE ROLE``) counts on purpose: the
    bootstrap superuser a fresh Postgres cluster/container ships with
    already exists before ``pg_dumpall`` runs, so ``pg_dumpall`` emits only
    an ``ALTER ROLE ... WITH ...`` for it (password/superuser flags), never
    a ``CREATE ROLE`` -- and that is still a full, restorable capture of
    that role's relevant state.

    Round 3 fix: a line whose first non-whitespace characters are ``--`` is
    a SQL comment and never counts, no matter what it mentions -- the
    previous version searched the raw text, so an explanatory ``pg_dumpall``
    comment that happened to mention "CREATE ROLE" in prose (or a
    hand-edited globals.sql with a commented-OUT statement) would have been
    read as a real capture. Comment lines are stripped before matching.

    Same-cluster-drill callers of this helper: see
    :func:`run_postgres_restore_drill`'s reduced use of it below -- the REAL
    role-restorability proof is
    :func:`run_postgres_cold_standby_drill`, which verifies role attributes
    and memberships by comparison on a cluster that never had them, not by
    pattern-matching the SQL text.
    """

    active_lines = "\n".join(
        line for line in globals_sql.splitlines() if not line.lstrip().startswith("--")
    )
    # Word-bounded match, not substring: role "test" must not be satisfied
    # by `CREATE ROLE testing`. The unquoted form requires a non-identifier
    # character (or end of line) after the name; the quoted form is exact.
    quoted = re.escape(f'"{role}"')
    bare = re.escape(role) + r"(?![A-Za-z0-9_$])"
    return bool(
        re.search(
            rf"\b(?:CREATE|ALTER) ROLE (?:{quoted}|{bare})",
            active_lines,
        )
    )


def _with_database_name(database_url: str, database_name: str) -> str:
    """Swap only the path (database name) component of a DATABASE_URL."""

    parts = urlsplit(database_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database_name}", "", ""))


def _verification_engine_url(database_url: str) -> str:
    """The URL this module's OWN SQLAlchemy reads connect through.

    Every ``create_engine`` in this module goes through here (beta BLOCKER
    #51's normalizer, :func:`civiccast.db.url.normalize_database_url`): a
    driver-less ``postgresql://`` -- the exact shape the native installer
    persists to ``HKLM\\SOFTWARE\\CivicCast\\Native\\DatabaseUrl`` -- resolves
    to the **psycopg2** dialect, and this product ships psycopg **v3** only
    (ADR 0008). SQLAlchemy imports the DBAPI at ENGINE CONSTRUCTION, so an
    unnormalized URL raises ``ModuleNotFoundError: No module named 'psycopg2'``
    before a socket is ever opened.

    That is not a hypothetical: D3 step 3 (``BACKUP_VERIFIED``) calls
    :func:`run_postgres_restore_drill` as its pre-upgrade restore-drill spot
    check (:func:`civiccast.native.upgrade.seams.default_backup`), so this
    module IS on the native install/upgrade path -- correcting
    ``civiccast/db/url.py``'s stale "operator/DR-drill only, not on the native
    service/control-plane/installer path" note. Real-hardware R7, 2026-08-01:
    the upgrade rolled back at exactly this call with exactly that error.

    NOT applied to the ``*_command`` / CLI-facing URLs (``source_database_url``
    as parsed by :func:`civiccast.dr.backup._parse_postgres_url`, and
    :func:`create_fresh_postgres_database`'s return): those are decomposed into
    ``--host``/``--port``/... for ``pg_dump``/``pg_restore``/``psql``, which
    know nothing about SQLAlchemy driver names.
    """

    return normalize_database_url(database_url)


def run_postgres_restore_drill(
    *,
    backup_dir: Path,
    manifest: BackupManifest,
    source_database_url: str,
    verification_database_url: str | None = None,
    restore_database_name: str = "civiccast_drill_restore",
    pg_restore_command: list[str] | None = None,
    psql_command: list[str] | None = None,
    expected_revision: str | None = None,
) -> RestoreDrillReport:
    """Restore ``manifest``'s Postgres dump into a brand-new database and verify it.

    Mirrors :func:`run_sqlite_restore_drill`'s proof shape (schema head +
    per-table row/checksum match via the same :func:`_table_results` +
    app-store read-through) and adds the checks that only make sense on
    Postgres: installed extensions (``pg_extension``), sequence STATE (name
    + ``last_value`` + ``is_called``, not just names -- see
    :func:`_pg_sequence_states`), constraint definitions
    (:func:`_pg_constraint_defs`), index definitions (:func:`_pg_index_defs`),
    and table grants (:func:`_pg_table_grants`, with its single-role-drill
    honesty note) must all match between the source database and the
    restored copy. Constraint/index comparisons canonicalize both sides
    through the RESTORED database's own deparse (:func:`_canonicalize_defs`
    via :func:`_def_lists_mismatch`) before comparing.

    This drill restores onto the SAME cluster the source database already
    lives on, where every owner/grantee role already exists by construction
    -- so the check below on the backup manifest's ``globals_artifact``
    (:func:`civiccast.dr.backup.run_postgres_globals_backup`) can only prove
    the artifact is PRESENT and non-empty, not that it faithfully captures
    every role this database's ownership/grants depend on (this drill never
    replays ``globals.sql`` -- that would mutate shared cluster state this
    drill does not own). The REAL role-restorability proof -- replaying
    ``globals.sql`` onto a cluster that has never seen these roles and
    verifying role attributes/memberships BY COMPARISON, not by
    pattern-matching the SQL text -- is
    :func:`run_postgres_cold_standby_drill`. ``RestoreDrillReport`` has no
    Postgres-specific fields (keeps the API/report contract identical across
    engines — see ``civiccast.dr.models.RestoreDrillReport`` consumers), so
    every mismatch above is reported as an ``errors`` entry, which already
    fails :attr:`RestoreDrillReport.ok`.

    The program charter's restore precondition -- source and restored copy
    report the IDENTICAL alembic revision -- is also enforced via
    ``errors``, independently of ``schema_ok`` (which keeps its existing
    meaning: does the restored copy's revision match the code's expected
    migration head, the same question :func:`run_sqlite_restore_drill`
    answers).

    ``source_database_url`` plays the same role it plays for
    :func:`civiccast.dr.backup.run_postgres_backup`: it is parsed for
    ``--host``/``--port``/... to build the ``pg_dump``/``pg_restore``/
    ``psql`` command lines, so it must be reachable from wherever
    ``pg_restore_command``/``psql_command`` actually execute (a plain local
    URL for the default unprefixed commands; the in-container view, e.g.
    ``postgresql://user:pass@localhost:5432/db``, when the prefix is
    ``docker exec -i <container> ...``). ``verification_database_url`` is a
    separate, independent URL this function's own SQLAlchemy reads (revision,
    extensions, sequences, table snapshots) connect through directly — it
    defaults to ``source_database_url`` (the normal production case: a
    single network-reachable Postgres, no container indirection). Pass it
    explicitly whenever the two views differ, e.g. a host-mapped
    testcontainers URL alongside an in-container exec prefix.

    ``expected_revision`` overrides what ``schema_ok`` is compared against.
    It defaults to :func:`civiccast.schema_check.expected_migration_head`
    (the running CODE's migration head) -- the correct question for a
    disaster-recovery drill, where the restored copy is meant to prove the
    backup can stand back up as a live replacement for today's code.

    A PRE-UPGRADE backup drill is a different question: it restores a dump
    taken from the OLD version's database, before the new version's
    migrations have run, so comparing against the NEW code's head is always
    false the moment a release ships any migration at all (D3 root cause,
    Gate A run 33681670855). Callers verifying a pre-upgrade backup should
    pass ``expected_revision=<the source database's own revision at backup
    time>`` -- ``schema_ok`` then asks the honest question for that context:
    does the restored copy match what was actually dumped.

    A second consequence of that same PRE-UPGRADE case (found via a real
    Postgres end-to-end run of :func:`civiccast.native.upgrade.orchestrator.
    run_upgrade` in ``tests/native/test_upgrade_engine_postgres.py``, not
    hypothetically): the ``app_store_reads`` block below queries through
    this RUNNING code's own ORM models, which only understand the CURRENT
    schema. Restoring a pre-migration backup on purpose (exactly what
    ``expected_revision`` above exists for) means the restored copy is
    missing columns a later migration adds, so an unconditional ORM read
    raises a real ``psycopg.errors.UndefinedColumn`` there -- not a backup
    defect, a wrong-tool-for-the-job defect. That block therefore only runs
    when the restored copy's ACTUAL revision equals the running code's real
    head (:func:`civiccast.schema_check.expected_migration_head`, never
    ``expected_revision`` itself, which is deliberately the OLD revision for
    a pre-upgrade drill) -- the one condition under which the ORM can
    honestly read the data back. A pre-upgrade drill that skips it still
    gets the schema-agnostic proof (the row/checksum table comparison,
    above) and the schema-revision proof; only the ORM-specific query is
    skipped, and only when it would be answering a question that does not
    apply.
    """

    started_at = datetime.now(UTC)
    errors: list[str] = []
    verify_source_url = _verification_engine_url(verification_database_url or source_database_url)

    source_engine = create_engine(
        verify_source_url, future=True, **connect_options(verify_source_url)
    )
    try:
        source_revision = schema_check.read_db_revision(verify_source_url)
        source_extensions = _pg_extension_names(source_engine)
        source_sequence_states = _pg_sequence_states(source_engine)
        source_constraints = _pg_constraint_defs(source_engine)
        source_indexes = _pg_index_defs(source_engine)
        source_grants = _pg_table_grants(source_engine)
    finally:
        source_engine.dispose()

    restore_cli_url = create_fresh_postgres_database(
        database_url=source_database_url,
        database_name=restore_database_name,
        psql_command=psql_command,
    )
    run_postgres_restore(
        backup_dir / manifest.db_artifact,
        restore_cli_url,
        pg_restore_command=pg_restore_command,
    )
    restored_verify_url = _with_database_name(verify_source_url, restore_database_name)

    db_revision = schema_check.read_db_revision(restored_verify_url)
    expected_head = (
        expected_revision
        if expected_revision is not None
        else schema_check.expected_migration_head()
    )
    schema_ok = db_revision == expected_head

    if source_revision != db_revision:
        errors.append(
            "alembic revision mismatch between source and restored copy "
            f"(program-charter precondition): source={source_revision!r} "
            f"restored={db_revision!r}"
        )

    engine = create_engine(restored_verify_url, future=True, **connect_options(restored_verify_url))
    table_results: list[RestoreTableResult] = []
    app_store_reads: dict[str, int] = {}
    try:
        restored_snapshot = snapshot_tables(engine)
        table_results = _table_results(manifest.tables, restored_snapshot)
        # <installer-path-audit MA-11> Name the vacuum explicitly rather than
        # leaving RestoreDrillReport.ok's own empty-list guard to be the only
        # thing standing between "[] == []" and "confirmed every row came back
        # exactly as it was". Both sides can be empty at once -- every Postgres
        # cross-check here hardcodes schema="civiccast", so a schema-resolution
        # regression empties both.
        if not manifest.tables:
            errors.append(
                "the backup manifest names ZERO tables, so there was nothing to compare the "
                "restored copy against -- this drill proves nothing"
            )
        if not restored_snapshot:
            errors.append(
                "the restored copy enumerated ZERO tables, so the comparison had no left-hand "
                "side -- this drill proves nothing"
            )

        restored_extensions = _pg_extension_names(engine)
        if restored_extensions != source_extensions:
            errors.append(
                "pg_extension mismatch after restore: "
                f"source={source_extensions!r} restored={restored_extensions!r}"
            )

        restored_sequence_states = _pg_sequence_states(engine)
        if restored_sequence_states != source_sequence_states:
            errors.append(
                "sequence state mismatch after restore (name, last_value, is_called): "
                f"source={source_sequence_states!r} restored={restored_sequence_states!r}"
            )

        restored_constraints = _pg_constraint_defs(engine)
        constraint_diff = _def_lists_mismatch(source_constraints, restored_constraints, engine)
        if constraint_diff:
            errors.append(f"constraint definition mismatch after restore: {constraint_diff}")

        restored_indexes = _pg_index_defs(engine)
        index_diff = _def_lists_mismatch(source_indexes, restored_indexes, engine)
        if index_diff:
            errors.append(f"index definition mismatch after restore: {index_diff}")

        restored_grants = _pg_table_grants(engine)
        if restored_grants != source_grants:
            errors.append(
                "table grant mismatch after restore (see _pg_table_grants for the "
                f"single-role-drill assumption this checks): source={source_grants!r} "
                f"restored={restored_grants!r}"
            )

        if manifest.globals_artifact:
            globals_path = backup_dir / manifest.globals_artifact
            globals_sql = globals_path.read_text(encoding="utf-8") if globals_path.exists() else ""
            if not globals_sql:
                errors.append(
                    f"globals artifact {manifest.globals_artifact!r} is missing or empty -- "
                    "cluster-global roles were not captured by this backup"
                )
            # Presence/non-empty is as far as a SAME-CLUSTER drill can
            # honestly prove -- every owner/grantee role already exists on
            # this cluster regardless of whether globals.sql actually
            # captures it faithfully (see this function's docstring). The
            # real proof lives in run_postgres_cold_standby_drill.
        else:
            errors.append(
                "backup manifest has no globals_artifact -- this backup predates "
                "cluster-global role capture (civiccast.dr.backup.run_postgres_globals_backup) "
                "and cannot prove its roles are restorable"
            )

        session_factory = sessionmaker(bind=engine, future=True)
        # The app-store read-through below queries through the RUNNING
        # code's own ORM models (PostgresAssetStore/PostgresEgressStore),
        # mapped to the CURRENT code's schema -- meaningful proof for a real
        # DR drill (restoring a CURRENT-head backup, which this ORM can
        # always read) but not for a PRE-UPGRADE drill restoring an OLD,
        # pre-migration backup ON PURPOSE (Fix A's whole point:
        # ``expected_revision`` lets a caller assert the restore matches
        # what was actually dumped, not today's code). Querying an old-schema
        # table for a column a LATER migration adds raises a real
        # ``UndefinedColumn`` there -- not a backup defect, a
        # mismatched-tool-for-the-job defect this fix closes. Real Postgres
        # proof: tests/native/test_upgrade_engine_postgres.py hit exactly
        # this restoring a database one migration behind
        # 0087_retention_terms (PostgresAssetStore.list() selects
        # ``assets.retention_term_unit``, which does not exist at N-1).
        # Gate on the RESTORED COPY's actual revision matching the CODE's
        # real head (not the caller's ``expected_revision``, which for a
        # pre-upgrade drill is deliberately the OLD revision) -- that is the
        # one condition under which this ORM can honestly read the data back.
        # A pre-upgrade drill that skips this therefore still gets the
        # schema-agnostic proof (row/checksum table comparison, above) and
        # the schema-revision proof; only the ORM-specific query is skipped.
        if db_revision == schema_check.expected_migration_head():
            try:
                app_store_reads["assets"] = len(PostgresAssetStore(session_factory).list())
            except Exception as exc:
                errors.append(f"asset store read-through failed: {exc}")
            try:
                app_store_reads["egress_configs"] = len(
                    PostgresEgressStore(session_factory).list_configs()
                )
            except Exception as exc:
                errors.append(f"egress store read-through failed: {exc}")
    finally:
        engine.dispose()

    return RestoreDrillReport(
        backup_id=manifest.backup_id,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        schema_ok=schema_ok,
        db_revision=db_revision,
        expected_head=expected_head,
        tables=table_results,
        app_store_reads=app_store_reads,
        errors=errors,
    )


_ROLE_ATTR_COLUMNS = (
    "rolsuper",
    "rolcreatedb",
    "rolcreaterole",
    "rolcanlogin",
    "rolreplication",
    "rolbypassrls",
    "rolconnlimit",
)


def _pg_role_attributes(
    engine: Engine, roles: set[str]
) -> dict[str, tuple[bool, bool, bool, bool, bool, bool, int]]:
    """``rolname`` -> the tuple of :data:`_ROLE_ATTR_COLUMNS`, for every role in ``roles``.

    Fetches every ``pg_roles`` row and filters in Python rather than an
    ``= ANY(:roles)`` bind, to stay agnostic of how a given SQLAlchemy/DBAPI
    combination adapts a Python list to a Postgres array parameter --
    clusters this drill targets have a small, bounded role count, so the
    extra rows read are immaterial.
    """

    if not roles:
        return {}
    columns_sql = ", ".join(_ROLE_ATTR_COLUMNS)
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT rolname, {columns_sql} FROM pg_roles")  # noqa: S608 -- columns_sql is a fixed module-level constant tuple, not user input  # nosec B608
        ).fetchall()
    return {row[0]: tuple(row[1:]) for row in rows if row[0] in roles}


def _pg_role_memberships(engine: Engine, roles: set[str]) -> set[tuple[str, str]]:
    """(member_rolname, role_rolname) edges among ``roles`` -- ``pg_auth_members``, name-resolved.

    A same-name-but-different-membership role is exactly the drift a pure
    existence/attribute check cannot see: two clusters can each have a role
    ``ops`` with identical ``pg_roles`` attributes while one grants it
    membership in ``ops_admin`` and the other does not -- a real, silent
    difference in what that role can actually do.
    """

    if not roles:
        return set()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT m.rolname AS member, r.rolname AS role FROM pg_auth_members am "
                "JOIN pg_roles m ON m.oid = am.member "
                "JOIN pg_roles r ON r.oid = am.roleid"
            )
        ).fetchall()
    # EITHER-endpoint filter, not both (round-4 auditor finding): requiring
    # both endpoints in ``roles`` silently accepted a standby-only edge whose
    # other endpoint was outside the source-derived set -- e.g. a relevant
    # role granted membership in some unexpected standby-local group, or an
    # unexpected member added to a relevant group. With either-endpoint
    # retention, such edges appear on exactly one side of the symmetric
    # comparison and are reported. Edges touching NO relevant role remain out
    # of the drill's declared scope (documented boundary, not an oversight).
    return {(row[0], row[1]) for row in rows if row[0] in roles or row[1] in roles}


def _pg_database_owner(engine: Engine, *, database: str) -> str | None:
    """The owning role's name for ``database``, or ``None`` if it has no catalog row."""

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :db"),
            {"db": database},
        ).fetchone()
    return row[0] if row else None


def _pg_schema_owner(engine: Engine, *, schema: str = "civiccast") -> str | None:
    """The owning role's name for ``schema``, or ``None`` if it has no catalog row."""

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname = :schema"),
            {"schema": schema},
        ).fetchone()
    return row[0] if row else None


def _pg_table_owners(engine: Engine, *, schema: str = "civiccast") -> dict[str, str]:
    """table name -> owning role's name, for every table in ``schema``."""

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = :schema ORDER BY 1"
            ),
            {"schema": schema},
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def _pg_sequence_owners(engine: Engine, *, schema: str = "civiccast") -> dict[str, str]:
    """sequence name -> owning role's name, for every sequence in ``schema``."""

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT sequencename, sequenceowner FROM pg_sequences "
                "WHERE schemaname = :schema ORDER BY 1"
            ),
            {"schema": schema},
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def _replay_globals_sql(
    globals_sql: str, *, database_url: str, psql_command: list[str] | None
) -> None:
    """Replay a ``pg_dumpall --globals-only`` capture into ``database_url``'s cluster.

    Runs against the ``postgres`` maintenance database (role/globals
    statements are cluster-wide, not scoped to one database) with
    ``ON_ERROR_STOP=0``: the bootstrap role a fresh cluster/container ships
    with already exists, so ``pg_dumpall``'s own ``CREATE ROLE``/``ALTER
    ROLE`` statement for it can legitimately collide on replay. This
    function deliberately does not raise on that collision (or on any other
    per-statement SQL error) -- :func:`run_postgres_cold_standby_drill`
    verifies OUTCOMES (role attributes and memberships actually present
    after this call returns), not this replay's exit code, so one expected
    collision must not abort every other role statement that would
    otherwise have applied cleanly.
    """

    conn = _parse_postgres_url(database_url)
    env = dict(os.environ)
    if conn["password"]:
        env["PGPASSWORD"] = conn["password"]
    argv = [
        *(psql_command or ["psql"]),
        "--host",
        conn["host"],
        "--port",
        conn["port"],
        "--username",
        conn["user"],
        "--no-password",
        "--dbname",
        "postgres",
        "--set",
        "ON_ERROR_STOP=0",
        "--file",
        "-",
    ]
    subprocess.run(  # noqa: S603 -- fixed arg list, no shell, args from parsed DATABASE_URL
        argv,
        input=globals_sql.encode("utf-8"),
        env=env,
        capture_output=True,
        timeout=120,
    )


def run_postgres_cold_standby_drill(
    *,
    backup_dir: Path,
    manifest: BackupManifest,
    standby_database_url: str,
    source_engine_url: str,
    standby_verification_database_url: str | None = None,
    restore_database_name: str = "civiccast_cold_standby",
    standby_psql_command: list[str] | None = None,
    standby_pg_restore_command: list[str] | None = None,
) -> RestoreDrillReport:
    """Prove a backup restores onto an INDEPENDENTLY FRESH cluster -- the
    cold-standby proof :func:`run_postgres_restore_drill` structurally
    cannot give, because that drill restores onto the SAME cluster the
    source database already lives on, where every owner/grantee role
    already exists by construction. A real disaster (the source cluster is
    gone) restores onto a cluster that has NEVER seen those roles; this
    function is the drill for that.

    Five steps, in order:

    1. Replay ``globals.sql`` into the standby cluster via ``psql``
       (:func:`_replay_globals_sql`, ``ON_ERROR_STOP=0`` -- see that
       function's docstring for why a collision there must not abort the
       replay).
    2. Verify every relevant role exists on the standby with matching
       attributes and memberships. "Relevant" (:func:`_pg_relevant_roles`,
       ``PUBLIC`` excluded from existence-checking) is the TRANSITIVE
       closure (:func:`_close_roles_over_memberships`) of: every role that
       owns the database, the schema, a table, or a sequence, UNION every
       non-``PUBLIC`` grantee across the database ACL
       (:func:`_pg_database_acl`), schema ACL (:func:`_pg_schema_acl`),
       table grants (:func:`_pg_table_grants`), sequence ACLs
       (:func:`_pg_sequence_acls`), and default ACLs
       (:func:`_pg_default_acls`) -- closed over every ``pg_auth_members``
       edge touching that set, so a role reachable only by inheriting (or
       being inherited by) a seeded role is included too. Role ATTRIBUTES
       (:func:`_pg_role_attributes`) and MEMBERSHIPS among that closed set
       (:func:`_pg_role_memberships`) are then compared by VALUE -- a real
       comparison, not a regex over the replayed SQL text the way
       :func:`_role_captured_in_globals` works.
    3. ``CREATE DATABASE`` on the standby, then ``pg_restore`` the backup's
       dump artifact WITH ownership (``preserve_ownership=True`` on
       :func:`civiccast.dr.backup.run_postgres_restore` -- ``--no-owner`` is
       dropped): correct here specifically BECAUSE steps 1-2 already proved
       the roles those ownership statements reference exist on this
       cluster. A restore failure at this step (e.g. an ownership/grant
       statement referencing a role step 2 already reported missing) is
       caught and reported as an ``errors`` entry rather than raised, so a
       broken globals capture is a red REPORT, not a crashed drill.
    4. Compare source vs. standby: database owner, schema owner, per-table
       owners, per-sequence owners, table grants, the FULL ACL surface
       (database CONNECT/TEMP/CREATE, schema USAGE/CREATE, sequence
       USAGE/SELECT/UPDATE, and default ACLs -- each its own comparison with
       a distinct error label), plus the full equivalence suite this drill
       shares with the same-cluster drill (tables/checksums, sequence
       STATE, constraints/indexes via the same-server canonicalized
       comparison -- with the STANDBY as the canon server -- extensions,
       alembic revision, app read-through).
    5. Return a :class:`~civiccast.dr.models.RestoreDrillReport` (the same
       report shape :func:`run_postgres_restore_drill` returns); every
       mismatch above is an ``errors`` entry prefixed ``"cold-standby:"``.

    Honest note on the ACL comparisons (per :func:`_pg_database_acl` and
    siblings): they are NAME-RESOLVED, like every other comparison in this
    module -- grantor and grantee identity are compared by role NAME
    (``pg_roles.rolname``), not internal oid. A grantor recreated under the
    same name but a different oid compares EQUAL here, which is correct for
    this program's single-station, name-is-identity deployment model.

    ``source_engine_url`` is a plain, directly-reachable URL this function's
    own SQLAlchemy reads connect through (mirrors ``verification_database_url``
    elsewhere in this module). ``standby_database_url`` plays the dual role
    ``source_database_url`` plays in :func:`run_postgres_restore_drill`: it
    is parsed for ``--host``/``--port``/... to build the ``psql``/
    ``pg_restore`` command lines AND is the default URL this function's own
    SQLAlchemy reads use, unless ``standby_verification_database_url`` is
    given (the same host-mapped-vs-in-container split as elsewhere in this
    module, needed whenever the standby cluster is, e.g., a second
    testcontainer).

    Honest boundary: proven by
    ``tests/dr/test_postgres_restore.py::test_cold_standby_fresh_cluster_round_trip``
    (two testcontainers, Docker-gated) and its negative controls. Wiring an
    operator-facing CLI entry point for this drill is explicit follow-up,
    NOT done in this pass -- :func:`civiccast.dr.report.run_full_drill` is
    intentionally left unchanged (same-cluster drill only); see that
    module's docstring.
    """

    started_at = datetime.now(UTC)
    errors: list[str] = []

    standby_verify_url = _verification_engine_url(
        standby_verification_database_url or standby_database_url
    )

    source_engine = create_engine(
        _verification_engine_url(source_engine_url),
        future=True,
        **connect_options(_verification_engine_url(source_engine_url)),
    )
    try:
        source_revision = schema_check.read_db_revision(source_engine_url)
        source_extensions = _pg_extension_names(source_engine)
        source_sequence_states = _pg_sequence_states(source_engine)
        source_constraints = _pg_constraint_defs(source_engine)
        source_indexes = _pg_index_defs(source_engine)
        source_grants = _pg_table_grants(source_engine)
        source_relevant_roles = _pg_relevant_roles(source_engine)
        source_role_attrs = _pg_role_attributes(source_engine, source_relevant_roles)
        source_role_memberships = _pg_role_memberships(source_engine, source_relevant_roles)
        source_schema_owner = _pg_schema_owner(source_engine)
        source_table_owners = _pg_table_owners(source_engine)
        source_sequence_owners = _pg_sequence_owners(source_engine)
        source_db_name = urlsplit(source_engine_url).path.lstrip("/")
        source_db_owner = _pg_database_owner(source_engine, database=source_db_name)
        source_database_acl = _pg_database_acl(source_engine, database=source_db_name)
        source_schema_acl = _pg_schema_acl(source_engine)
        source_sequence_acls = _pg_sequence_acls(source_engine)
        source_default_acls = _pg_default_acls(source_engine, owners=source_relevant_roles)
    finally:
        source_engine.dispose()

    globals_sql = ""
    if not manifest.globals_artifact:
        errors.append(
            "cold-standby: backup manifest has no globals_artifact -- cannot prove role "
            "restorability on a fresh cluster"
        )
    else:
        globals_path = backup_dir / manifest.globals_artifact
        globals_sql = globals_path.read_text(encoding="utf-8") if globals_path.exists() else ""
        if not globals_sql:
            errors.append(
                f"cold-standby: globals artifact {manifest.globals_artifact!r} is missing or "
                "empty -- cluster-global roles were not captured by this backup"
            )

    if globals_sql:
        _replay_globals_sql(
            globals_sql, database_url=standby_database_url, psql_command=standby_psql_command
        )

    standby_bootstrap_engine = create_engine(
        standby_verify_url, future=True, **connect_options(standby_verify_url)
    )
    try:
        standby_role_attrs = _pg_role_attributes(standby_bootstrap_engine, source_relevant_roles)
        standby_role_memberships = _pg_role_memberships(
            standby_bootstrap_engine, source_relevant_roles
        )
    finally:
        standby_bootstrap_engine.dispose()

    missing_roles = sorted(source_relevant_roles - set(standby_role_attrs))
    if missing_roles:
        errors.append(
            f"cold-standby: role(s) missing on standby after globals replay: {missing_roles!r}"
        )
    mismatched_roles = sorted(
        role
        for role in source_relevant_roles & set(standby_role_attrs)
        if source_role_attrs[role] != standby_role_attrs[role]
    )
    if mismatched_roles:
        errors.append(
            "cold-standby: role attribute mismatch after globals replay: "
            + "; ".join(
                f"{role!r}: source={source_role_attrs[role]!r} standby={standby_role_attrs[role]!r}"
                for role in mismatched_roles
            )
        )
    if source_role_memberships != standby_role_memberships:
        errors.append(
            "cold-standby: role membership mismatch after globals replay: "
            f"source={sorted(source_role_memberships)!r} "
            f"standby={sorted(standby_role_memberships)!r}"
        )

    restore_cli_url = create_fresh_postgres_database(
        database_url=standby_database_url,
        database_name=restore_database_name,
        psql_command=standby_psql_command,
    )
    restored_verify_url = _with_database_name(standby_verify_url, restore_database_name)

    try:
        run_postgres_restore(
            backup_dir / manifest.db_artifact,
            restore_cli_url,
            pg_restore_command=standby_pg_restore_command,
            preserve_ownership=True,
        )
    except RuntimeError as exc:
        errors.append(
            f"cold-standby: pg_restore failed (see role-verification errors above for a "
            f"likely cause): {exc}"
        )
        return RestoreDrillReport(
            backup_id=manifest.backup_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            schema_ok=False,
            db_revision=None,
            expected_head=schema_check.expected_migration_head(),
            tables=[],
            app_store_reads={},
            errors=errors,
        )

    db_revision = schema_check.read_db_revision(restored_verify_url)
    expected_head = schema_check.expected_migration_head()
    schema_ok = db_revision == expected_head
    if source_revision != db_revision:
        errors.append(
            "cold-standby: alembic revision mismatch between source and standby copy: "
            f"source={source_revision!r} standby={db_revision!r}"
        )

    engine = create_engine(restored_verify_url, future=True, **connect_options(restored_verify_url))
    table_results: list[RestoreTableResult] = []
    app_store_reads: dict[str, int] = {}
    try:
        table_results = _table_results(manifest.tables, snapshot_tables(engine))

        standby_extensions = _pg_extension_names(engine)
        if standby_extensions != source_extensions:
            errors.append(
                "cold-standby: pg_extension mismatch: "
                f"source={source_extensions!r} standby={standby_extensions!r}"
            )

        standby_sequence_states = _pg_sequence_states(engine)
        if standby_sequence_states != source_sequence_states:
            errors.append(
                "cold-standby: sequence state mismatch (name, last_value, is_called): "
                f"source={source_sequence_states!r} standby={standby_sequence_states!r}"
            )

        standby_constraints = _pg_constraint_defs(engine)
        constraint_diff = _def_lists_mismatch(source_constraints, standby_constraints, engine)
        if constraint_diff:
            errors.append(f"cold-standby: constraint definition mismatch: {constraint_diff}")

        standby_indexes = _pg_index_defs(engine)
        index_diff = _def_lists_mismatch(source_indexes, standby_indexes, engine)
        if index_diff:
            errors.append(f"cold-standby: index definition mismatch: {index_diff}")

        standby_grants = _pg_table_grants(engine)
        if standby_grants != source_grants:
            errors.append(
                f"cold-standby: table grant mismatch: source={source_grants!r} "
                f"standby={standby_grants!r}"
            )

        standby_db_name = urlsplit(restored_verify_url).path.lstrip("/")
        standby_db_owner = _pg_database_owner(engine, database=standby_db_name)
        if standby_db_owner != source_db_owner:
            errors.append(
                f"cold-standby: database owner mismatch: source={source_db_owner!r} "
                f"standby={standby_db_owner!r}"
            )

        standby_schema_owner = _pg_schema_owner(engine)
        if standby_schema_owner != source_schema_owner:
            errors.append(
                f"cold-standby: schema owner mismatch: source={source_schema_owner!r} "
                f"standby={standby_schema_owner!r}"
            )

        standby_table_owners = _pg_table_owners(engine)
        if standby_table_owners != source_table_owners:
            errors.append(
                f"cold-standby: table owner mismatch: source={source_table_owners!r} "
                f"standby={standby_table_owners!r}"
            )

        standby_sequence_owners = _pg_sequence_owners(engine)
        if standby_sequence_owners != source_sequence_owners:
            errors.append(
                f"cold-standby: sequence owner mismatch: source={source_sequence_owners!r} "
                f"standby={standby_sequence_owners!r}"
            )

        # Round-4 additions (CC-WS2-001, Critical): full ACL surface, not
        # just table grants -- database CONNECT/TEMP/CREATE, schema
        # USAGE/CREATE, sequence USAGE/SELECT/UPDATE, and default ACLs
        # (privileges an owner has pre-declared for objects it creates in
        # the future). Each compared source vs standby independently, with
        # its own distinct error-message label, so an operator sees exactly
        # which ACL CLASS drifted rather than one undifferentiated blob.
        standby_database_acl = _pg_database_acl(engine, database=standby_db_name)
        if standby_database_acl != source_database_acl:
            errors.append(
                "cold-standby: database privilege (ACL) mismatch: "
                f"source={source_database_acl!r} standby={standby_database_acl!r}"
            )

        standby_schema_acl = _pg_schema_acl(engine)
        if standby_schema_acl != source_schema_acl:
            errors.append(
                "cold-standby: schema privilege (ACL) mismatch: "
                f"source={source_schema_acl!r} standby={standby_schema_acl!r}"
            )

        standby_sequence_acls = _pg_sequence_acls(engine)
        if standby_sequence_acls != source_sequence_acls:
            errors.append(
                "cold-standby: sequence privilege (ACL) mismatch: "
                f"source={source_sequence_acls!r} standby={standby_sequence_acls!r}"
            )

        standby_default_acls = _pg_default_acls(engine, owners=source_relevant_roles)
        if standby_default_acls != source_default_acls:
            errors.append(
                "cold-standby: default ACL (ALTER DEFAULT PRIVILEGES) mismatch: "
                f"source={source_default_acls!r} standby={standby_default_acls!r}"
            )

        session_factory = sessionmaker(bind=engine, future=True)
        try:
            app_store_reads["assets"] = len(PostgresAssetStore(session_factory).list())
        except Exception as exc:
            errors.append(f"cold-standby: asset store read-through failed: {exc}")
        try:
            app_store_reads["egress_configs"] = len(
                PostgresEgressStore(session_factory).list_configs()
            )
        except Exception as exc:
            errors.append(f"cold-standby: egress store read-through failed: {exc}")
    finally:
        engine.dispose()

    return RestoreDrillReport(
        backup_id=manifest.backup_id,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        schema_ok=schema_ok,
        db_revision=db_revision,
        expected_head=expected_head,
        tables=table_results,
        app_store_reads=app_store_reads,
        errors=errors,
    )
