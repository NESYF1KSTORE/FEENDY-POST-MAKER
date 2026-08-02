"""Declarative base, id/timestamp mixins and the shared enum vocabulary."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """All timestamps are stored in UTC (NFR-014)."""
    return datetime.now(UTC)


class TZDateTime(TypeDecorator):
    """A timestamp that is always timezone-aware UTC in Python.

    PostgreSQL round-trips `timestamptz` fine, but SQLite (and some drivers)
    hand back a naive value, which then raises on any comparison with
    `utcnow()`. Normalising in one place means no caller has to remember.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class TenantScopedMixin:
    """Every row that belongs to a customer carries its tenant (NFR-006)."""

    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)


# --------------------------------------------------------------------------
# Vocabulary shared across modules. Stored as plain strings so that adding a
# value never requires a database migration of an enum type.
# --------------------------------------------------------------------------


class StrEnum(str, enum.Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class ProjectState(StrEnum):
    """Spec §3 — the end-to-end project state machine."""

    S0_LEAD = "S0_LEAD"
    S1_DISCOVERY = "S1_DISCOVERY"
    S2_SOLUTION = "S2_SOLUTION"
    S3_CONTRACT = "S3_CONTRACT"
    S4_BOOTSTRAP = "S4_BOOTSTRAP"
    S5_BUILD = "S5_BUILD"
    S6_VERIFY = "S6_VERIFY"
    S7_DELIVER = "S7_DELIVER"
    S8_OPERATE = "S8_OPERATE"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class Role(StrEnum):
    """Spec §2 — roles and their zone of decision."""

    CLIENT_OWNER = "client_owner"
    PROJECT_MANAGER = "project_manager"
    SOLUTION_ARCHITECT = "solution_architect"
    AI_OPERATOR = "ai_operator"
    DEVELOPER = "developer"
    QA_ENGINEER = "qa_engineer"
    DEVOPS_SRE = "devops_sre"
    SECURITY_COMPLIANCE = "security_compliance"
    FINANCE_ADMIN = "finance_admin"
    PLATFORM_ADMIN = "platform_admin"
    SERVICE_ACCOUNT = "service_account"


class DataClass(StrEnum):
    """Spec §8.1 — data classification drives what may leave the perimeter."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"

    @property
    def rank(self) -> int:
        return {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}[
            self.value
        ]


class TaskStatus(StrEnum):
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    REVIEW = "review"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    """Spec §5.2 — the agent run contract."""

    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    STALE = "stale"


class GateStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAIVED = "waived"


class JobStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD = "dead"


class DeploymentStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    RUNNING = "running"
    HEALTHY = "healthy"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class Severity(StrEnum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"
