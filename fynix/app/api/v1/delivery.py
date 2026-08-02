"""Release and deployment endpoints (spec §9.1, FR-014/015/016)."""

from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import select

from app.api.deps import CurrentPrincipal, DbSession, IdempotencyHeader
from app.api.schemas import (
    DeploymentCreate,
    DeploymentOut,
    HealthReport,
    ReleaseCreate,
    ReleaseOut,
    RollbackRequest,
)
from app.core import idempotency
from app.core.errors import NotFoundError
from app.core.policy import Resource, authorize
from app.core.tenancy import get_scoped, scoped
from app.delivery import service as delivery_service
from app.models.delivery import Deployment, Environment, Release
from app.models.project import Project
from app.quality import evidence as evidence_module
from app.services import artifacts as artifacts_service

router = APIRouter(prefix="/v1", tags=["delivery"])


@router.post("/releases", response_model=ReleaseOut, status_code=201)
def create_release(
    payload: ReleaseCreate,
    principal: CurrentPrincipal,
    session: DbSession,
    response: Response,
    idempotency_key: IdempotencyHeader = None,
):
    project = get_scoped(session, Project, principal.tenant_id, payload.project_id)
    authorize(
        principal,
        "release:create",
        Resource(type="release", tenant_id=project.tenant_id, project_id=project.id),
    )

    body = payload.model_dump()
    replay = idempotency.lookup(
        session,
        tenant_id=principal.tenant_id,
        key=idempotency_key or "",
        endpoint="POST /v1/releases",
        request_body=body,
    )
    if replay is not None:
        response.headers["Idempotent-Replay"] = "true"
        return get_scoped(session, Release, principal.tenant_id, replay.response_body["id"])

    release = delivery_service.create_release(
        session,
        tenant_id=principal.tenant_id,
        project_id=project.id,
        version=payload.version,
        artifact_digest=payload.artifact_digest,
        change_set_ids=payload.change_set_ids,
        release_notes=payload.release_notes,
        rollback_plan=payload.rollback_plan,
        migration_plan=payload.migration_plan,
        actor_id=principal.user_id,
    )
    evidence_module.store_bundle(
        session, tenant_id=principal.tenant_id, project_id=project.id, release=release
    )
    idempotency.store(
        session,
        tenant_id=principal.tenant_id,
        key=idempotency_key or "",
        endpoint="POST /v1/releases",
        request_body=body,
        response_status=201,
        response_body={"id": release.id},
    )
    return release


@router.get("/releases", response_model=list[ReleaseOut])
def list_releases(principal: CurrentPrincipal, session: DbSession, project_id: str | None = None):
    authorize(principal, "release:read", Resource(type="release", tenant_id=principal.tenant_id))
    stmt = scoped(Release, principal.tenant_id).order_by(Release.created_at.desc())
    if project_id:
        stmt = stmt.where(Release.project_id == project_id)
    return list(session.execute(stmt).scalars().all())


@router.get("/releases/{release_id}/evidence")
def get_evidence(release_id: str, principal: CurrentPrincipal, session: DbSession):
    """Return the immutable evidence bundle backing a release (Appendix B)."""
    release = get_scoped(session, Release, principal.tenant_id, release_id)
    authorize(
        principal,
        "release:read",
        Resource(type="release", tenant_id=release.tenant_id, project_id=release.project_id),
    )
    if not release.evidence_artifact_id:
        raise NotFoundError("evidence bundle has not been generated for this release")
    artifact = artifacts_service.get(
        session, tenant_id=principal.tenant_id, artifact_id=release.evidence_artifact_id
    )
    return {
        "artifact_id": artifact.id,
        "checksum": artifact.checksum,
        "bundle": artifacts_service.read_json(artifact),
    }


@router.post("/deployments", response_model=DeploymentOut, status_code=201)
def create_deployment(
    payload: DeploymentCreate,
    principal: CurrentPrincipal,
    session: DbSession,
    response: Response,
    idempotency_key: IdempotencyHeader = None,
):
    """Start a deployment. Production requires valid dual approval (AC-07)."""
    project = get_scoped(session, Project, principal.tenant_id, payload.project_id)
    authorize(
        principal,
        "deployment:create",
        Resource(
            type="deployment",
            tenant_id=project.tenant_id,
            project_id=project.id,
            environment=payload.environment,
        ),
    )

    body = payload.model_dump()
    replay = idempotency.lookup(
        session,
        tenant_id=principal.tenant_id,
        key=idempotency_key or "",
        endpoint="POST /v1/deployments",
        request_body=body,
    )
    if replay is not None:
        response.headers["Idempotent-Replay"] = "true"
        return get_scoped(session, Deployment, principal.tenant_id, replay.response_body["id"])

    deployment = delivery_service.request_deployment(
        session,
        tenant_id=principal.tenant_id,
        project_id=project.id,
        release_id=payload.release_id,
        environment_name=payload.environment,
        strategy=payload.strategy,
        requested_by=principal.user_id,
    )
    idempotency.store(
        session,
        tenant_id=principal.tenant_id,
        key=idempotency_key or "",
        endpoint="POST /v1/deployments",
        request_body=body,
        response_status=201,
        response_body={"id": deployment.id},
    )
    return deployment


@router.post("/deployments/{deployment_id}:health", response_model=DeploymentOut)
def report_health(
    deployment_id: str,
    payload: HealthReport,
    principal: CurrentPrincipal,
    session: DbSession,
):
    """Post-deploy health. An unhealthy report rolls back automatically (§15)."""
    deployment = get_scoped(session, Deployment, principal.tenant_id, deployment_id)
    authorize(
        principal,
        "deployment:create",
        Resource(
            type="deployment",
            tenant_id=deployment.tenant_id,
            project_id=deployment.project_id,
        ),
    )
    delivery_service.mark_running(session, deployment)
    return delivery_service.complete_deployment(
        session, deployment=deployment, health=payload.model_dump()
    )


@router.post("/deployments/{deployment_id}:rollback", response_model=DeploymentOut)
def rollback_deployment(
    deployment_id: str,
    payload: RollbackRequest,
    principal: CurrentPrincipal,
    session: DbSession,
):
    deployment = get_scoped(session, Deployment, principal.tenant_id, deployment_id)
    authorize(
        principal,
        "deployment:rollback",
        Resource(
            type="deployment",
            tenant_id=deployment.tenant_id,
            project_id=deployment.project_id,
        ),
    )
    return delivery_service.rollback(
        session, deployment=deployment, reason=payload.reason, actor_id=principal.user_id
    )


@router.get("/deployments", response_model=list[DeploymentOut])
def list_deployments(
    principal: CurrentPrincipal, session: DbSession, project_id: str | None = None
):
    authorize(
        principal, "deployment:read", Resource(type="deployment", tenant_id=principal.tenant_id)
    )
    stmt = scoped(Deployment, principal.tenant_id).order_by(Deployment.created_at.desc())
    if project_id:
        stmt = stmt.where(Deployment.project_id == project_id)
    return list(session.execute(stmt).scalars().all())


@router.get("/environments")
def list_environments(
    principal: CurrentPrincipal, session: DbSession, project_id: str | None = None
):
    authorize(
        principal, "environment:read", Resource(type="environment", tenant_id=principal.tenant_id)
    )
    stmt = scoped(Environment, principal.tenant_id)
    if project_id:
        stmt = stmt.where(Environment.project_id == project_id)
    rows = list(session.execute(stmt).scalars().all())
    return [
        {
            "id": e.id,
            "project_id": e.project_id,
            "name": e.name,
            "url": e.url,
            "requires_approval": e.requires_approval,
            "current_release_id": e.current_release_id,
            "previous_release_id": e.previous_release_id,
            "drift_detected_at": e.drift_detected_at,
        }
        for e in rows
    ]


_ = select
