"""Approvals, waivers and the tamper-evident audit log (spec §2.1, §10.2)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    ApprovalStatus,
    Base,
    TenantScopedMixin,
    TimestampMixin,
    TZDateTime,
    new_id,
)


class Approval(Base, TimestampMixin, TenantScopedMixin):
    """One required signature. A gated action needs every pending approval resolved."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("apr"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # What is being approved: "blueprint", "budget_change", "deploy_prod", ...
    subject_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Which role must sign. Any active user holding it may decide.
    required_role: Mapped[str] = mapped_column(String(40), nullable=False)
    # Approvals with the same sequence run in parallel; higher sequence waits.
    sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), default=ApprovalStatus.PENDING.value, nullable=False, index=True
    )
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    delegated_to: Mapped[str | None] = mapped_column(String(64))

    policy_version: Mapped[str] = mapped_column(String(20), default="v1", nullable=False)
    # The subject checksum at request time. If the subject changes the approval
    # is marked stale and must be re-collected (spec §3.1).
    subject_checksum: Mapped[str] = mapped_column(String(72), default="", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    @property
    def status_enum(self) -> ApprovalStatus:
        return ApprovalStatus(self.status)

    def is_effective(self, now: datetime) -> bool:
        """Approved *and* still within its validity window (spec §3.1)."""
        if self.status != ApprovalStatus.APPROVED.value:
            return False
        return self.expires_at is None or self.expires_at > now


class PolicyWaiver(Base, TimestampMixin, TenantScopedMixin):
    """Time-bound exception to a blocking gate. Only security/compliance may issue."""

    __tablename__ = "policy_waivers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("wvr"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    gate: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    finding_key: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    justification: Mapped[str] = mapped_column(Text, nullable=False)
    issued_by: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now


class AuditEvent(Base, TenantScopedMixin):
    """Append-only, hash-chained record of every consequential action (NFR-009).

    `prev_hash` links each row to the previous one for the tenant, so silently
    deleting or editing history breaks the chain and is detectable by
    `app.core.audit.verify_chain`.
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("aud"))
    seq: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, index=True
    )
    actor_type: Mapped[str] = mapped_column(String(20), default="user", nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    resource_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(20), default="allow", nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    prev_hash: Mapped[str] = mapped_column(String(72), default="", nullable=False)
    hash: Mapped[str] = mapped_column(String(72), nullable=False)
