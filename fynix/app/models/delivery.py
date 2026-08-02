"""Repositories, change sets, quality gates, releases and deployments (§11, §12)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    DeploymentStatus,
    GateStatus,
    Severity,
    TenantScopedMixin,
    TimestampMixin,
    TZDateTime,
    new_id,
)


class Repository(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "repositories"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("repo"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(30), default="github", nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(80), default="main", nullable=False)
    # Protected branches never accept a direct agent push (FR-011).
    protected_branches: Mapped[list] = mapped_column(
        JSON, default=lambda: ["main"], nullable=False
    )
    code_owners: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    required_checks: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    template: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    clone_url: Mapped[str] = mapped_column(String(300), default="", nullable=False)


class ChangeSet(Base, TimestampMixin, TenantScopedMixin):
    """A branch/PR produced by a run. Links code back to task, run and prompt (AC-04)."""

    __tablename__ = "change_sets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("chg"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    repository_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    branch: Mapped[str] = mapped_column(String(200), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(80), default="main", nullable=False)
    title: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    diff: Mapped[str] = mapped_column(Text, default="", nullable=False)
    files_changed: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    pr_url: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="open", nullable=False)
    merged_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class QualityGateResult(Base, TimestampMixin, TenantScopedMixin):
    """Outcome of one gate G0..G7 (spec §11.1) against one subject."""

    __tablename__ = "quality_gate_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("gate"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), default="change_set", nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    gate: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    blocking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    findings: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    waiver_id: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    @property
    def status_enum(self) -> GateStatus:
        return GateStatus(self.status)


class Release(Base, TimestampMixin, TenantScopedMixin):
    """A release candidate plus its immutable evidence bundle (FR-014, Appendix B)."""

    __tablename__ = "releases"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_release_version"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("rel"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    artifact_digest: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    change_set_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    evidence_artifact_id: Mapped[str | None] = mapped_column(String(64))
    sbom_artifact_id: Mapped[str | None] = mapped_column(String(64))
    release_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    rollback_plan: Mapped[str] = mapped_column(Text, default="", nullable=False)
    migration_plan: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="candidate", nullable=False)
    verdict: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)


class Environment(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "environments"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_env_name"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("env"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(40), nullable=False)  # dev|staging|prod
    url: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    config_version: Mapped[str] = mapped_column(String(40), default="1", nullable=False)
    # Secret *references* only — never values (NFR-008).
    secret_refs: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_release_id: Mapped[str | None] = mapped_column(String(64))
    previous_release_id: Mapped[str | None] = mapped_column(String(64))
    drift_detected_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class Deployment(Base, TimestampMixin, TenantScopedMixin):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("dep"))
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    environment_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    release_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    strategy: Mapped[str] = mapped_column(String(20), default="rolling", nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=DeploymentStatus.PENDING.value, nullable=False, index=True
    )
    requested_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    approval_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    health: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    rollback_of_id: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    @property
    def status_enum(self) -> DeploymentStatus:
        return DeploymentStatus(self.status)


class Incident(Base, TimestampMixin, TenantScopedMixin):
    """Operational incident (spec §13). SEV1/SEV2 require a postmortem."""

    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("inc"))
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(
        String(10), default=Severity.SEV3.value, nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(40), default="alert", nullable=False)
    deployment_id: Mapped[str | None] = mapped_column(String(64))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    postmortem_url: Mapped[str] = mapped_column(String(400), default="", nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime)
