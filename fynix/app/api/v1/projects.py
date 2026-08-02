"""Project, brief, blueprint, task, run and cost endpoints (spec §9.1)."""

from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import select

from app.api.deps import CurrentPrincipal, DbSession, IdempotencyHeader
from app.api.schemas import (
    BlueprintOut,
    BriefCreate,
    BriefOut,
    BudgetOverrideRequest,
    OperationOut,
    ProjectCreate,
    ProjectDetail,
    ProjectOut,
    RunOut,
    RunRequest,
    StateTransitionRequest,
    TaskOut,
)
from app.core import audit, idempotency
from app.core.errors import NotFoundError, ValidationError
from app.core.policy import Resource, authorize
from app.core.tenancy import get_scoped, scoped
from app.models.base import DataClass, ProjectState, TaskStatus
from app.models.execution import AgentRun, Operation
from app.models.project import BlueprintVersion, BriefVersion, Project, Task
from app.orchestrator import dag, handlers, jobs
from app.orchestrator import state_machine as fsm
from app.services import budgets as budgets_service
from app.services import repositories as repo_service

router = APIRouter(prefix="/v1/projects", tags=["projects"])


def _load(session, principal, project_id: str) -> Project:
    return get_scoped(session, Project, principal.tenant_id, project_id)


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(
    payload: ProjectCreate,
    principal: CurrentPrincipal,
    session: DbSession,
    response: Response,
    idempotency_key: IdempotencyHeader = None,
) -> Project:
    authorize(
        principal,
        "project:create",
        Resource(type="project", tenant_id=principal.tenant_id),
    )

    body = payload.model_dump()
    replay = idempotency.lookup(
        session,
        tenant_id=principal.tenant_id,
        key=idempotency_key or "",
        endpoint="POST /v1/projects",
        request_body=body,
    )
    if replay is not None:
        response.headers["Idempotent-Replay"] = "true"
        return get_scoped(session, Project, principal.tenant_id, replay.response_body["id"])

    project = Project(
        tenant_id=principal.tenant_id,
        name=payload.name,
        golden_path=payload.golden_path,
        owner_user_id=payload.owner_user_id or principal.user_id,
        data_class=payload.data_class,
        budget_rub=payload.budget_rub,
        source=payload.source,
    )
    session.add(project)
    session.flush()

    budgets_service.ensure_budget(
        session,
        tenant_id=principal.tenant_id,
        project_id=project.id,
        limit=payload.budget_rub or None,
    )
    audit.record(
        session,
        tenant_id=principal.tenant_id,
        action="project.created",
        actor_id=principal.user_id,
        resource_type="project",
        resource_id=project.id,
        payload={"name": project.name, "golden_path": project.golden_path},
    )
    idempotency.store(
        session,
        tenant_id=principal.tenant_id,
        key=idempotency_key or "",
        endpoint="POST /v1/projects",
        request_body=body,
        response_status=201,
        response_body={"id": project.id},
    )
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(principal: CurrentPrincipal, session: DbSession, state: str | None = None):
    authorize(principal, "project:read", Resource(type="project", tenant_id=principal.tenant_id))
    stmt = scoped(Project, principal.tenant_id).order_by(Project.created_at.desc())
    if state:
        stmt = stmt.where(Project.state == state)
    return list(session.execute(stmt).scalars().all())


@router.get("/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, principal: CurrentPrincipal, session: DbSession):
    project = _load(session, principal, project_id)
    authorize(
        principal,
        "project:read",
        Resource(
            type="project",
            id=project.id,
            tenant_id=project.tenant_id,
            project_id=project.id,
            data_class=DataClass(project.data_class),
        ),
    )
    verdict = fsm.evaluate_gate(session, project)
    return ProjectDetail(
        **ProjectOut.model_validate(project).model_dump(),
        gate={"passed": verdict.passed, "reason": verdict.reason, "evidence": verdict.evidence},
        progress=dag.progress(session, project.id),
        budget=budgets_service.summary(
            session, tenant_id=principal.tenant_id, project_id=project.id
        ),
    )


@router.post("/{project_id}:transition", response_model=ProjectOut)
def transition_project(
    project_id: str,
    payload: StateTransitionRequest,
    principal: CurrentPrincipal,
    session: DbSession,
):
    project = _load(session, principal, project_id)
    authorize(
        principal,
        "project:update",
        Resource(type="project", id=project.id, tenant_id=project.tenant_id, project_id=project.id),
    )
    try:
        target = ProjectState(payload.target)
    except ValueError as exc:
        raise ValidationError(f"unknown target state '{payload.target}'") from exc

    # Forcing past a failed gate is a privileged act, not a convenience flag.
    if payload.force:
        authorize(
            principal,
            "waiver:issue",
            Resource(type="project", id=project.id, tenant_id=project.tenant_id, project_id=project.id),
        )

    fsm.transition(
        session,
        project=project,
        target=target,
        actor_id=principal.user_id,
        reason=payload.reason,
        force=payload.force,
    )

    # Bootstrapping is the side effect of entering S4.
    if target == ProjectState.S4_BOOTSTRAP:
        jobs.enqueue(
            session,
            tenant_id=principal.tenant_id,
            kind=handlers.BOOTSTRAP_PROJECT,
            project_id=project.id,
            payload={"project_id": project.id, "actor_id": principal.user_id},
            dedupe_key=f"bootstrap:{project.id}",
        )
    return project


# --- briefs ---------------------------------------------------------------


@router.post("/{project_id}/briefs", response_model=OperationOut, status_code=202)
def create_brief(
    project_id: str,
    payload: BriefCreate,
    principal: CurrentPrincipal,
    session: DbSession,
    idempotency_key: IdempotencyHeader = None,
):
    """Queue brief analysis. Returns an operation handle (spec §9.1 async)."""
    project = _load(session, principal, project_id)
    authorize(
        principal,
        "brief:write",
        Resource(type="brief", tenant_id=project.tenant_id, project_id=project.id),
    )

    body = payload.model_dump()
    replay = idempotency.lookup(
        session,
        tenant_id=principal.tenant_id,
        key=idempotency_key or "",
        endpoint=f"POST /v1/projects/{project_id}/briefs",
        request_body=body,
    )
    if replay is not None:
        return get_scoped(session, Operation, principal.tenant_id, replay.response_body["id"])

    operation = Operation(
        tenant_id=principal.tenant_id, project_id=project.id, kind="brief.analyze"
    )
    session.add(operation)
    session.flush()

    job = jobs.enqueue(
        session,
        tenant_id=principal.tenant_id,
        kind=handlers.INTAKE_ANALYZE,
        project_id=project.id,
        payload={
            "project_id": project.id,
            "raw_text": payload.raw_text,
            "answers": payload.answers,
            "actor_id": principal.user_id,
            "operation_id": operation.id,
        },
        correlation_id=operation.id,
    )
    operation.job_id = job.id
    session.flush()

    idempotency.store(
        session,
        tenant_id=principal.tenant_id,
        key=idempotency_key or "",
        endpoint=f"POST /v1/projects/{project_id}/briefs",
        request_body=body,
        response_status=202,
        response_body={"id": operation.id},
    )
    return operation


@router.get("/{project_id}/briefs", response_model=list[BriefOut])
def list_briefs(project_id: str, principal: CurrentPrincipal, session: DbSession):
    project = _load(session, principal, project_id)
    authorize(
        principal,
        "brief:read",
        Resource(type="brief", tenant_id=project.tenant_id, project_id=project.id),
    )
    return list(
        session.execute(
            select(BriefVersion)
            .where(BriefVersion.project_id == project.id)
            .order_by(BriefVersion.version.desc())
        )
        .scalars()
        .all()
    )


# --- blueprints -----------------------------------------------------------


@router.post("/{project_id}/blueprints:generate", response_model=OperationOut, status_code=202)
def generate_blueprint(project_id: str, principal: CurrentPrincipal, session: DbSession):
    project = _load(session, principal, project_id)
    authorize(
        principal,
        "blueprint:request",
        Resource(type="blueprint", tenant_id=project.tenant_id, project_id=project.id),
    )
    brief = session.execute(
        select(BriefVersion)
        .where(BriefVersion.project_id == project.id)
        .order_by(BriefVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if brief is None:
        raise NotFoundError("project has no brief yet")

    operation = Operation(
        tenant_id=principal.tenant_id, project_id=project.id, kind="blueprint.generate"
    )
    session.add(operation)
    session.flush()

    job = jobs.enqueue(
        session,
        tenant_id=principal.tenant_id,
        kind=handlers.SOLUTION_BLUEPRINT,
        project_id=project.id,
        payload={"project_id": project.id, "brief_version_id": brief.id},
        dedupe_key=f"blueprint:{brief.id}",
        correlation_id=operation.id,
    )
    operation.job_id = job.id
    session.flush()
    return operation


@router.get("/{project_id}/blueprints", response_model=list[BlueprintOut])
def list_blueprints(project_id: str, principal: CurrentPrincipal, session: DbSession):
    project = _load(session, principal, project_id)
    authorize(
        principal,
        "blueprint:read",
        Resource(type="blueprint", tenant_id=project.tenant_id, project_id=project.id),
    )
    return list(
        session.execute(
            select(BlueprintVersion)
            .where(BlueprintVersion.project_id == project.id)
            .order_by(BlueprintVersion.version.desc())
        )
        .scalars()
        .all()
    )


# --- tasks and runs -------------------------------------------------------


@router.get("/{project_id}/tasks", response_model=list[TaskOut])
def list_tasks(project_id: str, principal: CurrentPrincipal, session: DbSession):
    project = _load(session, principal, project_id)
    authorize(
        principal,
        "task:read",
        Resource(type="task", tenant_id=project.tenant_id, project_id=project.id),
    )
    return list(
        session.execute(
            select(Task).where(Task.project_id == project.id).order_by(Task.priority, Task.key)
        )
        .scalars()
        .all()
    )


@router.post("/{project_id}/tasks/{task_id}/runs", response_model=OperationOut, status_code=202)
def start_run(
    project_id: str,
    task_id: str,
    payload: RunRequest,
    principal: CurrentPrincipal,
    session: DbSession,
):
    """Start an agent run for a task (spec §9.1 POST /v1/tasks/{id}/runs)."""
    project = _load(session, principal, project_id)
    authorize(
        principal,
        "run:start",
        Resource(type="agent_run", tenant_id=project.tenant_id, project_id=project.id),
    )
    task = get_scoped(session, Task, principal.tenant_id, task_id)
    if task.project_id != project.id:
        raise NotFoundError("task does not belong to this project")
    if task.status in {TaskStatus.DONE.value, TaskStatus.CANCELLED.value}:
        raise ValidationError(f"task is already {task.status}")

    # Budget is checked before the job is even queued, so a hard stop is visible
    # to the caller instead of failing silently in the worker.
    budgets_service.assert_can_spend(
        session, tenant_id=principal.tenant_id, project_id=project.id
    )

    operation = Operation(
        tenant_id=principal.tenant_id, project_id=project.id, kind="task.execute"
    )
    session.add(operation)
    session.flush()

    job = jobs.enqueue(
        session,
        tenant_id=principal.tenant_id,
        kind=handlers.TASK_EXECUTE,
        project_id=project.id,
        payload={
            "project_id": project.id,
            "task_id": task.id,
            "override_profile": payload.override_profile,
        },
        dedupe_key=f"task:{task.id}:attempt:{task.attempts + 1}",
        correlation_id=operation.id,
    )
    operation.job_id = job.id
    session.flush()
    return operation


@router.get("/{project_id}/runs", response_model=list[RunOut])
def list_runs(project_id: str, principal: CurrentPrincipal, session: DbSession, limit: int = 50):
    project = _load(session, principal, project_id)
    authorize(
        principal,
        "run:read",
        Resource(type="agent_run", tenant_id=project.tenant_id, project_id=project.id),
    )
    return list(
        session.execute(
            select(AgentRun)
            .where(AgentRun.tenant_id == principal.tenant_id, AgentRun.project_id == project.id)
            .order_by(AgentRun.created_at.desc())
            .limit(min(limit, 200))
        )
        .scalars()
        .all()
    )


# --- cost -----------------------------------------------------------------


@router.get("/{project_id}/costs")
def project_costs(project_id: str, principal: CurrentPrincipal, session: DbSession):
    project = _load(session, principal, project_id)
    authorize(
        principal,
        "cost:read",
        Resource(type="budget", tenant_id=project.tenant_id, project_id=project.id),
    )
    return budgets_service.summary(
        session, tenant_id=principal.tenant_id, project_id=project.id
    )


@router.post("/{project_id}/budget:override")
def override_budget(
    project_id: str,
    payload: BudgetOverrideRequest,
    principal: CurrentPrincipal,
    session: DbSession,
):
    project = _load(session, principal, project_id)
    authorize(
        principal,
        "budget:override",
        Resource(type="budget", tenant_id=project.tenant_id, project_id=project.id),
    )
    budget = budgets_service.grant_override(
        session,
        principal=principal,
        project_id=project.id,
        amount=payload.amount,
        hours=payload.hours,
        reason=payload.reason,
    )
    return {
        "project_id": project.id,
        "effective_limit": budget.effective_limit,
        "expires_at": budget.override_until,
        "usage_pct": budget.usage_pct,
    }


@router.get("/{project_id}/repository")
def get_repository(project_id: str, principal: CurrentPrincipal, session: DbSession):
    project = _load(session, principal, project_id)
    authorize(
        principal,
        "project:read",
        Resource(type="repository", tenant_id=project.tenant_id, project_id=project.id),
    )
    repository = repo_service.get_for_project(
        session, tenant_id=principal.tenant_id, project_id=project.id
    )
    return {
        "id": repository.id,
        "full_name": repository.full_name,
        "default_branch": repository.default_branch,
        "protected_branches": repository.protected_branches,
        "required_checks": repository.required_checks,
        "clone_url": repository.clone_url,
    }
