"""Project lifecycle entities: project, brief, blueprint, task DAG (spec §3, §8)."""

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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    DataClass,
    ProjectState,
    TaskStatus,
    TenantScopedMixin,
    TimestampMixin,
    TZDateTime,
    new_id,
)


class Project(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("prj"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    state: Mapped[str] = mapped_column(
        String(30), default=ProjectState.S0_LEAD.value, nullable=False, index=True
    )
    golden_path: Mapped[str] = mapped_column(String(60), default="", nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    manager_user_id: Mapped[str | None] = mapped_column(String(64))
    data_class: Mapped[str] = mapped_column(
        String(20), default=DataClass.CONFIDENTIAL.value, nullable=False
    )
    budget_rub: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    source: Mapped[str] = mapped_column(String(60), default="portal", nullable=False)
    repository_id: Mapped[str | None] = mapped_column(String(64))
    labels: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    briefs: Mapped[list[BriefVersion]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    blueprints: Mapped[list[BlueprintVersion]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    tasks: Mapped[list[Task]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    @property
    def state_enum(self) -> ProjectState:
        return ProjectState(self.state)


class BriefVersion(Base, TimestampMixin, TenantScopedMixin):
    """Immutable brief snapshot (FR-002/FR-003). A new answer creates a new version."""

    __tablename__ = "brief_versions"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_brief_version"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("brf"))
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # 0..100; the S1 exit gate requires >= 80 (spec §3).
    completeness: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    open_questions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    risks: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    checksum: Mapped[str] = mapped_column(String(72), default="", nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    attachments: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    project: Mapped[Project] = relationship(back_populates="briefs")


class BlueprintVersion(Base, TimestampMixin, TenantScopedMixin):
    """Solution Blueprint per Appendix A. Bound to the brief version it was built from."""

    __tablename__ = "blueprint_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_blueprint_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("bpv"))
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    brief_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    architecture: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    backlog: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    nfr: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    risks: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    estimate: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    adr_refs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    checksum: Mapped[str] = mapped_column(String(72), default="", nullable=False)
    # Set when the brief it was derived from is superseded (spec §3.1 invariant).
    is_stale: Mapped[bool] = mapped_column(default=False, nullable=False)
    produced_by_run_id: Mapped[str | None] = mapped_column(String(64))

    project: Mapped[Project] = relationship(back_populates="blueprints")


class Task(Base, TimestampMixin, TenantScopedMixin):
    """A DAG unit (FR-008). Executed by an agent or a human."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("tsk"))
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    blueprint_version_id: Mapped[str | None] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="code", nullable=False)
    executor: Mapped[str] = mapped_column(String(20), default="agent", nullable=False)
    agent_type: Mapped[str | None] = mapped_column(String(40))
    assignee_user_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(30), default=TaskStatus.BLOCKED.value, nullable=False, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    acceptance_criteria: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    estimate_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimate_hours: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    result: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    project: Mapped[Project] = relationship(back_populates="tasks")

    @property
    def status_enum(self) -> TaskStatus:
        return TaskStatus(self.status)


class TaskDependency(Base, TimestampMixin):
    """Edge of the task DAG: `task_id` cannot start until `depends_on_id` is done."""

    __tablename__ = "task_dependencies"
    __table_args__ = (
        UniqueConstraint("task_id", "depends_on_id", name="uq_task_edge"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("dep"))
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    depends_on_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
