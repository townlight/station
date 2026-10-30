# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""AutoScheduleService — CRUD + (later) preview/compile for S18 auto-scheduling.

The application service behind the staff API (S18 slice 5). Slice 5a wires the
CRUD for the three entities (saved searches, daypart blocks, auto-schedule
rules); the preview (dry-run) and compile (run the materializer) operations are
added in slice 5b — the service already holds the ``session_factory`` / ``clock``
/ ``tz`` / ``rng`` they need so that extension is additive.

Server-set fields are owned here, not by the caller: ``create_*`` mints the id
(``ss_`` / ``sb_`` / ``asr_`` + a url-safe token) and stamps ``created_at`` /
``updated_at``; ``update_*`` keeps the path id and lets the store preserve the
original ``created_at`` (its upsert updates mutable fields + ``updated_at``
only). ``update_*`` returns ``None`` when the id is absent so the router can
404. Building the domain model runs its validators, so an out-of-contract value
(e.g. an empty weekday set) raises ``pydantic.ValidationError`` here for the
router to translate to 422.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, date, datetime, tzinfo

from pydantic import BaseModel, Field

from civiccast.schedule.autoschedule_materializer import (
    SlotAction,
    compile_rules,
    plan_rule_slots,
)
from civiccast.schedule.autoschedule_models import (
    AssetQuery,
    AutoScheduleRule,
    PickStrategyValue,
    SavedSearch,
    ScheduleBlock,
)
from civiccast.schedule.autoschedule_store import AutoScheduleStore, SessionFactory


def _new_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(9)}"


# ---------------------------------------------------------------------------
# Preview / compile response shapes (the materializer's dataclasses are
# internal; these are the API-facing pydantic peers the router serializes).
# ---------------------------------------------------------------------------


class SlotPreview(BaseModel):
    """One slot in a rule's dry-run preview — what compile WOULD do with it."""

    starts_at: datetime
    ends_at: datetime
    action: SlotAction
    asset_id: str | None = None
    title: str | None = None


class RulePreview(BaseModel):
    """Dry-run of one rule: the slots it would fill, without writing anything."""

    rule_id: str
    channel_id: str
    missing_dependency: bool = False
    would_fill_count: int = 0
    slots: list[SlotPreview] = Field(default_factory=list)


class RuleCompileResult(BaseModel):
    """Per-rule outcome of a compile run (pydantic peer of the materializer's
    RuleMaterializeResult)."""

    rule_id: str
    channel_id: str
    slots_considered: int = 0
    items_created: int = 0
    skipped_occupied: int = 0
    skipped_no_asset: int = 0
    skipped_unplayable: int = 0
    skipped_conflict: int = 0
    missing_dependency: bool = False


class CompileReport(BaseModel):
    """Outcome of compiling all enabled rules into schedule_items."""

    items_created: int = 0
    results: list[RuleCompileResult] = Field(default_factory=list)


class AutoScheduleService:
    """CRUD over the S18 entities (+ preview/compile in slice 5b)."""

    def __init__(
        self,
        store: AutoScheduleStore,
        *,
        session_factory: SessionFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        tz: tzinfo | Callable[[], tzinfo] = UTC,
        rng: object | None = None,
    ) -> None:
        self._store = store
        self._session_factory = session_factory
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._tz = tz
        self._rng = rng

    def _require_session_factory(self) -> SessionFactory:
        """Return the session factory, or raise a clear error if unset.

        ``session_factory`` is optional on the CRUD path but required for
        preview/compile (which open a session). Guarding here turns a stray
        ``None`` into a clear error the router maps to 503, not a cryptic
        ``TypeError`` 500 (step-6 audit ENG-002/QA-004)."""
        if self._session_factory is None:
            raise RuntimeError(
                "AutoScheduleService has no session factory; preview/compile "
                "require active durable storage."
            )
        return self._session_factory

    # ------------------------------------------------------------------
    # SavedSearch
    # ------------------------------------------------------------------
    def create_saved_search(
        self, *, name: str, description: str | None, query: AssetQuery
    ) -> SavedSearch:
        now = self._clock()
        return self._store.upsert_saved_search(
            SavedSearch(
                saved_search_id=_new_id("ss_"),
                name=name,
                description=description,
                query=query,
                created_at=now,
                updated_at=now,
            )
        )

    def update_saved_search(
        self, saved_search_id: str, *, name: str, description: str | None, query: AssetQuery
    ) -> SavedSearch | None:
        if self._store.get_saved_search(saved_search_id) is None:
            return None
        now = self._clock()
        return self._store.upsert_saved_search(
            SavedSearch(
                saved_search_id=saved_search_id,
                name=name,
                description=description,
                query=query,
                created_at=now,  # ignored on update — the store preserves the original
                updated_at=now,
            )
        )

    def get_saved_search(self, saved_search_id: str) -> SavedSearch | None:
        return self._store.get_saved_search(saved_search_id)

    def list_saved_searches(self) -> list[SavedSearch]:
        return self._store.list_saved_searches()

    def delete_saved_search(self, saved_search_id: str) -> bool:
        return self._store.delete_saved_search(saved_search_id)

    # ------------------------------------------------------------------
    # ScheduleBlock
    # ------------------------------------------------------------------
    def create_schedule_block(
        self,
        *,
        channel_id: str,
        name: str,
        start_minute: int,
        end_minute: int,
        days_of_week: list[int],
        active_from: date | None,
        active_until: date | None,
        enabled: bool,
    ) -> ScheduleBlock:
        now = self._clock()
        return self._store.upsert_schedule_block(
            ScheduleBlock(
                block_id=_new_id("sb_"),
                channel_id=channel_id,
                name=name,
                start_minute=start_minute,
                end_minute=end_minute,
                days_of_week=days_of_week,
                active_from=active_from,
                active_until=active_until,
                enabled=enabled,
                created_at=now,
                updated_at=now,
            )
        )

    def update_schedule_block(
        self,
        block_id: str,
        *,
        channel_id: str,
        name: str,
        start_minute: int,
        end_minute: int,
        days_of_week: list[int],
        active_from: date | None,
        active_until: date | None,
        enabled: bool,
    ) -> ScheduleBlock | None:
        if self._store.get_schedule_block(block_id) is None:
            return None
        now = self._clock()
        return self._store.upsert_schedule_block(
            ScheduleBlock(
                block_id=block_id,
                channel_id=channel_id,
                name=name,
                start_minute=start_minute,
                end_minute=end_minute,
                days_of_week=days_of_week,
                active_from=active_from,
                active_until=active_until,
                enabled=enabled,
                created_at=now,
                updated_at=now,
            )
        )

    def get_schedule_block(self, block_id: str) -> ScheduleBlock | None:
        return self._store.get_schedule_block(block_id)

    def list_schedule_blocks(
        self, *, channel_id: str | None = None, enabled_only: bool = False
    ) -> list[ScheduleBlock]:
        return self._store.list_schedule_blocks(channel_id=channel_id, enabled_only=enabled_only)

    def delete_schedule_block(self, block_id: str) -> bool:
        return self._store.delete_schedule_block(block_id)

    # ------------------------------------------------------------------
    # AutoScheduleRule
    # ------------------------------------------------------------------
    def create_rule(
        self,
        *,
        name: str,
        saved_search_id: str,
        channel_id: str,
        schedule_block_id: str,
        pick_strategy: PickStrategyValue,
        rolling_window_days: int,
        repeat_prevention_days: int,
        priority: int,
        enabled: bool,
    ) -> AutoScheduleRule:
        now = self._clock()
        return self._store.upsert_auto_schedule_rule(
            AutoScheduleRule(
                rule_id=_new_id("asr_"),
                name=name,
                saved_search_id=saved_search_id,
                channel_id=channel_id,
                schedule_block_id=schedule_block_id,
                pick_strategy=pick_strategy,
                rolling_window_days=rolling_window_days,
                repeat_prevention_days=repeat_prevention_days,
                priority=priority,
                enabled=enabled,
                created_at=now,
                updated_at=now,
            )
        )

    def update_rule(
        self,
        rule_id: str,
        *,
        name: str,
        saved_search_id: str,
        channel_id: str,
        schedule_block_id: str,
        pick_strategy: PickStrategyValue,
        rolling_window_days: int,
        repeat_prevention_days: int,
        priority: int,
        enabled: bool,
    ) -> AutoScheduleRule | None:
        existing = self._store.get_auto_schedule_rule(rule_id)
        if existing is None:
            return None
        now = self._clock()
        return self._store.upsert_auto_schedule_rule(
            AutoScheduleRule(
                rule_id=rule_id,
                name=name,
                saved_search_id=saved_search_id,
                channel_id=channel_id,
                schedule_block_id=schedule_block_id,
                pick_strategy=pick_strategy,
                rolling_window_days=rolling_window_days,
                repeat_prevention_days=repeat_prevention_days,
                priority=priority,
                enabled=enabled,
                # Preserve the materializer's stamp across an operator edit.
                last_materialized_at=existing.last_materialized_at,
                created_at=now,
                updated_at=now,
            )
        )

    def get_rule(self, rule_id: str) -> AutoScheduleRule | None:
        return self._store.get_auto_schedule_rule(rule_id)

    def list_rules(
        self, *, channel_id: str | None = None, enabled_only: bool = False
    ) -> list[AutoScheduleRule]:
        return self._store.list_auto_schedule_rules(
            channel_id=channel_id, enabled_only=enabled_only
        )

    def delete_rule(self, rule_id: str) -> bool:
        return self._store.delete_auto_schedule_rule(rule_id)

    # ------------------------------------------------------------------
    # Preview (dry-run) + compile (slice 5b)
    # ------------------------------------------------------------------
    def preview_rule(self, rule_id: str) -> RulePreview | None:
        """Dry-run one rule: the slots it would fill, WITHOUT writing.

        Returns None if the rule is absent (router → 404); a preview flagged
        ``missing_dependency`` if its saved search or block was deleted. Uses
        the same ``plan_rule_slots`` the compiler uses, so the preview matches a
        real compile (modulo a Postgres write-time conflict).
        """
        rule = self._store.get_auto_schedule_rule(rule_id)
        if rule is None:
            return None
        search = self._store.get_saved_search(rule.saved_search_id)
        block = self._store.get_schedule_block(rule.schedule_block_id)
        if search is None or block is None:
            return RulePreview(
                rule_id=rule.rule_id, channel_id=rule.channel_id, missing_dependency=True
            )
        factory = self._require_session_factory()
        with factory() as session:
            decisions = plan_rule_slots(
                session,
                rule=rule,
                search=search,
                block=block,
                now=self._clock(),
                tz=self._tz() if callable(self._tz) else self._tz,
                rng=self._rng,
            )
        slots = [
            SlotPreview(
                starts_at=d.starts_at,
                ends_at=d.ends_at,
                action=d.action,
                asset_id=d.asset_id,
                title=d.title,
            )
            for d in decisions
        ]
        return RulePreview(
            rule_id=rule.rule_id,
            channel_id=rule.channel_id,
            would_fill_count=sum(1 for d in decisions if d.action == "fill"),
            slots=slots,
        )

    def compile(self) -> CompileReport:
        """Compile every enabled rule into schedule_items now; return the report.

        The created items are ``scheduled`` and still flow through the S4 commit
        gate before air — this does not put anything on air.
        """
        factory = self._require_session_factory()
        with factory() as session:
            report = compile_rules(
                session, self._store, now=self._clock(), tz=self._tz, rng=self._rng
            )
        return CompileReport(
            items_created=report.items_created,
            results=[
                RuleCompileResult(
                    rule_id=r.rule_id,
                    channel_id=r.channel_id,
                    slots_considered=r.slots_considered,
                    items_created=r.items_created,
                    skipped_occupied=r.skipped_occupied,
                    skipped_no_asset=r.skipped_no_asset,
                    skipped_unplayable=r.skipped_unplayable,
                    skipped_conflict=r.skipped_conflict,
                    missing_dependency=r.missing_dependency,
                )
                for r in report.results
            ],
        )
