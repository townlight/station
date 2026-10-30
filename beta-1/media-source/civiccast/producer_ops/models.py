# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Producer ops pydantic + SQLAlchemy models. See package docstring.

Schema lives in migration ``0063_producer_ops``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from civiccast.db import Base

Slug = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9_-]*$")]

SeriesApplicationState = Literal["submitted", "under_review", "approved", "declined"]
SERIES_APPLICATION_STATE_VALUES: tuple[str, ...] = (
    "submitted",
    "under_review",
    "approved",
    "declined",
)

CheckoutState = Literal["checked_out", "returned"]
CHECKOUT_STATE_VALUES: tuple[str, ...] = ("checked_out", "returned")


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pydantic domain models
# ---------------------------------------------------------------------------


class SeriesApplication(BaseModel):
    """A producer's request for a recurring series slot.

    Distinct from :class:`civiccast.contribute.models.ContributorSubmission`
    (one-off show intake) — this is "I want a standing weekly/monthly slot",
    reviewed the same accept/decline way but tracked separately because a
    series carries an ongoing schedule commitment, not a single air date.
    """

    model_config = ConfigDict(extra="forbid")

    application_id: Slug
    contributor_id: Slug
    series_title: Annotated[str, Field(min_length=1, max_length=240)]
    proposed_cadence: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(min_length=1, max_length=4000)]
    state: SeriesApplicationState = "submitted"
    review_notes: Annotated[str | None, Field(default=None, max_length=2000)] = None
    series_id: Annotated[str | None, Field(default=None, max_length=120)] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class VolunteerRole(BaseModel):
    """One volunteer roster entry: a person with a named station role."""

    model_config = ConfigDict(extra="forbid")

    volunteer_id: Slug
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    role_name: Annotated[str, Field(min_length=1, max_length=120)]
    contact_email: Annotated[str | None, Field(default=None, max_length=320)] = None
    active: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class CallSheet(BaseModel):
    """A shoot's crew/schedule plan."""

    model_config = ConfigDict(extra="forbid")

    call_sheet_id: Slug
    title: Annotated[str, Field(min_length=1, max_length=240)]
    shoot_date: datetime
    location: Annotated[str | None, Field(default=None, max_length=300)] = None
    notes: Annotated[str | None, Field(default=None, max_length=2000)] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class CallSheetAssignment(BaseModel):
    """One volunteer's crew role on a call sheet."""

    model_config = ConfigDict(extra="forbid")

    assignment_id: Slug
    call_sheet_id: Slug
    volunteer_id: Slug
    role_name: Annotated[str, Field(min_length=1, max_length=120)]
    call_time: datetime | None = None
    created_at: datetime = Field(default_factory=_now)


class EquipmentItem(BaseModel):
    """One piece of station equipment tracked for checkout."""

    model_config = ConfigDict(extra="forbid")

    equipment_id: Slug
    name: Annotated[str, Field(min_length=1, max_length=240)]
    category: Annotated[str, Field(min_length=1, max_length=120)]
    active: bool = True
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class EquipmentCheckout(BaseModel):
    """One checkout/return record for an equipment item.

    Exactly one ``checked_out`` row may exist per ``equipment_id`` at a
    time (enforced by a partial-unique index in the migration) — the
    hardware only exists once, so it cannot be checked out twice at once.
    """

    model_config = ConfigDict(extra="forbid")

    checkout_id: Slug
    equipment_id: Slug
    volunteer_id: Slug
    state: CheckoutState = "checked_out"
    checked_out_at: datetime = Field(default_factory=_now)
    returned_at: datetime | None = None
    notes: Annotated[str | None, Field(default=None, max_length=1000)] = None

    @model_validator(mode="after")
    def _returned_needs_timestamp(self) -> EquipmentCheckout:
        if self.state == "returned" and self.returned_at is None:
            raise ValueError("returned checkouts require returned_at")
        if self.state == "checked_out" and self.returned_at is not None:
            raise ValueError("checked_out checkouts must not have returned_at set")
        return self


class TrainingBadge(BaseModel):
    """A badge a volunteer has earned (e.g. ``camera-1``, ``live-switcher``)."""

    model_config = ConfigDict(extra="forbid")

    badge_id: Slug
    volunteer_id: Slug
    badge_name: Annotated[str, Field(min_length=1, max_length=120)]
    earned_at: datetime = Field(default_factory=_now)
    expires_at: datetime | None = None


class EquipmentAccessRule(BaseModel):
    """The badge required to check out an equipment item.

    An equipment item with no rule row is uncontrolled — any active
    volunteer may check it out. At most one rule per ``equipment_id``
    (enforced by a unique index in the migration); a piece of gear needs
    exactly one gate, not a list to reconcile.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: Slug
    equipment_id: Slug
    required_badge_name: Annotated[str, Field(min_length=1, max_length=120)]
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# SQLAlchemy ORM twins (schema applied by migration 0062, not here)
# ---------------------------------------------------------------------------


class SeriesApplicationDb(Base):
    __tablename__ = "series_applications"
    __table_args__ = (
        CheckConstraint(
            "state IN ('submitted', 'under_review', 'approved', 'declined')",
            name="series_applications_state_check",
        ),
        Index("ix_series_applications_contributor", "contributor_id"),
    )

    application_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    contributor_id: Mapped[str] = mapped_column(String(120), nullable=False)
    series_title: Mapped[str] = mapped_column(String(240), nullable=False)
    proposed_cadence: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(4000), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="submitted")
    review_notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    series_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class VolunteerRoleDb(Base):
    __tablename__ = "volunteer_roles"

    volunteer_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role_name: Mapped[str] = mapped_column(String(120), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class CallSheetDb(Base):
    __tablename__ = "call_sheets"

    call_sheet_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    shoot_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class CallSheetAssignmentDb(Base):
    __tablename__ = "call_sheet_assignments"
    __table_args__ = (
        Index("ix_call_sheet_assignments_call_sheet", "call_sheet_id"),
        Index("ix_call_sheet_assignments_volunteer", "volunteer_id"),
    )

    assignment_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    # Loose string columns (no SQLAlchemy relationship), matching the
    # eas / ai_models / metadata / reporting / underwriting / agenda /
    # paywall convention — no other module in this repo uses ForeignKey.
    call_sheet_id: Mapped[str] = mapped_column(String(120), nullable=False)
    volunteer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    role_name: Mapped[str] = mapped_column(String(120), nullable=False)
    call_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class EquipmentItemDb(Base):
    __tablename__ = "equipment_items"

    equipment_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    category: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


class EquipmentCheckoutDb(Base):
    __tablename__ = "equipment_checkouts"
    __table_args__ = (
        CheckConstraint(
            "state IN ('checked_out', 'returned')",
            name="equipment_checkouts_state_check",
        ),
        Index("ix_equipment_checkouts_equipment", "equipment_id"),
        Index("ix_equipment_checkouts_volunteer", "volunteer_id"),
    )

    checkout_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    equipment_id: Mapped[str] = mapped_column(String(120), nullable=False)
    volunteer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="checked_out")
    checked_out_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)


# Partial-unique: at most one OPEN checkout per equipment item. Declared
# as a module-level Index (not inside __table_args__) so the
# postgresql_where / sqlite_where dialect kwargs can reference the real
# column object — mirrors civiccast/schedule/models.py's
# assets_source_live_session_unique pattern.
Index(
    "ix_equipment_checkouts_one_open_per_item",
    EquipmentCheckoutDb.equipment_id,
    unique=True,
    postgresql_where=EquipmentCheckoutDb.state == "checked_out",
    sqlite_where=EquipmentCheckoutDb.state == "checked_out",
)


class TrainingBadgeDb(Base):
    __tablename__ = "training_badges"
    __table_args__ = (Index("ix_training_badges_volunteer", "volunteer_id"),)

    badge_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    volunteer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    badge_name: Mapped[str] = mapped_column(String(120), nullable=False)
    earned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EquipmentAccessRuleDb(Base):
    __tablename__ = "equipment_access_rules"
    __table_args__ = (
        Index("ix_equipment_access_rules_equipment_unique", "equipment_id", unique=True),
    )

    rule_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    equipment_id: Mapped[str] = mapped_column(String(120), nullable=False)
    required_badge_name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )


__all__ = [
    "CHECKOUT_STATE_VALUES",
    "SERIES_APPLICATION_STATE_VALUES",
    "CallSheet",
    "CallSheetAssignment",
    "CallSheetAssignmentDb",
    "CallSheetDb",
    "CheckoutState",
    "EquipmentAccessRule",
    "EquipmentAccessRuleDb",
    "EquipmentCheckout",
    "EquipmentCheckoutDb",
    "EquipmentItem",
    "EquipmentItemDb",
    "SeriesApplication",
    "SeriesApplicationDb",
    "SeriesApplicationState",
    "Slug",
    "TrainingBadge",
    "TrainingBadgeDb",
    "VolunteerRole",
    "VolunteerRoleDb",
]
