"""Operations, webhooks, health and metrics (FR-024/FR-025, spec §13)."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Response
from sqlalchemy import func, select

from app.api.deps import CurrentPrincipal, DbSession
from app.api.schemas import OperationOut, WebhookCreate, WebhookCreated, WebhookOut
from app.core import audit
from app.core.policy import Resource, authorize
from app.core.tenancy import get_scoped, scoped
from app.db import get_engine
from app.models.base import JobStatus, ProjectState, RunStatus, utcnow
from app.models.delivery import Deployment, Incident
from app.models.execution import AgentRun, Job, Operation
from app.models.finance import CostLedgerEntry
from app.models.messaging import OutboxEvent, WebhookSubscription
from app.models.project import Project
from app.orchestrator import jobs as jobs_module

router = APIRouter(tags=["ops"])


@router.get("/v1/operations/{operation_id}", response_model=OperationOut)
def get_operation(operation_id: str, principal: CurrentPrincipal, session: DbSession):
    """Poll a long-running operation. Reflects the backing job's live state."""
    operation = get_scoped(session, Operation, principal.tenant_id, operation_id)
    authorize(
        principal,
        "project:read",
        Resource(type="operation", tenant_id=operation.tenant_id, project_id=operation.project_id),
    )
    if operation.job_id and operation.status == "running":
        job = session.get(Job, operation.job_id)
        if job is not None:
            if job.status == JobStatus.SUCCEEDED.value:
                operation.status = "succeeded"
                operation.finished_at = job.finished_at
            elif job.status in {JobStatus.DEAD.value, JobStatus.CANCELLED.value}:
                operation.status = "failed"
                operation.error = job.last_error
                operation.finished_at = job.finished_at
            session.flush()
    return operation


@router.post("/v1/webhooks", response_model=WebhookCreated, status_code=201)
def create_webhook(payload: WebhookCreate, principal: CurrentPrincipal, session: DbSession):
    """Register a signed webhook. The secret is returned exactly once."""
    authorize(
        principal, "notification:send", Resource(type="webhook", tenant_id=principal.tenant_id)
    )
    secret = secrets.token_urlsafe(32)
    subscription = WebhookSubscription(
        tenant_id=principal.tenant_id,
        project_id=payload.project_id,
        url=payload.url,
        event_types=payload.event_types,
        secret=secret,
    )
    session.add(subscription)
    session.flush()
    audit.record(
        session,
        tenant_id=principal.tenant_id,
        action="webhook.created",
        actor_id=principal.user_id,
        resource_type="webhook",
        resource_id=subscription.id,
        payload={"url": payload.url, "event_types": payload.event_types},
    )
    return WebhookCreated(
        id=subscription.id,
        url=subscription.url,
        event_types=subscription.event_types,
        project_id=subscription.project_id,
        is_active=subscription.is_active,
        secret=secret,
    )


@router.get("/v1/webhooks", response_model=list[WebhookOut])
def list_webhooks(principal: CurrentPrincipal, session: DbSession):
    authorize(
        principal, "notification:send", Resource(type="webhook", tenant_id=principal.tenant_id)
    )
    return list(session.execute(scoped(WebhookSubscription, principal.tenant_id)).scalars().all())


@router.get("/v1/incidents")
def list_incidents(principal: CurrentPrincipal, session: DbSession, open_only: bool = True):
    authorize(principal, "incident:read", Resource(type="incident", tenant_id=principal.tenant_id))
    stmt = scoped(Incident, principal.tenant_id).order_by(Incident.created_at.desc())
    if open_only:
        stmt = stmt.where(Incident.status != "resolved")
    rows = list(session.execute(stmt).scalars().all())
    return [
        {
            "id": i.id,
            "project_id": i.project_id,
            "severity": i.severity,
            "title": i.title,
            "status": i.status,
            "source": i.source,
            "created_at": i.created_at,
            "postmortem_url": i.postmortem_url,
        }
        for i in rows
    ]


@router.get("/v1/dashboard")
def dashboard(principal: CurrentPrincipal, session: DbSession):
    """SLI dashboard for API, workflow, runners, AI and cost (AC-13)."""
    authorize(principal, "project:read", Resource(type="dashboard", tenant_id=principal.tenant_id))
    tenant_id = principal.tenant_id

    project_states = dict(
        session.execute(
            select(Project.state, func.count(Project.id))
            .where(Project.tenant_id == tenant_id)
            .group_by(Project.state)
        ).all()
    )
    run_states = dict(
        session.execute(
            select(AgentRun.status, func.count(AgentRun.id))
            .where(AgentRun.tenant_id == tenant_id)
            .group_by(AgentRun.status)
        ).all()
    )
    total_runs = sum(run_states.values())
    succeeded = run_states.get(RunStatus.SUCCEEDED.value, 0)

    spend = float(
        session.execute(
            select(func.coalesce(func.sum(CostLedgerEntry.amount), 0.0)).where(
                CostLedgerEntry.tenant_id == tenant_id
            )
        ).scalar_one()
    )
    tokens = int(
        session.execute(
            select(func.coalesce(func.sum(AgentRun.prompt_tokens + AgentRun.completion_tokens), 0))
            .where(AgentRun.tenant_id == tenant_id)
        ).scalar_one()
    )
    avg_latency = session.execute(
        select(func.avg(AgentRun.latency_ms)).where(
            AgentRun.tenant_id == tenant_id, AgentRun.status == RunStatus.SUCCEEDED.value
        )
    ).scalar()

    open_incidents = int(
        session.execute(
            select(func.count(Incident.id)).where(
                Incident.tenant_id == tenant_id, Incident.status != "resolved"
            )
        ).scalar_one()
    )
    rollbacks = int(
        session.execute(
            select(func.count(Deployment.id)).where(
                Deployment.tenant_id == tenant_id, Deployment.strategy == "rollback"
            )
        ).scalar_one()
    )
    pending_events = int(
        session.execute(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.tenant_id == tenant_id, OutboxEvent.published_at.is_(None)
            )
        ).scalar_one()
    )
    dead_letters = int(
        session.execute(
            select(func.count(OutboxEvent.id)).where(
                OutboxEvent.tenant_id == tenant_id, OutboxEvent.dead_lettered_at.isnot(None)
            )
        ).scalar_one()
    )

    return {
        "generated_at": utcnow(),
        "projects": {
            "by_state": project_states,
            "active": sum(
                count
                for state, count in project_states.items()
                if state
                not in {
                    ProjectState.CLOSED.value,
                    ProjectState.CANCELLED.value,
                    ProjectState.REJECTED.value,
                }
            ),
        },
        "workflow": {"jobs": jobs_module.stats(session)},
        "ai": {
            "runs": run_states,
            "success_rate_pct": round(succeeded / total_runs * 100, 1) if total_runs else 0.0,
            "tokens": tokens,
            "avg_latency_ms": int(avg_latency or 0),
        },
        "cost": {"spend_rub": round(spend, 2)},
        "delivery": {"rollbacks": rollbacks},
        "operations": {
            "open_incidents": open_incidents,
            "events_pending": pending_events,
            "events_dead_lettered": dead_letters,
        },
    }


@router.get("/health")
def health():
    """Liveness — intentionally does not touch the database."""
    return {"status": "ok", "time": utcnow()}


@router.get("/health/ready")
def readiness(response: Response):
    """Readiness — fails loudly if the database is not reachable."""
    try:
        with get_engine().connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception as exc:
        response.status_code = 503
        return {"status": "unavailable", "detail": str(exc)[:200]}
    return {"status": "ready"}


@router.get("/metrics")
def metrics(session: DbSession):
    """Prometheus exposition of the platform's core SLIs (NFR-010)."""
    lines = [
        "# HELP fynix_jobs_total Jobs by status",
        "# TYPE fynix_jobs_total gauge",
    ]
    for status, count in jobs_module.stats(session).items():
        lines.append(f'fynix_jobs_total{{status="{status}"}} {count}')

    run_rows = session.execute(
        select(AgentRun.status, func.count(AgentRun.id)).group_by(AgentRun.status)
    ).all()
    lines += ["# HELP fynix_agent_runs_total Agent runs by status", "# TYPE fynix_agent_runs_total gauge"]
    for status, count in run_rows:
        lines.append(f'fynix_agent_runs_total{{status="{status}"}} {count}')

    spend = float(
        session.execute(
            select(func.coalesce(func.sum(CostLedgerEntry.amount), 0.0))
        ).scalar_one()
    )
    lines += [
        "# HELP fynix_spend_rub_total Total recorded spend in RUB",
        "# TYPE fynix_spend_rub_total counter",
        f"fynix_spend_rub_total {spend:.4f}",
    ]

    pending = int(
        session.execute(
            select(func.count(OutboxEvent.id)).where(OutboxEvent.published_at.is_(None))
        ).scalar_one()
    )
    lines += [
        "# HELP fynix_outbox_pending Unpublished domain events",
        "# TYPE fynix_outbox_pending gauge",
        f"fynix_outbox_pending {pending}",
    ]
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
