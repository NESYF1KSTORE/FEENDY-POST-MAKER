"""Cost ledger, budgets and licences (spec §14)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScopedMixin, TimestampMixin, TZDateTime, new_id


class CostLedgerEntry(Base, TimestampMixin, TenantScopedMixin):
    """Spec §14.1 — one immutable usage record.

    Manual hours are recorded here too (with provider="human") so unit economics
    cover the whole cost of a project, not just tokens.
    """

    __tablename__ = "cost_ledger_entries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("cle"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    stage: Mapped[str] = mapped_column(String(30), default="", nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)

    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    sku: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), default="token", nullable=False)
    effective_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="RUB", nullable=False)
    invoice_ref: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    billable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, index=True
    )


class Budget(Base, TimestampMixin, TenantScopedMixin):
    """Per-project budget envelope with the 50/80/100 thresholds from §14.1."""

    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("project_id", name="uq_budget_project"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("bud"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    limit_amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="RUB", nullable=False)
    spent_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    soft_pct: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    warning_pct: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    # Set when spend crosses 100%: new billable runs are refused (AC-10).
    hard_stopped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notified_thresholds: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    override_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    override_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    override_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    override_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)

    @property
    def effective_limit(self) -> float:
        return self.limit_amount + (self.override_amount or 0.0)

    @property
    def usage_pct(self) -> float:
        if self.effective_limit <= 0:
            return 0.0
        return round(self.spent_amount / self.effective_limit * 100, 2)


class LicenseRecord(Base, TimestampMixin, TenantScopedMixin):
    """Dependency or commercial licence inventory (FR-020, §14.2)."""

    __tablename__ = "license_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("lic"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="dependency", nullable=False)
    component: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    license_id: Mapped[str] = mapped_column(String(80), default="", nullable=False, index=True)
    policy: Mapped[str] = mapped_column(String(20), default="review", nullable=False)
    owner: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    scope: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    seats: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    source: Mapped[str] = mapped_column(String(200), default="", nullable=False)
