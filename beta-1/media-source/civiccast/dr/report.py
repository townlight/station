# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Orchestrate backup -> restore drill -> crash drill, and render the report."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from civiccast.dr.backup import run_full_backup
from civiccast.dr.crash_drill import run_daemon_crash_restart_drill
from civiccast.dr.models import CrashDrillReport, DrillReport
from civiccast.dr.restore_drill import run_postgres_restore_drill, run_sqlite_restore_drill

_HONEST_NOTES = [
    "Media backup is a manifest + bounded sampled hash, not full-file replication; "
    "keep your own file-level backup of the media library.",
    "Postgres backup/restore is implemented AND executed via the SHIPPING entry point: "
    "tests/dr/test_postgres_restore.py's test_run_full_drill_postgres_end_to_end calls "
    "this module's own run_full_drill (not a test-only helper reimplementation) end-to-end "
    "-- pg_dump -> fresh-database -> pg_restore, plus cluster-global role capture -- under "
    "CI's Docker gate (CIVICCAST_RUN_POSTGRES_TESTS=1). It is not executed by this "
    "particular run unless database_url above is a postgresql:// URL.",
    "Crash-recovery drill covers daemon auto-restart only; recording-finalization "
    "mid-settle recovery is not yet a drill (see civiccast.dr package docstring).",
    "Multi-machine hot failover is out of scope; a fresh-cluster cold-standby restore "
    "has a dedicated proof function (civiccast.dr.restore_drill.run_postgres_cold_"
    "standby_drill -- role/ownership/privilege restorability verified by comparison "
    "against an independently fresh cluster), but an operator-facing CLI entry point "
    "for it is follow-up, not wired into run_full_drill in this pass.",
]


def run_full_drill(
    *,
    database_url: str,
    backup_dir: Path,
    work_dir: Path,
    media_root: Path | None = None,
    command_database_url: str | None = None,
    pg_dump_command: list[str] | None = None,
    pg_dumpall_command: list[str] | None = None,
    pg_restore_command: list[str] | None = None,
    psql_command: list[str] | None = None,
) -> DrillReport:
    """Run backup -> restore -> crash drills and return the combined report.

    The restore drill is dispatched by ``database_url``'s scheme: SQLite
    runs the in-process file-copy restore
    (:func:`civiccast.dr.restore_drill.run_sqlite_restore_drill`); Postgres
    runs a real ``pg_dump``/``pg_restore`` round trip into a freshly created
    database (:func:`civiccast.dr.restore_drill.run_postgres_restore_drill`).
    Any other scheme is rejected rather than silently skipped.

    The five ``*_command``/``command_database_url`` parameters exist so this
    SHIPPING entry point -- not a test-only reimplementation of it -- can be
    driven against a containerized Postgres server in CI
    (tests/dr/test_postgres_restore.py's ``test_run_full_drill_postgres_end_to_end``):
    ``command_database_url`` defaults to ``database_url`` and is what
    ``pg_dump``/``pg_dumpall``/``pg_restore``/``psql`` parse for their own
    argv (the in-container view); ``database_url`` itself stays the
    SQLAlchemy view every direct connection in this call graph uses. All
    default to ``None``, which reproduces the exact prior behavior (plain
    local commands, single database view) for every existing caller.
    """

    manifest = run_full_backup(
        database_url=database_url,
        dest_dir=backup_dir,
        media_root=media_root,
        command_database_url=command_database_url,
        pg_dump_command=pg_dump_command,
        pg_dumpall_command=pg_dumpall_command,
    )
    if database_url.startswith("sqlite"):
        restore_report = run_sqlite_restore_drill(
            backup_dir=backup_dir, manifest=manifest, work_dir=work_dir / "restore"
        )
    elif database_url.startswith("postgresql"):
        restore_report = run_postgres_restore_drill(
            backup_dir=backup_dir,
            manifest=manifest,
            source_database_url=command_database_url or database_url,
            verification_database_url=database_url,
            pg_restore_command=pg_restore_command,
            psql_command=psql_command,
        )
    else:
        raise ValueError(f"Unsupported DATABASE_URL scheme for the restore drill: {database_url!r}")
    crash_result = run_daemon_crash_restart_drill(work_dir=work_dir / "crash")
    crash_report = CrashDrillReport(results=[crash_result])
    return DrillReport(
        generated_at=datetime.now(UTC),
        backup=manifest,
        restore=restore_report,
        crash=crash_report,
        honest_notes=list(_HONEST_NOTES),
    )


def render_markdown(report: DrillReport) -> str:
    """Two-voice markdown: plain-language verdict, then technical detail."""

    verdict = "PASSED" if report.ok else "FAILED"
    lines = [
        f"# CivicCast disaster-recovery drill — {verdict}",
        "",
        f"_Generated {report.generated_at.isoformat()}_",
        "",
        "## Plain-language verdict",
        "",
    ]
    if report.ok:
        lines.append(
            "Backup and restore were tested for real: CivicCast backed up the actual "
            "station database, restored it into a brand-new database, and confirmed "
            "every row came back exactly as it was. The channel-output process was "
            "also crashed on purpose, and CivicCast brought it back automatically."
        )
    else:
        lines.append(
            "This drill FAILED. Do not treat this station's backups as trustworthy "
            "until the technical detail below is fixed and the drill passes."
        )
    lines += [
        "",
        "## Technical detail",
        "",
        f"- Backup ID: `{report.backup.backup_id}` (engine: {report.backup.engine})",
        f"- Tables backed up: {len(report.backup.tables)}",
        f"- Media files manifested: {len(report.backup.media_entries)}",
        "",
        f"### Restore drill: {'PASSED' if report.restore.ok else 'FAILED'}",
        f"- Schema head match: {report.restore.schema_ok} "
        f"(db={report.restore.db_revision!r}, expected={report.restore.expected_head!r})",
        f"- App-store read-through: {report.restore.app_store_reads}",
    ]
    for t in report.restore.tables:
        mark = "OK" if t.matched else "MISMATCH"
        lines.append(f"  - [{mark}] `{t.name}`: rows {t.actual_row_count}/{t.expected_row_count}")
    if report.restore.errors:
        lines.append("- Errors:")
        lines.extend(f"  - {e}" for e in report.restore.errors)
    lines += [
        "",
        f"### Crash-recovery drill: {'PASSED' if report.crash.ok else 'FAILED'}",
    ]
    for r in report.crash.results:
        lines.append(
            f"  - [{'OK' if r.ok else 'FAIL'}] `{r.name}` ({r.duration_seconds:.2f}s): {r.detail}"
        )
    lines += ["", "## Honest boundaries of this drill", ""]
    lines.extend(f"- {note}" for note in report.honest_notes)
    return "\n".join(lines) + "\n"


def write_report(report: DrillReport, out_dir: Path) -> tuple[Path, Path]:
    """Write both docs-ready markdown and machine-readable JSON evidence."""

    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "dr-drill-report.md"
    json_path = out_dir / "dr-drill-report.json"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return md_path, json_path
