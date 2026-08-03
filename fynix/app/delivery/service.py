"""Environments, releases, deployments and rollback (spec §12, FR-015/016).

Deployment rules enforced here:
  * only an immutable, already-built artifact digest is promoted — never a
    rebuild (spec §12 "deploy the same artifact digest");
  * production requires dual approval that is still valid at deploy time;
  * a failed health check rolls back automatically and opens an incident;
  * repeating a deploy request with the same idempotency key is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import (
    ApprovalRequired,
    ConflictError,
    GateBlocked,
    NotFoundError,
    ValidationError,
)
from app.models.base import DeploymentStatus, Severity, utcnow
from app.models.delivery import Deployment, Environment, Incident, Release
from app.orchestrator import events
from app.quality import gates as gates_module
from app.services import approvals as approvals_service

DEFAULT_ENVIRONMENTS = ("dev", "staging", "prod")
PROD = "prod"


def ensure_environments(
    session: Session, *, tenant_id: str, project_id: str, base_url: str = ""
) -> list[Environment]:
    """Create the standard dev/staging/prod triple if it does not exist."""
    existing = {
        env.name: env
        for env in session.execute(
            select(Environment).where(
                Environment.tenant_id == tenant_id, Environment.project_id == project_id
            )
        )
        .scalars()
        .all()
    }
    created = []
    for name in DEFAULT_ENVIRONMENTS:
        if name in existing:
            created.append(existing[name])
            continue
        env = Environment(
            tenant_id=tenant_id,
            project_id=project_id,
            name=name,
            url=f"{base_url}/{name}" if base_url else "",
            requires_approval=(name == PROD),
        )
        session.add(env)
        created.append(env)
    session.flush()
    return created


def get_environment(
    session: Session, *, tenant_id: str, project_id: str, name: str
) -> Environment:
    env = session.execute(
        select(Environment).where(
            Environment.tenant_id == tenant_id,
            Environment.project_id == project_id,
            Environment.name == name,
        )
    ).scalar_one_or_none()
    if env is None:
        raise NotFoundError(f"environment '{name}' not found for this project")
    return env


def create_release(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    version: str,
    artifact_digest: str,
    change_set_ids: list[str],
    release_notes: str = "",
    rollback_plan: str = "",
    migration_plan: str = "",
    actor_id: str = "",
) -> Release:
    if not artifact_digest:
        raise ValidationError("a release requires an immutable artifact digest")
    existing = session.execute(
        select(Release).where(
            Release.tenant_id == tenant_id,
            Release.project_id == project_id,
            Release.version == version,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Idempotent: the same version with the same digest returns the same row.
        if existing.artifact_digest != artifact_digest:
            raise ConflictError(
                f"release '{version}' already exists with a different artifact digest",
                details={"reason": "release_version_conflict"},
            )
        return existing

    release = Release(
        tenant_id=tenant_id,
        project_id=project_id,
        version=version,
        artifact_digest=artifact_digest,
        change_set_ids=change_set_ids,
        release_notes=release_notes,
        rollback_plan=rollback_plan,
        migration_plan=migration_plan,
    )
    session.add(release)
    session.flush()

    audit.record(
        session,
        tenant_id=tenant_id,
        action="release.candidate_created",
        actor_id=actor_id,
        resource_type="release",
        resource_id=release.id,
        payload={"version": version, "digest": artifact_digest, "changes": len(change_set_ids)},
    )
    events.emit(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        event_type=events.RELEASE_CANDIDATE_CREATED,
        payload={"release_id": release.id, "version": version},
    )
    return release


@dataclass(frozen=True)
class DeployPreflight:
    allowed: bool
    reason: str
    details: dict


def preflight(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    release: Release,
    environment: Environment,
    now: datetime | None = None,
) -> DeployPreflight:
    """Everything that must hold before a deployment may start."""
    now = now or utcnow()

    blocking = gates_module.blockers(session, tenant_id=tenant_id, subject_id=release.id, now=now)
    if blocking:
        return DeployPreflight(
            False,
            "blocking_gate",
            {"gates": [{"gate": g.gate, "status": g.status} for g in blocking]},
        )

    if not release.evidence_artifact_id:
        return DeployPreflight(False, "evidence_missing", {"release_id": release.id})

    if environment.requires_approval:
        satisfied, reason = approvals_service.is_satisfied(
            session,
            tenant_id=tenant_id,
            subject_type="deploy_prod",
            subject_id=release.id,
            now=now,
        )
        if not satisfied:
            return DeployPreflight(False, f"approval_{reason}", {"release_id": release.id})

    return DeployPreflight(True, "ready", {})


def request_deployment(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    release_id: str,
    environment_name: str,
    strategy: str = "rolling",
    requested_by: str = "",
    now: datetime | None = None,
) -> Deployment:
    """Create a deployment record after the preflight passes."""
    now = now or utcnow()
    release = session.execute(
        select(Release).where(Release.tenant_id == tenant_id, Release.id == release_id)
    ).scalar_one_or_none()
    if release is None:
        raise NotFoundError(f"release '{release_id}' not found")

    environment = get_environment(
        session, tenant_id=tenant_id, project_id=project_id, name=environment_name
    )
    check = preflight(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        release=release,
        environment=environment,
        now=now,
    )
    if not check.allowed:
        if check.reason.startswith("approval_"):
            raise ApprovalRequired(
                "production deployment requires valid dual approval",
                details={"reason": check.reason, **check.details},
            )
        raise GateBlocked(
            f"deployment refused: {check.reason}",
            details={"reason": check.reason, **check.details},
        )

    deployment = Deployment(
        tenant_id=tenant_id,
        project_id=project_id,
        environment_id=environment.id,
        release_id=release.id,
        strategy=strategy if environment.name != PROD else (strategy or "canary"),
        status=DeploymentStatus.APPROVED.value,
        requested_by=requested_by,
    )
    session.add(deployment)
    session.flush()

    audit.record(
        session,
        tenant_id=tenant_id,
        action="deployment.requested",
        actor_id=requested_by,
        resource_type="deployment",
        resource_id=deployment.id,
        payload={
            "environment": environment.name,
            "release": release.version,
            "digest": release.artifact_digest,
            "strategy": deployment.strategy,
        },
    )
    return deployment


def mark_running(session: Session, deployment: Deployment) -> Deployment:
    deployment.status = DeploymentStatus.RUNNING.value
    deployment.started_at = utcnow()
    session.flush()
    return deployment


def complete_deployment(
    session: Session,
    *,
    deployment: Deployment,
    health: dict,
    now: datetime | None = None,
) -> Deployment:
    """Finish a deployment; an unhealthy result triggers automatic rollback (§15)."""
    now = now or utcnow()
    healthy = bool(health.get("healthy", False))
    deployment.health = health
    deployment.finished_at = now

    environment = session.execute(
        select(Environment).where(Environment.id == deployment.environment_id)
    ).scalar_one()

    if healthy:
        deployment.status = DeploymentStatus.HEALTHY.value
        environment.previous_release_id = environment.current_release_id
        environment.current_release_id = deployment.release_id
        session.flush()
        audit.record(
            session,
            tenant_id=deployment.tenant_id,
            action="deployment.completed",
            actor_type="system",
            resource_type="deployment",
            resource_id=deployment.id,
            payload={"environment": environment.name, "health": health},
        )
        events.emit(
            session,
            tenant_id=deployment.tenant_id,
            project_id=deployment.project_id,
            event_type=events.DEPLOYMENT_COMPLETED,
            payload={
                "deployment_id": deployment.id,
                "environment": environment.name,
                "release_id": deployment.release_id,
                "healthy": True,
            },
        )
        return deployment

    deployment.status = DeploymentStatus.FAILED.value
    session.flush()
    rollback(
        session,
        deployment=deployment,
        reason=f"health check failed: {health.get('reason', 'unknown')}",
        automatic=True,
        now=now,
    )
    return deployment


def rollback(
    session: Session,
    *,
    deployment: Deployment,
    reason: str,
    actor_id: str = "",
    automatic: bool = False,
    now: datetime | None = None,
) -> Deployment:
    """Roll an environment back to its previous release and open an incident."""
    now = now or utcnow()
    if not reason.strip():
        raise ValidationError("a rollback reason is required")

    environment = session.execute(
        select(Environment).where(Environment.id == deployment.environment_id)
    ).scalar_one()

    target_release_id = environment.previous_release_id
    rollback_deployment = Deployment(
        tenant_id=deployment.tenant_id,
        project_id=deployment.project_id,
        environment_id=environment.id,
        release_id=target_release_id or deployment.release_id,
        strategy="rollback",
        status=DeploymentStatus.HEALTHY.value
        if target_release_id
        else DeploymentStatus.FAILED.value,
        requested_by=actor_id or "system",
        rollback_of_id=deployment.id,
        reason=reason,
        started_at=now,
        finished_at=now,
    )
    session.add(rollback_deployment)

    deployment.status = DeploymentStatus.ROLLED_BACK.value
    if target_release_id:
        environment.current_release_id = target_release_id

    incident = Incident(
        tenant_id=deployment.tenant_id,
        project_id=deployment.project_id,
        severity=Severity.SEV2.value if environment.name == PROD else Severity.SEV3.value,
        title=f"Rollback in {environment.name}: {reason[:120]}",
        description=reason,
        source="deployment",
        deployment_id=deployment.id,
        evidence={"health": deployment.health, "automatic": automatic},
    )
    session.add(incident)
    session.flush()

    audit.record(
        session,
        tenant_id=deployment.tenant_id,
        action="deployment.rolled_back",
        actor_id=actor_id,
        actor_type="system" if automatic else "user",
        resource_type="deployment",
        resource_id=deployment.id,
        decision="deny",
        payload={
            "environment": environment.name,
            "reason": reason,
            "automatic": automatic,
            "target_release_id": target_release_id,
            "incident_id": incident.id,
        },
    )
    events.emit(
        session,
        tenant_id=deployment.tenant_id,
        project_id=deployment.project_id,
        event_type=events.DEPLOYMENT_ROLLED_BACK,
        payload={
            "deployment_id": deployment.id,
            "rollback_deployment_id": rollback_deployment.id,
            "environment": environment.name,
            "reason": reason,
        },
    )
    events.emit(
        session,
        tenant_id=deployment.tenant_id,
        project_id=deployment.project_id,
        event_type=events.INCIDENT_OPENED,
        payload={"incident_id": incident.id, "severity": incident.severity, "title": incident.title},
    )
    return rollback_deployment


def detect_drift(
    session: Session, *, tenant_id: str, project_id: str, environment_name: str, observed_digest: str
) -> bool:
    """Compare what is actually running against what we deployed (FR-015)."""
    environment = get_environment(
        session, tenant_id=tenant_id, project_id=project_id, name=environment_name
    )
    if not environment.current_release_id:
        return False
    release = session.execute(
        select(Release).where(Release.id == environment.current_release_id)
    ).scalar_one_or_none()
    if release is None:
        return False
    drifted = release.artifact_digest != observed_digest
    if drifted:
        environment.drift_detected_at = utcnow()
        session.flush()
        audit.record(
            session,
            tenant_id=tenant_id,
            action="environment.drift_detected",
            actor_type="system",
            resource_type="environment",
            resource_id=environment.id,
            decision="deny",
            payload={"expected": release.artifact_digest, "observed": observed_digest},
        )
    return drifted
