# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Durable store for producer ops: series applications, volunteer roster,
call sheets, equipment + checkouts, training badges, and access rules.

Per-request store over the single global session factory (same lazy posture
as eas / ai_models / metadata / reporting / underwriting / agenda / paywall).
All comparisons bind through parameters (no string interpolation) and ride
the indexes defined in migration ``0063_producer_ops``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from civiccast.producer_ops.models import (
    CallSheet,
    CallSheetAssignment,
    CallSheetAssignmentDb,
    CallSheetDb,
    CheckoutState,
    EquipmentAccessRule,
    EquipmentAccessRuleDb,
    EquipmentCheckout,
    EquipmentCheckoutDb,
    EquipmentItem,
    EquipmentItemDb,
    SeriesApplication,
    SeriesApplicationDb,
    SeriesApplicationState,
    TrainingBadge,
    TrainingBadgeDb,
    VolunteerRole,
    VolunteerRoleDb,
)

SessionFactory = Callable[[], AbstractContextManager[Session]]


class ProducerOpsStoreError(RuntimeError):
    """Base error for producer-ops persistence failures."""


class SeriesApplicationNotFoundError(ProducerOpsStoreError):
    """Raised when an ``application_id`` does not resolve."""


class VolunteerNotFoundError(ProducerOpsStoreError):
    """Raised when a ``volunteer_id`` does not resolve."""


class CallSheetNotFoundError(ProducerOpsStoreError):
    """Raised when a ``call_sheet_id`` does not resolve."""


class EquipmentNotFoundError(ProducerOpsStoreError):
    """Raised when an ``equipment_id`` does not resolve."""


class EquipmentAlreadyCheckedOutError(ProducerOpsStoreError):
    """Raised when checkout is attempted on an item already checked out."""


class MissingRequiredBadgeError(ProducerOpsStoreError):
    """Raised when a volunteer lacks the badge an access rule requires."""


class CheckoutNotFoundError(ProducerOpsStoreError):
    """Raised when a ``checkout_id`` does not resolve."""


class SeriesApplicationAlreadyExistsError(ProducerOpsStoreError):
    """Raised when a client-supplied ``application_id`` already exists."""


class CallSheetAlreadyExistsError(ProducerOpsStoreError):
    """Raised when a client-supplied ``call_sheet_id`` already exists."""


class CallSheetAssignmentAlreadyExistsError(ProducerOpsStoreError):
    """Raised when a client-supplied ``assignment_id`` already exists."""


class TrainingBadgeAlreadyExistsError(ProducerOpsStoreError):
    """Raised when a client-supplied ``badge_id`` already exists."""


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(dt: datetime | None) -> datetime | None:
    """Promote a naive datetime to UTC-aware (SQLite round-trip quirk).

    Same helper as ``civiccast.paywall.store._as_utc`` — SQLite returns
    ``DateTime(timezone=True)`` columns as tz-naive even though the column
    is typed timezone=True; Postgres returns aware datetimes.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _series_application_db_to_model(row: SeriesApplicationDb) -> SeriesApplication:
    return SeriesApplication(
        application_id=row.application_id,
        contributor_id=row.contributor_id,
        series_title=row.series_title,
        proposed_cadence=row.proposed_cadence,
        description=row.description,
        state=cast(SeriesApplicationState, row.state),
        review_notes=row.review_notes,
        series_id=row.series_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _volunteer_db_to_model(row: VolunteerRoleDb) -> VolunteerRole:
    return VolunteerRole(
        volunteer_id=row.volunteer_id,
        display_name=row.display_name,
        role_name=row.role_name,
        contact_email=row.contact_email,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _call_sheet_db_to_model(row: CallSheetDb) -> CallSheet:
    return CallSheet(
        call_sheet_id=row.call_sheet_id,
        title=row.title,
        shoot_date=row.shoot_date,
        location=row.location,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _assignment_db_to_model(row: CallSheetAssignmentDb) -> CallSheetAssignment:
    return CallSheetAssignment(
        assignment_id=row.assignment_id,
        call_sheet_id=row.call_sheet_id,
        volunteer_id=row.volunteer_id,
        role_name=row.role_name,
        call_time=row.call_time,
        created_at=row.created_at,
    )


def _equipment_db_to_model(row: EquipmentItemDb) -> EquipmentItem:
    return EquipmentItem(
        equipment_id=row.equipment_id,
        name=row.name,
        category=row.category,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _checkout_db_to_model(row: EquipmentCheckoutDb) -> EquipmentCheckout:
    return EquipmentCheckout(
        checkout_id=row.checkout_id,
        equipment_id=row.equipment_id,
        volunteer_id=row.volunteer_id,
        state=cast(CheckoutState, row.state),
        checked_out_at=row.checked_out_at,
        returned_at=row.returned_at,
        notes=row.notes,
    )


def _badge_db_to_model(row: TrainingBadgeDb) -> TrainingBadge:
    return TrainingBadge(
        badge_id=row.badge_id,
        volunteer_id=row.volunteer_id,
        badge_name=row.badge_name,
        earned_at=row.earned_at,
        expires_at=row.expires_at,
    )


def _rule_db_to_model(row: EquipmentAccessRuleDb) -> EquipmentAccessRule:
    return EquipmentAccessRule(
        rule_id=row.rule_id,
        equipment_id=row.equipment_id,
        required_badge_name=row.required_badge_name,
        created_at=row.created_at,
    )


class ProducerOpsStore:
    """Per-request CRUD over the six producer-ops tables."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Series applications
    # ------------------------------------------------------------------

    def create_series_application(self, application: SeriesApplication) -> SeriesApplication:
        """Raises :class:`SeriesApplicationAlreadyExistsError` when
        ``application_id`` already exists (mirrors
        ``civiccast.paywall.store`` / ``civiccast.schedule.store``'s
        IntegrityError-to-domain-error convention for one-shot creates).
        """
        now = _now()
        with self._session_factory() as session:
            row = SeriesApplicationDb(
                application_id=application.application_id,
                contributor_id=application.contributor_id,
                series_title=application.series_title,
                proposed_cadence=application.proposed_cadence,
                description=application.description,
                state=application.state,
                review_notes=application.review_notes,
                series_id=application.series_id,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise SeriesApplicationAlreadyExistsError(
                    f"Series application {application.application_id!r} already exists."
                ) from exc
            stored = session.get(SeriesApplicationDb, application.application_id)
            assert stored is not None
            return _series_application_db_to_model(stored)

    def get_series_application(self, application_id: str) -> SeriesApplication:
        with self._session_factory() as session:
            row = session.get(SeriesApplicationDb, application_id)
            if row is None:
                raise SeriesApplicationNotFoundError(
                    f"Series application {application_id!r} not found."
                )
            return _series_application_db_to_model(row)

    def list_series_applications(self) -> list[SeriesApplication]:
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(SeriesApplicationDb).order_by(SeriesApplicationDb.created_at)
                )
                .scalars()
                .all()
            )
            return [_series_application_db_to_model(r) for r in rows]

    def review_series_application(
        self,
        application_id: str,
        *,
        state: SeriesApplicationState,
        review_notes: str | None = None,
        series_id: str | None = None,
    ) -> SeriesApplication:
        with self._session_factory() as session:
            row = session.get(SeriesApplicationDb, application_id)
            if row is None:
                raise SeriesApplicationNotFoundError(
                    f"Series application {application_id!r} not found."
                )
            row.state = state
            if review_notes is not None:
                row.review_notes = review_notes
            if series_id is not None:
                row.series_id = series_id
            row.updated_at = _now()
            session.commit()
            session.refresh(row)
            return _series_application_db_to_model(row)

    # ------------------------------------------------------------------
    # Volunteer roster
    # ------------------------------------------------------------------

    def upsert_volunteer(self, volunteer: VolunteerRole) -> VolunteerRole:
        now = _now()
        with self._session_factory() as session:
            existing = session.get(VolunteerRoleDb, volunteer.volunteer_id)
            if existing is None:
                row = VolunteerRoleDb(
                    volunteer_id=volunteer.volunteer_id,
                    display_name=volunteer.display_name,
                    role_name=volunteer.role_name,
                    contact_email=volunteer.contact_email,
                    active=volunteer.active,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                existing.display_name = volunteer.display_name
                existing.role_name = volunteer.role_name
                existing.contact_email = volunteer.contact_email
                existing.active = volunteer.active
                existing.updated_at = now
            session.commit()
            stored = session.get(VolunteerRoleDb, volunteer.volunteer_id)
            assert stored is not None
            return _volunteer_db_to_model(stored)

    def get_volunteer(self, volunteer_id: str) -> VolunteerRole:
        with self._session_factory() as session:
            row = session.get(VolunteerRoleDb, volunteer_id)
            if row is None:
                raise VolunteerNotFoundError(f"Volunteer {volunteer_id!r} not found.")
            return _volunteer_db_to_model(row)

    def list_volunteers(self, *, active_only: bool = False) -> list[VolunteerRole]:
        with self._session_factory() as session:
            stmt = select(VolunteerRoleDb).order_by(VolunteerRoleDb.display_name)
            if active_only:
                stmt = stmt.where(VolunteerRoleDb.active.is_(True))
            rows = session.execute(stmt).scalars().all()
            return [_volunteer_db_to_model(r) for r in rows]

    # ------------------------------------------------------------------
    # Call sheets + assignments
    # ------------------------------------------------------------------

    def create_call_sheet(self, call_sheet: CallSheet) -> CallSheet:
        """Raises :class:`CallSheetAlreadyExistsError` when
        ``call_sheet_id`` already exists.
        """
        now = _now()
        with self._session_factory() as session:
            row = CallSheetDb(
                call_sheet_id=call_sheet.call_sheet_id,
                title=call_sheet.title,
                shoot_date=call_sheet.shoot_date,
                location=call_sheet.location,
                notes=call_sheet.notes,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise CallSheetAlreadyExistsError(
                    f"Call sheet {call_sheet.call_sheet_id!r} already exists."
                ) from exc
            stored = session.get(CallSheetDb, call_sheet.call_sheet_id)
            assert stored is not None
            return _call_sheet_db_to_model(stored)

    def get_call_sheet(self, call_sheet_id: str) -> CallSheet:
        with self._session_factory() as session:
            row = session.get(CallSheetDb, call_sheet_id)
            if row is None:
                raise CallSheetNotFoundError(f"Call sheet {call_sheet_id!r} not found.")
            return _call_sheet_db_to_model(row)

    def list_call_sheets(self) -> list[CallSheet]:
        with self._session_factory() as session:
            rows = (
                session.execute(select(CallSheetDb).order_by(CallSheetDb.shoot_date))
                .scalars()
                .all()
            )
            return [_call_sheet_db_to_model(r) for r in rows]

    def add_call_sheet_assignment(self, assignment: CallSheetAssignment) -> CallSheetAssignment:
        """Raises :class:`CallSheetAssignmentAlreadyExistsError` when
        ``assignment_id`` already exists.
        """
        with self._session_factory() as session:
            if session.get(CallSheetDb, assignment.call_sheet_id) is None:
                raise CallSheetNotFoundError(f"Call sheet {assignment.call_sheet_id!r} not found.")
            if session.get(VolunteerRoleDb, assignment.volunteer_id) is None:
                raise VolunteerNotFoundError(f"Volunteer {assignment.volunteer_id!r} not found.")
            row = CallSheetAssignmentDb(
                assignment_id=assignment.assignment_id,
                call_sheet_id=assignment.call_sheet_id,
                volunteer_id=assignment.volunteer_id,
                role_name=assignment.role_name,
                call_time=assignment.call_time,
                created_at=_now(),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise CallSheetAssignmentAlreadyExistsError(
                    f"Call sheet assignment {assignment.assignment_id!r} already exists."
                ) from exc
            stored = session.get(CallSheetAssignmentDb, assignment.assignment_id)
            assert stored is not None
            return _assignment_db_to_model(stored)

    def list_call_sheet_assignments(self, call_sheet_id: str) -> list[CallSheetAssignment]:
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(CallSheetAssignmentDb)
                    .where(CallSheetAssignmentDb.call_sheet_id == call_sheet_id)
                    .order_by(CallSheetAssignmentDb.created_at)
                )
                .scalars()
                .all()
            )
            return [_assignment_db_to_model(r) for r in rows]

    # ------------------------------------------------------------------
    # Equipment roster
    # ------------------------------------------------------------------

    def upsert_equipment(self, item: EquipmentItem) -> EquipmentItem:
        now = _now()
        with self._session_factory() as session:
            existing = session.get(EquipmentItemDb, item.equipment_id)
            if existing is None:
                row = EquipmentItemDb(
                    equipment_id=item.equipment_id,
                    name=item.name,
                    category=item.category,
                    active=item.active,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            else:
                existing.name = item.name
                existing.category = item.category
                existing.active = item.active
                existing.updated_at = now
            session.commit()
            stored = session.get(EquipmentItemDb, item.equipment_id)
            assert stored is not None
            return _equipment_db_to_model(stored)

    def get_equipment(self, equipment_id: str) -> EquipmentItem:
        with self._session_factory() as session:
            row = session.get(EquipmentItemDb, equipment_id)
            if row is None:
                raise EquipmentNotFoundError(f"Equipment {equipment_id!r} not found.")
            return _equipment_db_to_model(row)

    def list_equipment(self, *, active_only: bool = False) -> list[EquipmentItem]:
        with self._session_factory() as session:
            stmt = select(EquipmentItemDb).order_by(EquipmentItemDb.name)
            if active_only:
                stmt = stmt.where(EquipmentItemDb.active.is_(True))
            rows = session.execute(stmt).scalars().all()
            return [_equipment_db_to_model(r) for r in rows]

    # ------------------------------------------------------------------
    # Training badges
    # ------------------------------------------------------------------

    def grant_badge(self, badge: TrainingBadge) -> TrainingBadge:
        """Raises :class:`TrainingBadgeAlreadyExistsError` when
        ``badge_id`` already exists.
        """
        with self._session_factory() as session:
            if session.get(VolunteerRoleDb, badge.volunteer_id) is None:
                raise VolunteerNotFoundError(f"Volunteer {badge.volunteer_id!r} not found.")
            row = TrainingBadgeDb(
                badge_id=badge.badge_id,
                volunteer_id=badge.volunteer_id,
                badge_name=badge.badge_name,
                earned_at=badge.earned_at,
                expires_at=badge.expires_at,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise TrainingBadgeAlreadyExistsError(
                    f"Training badge {badge.badge_id!r} already exists."
                ) from exc
            stored = session.get(TrainingBadgeDb, badge.badge_id)
            assert stored is not None
            return _badge_db_to_model(stored)

    def list_badges_for_volunteer(self, volunteer_id: str) -> list[TrainingBadge]:
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(TrainingBadgeDb)
                    .where(TrainingBadgeDb.volunteer_id == volunteer_id)
                    .order_by(TrainingBadgeDb.earned_at)
                )
                .scalars()
                .all()
            )
            return [_badge_db_to_model(r) for r in rows]

    def has_active_badge(
        self, volunteer_id: str, badge_name: str, *, now: datetime | None = None
    ) -> bool:
        """True if the volunteer holds ``badge_name`` and it is not expired."""
        comparison_now = _as_utc(now) or _now()
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(TrainingBadgeDb)
                    .where(TrainingBadgeDb.volunteer_id == volunteer_id)
                    .where(TrainingBadgeDb.badge_name == badge_name)
                )
                .scalars()
                .all()
            )
            for row in rows:
                expires = _as_utc(row.expires_at)
                if expires is None or expires > comparison_now:
                    return True
            return False

    # ------------------------------------------------------------------
    # Equipment access rules
    # ------------------------------------------------------------------

    def set_access_rule(self, rule: EquipmentAccessRule) -> EquipmentAccessRule:
        """Insert or replace the single access rule for an equipment item."""
        with self._session_factory() as session:
            if session.get(EquipmentItemDb, rule.equipment_id) is None:
                raise EquipmentNotFoundError(f"Equipment {rule.equipment_id!r} not found.")
            existing = session.execute(
                select(EquipmentAccessRuleDb).where(
                    EquipmentAccessRuleDb.equipment_id == rule.equipment_id
                )
            ).scalar_one_or_none()
            if existing is None:
                row = EquipmentAccessRuleDb(
                    rule_id=rule.rule_id,
                    equipment_id=rule.equipment_id,
                    required_badge_name=rule.required_badge_name,
                    created_at=_now(),
                )
                session.add(row)
                session.commit()
                stored = session.get(EquipmentAccessRuleDb, rule.rule_id)
            else:
                existing.required_badge_name = rule.required_badge_name
                session.commit()
                stored = existing
            assert stored is not None
            return _rule_db_to_model(stored)

    def get_access_rule(self, equipment_id: str) -> EquipmentAccessRule | None:
        with self._session_factory() as session:
            row = session.execute(
                select(EquipmentAccessRuleDb).where(
                    EquipmentAccessRuleDb.equipment_id == equipment_id
                )
            ).scalar_one_or_none()
            return _rule_db_to_model(row) if row else None

    # ------------------------------------------------------------------
    # Equipment checkouts
    # ------------------------------------------------------------------

    def check_out(self, checkout: EquipmentCheckout) -> EquipmentCheckout:
        """Check out an equipment item to a volunteer.

        Enforces, in this order: the equipment item exists, the volunteer
        exists, any :class:`EquipmentAccessRule` badge requirement is met,
        and no other OPEN checkout exists for this item. The last check is
        also enforced by the database's partial-unique index
        (``ix_equipment_checkouts_one_open_per_item``) so a race between
        two concurrent checkouts cannot both succeed even if the
        pre-check below both observe "no open checkout."
        """
        with self._session_factory() as session:
            if session.get(EquipmentItemDb, checkout.equipment_id) is None:
                raise EquipmentNotFoundError(f"Equipment {checkout.equipment_id!r} not found.")
            if session.get(VolunteerRoleDb, checkout.volunteer_id) is None:
                raise VolunteerNotFoundError(f"Volunteer {checkout.volunteer_id!r} not found.")

            rule = session.execute(
                select(EquipmentAccessRuleDb).where(
                    EquipmentAccessRuleDb.equipment_id == checkout.equipment_id
                )
            ).scalar_one_or_none()
            if rule is not None:
                badge_rows = (
                    session.execute(
                        select(TrainingBadgeDb)
                        .where(TrainingBadgeDb.volunteer_id == checkout.volunteer_id)
                        .where(TrainingBadgeDb.badge_name == rule.required_badge_name)
                    )
                    .scalars()
                    .all()
                )
                now = _now()
                has_badge = any(
                    _as_utc(b.expires_at) is None or cast(datetime, _as_utc(b.expires_at)) > now
                    for b in badge_rows
                )
                if not has_badge:
                    raise MissingRequiredBadgeError(
                        f"Volunteer {checkout.volunteer_id!r} lacks required badge "
                        f"{rule.required_badge_name!r} for equipment {checkout.equipment_id!r}."
                    )

            open_existing = session.execute(
                select(EquipmentCheckoutDb)
                .where(EquipmentCheckoutDb.equipment_id == checkout.equipment_id)
                .where(EquipmentCheckoutDb.state == "checked_out")
            ).scalar_one_or_none()
            if open_existing is not None:
                raise EquipmentAlreadyCheckedOutError(
                    f"Equipment {checkout.equipment_id!r} is already checked out."
                )

            row = EquipmentCheckoutDb(
                checkout_id=checkout.checkout_id,
                equipment_id=checkout.equipment_id,
                volunteer_id=checkout.volunteer_id,
                state="checked_out",
                checked_out_at=_now(),
                returned_at=None,
                notes=checkout.notes,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                # Belt-and-suspenders against the true race window: the
                # partial-unique index caught it even though our pre-check
                # (above) didn't see the concurrent writer.
                raise EquipmentAlreadyCheckedOutError(
                    f"Equipment {checkout.equipment_id!r} is already checked out."
                ) from exc
            stored = session.get(EquipmentCheckoutDb, checkout.checkout_id)
            assert stored is not None
            return _checkout_db_to_model(stored)

    def return_checkout(self, checkout_id: str, *, notes: str | None = None) -> EquipmentCheckout:
        with self._session_factory() as session:
            row = session.get(EquipmentCheckoutDb, checkout_id)
            if row is None:
                raise CheckoutNotFoundError(f"Checkout {checkout_id!r} not found.")
            row.state = "returned"
            row.returned_at = _now()
            if notes is not None:
                row.notes = notes
            session.commit()
            session.refresh(row)
            return _checkout_db_to_model(row)

    def get_open_checkout(self, equipment_id: str) -> EquipmentCheckout | None:
        with self._session_factory() as session:
            row = session.execute(
                select(EquipmentCheckoutDb)
                .where(EquipmentCheckoutDb.equipment_id == equipment_id)
                .where(EquipmentCheckoutDb.state == "checked_out")
            ).scalar_one_or_none()
            return _checkout_db_to_model(row) if row else None

    def list_checkouts_for_volunteer(self, volunteer_id: str) -> list[EquipmentCheckout]:
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(EquipmentCheckoutDb)
                    .where(EquipmentCheckoutDb.volunteer_id == volunteer_id)
                    .order_by(EquipmentCheckoutDb.checked_out_at)
                )
                .scalars()
                .all()
            )
            return [_checkout_db_to_model(r) for r in rows]


__all__ = [
    "CallSheetAlreadyExistsError",
    "CallSheetAssignmentAlreadyExistsError",
    "CallSheetNotFoundError",
    "CheckoutNotFoundError",
    "EquipmentAlreadyCheckedOutError",
    "EquipmentNotFoundError",
    "MissingRequiredBadgeError",
    "ProducerOpsStore",
    "ProducerOpsStoreError",
    "SeriesApplicationAlreadyExistsError",
    "SeriesApplicationNotFoundError",
    "SessionFactory",
    "TrainingBadgeAlreadyExistsError",
    "VolunteerNotFoundError",
]
