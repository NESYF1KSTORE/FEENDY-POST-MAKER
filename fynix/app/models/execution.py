"""Agent runs, durable jobs and artifacts (spec §5.2, §8)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    DataClass,
    JobStatus,
    RunStatus,
    TenantScopedMixin,
    TimestampMixin,
    TZDateTime,
    new_id,
)


class AgentRun(Base, TimestampMixin, TenantScopedMixin):
    """The Agent Run contract from spec §5.2 — one row per invocation.

    Everything needed to answer "why does this line of code exist" is here:
    model profile, prompt version, granted tools, budget, evidence.
    """

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("run"))
    parent_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), index=True)

    agent_type: Mapped[str] = mapped_column(String(40), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(20), default="v1", nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(20), default="v1", nullable=False)
    prompt_checksum: Mapped[str] = mapped_column(String(72), default="", nullable=False)

    model_profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    tool_grants: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    budget: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    input_artifacts: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    output_artifacts: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    status: Mapped[str] = mapped_column(
        String(30), default=RunStatus.QUEUED.value, nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_rub: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    data_class: Mapped[str] = mapped_column(
        String(20), default=DataClass.INTERNAL.value, nullable=False
    )
    correlation_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    output: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    @property
    def status_enum(self) -> RunStatus:
        return RunStatus(self.status)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class Job(Base, TimestampMixin, TenantScopedMixin):
    """Durable work item for the orchestrator worker.

    This is the port that a Temporal-compatible engine would replace (spec
    §4.3). It gives us the properties the spec actually requires: durable
    timers, at-least-once execution, retries with backoff and idempotency.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_job_dedupe"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("job"))
    kind: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Idempotency: a re-submitted job with the same key is a no-op (AC-11).
    dedupe_key: Mapped[str | None] = mapped_column(String(200))

    status: Mapped[str] = mapped_column(
        String(20), default=JobStatus.PENDING.value, nullable=False, index=True
    )
    run_after: Mapped[datetime] = mapped_column(
        TZDateTime, nullable=False, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    leased_by: Mapped[str | None] = mapped_column(String(80))
    lease_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class Artifact(Base, TimestampMixin, TenantScopedMixin):
    """Immutable output with a checksum (spec §4.1: artifacts never mutate)."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("art"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    uri: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    checksum: Mapped[str] = mapped_column(String(72), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    data_class: Mapped[str] = mapped_column(
        String(20), default=DataClass.INTERNAL.value, nullable=False
    )
    producer_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    producer_kind: Mapped[str] = mapped_column(String(40), default="agent", nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    retention_until: Mapped[datetime | None] = mapped_column(TZDateTime)
    legal_hold: Mapped[bool] = mapped_column(default=False, nullable=False)


class RunnerWorkspace(Base, TimestampMixin, TenantScopedMixin):
    """Ephemeral sandbox record — used by the reconciler to kill orphans (AC-11)."""

    __tablename__ = "runner_workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("wsp"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    driver: Mapped[str] = mapped_column(String(20), default="local", nullable=False)
    path: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    container_id: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class Operation(Base, TimestampMixin, TenantScopedMixin):
    """Long-running async operation handle behind GET /v1/operations/{id}."""

    __tablename__ = "operations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("op"))
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class IdempotencyKey(Base, TimestampMixin, TenantScopedMixin):
    """Replay protection for POST endpoints (spec §9.1 Idempotency-Key)."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", "endpoint", name="uq_idem_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("idem"))
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


# Explicit FK declaration kept out of the class bodies above so that model
# import order stays free of cycles.
_ = ForeignKey
