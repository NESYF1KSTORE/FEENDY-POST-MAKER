"""Project and brief operations shared by the REST API and the Telegram bot.

Both entry points must produce identical state: the same audit entries, the same
budget row, the same intake job. Keeping the logic here rather than in a router
is what makes that true by construction instead of by review.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core import audit
from app.core.policy import Resource, authorize
from app.core.security import Principal
from app.models.execution import Operation
from app.models.project import Project
from app.orchestrator import handlers, jobs
from app.services import budgets as budgets_service


def create_project(
    session: Session,
    *,
    principal: Principal,
    name: str,
    golden_path: str = "",
    owner_user_id: str | None = None,
    data_class: str = "confidential",
    budget_rub: float = 0.0,
    source: str = "portal",
) -> Project:
    """Create a project, its budget envelope and the audit trail."""
    authorize(
        principal,
        "project:create",
        Resource(type="project", tenant_id=principal.tenant_id),
    )

    project = Project(
        tenant_id=principal.tenant_id,
        name=name,
        golden_path=golden_path,
        owner_user_id=owner_user_id or principal.user_id,
        data_class=data_class,
        budget_rub=budget_rub,
        source=source,
    )
    session.add(project)
    session.flush()

    budgets_service.ensure_budget(
        session,
        tenant_id=principal.tenant_id,
        project_id=project.id,
        limit=budget_rub or None,
    )
    audit.record(
        session,
        tenant_id=principal.tenant_id,
        action="project.created",
        actor_id=principal.user_id,
        resource_type="project",
        resource_id=project.id,
        payload={"name": project.name, "golden_path": project.golden_path, "source": source},
    )
    return project


def submit_brief(
    session: Session,
    *,
    principal: Principal,
    project: Project,
    raw_text: str,
    answers: dict[str, str] | None = None,
    attachments: list[dict] | None = None,
    source: str = "portal",
) -> Operation:
    """Queue brief analysis and return the operation handle.

    The raw text is never trusted: it reaches the analyst agent inside an
    `<untrusted_data>` fence (spec §5.4), so a brief that contains instructions
    is treated as data.
    """
    authorize(
        principal,
        "brief:write",
        Resource(type="brief", tenant_id=project.tenant_id, project_id=project.id),
    )

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
            "raw_text": raw_text,
            "answers": answers or {},
            "actor_id": principal.user_id,
            "operation_id": operation.id,
            "attachments": attachments or [],
            "source": source,
        },
        correlation_id=operation.id,
    )
    operation.job_id = job.id
    session.flush()
    return operation
