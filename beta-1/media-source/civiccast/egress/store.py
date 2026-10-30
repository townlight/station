# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Store contracts for channel egress."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Literal, Protocol, TypedDict, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.egress.models import (
    CanonicalProfile,
    EgressCaptionProofSample,
    EgressCaptionProofSampleDb,
    EgressCommand,
    EgressCommandDb,
    EgressConfig,
    EgressConfigDb,
    EgressHealthSample,
    EgressHealthSampleDb,
    EgressProofEvent,
    EgressProofEventDb,
    EgressSinkDb,
    EgressSinkSpec,
    EgressStateDb,
    EgressStateRow,
    LoudnessRegime,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]

# S9 §6.5 — proof-event churn cap. A broken source can write ~900 proof events/hr; left
# unbounded that eats disk and makes the proof log unreadable. Keep the most recent
# 10k per channel; trim the oldest 1k when an append would exceed it (GC-style; no
# operator action). 10k ≈ ~10h of churn — a forensic window without unbounded growth.
MAX_PROOF_EVENTS_PER_CHANNEL = 10_000
TRIM_BATCH_SIZE = 1_000

# Keep consumed command history in the same bounded forensic window as egress
# proof events. Pending commands are never eligible for pruning.
MAX_COMMAND_HISTORY_PER_CHANNEL = MAX_PROOF_EVENTS_PER_CHANNEL
COMMAND_HISTORY_TRIM_BATCH_SIZE = TRIM_BATCH_SIZE


class CommandStorageUsage(TypedDict):
    row_count: int
    pending_count: int
    storage_bytes: int | None


# S11a — caption decode-back proofs are sampled periodically per ON_AIR channel;
# keep a bounded forensic window the same GC way as proof events.
MAX_CAPTION_PROOFS_PER_CHANNEL = 5_000
CAPTION_TRIM_BATCH_SIZE = 500


class EgressStore(Protocol):
    """Persistence contract shared by the web control plane and egress daemon."""

    def get_config(self, channel_id: str) -> EgressConfig | None: ...

    def list_configs(self) -> list[EgressConfig]: ...

    def upsert_config(self, config: EgressConfig) -> None: ...

    def enqueue_command(self, cmd: EgressCommand) -> None: ...

    def enqueue_commands(self, cmds: Sequence[EgressCommand]) -> None:
        """Enqueue several commands as ONE durable write: all land or none do.

        For a pair that only makes sense together -- the headend-preset
        route's ``stop`` + ``start`` restart of a slate channel -- two
        separate ``enqueue_command`` commits leave two gaps (review round 3
        delta, MINOR 1): a daemon poll between them drains only the ``stop``
        and the channel is dark for a whole poll interval, and an API-process
        death between them leaves the channel STOPPED with no start queued."""
        ...

    def pop_pending_commands(self, channel_id: str) -> list[EgressCommand]: ...

    def command_storage_usage(self) -> CommandStorageUsage:
        """Return command-row counts and table bytes when the dialect reports them."""
        ...

    def peek_pending_commands(self, channel_id: str) -> list[EgressCommand]:
        """The channel's not-yet-consumed commands WITHOUT marking them consumed
        (same order as ``pop_pending_commands``). For a decision that must
        yield to a queued operator command -- e.g. the daemon's automatic
        slate-EOS relaunch stepping aside for a queued ``stop``/``drain``
        that ``process_once`` will drain later this same tick."""
        ...

    def write_state(self, row: EgressStateRow) -> None: ...

    def read_state(self, channel_id: str) -> EgressStateRow | None: ...

    def recover_stale_state(self, row: EgressStateRow, command: EgressCommand) -> None:
        """Atomically clear a stale state row AND enqueue a recovery command.

        Restart reconciliation must not be able to leave the channel dark: if
        the state were cleared to STOPPED and the process died before the start
        was enqueued, the next startup would not retry (STOPPED is not
        reconciled). Implementations MUST persist BOTH in ONE transaction --
        all or nothing.

        STRICT FAIL-CLOSED on duplicate/failed command insert. The caller mints
        a fresh uuid4 command id, so a duplicate id is never a legitimate
        expected case (it implies a consumed command, a wrong-channel/action
        collision, or an unrelated constraint failure). Implementations MUST
        NOT tolerate it by committing the state alone: ANY failure rolls back
        and raises so the original stale row survives for the next startup.
        """
        ...

    def append_health(self, sample: EgressHealthSample) -> None: ...

    def recent_health(self, channel_id: str, limit: int) -> list[EgressHealthSample]: ...

    def trim_health_before(self, cutoff: datetime) -> int: ...

    def append_proof_event(self, event: EgressProofEvent) -> None: ...

    def recent_proof_events(self, channel_id: str, limit: int) -> list[EgressProofEvent]: ...

    def count_proof_events_since(self, channel_id: str, since: datetime | None) -> int: ...

    def append_caption_proof_sample(self, sample: EgressCaptionProofSample) -> None: ...

    def recent_caption_proof_samples(
        self, channel_id: str, limit: int
    ) -> list[EgressCaptionProofSample]: ...

    def latest_caption_proof_sample(self, channel_id: str) -> EgressCaptionProofSample | None: ...


class InMemoryEgressStore:
    """In-memory egress store for tests and local development."""

    def __init__(self) -> None:
        # Guards the compound read-modify-write in recover_stale_state so a
        # concurrent reader can never observe STOPPED with no queued start.
        self._recover_lock = threading.RLock()
        self._configs: dict[str, EgressConfig] = {}
        self._commands: list[EgressCommand] = []
        self._consumed_command_channels: dict[str, str] = {}
        self._consumed_command_ids_by_channel: dict[str, OrderedDict[str, None]] = {}
        self._states: dict[str, EgressStateRow] = {}
        self._health: list[EgressHealthSample] = []
        self._proof_events: list[EgressProofEvent] = []
        self._caption_proofs: list[EgressCaptionProofSample] = []

    def get_config(self, channel_id: str) -> EgressConfig | None:
        return self._configs.get(channel_id)

    def list_configs(self) -> list[EgressConfig]:
        return sorted(self._configs.values(), key=lambda config: config.channel_id)

    def upsert_config(self, config: EgressConfig) -> None:
        self._configs[config.channel_id] = config

    def enqueue_command(self, cmd: EgressCommand) -> None:
        self.enqueue_commands([cmd])

    def enqueue_commands(self, cmds: Sequence[EgressCommand]) -> None:
        # One list assignment at the end: a reader on another thread sees
        # either none of the batch or all of it, never a prefix.
        pending = list(self._commands)
        for cmd in cmds:
            if cmd.command_id in self._consumed_command_channels:
                continue
            if any(existing.command_id == cmd.command_id for existing in pending):
                continue
            pending.append(cmd)
        self._commands = pending

    def pop_pending_commands(self, channel_id: str) -> list[EgressCommand]:
        pending = [cmd for cmd in self._commands if cmd.channel_id == channel_id]
        if not pending:
            return []
        pending_ids = {cmd.command_id for cmd in pending}
        self._commands = [cmd for cmd in self._commands if cmd.command_id not in pending_ids]
        ordered_pending = sorted(pending, key=lambda cmd: (cmd.issued_at, cmd.command_id))
        self._remember_consumed_commands(channel_id, [cmd.command_id for cmd in ordered_pending])
        return ordered_pending

    def _remember_consumed_commands(self, channel_id: str, command_ids: Sequence[str]) -> None:
        recent = self._consumed_command_ids_by_channel.setdefault(channel_id, OrderedDict())
        for command_id in command_ids:
            recent[command_id] = None
            self._consumed_command_channels[command_id] = channel_id
        while len(recent) > MAX_COMMAND_HISTORY_PER_CHANNEL:
            oldest_id, _ = recent.popitem(last=False)
            self._consumed_command_channels.pop(oldest_id, None)

    def command_storage_usage(self) -> CommandStorageUsage:
        consumed_rows = sum(
            len(command_ids) for command_ids in self._consumed_command_ids_by_channel.values()
        )
        return {
            "row_count": len(self._commands) + consumed_rows,
            "pending_count": len(self._commands),
            "storage_bytes": None,
        }

    def peek_pending_commands(self, channel_id: str) -> list[EgressCommand]:
        pending = [cmd for cmd in self._commands if cmd.channel_id == channel_id]
        return sorted(pending, key=lambda cmd: (cmd.issued_at, cmd.command_id))

    def write_state(self, row: EgressStateRow) -> None:
        self._states[row.channel_id] = row

    def recover_stale_state(self, row: EgressStateRow, command: EgressCommand) -> None:
        # STRICT FAIL-CLOSED CONTRACT (mirrors the SQL store): the caller mints
        # a fresh uuid4 id, so a duplicate/consumed id is never legitimate. If
        # this id is already known, raise and change NOTHING -- do not publish
        # a STOPPED state with no real pending start. The lock serializes the
        # check-then-write; the command is published BEFORE the state so a
        # concurrent reader never sees STOPPED without its queued start.
        with self._recover_lock:
            if command.command_id in self._consumed_command_channels or any(
                existing.command_id == command.command_id for existing in self._commands
            ):
                raise ValueError(f"recovery command id already exists: {command.command_id!r}")
            self._commands = [*self._commands, command]
            self._states[row.channel_id] = row

    def read_state(self, channel_id: str) -> EgressStateRow | None:
        return self._states.get(channel_id)

    def append_health(self, sample: EgressHealthSample) -> None:
        self._health.append(sample)

    def recent_health(self, channel_id: str, limit: int) -> list[EgressHealthSample]:
        if limit <= 0:
            return []
        samples = [
            (index, sample)
            for index, sample in enumerate(self._health)
            if sample.channel_id == channel_id
        ]
        return [
            sample
            for _, sample in sorted(
                samples,
                key=lambda item: (item[1].sampled_at, item[0]),
                reverse=True,
            )[:limit]
        ]

    def trim_health_before(self, cutoff: datetime) -> int:
        kept = [sample for sample in self._health if sample.sampled_at >= cutoff]
        deleted = len(self._health) - len(kept)
        self._health = kept
        return deleted

    def append_proof_event(self, event: EgressProofEvent) -> None:
        if any(existing.event_id == event.event_id for existing in self._proof_events):
            return
        self._proof_events.append(event)
        self._trim_proof_events(event.channel_id)

    def _trim_proof_events(self, channel_id: str) -> None:
        channel_events = [e for e in self._proof_events if e.channel_id == channel_id]
        if len(channel_events) <= MAX_PROOF_EVENTS_PER_CHANNEL:
            return
        drop = {
            e.event_id
            for e in sorted(channel_events, key=lambda e: e.observed_at)[:TRIM_BATCH_SIZE]
        }
        self._proof_events = [e for e in self._proof_events if e.event_id not in drop]

    def recent_proof_events(self, channel_id: str, limit: int) -> list[EgressProofEvent]:
        if limit <= 0:
            return []
        events = [
            (index, event)
            for index, event in enumerate(self._proof_events)
            if event.channel_id == channel_id
        ]
        return [
            event
            for _, event in sorted(
                events,
                key=lambda item: (item[1].observed_at, item[0]),
                reverse=True,
            )[:limit]
        ]

    def count_proof_events_since(self, channel_id: str, since: datetime | None) -> int:
        return sum(
            1
            for event in self._proof_events
            if event.channel_id == channel_id and (since is None or event.observed_at > since)
        )

    def append_caption_proof_sample(self, sample: EgressCaptionProofSample) -> None:
        self._caption_proofs.append(sample)
        channel = [s for s in self._caption_proofs if s.channel_id == sample.channel_id]
        if len(channel) <= MAX_CAPTION_PROOFS_PER_CHANNEL:
            return
        keep_n = len(channel) - CAPTION_TRIM_BATCH_SIZE
        keep = {id(s) for s in sorted(channel, key=lambda s: s.sampled_at)[-keep_n:]}
        self._caption_proofs = [
            s for s in self._caption_proofs if s.channel_id != sample.channel_id or id(s) in keep
        ]

    def recent_caption_proof_samples(
        self, channel_id: str, limit: int
    ) -> list[EgressCaptionProofSample]:
        if limit <= 0:
            return []
        samples = [
            (index, sample)
            for index, sample in enumerate(self._caption_proofs)
            if sample.channel_id == channel_id
        ]
        return [
            sample
            for _, sample in sorted(
                samples, key=lambda item: (item[1].sampled_at, item[0]), reverse=True
            )[:limit]
        ]

    def latest_caption_proof_sample(self, channel_id: str) -> EgressCaptionProofSample | None:
        recent = self.recent_caption_proof_samples(channel_id, 1)
        return recent[0] if recent else None


class PostgresEgressStore:
    """SQLAlchemy-backed egress store for installer-managed SQLite and Postgres."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def get_config(self, channel_id: str) -> EgressConfig | None:
        with self._session_factory() as session:
            row = session.execute(
                select(EgressConfigDb).where(EgressConfigDb.channel_id == channel_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            sink_rows = (
                session.execute(
                    select(EgressSinkDb)
                    .where(EgressSinkDb.channel_id == channel_id)
                    .order_by(EgressSinkDb.position.asc(), EgressSinkDb.label.asc())
                )
                .scalars()
                .all()
            )
            return _config_from_rows(row, sink_rows)

    def list_configs(self) -> list[EgressConfig]:
        with self._session_factory() as session:
            rows = (
                session.execute(select(EgressConfigDb).order_by(EgressConfigDb.channel_id.asc()))
                .scalars()
                .all()
            )
            if not rows:
                return []
            channel_ids = [row.channel_id for row in rows]
            sink_rows = (
                session.execute(
                    select(EgressSinkDb)
                    .where(EgressSinkDb.channel_id.in_(channel_ids))
                    .order_by(
                        EgressSinkDb.channel_id.asc(),
                        EgressSinkDb.position.asc(),
                        EgressSinkDb.label.asc(),
                    )
                )
                .scalars()
                .all()
            )
            sinks_by_channel: dict[str, list[EgressSinkDb]] = {
                channel_id: [] for channel_id in channel_ids
            }
            for sink in sink_rows:
                sinks_by_channel.setdefault(sink.channel_id, []).append(sink)
            return [_config_from_rows(row, sinks_by_channel[row.channel_id]) for row in rows]

    def upsert_config(self, config: EgressConfig) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            row = session.execute(
                select(EgressConfigDb).where(EgressConfigDb.channel_id == config.channel_id)
            ).scalar_one_or_none()
            values = {
                "enabled": config.enabled,
                "auto_start": config.auto_start,
                "allow_software_fallback": config.allow_software_fallback,
                "fill_policy": config.fill_policy,
                "ndi_relay_name": config.ndi_relay_name,
                "sdi_relay_device": config.sdi_relay_device,
                "slate_message": config.slate_message,
                "graphics_overlay_enabled": config.graphics_overlay_enabled,
                "graphics_overlay_lower_third_text": config.graphics_overlay_lower_third_text,
                "loudness_target_lufs": config.loudness_target_lufs,
                "loudness_tolerance_lufs": config.loudness_tolerance_lufs,
                "canonical_profile_json": config.canonical_profile.model_dump_json(),
                "updated_at": now,
            }
            if row is None:
                session.add(EgressConfigDb(channel_id=config.channel_id, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)

            session.execute(
                delete(EgressSinkDb).where(EgressSinkDb.channel_id == config.channel_id)
            )
            for position, sink in enumerate(config.sinks):
                session.add(
                    EgressSinkDb(
                        channel_id=config.channel_id,
                        label=sink.label,
                        position=position,
                        kind=sink.kind,
                        uri=sink.uri,
                        secret_ref=sink.secret_ref,
                        latency_ms=sink.latency_ms,
                        extra_output_args_json=json.dumps(sink.extra_output_args),
                        loudness_regime=sink.loudness_regime,
                        loudness_target_lufs=sink.loudness_target_lufs,
                        loudness_tolerance_lufs=sink.loudness_tolerance_lufs,
                        eas_tone_strip_enabled=sink.eas_tone_strip_enabled,
                    )
                )
            session.commit()

    def enqueue_command(self, cmd: EgressCommand) -> None:
        self.enqueue_commands([cmd])

    def enqueue_commands(self, cmds: Sequence[EgressCommand]) -> None:
        # One session, one commit: the whole batch becomes visible to
        # ``pop_pending_commands`` atomically, and a crash before the commit
        # leaves none of it behind (see the Protocol docstring).
        with self._session_factory() as session:
            added = False
            for cmd in cmds:
                existing = session.execute(
                    select(EgressCommandDb).where(EgressCommandDb.command_id == cmd.command_id)
                ).scalar_one_or_none()
                if existing is not None:
                    continue
                session.add(
                    EgressCommandDb(
                        command_id=cmd.command_id,
                        channel_id=cmd.channel_id,
                        action=cmd.action,
                        issued_at=cmd.issued_at,
                        issued_by=cmd.issued_by,
                    )
                )
                added = True
            if not added:
                return
            try:
                session.commit()
            except IntegrityError:
                session.rollback()

    def pop_pending_commands(self, channel_id: str) -> list[EgressCommand]:
        consumed_at = datetime.now(UTC)
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(EgressCommandDb)
                    .where(EgressCommandDb.channel_id == channel_id)
                    .where(EgressCommandDb.consumed_at.is_(None))
                    .order_by(EgressCommandDb.issued_at.asc(), EgressCommandDb.command_id.asc())
                )
                .scalars()
                .all()
            )
            commands = [_command_from_row(row) for row in rows]
            if rows:
                ids = [row.command_id for row in rows]
                session.execute(
                    update(EgressCommandDb)
                    .where(EgressCommandDb.command_id.in_(ids))
                    .values(consumed_at=consumed_at)
                )
            trimmed = self._trim_consumed_command_history(session, channel_id)
            if rows or trimmed:
                session.commit()
            return commands

    @staticmethod
    def _trim_consumed_command_history(session: Session, channel_id: str) -> int:
        """Prune one batch only when an indexed lookup finds row cap + 1."""
        oldest_over_limit = session.execute(
            select(EgressCommandDb.command_id)
            .where(EgressCommandDb.channel_id == channel_id)
            .where(EgressCommandDb.consumed_at.is_not(None))
            .order_by(
                EgressCommandDb.consumed_at.asc(),
                EgressCommandDb.issued_at.asc(),
                EgressCommandDb.command_id.asc(),
            )
            .offset(MAX_COMMAND_HISTORY_PER_CHANNEL)
            .limit(1)
        ).scalar_one_or_none()
        if oldest_over_limit is None:
            return 0
        oldest_ids = (
            session.execute(
                select(EgressCommandDb.command_id)
                .where(EgressCommandDb.channel_id == channel_id)
                .where(EgressCommandDb.consumed_at.is_not(None))
                .order_by(
                    EgressCommandDb.consumed_at.asc(),
                    EgressCommandDb.issued_at.asc(),
                    EgressCommandDb.command_id.asc(),
                )
                .limit(COMMAND_HISTORY_TRIM_BATCH_SIZE)
            )
            .scalars()
            .all()
        )
        if not oldest_ids:
            return 0
        session.execute(delete(EgressCommandDb).where(EgressCommandDb.command_id.in_(oldest_ids)))
        return len(oldest_ids)

    def command_storage_usage(self) -> CommandStorageUsage:
        with self._session_factory() as session:
            row_count = session.execute(
                select(func.count()).select_from(EgressCommandDb)
            ).scalar_one()
            pending_count = session.execute(
                select(func.count())
                .select_from(EgressCommandDb)
                .where(EgressCommandDb.consumed_at.is_(None))
            ).scalar_one()
            storage_bytes: int | None = None
            if session.get_bind().dialect.name == "postgresql":
                from sqlalchemy import text

                storage_bytes = int(
                    session.execute(
                        text("SELECT pg_total_relation_size('civiccast.egress_commands')")
                    ).scalar_one()
                )
            return {
                "row_count": int(row_count),
                "pending_count": int(pending_count),
                "storage_bytes": storage_bytes,
            }

    def peek_pending_commands(self, channel_id: str) -> list[EgressCommand]:
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(EgressCommandDb)
                    .where(EgressCommandDb.channel_id == channel_id)
                    .where(EgressCommandDb.consumed_at.is_(None))
                    .order_by(EgressCommandDb.issued_at.asc(), EgressCommandDb.command_id.asc())
                )
                .scalars()
                .all()
            )
            return [_command_from_row(row) for row in rows]

    def write_state(self, row: EgressStateRow) -> None:
        with self._session_factory() as session:
            existing = session.execute(
                select(EgressStateDb).where(EgressStateDb.channel_id == row.channel_id)
            ).scalar_one_or_none()
            values = row.model_dump()
            if existing is None:
                session.add(EgressStateDb(**values))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            session.commit()

    def recover_stale_state(self, row: EgressStateRow, command: EgressCommand) -> None:
        """One transaction: upsert the STOPPED row AND insert the start command.

        STRICT FAIL-CLOSED CONTRACT. The caller mints a fresh uuid4 command id,
        so a duplicate id is never a legitimate expected case -- it means
        either a consumed command, a wrong-channel/action collision, or an
        unrelated constraint failure. In ALL of those cases writing STOPPED
        without a genuinely-pending start would strand the channel dark, which
        is the exact hazard this method exists to prevent. Therefore:

        * the state upsert and the command insert are committed together, and
        * ANY failure (IntegrityError or otherwise) rolls back and re-raises,
          leaving the ORIGINAL stale row fully intact for the next startup
          reconciliation to retry. There is NO state-only commit path and NO
          duplicate-id tolerance.

        A crash before the commit has the same effect: the old stale row
        survives.
        """
        with self._session_factory() as session:
            existing = session.execute(
                select(EgressStateDb).where(EgressStateDb.channel_id == row.channel_id)
            ).scalar_one_or_none()
            values = row.model_dump()
            if existing is None:
                session.add(EgressStateDb(**values))
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            session.add(
                EgressCommandDb(
                    command_id=command.command_id,
                    channel_id=command.channel_id,
                    action=command.action,
                    issued_at=command.issued_at,
                    issued_by=command.issued_by,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                # Fail closed: no state-only commit, no retry. The stale row is
                # preserved because the whole transaction (state + command) is
                # rolled back together.
                session.rollback()
                raise

    def read_state(self, channel_id: str) -> EgressStateRow | None:
        with self._session_factory() as session:
            row = session.execute(
                select(EgressStateDb).where(EgressStateDb.channel_id == channel_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            return EgressStateRow.model_validate(row, from_attributes=True)

    def append_health(self, sample: EgressHealthSample) -> None:
        with self._session_factory() as session:
            session.add(
                EgressHealthSampleDb(
                    channel_id=sample.channel_id,
                    sampled_at=sample.sampled_at,
                    state=sample.state,
                    sink_connected_json=json.dumps(sample.sink_connected),
                    encoder_fps=sample.encoder_fps,
                    encoder_bitrate_kbps=sample.encoder_bitrate_kbps,
                    dropped_frames=sample.dropped_frames,
                    seconds_on_air=sample.seconds_on_air,
                    last_loudness_lufs=sample.last_loudness_lufs,
                    caption_status=sample.caption_status,
                    schema_version=sample.schema_version,
                    proof_events_appended=sample.proof_events_appended_since_last_sample,
                )
            )
            session.commit()

    def recent_health(self, channel_id: str, limit: int) -> list[EgressHealthSample]:
        if limit <= 0:
            return []
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(EgressHealthSampleDb)
                    .where(EgressHealthSampleDb.channel_id == channel_id)
                    .order_by(
                        EgressHealthSampleDb.sampled_at.desc(),
                        EgressHealthSampleDb.sample_id.desc(),
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [_health_from_row(row) for row in rows]

    def trim_health_before(self, cutoff: datetime) -> int:
        with self._session_factory() as session:
            result = session.execute(
                delete(EgressHealthSampleDb).where(EgressHealthSampleDb.sampled_at < cutoff)
            )
            session.commit()
            rowcount = cast(int | None, getattr(result, "rowcount", None))
            return int(rowcount or 0)

    def append_proof_event(self, event: EgressProofEvent) -> None:
        with self._session_factory() as session:
            if (
                session.execute(
                    select(EgressProofEventDb).where(EgressProofEventDb.event_id == event.event_id)
                ).scalar_one_or_none()
                is not None
            ):
                return
            session.add(EgressProofEventDb(**event.model_dump()))
            session.commit()
        self._trim_proof_events(event.channel_id)

    def _trim_proof_events(self, channel_id: str) -> None:
        with self._session_factory() as session:
            count = session.execute(
                select(func.count())
                .select_from(EgressProofEventDb)
                .where(EgressProofEventDb.channel_id == channel_id)
            ).scalar_one()
            if count <= MAX_PROOF_EVENTS_PER_CHANNEL:
                return
            oldest = (
                session.execute(
                    select(EgressProofEventDb.event_id)
                    .where(EgressProofEventDb.channel_id == channel_id)
                    .order_by(
                        EgressProofEventDb.observed_at.asc(),
                        EgressProofEventDb.event_id.asc(),
                    )
                    .limit(TRIM_BATCH_SIZE)
                )
                .scalars()
                .all()
            )
            session.execute(
                delete(EgressProofEventDb).where(EgressProofEventDb.event_id.in_(oldest))
            )
            session.commit()

    def recent_proof_events(self, channel_id: str, limit: int) -> list[EgressProofEvent]:
        if limit <= 0:
            return []
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(EgressProofEventDb)
                    .where(EgressProofEventDb.channel_id == channel_id)
                    .order_by(EgressProofEventDb.observed_at.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [EgressProofEvent.model_validate(row, from_attributes=True) for row in rows]

    def count_proof_events_since(self, channel_id: str, since: datetime | None) -> int:
        with self._session_factory() as session:
            query = (
                select(func.count())
                .select_from(EgressProofEventDb)
                .where(EgressProofEventDb.channel_id == channel_id)
            )
            if since is not None:
                query = query.where(EgressProofEventDb.observed_at > since)
            return int(session.execute(query).scalar_one())

    def append_caption_proof_sample(self, sample: EgressCaptionProofSample) -> None:
        with self._session_factory() as session:
            session.add(EgressCaptionProofSampleDb(**sample.model_dump(exclude={"sample_id"})))
            session.commit()
        self._trim_caption_proofs(sample.channel_id)

    def _trim_caption_proofs(self, channel_id: str) -> None:
        with self._session_factory() as session:
            count = session.execute(
                select(func.count())
                .select_from(EgressCaptionProofSampleDb)
                .where(EgressCaptionProofSampleDb.channel_id == channel_id)
            ).scalar_one()
            if count <= MAX_CAPTION_PROOFS_PER_CHANNEL:
                return
            oldest = (
                session.execute(
                    select(EgressCaptionProofSampleDb.sample_id)
                    .where(EgressCaptionProofSampleDb.channel_id == channel_id)
                    .order_by(
                        EgressCaptionProofSampleDb.sampled_at.asc(),
                        EgressCaptionProofSampleDb.sample_id.asc(),
                    )
                    .limit(CAPTION_TRIM_BATCH_SIZE)
                )
                .scalars()
                .all()
            )
            session.execute(
                delete(EgressCaptionProofSampleDb).where(
                    EgressCaptionProofSampleDb.sample_id.in_(oldest)
                )
            )
            session.commit()

    def recent_caption_proof_samples(
        self, channel_id: str, limit: int
    ) -> list[EgressCaptionProofSample]:
        if limit <= 0:
            return []
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(EgressCaptionProofSampleDb)
                    .where(EgressCaptionProofSampleDb.channel_id == channel_id)
                    .order_by(
                        EgressCaptionProofSampleDb.sampled_at.desc(),
                        EgressCaptionProofSampleDb.sample_id.desc(),
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [
                EgressCaptionProofSample.model_validate(row, from_attributes=True) for row in rows
            ]

    def latest_caption_proof_sample(self, channel_id: str) -> EgressCaptionProofSample | None:
        recent = self.recent_caption_proof_samples(channel_id, 1)
        return recent[0] if recent else None


def _config_from_rows(row: EgressConfigDb, sink_rows: Sequence[EgressSinkDb]) -> EgressConfig:
    return EgressConfig(
        channel_id=row.channel_id,
        enabled=row.enabled,
        auto_start=row.auto_start,
        allow_software_fallback=row.allow_software_fallback,
        fill_policy=cast(Literal["slate", "bulletins"], row.fill_policy),
        ndi_relay_name=row.ndi_relay_name,
        sdi_relay_device=row.sdi_relay_device,
        slate_message=row.slate_message,
        graphics_overlay_enabled=row.graphics_overlay_enabled,
        graphics_overlay_lower_third_text=row.graphics_overlay_lower_third_text,
        loudness_target_lufs=row.loudness_target_lufs,
        loudness_tolerance_lufs=row.loudness_tolerance_lufs,
        canonical_profile=CanonicalProfile.model_validate_json(row.canonical_profile_json),
        sinks=[
            EgressSinkSpec(
                kind=sink.kind,  # type: ignore[arg-type]
                label=sink.label,
                uri=sink.uri,
                secret_ref=sink.secret_ref,
                latency_ms=sink.latency_ms,
                extra_output_args=json.loads(sink.extra_output_args_json),
                loudness_regime=cast(LoudnessRegime, sink.loudness_regime),
                loudness_target_lufs=sink.loudness_target_lufs,
                loudness_tolerance_lufs=sink.loudness_tolerance_lufs,
                eas_tone_strip_enabled=sink.eas_tone_strip_enabled,
            )
            for sink in sink_rows
        ],
    )


def _command_from_row(row: EgressCommandDb) -> EgressCommand:
    return EgressCommand(
        channel_id=row.channel_id,
        action=row.action,  # type: ignore[arg-type]
        issued_at=row.issued_at,
        issued_by=row.issued_by,
        command_id=row.command_id,
    )


def _health_from_row(row: EgressHealthSampleDb) -> EgressHealthSample:
    return EgressHealthSample(
        channel_id=row.channel_id,
        sampled_at=row.sampled_at,
        state=row.state,  # type: ignore[arg-type]
        sink_connected=json.loads(row.sink_connected_json),
        encoder_fps=row.encoder_fps,
        encoder_bitrate_kbps=row.encoder_bitrate_kbps,
        dropped_frames=row.dropped_frames,
        seconds_on_air=row.seconds_on_air,
        last_loudness_lufs=row.last_loudness_lufs,
        caption_status=row.caption_status,  # type: ignore[arg-type]
        schema_version=row.schema_version,
        proof_events_appended_since_last_sample=row.proof_events_appended,
    )
