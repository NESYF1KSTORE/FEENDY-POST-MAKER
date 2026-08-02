"""Approvals, waivers and audit endpoints (FR-005, FR-023)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentPrincipal, DbSession
from app.api.schemas import ApprovalDecision, ApprovalOut, WaiverCreate
from app.core import audit
from app.core.policy import Resource, authorize
from app.core.tenancy import get_scoped, scoped
from app.models.base import ApprovalStatus, utcnow
from app.models.governance import Approval, AuditEvent, PolicyWaiver
from app.models.project import Project
from app.services import approvals as approvals_service

router = APIRouter(prefix="/v1", tags=["governance"])


@router.get("/approvals", response_model=list[ApprovalOut])
def list_approvals(
    principal: CurrentPrincipal,
    session: DbSession,
    project_id: str | None = None,
    pending_only: bool = True,
):
    """Approvals visible to the caller. `mine_only` semantics are applied by the
    portal; the API returns everything in the tenant the caller may read."""
    authorize(principal, "project:read", Resource(type="approval", tenant_id=principal.tenant_id))
    stmt = scoped(Approval, principal.tenant_id).order_by(Approval.created_at.desc())
    if project_id:
        stmt = stmt.where(Approval.project_id == project_id)
    if pending_only:
        stmt = stmt.where(Approval.status == ApprovalStatus.PENDING.value)
    return list(session.execute(stmt).scalars().all())


@router.get("/approvals/actionable", response_model=list[ApprovalOut])
def actionable_approvals(principal: CurrentPrincipal, session: DbSession):
    """Only the approvals this principal can actually decide right now."""
    authorize(principal, "project:read", Resource(type="approval", tenant_id=principal.tenant_id))
    pending = list(
        session.execute(
            scoped(Approval, principal.tenant_id).where(
                Approval.status == ApprovalStatus.PENDING.value
            )
        )
        .scalars()
        .all()
    )

    by_subject: dict[tuple[str, str], list[Approval]] = {}
    for approval in pending:
        by_subject.setdefault((approval.subject_type, approval.subject_id), [])

    out: list[Approval] = []
    for subject_type, subject_id in by_subject:
        siblings = approvals_service.list_for_subject(
            session,
            tenant_id=principal.tenant_id,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        for approval in approvals_service.actionable(siblings):
            from app.models.base import Role

            if approvals_service.can_decide(
                principal, Role(approval.required_role), approval.project_id
            ):
                out.append(approval)
    return out


@router.post("/approvals/{approval_id}:decide", response_model=ApprovalOut)
def decide_approval(
    approval_id: str,
    payload: ApprovalDecision,
    principal: CurrentPrincipal,
    session: DbSession,
):
    approval = approvals_service.decide(
        session,
        principal=principal,
        approval_id=approval_id,
        approved=payload.approved,
        comment=payload.comment,
    )

    # An approved blueprint moves the project forward automatically.
    if approval.subject_type == "blueprint" and payload.approved:
        satisfied, _ = approvals_service.is_satisfied(
            session,
            tenant_id=principal.tenant_id,
            subject_type="blueprint",
            subject_id=approval.subject_id,
        )
        if satisfied:
            from app.orchestrator import events as events_module

            project = session.get(Project, approval.project_id)
            if project is not None:
                events_module.emit(
                    session,
                    tenant_id=principal.tenant_id,
                    project_id=project.id,
                    event_type=events_module.BLUEPRINT_APPROVED,
                    payload={"blueprint_id": approval.subject_id},
                )
    return approval


@router.post("/waivers", status_code=201)
def create_waiver(payload: WaiverCreate, principal: CurrentPrincipal, session: DbSession):
    """Time-bound exception to a blocking gate — security/compliance only."""
    project = get_scoped(session, Project, principal.tenant_id, payload.project_id)
    authorize(
        principal,
        "waiver:issue",
        Resource(type="waiver", tenant_id=project.tenant_id, project_id=project.id),
    )

    waiver = PolicyWaiver(
        tenant_id=principal.tenant_id,
        project_id=project.id,
        gate=payload.gate,
        finding_key=payload.finding_key,
        justification=payload.justification,
        issued_by=principal.user_id,
        expires_at=utcnow() + timedelta(hours=payload.hours),
    )
    session.add(waiver)
    session.flush()

    audit.record(
        session,
        tenant_id=principal.tenant_id,
        action="waiver.issued",
        actor_id=principal.user_id,
        resource_type="waiver",
        resource_id=waiver.id,
        payload={
            "gate": payload.gate,
            "project_id": project.id,
            "expires_at": waiver.expires_at.isoformat(),
            "justification": payload.justification,
        },
    )
    return {
        "id": waiver.id,
        "gate": waiver.gate,
        "expires_at": waiver.expires_at,
        "project_id": project.id,
    }


@router.post("/waivers/{waiver_id}:revoke")
def revoke_waiver(waiver_id: str, principal: CurrentPrincipal, session: DbSession):
    waiver = get_scoped(session, PolicyWaiver, principal.tenant_id, waiver_id)
    authorize(
        principal,
        "waiver:revoke",
        Resource(type="waiver", tenant_id=waiver.tenant_id, project_id=waiver.project_id),
    )
    waiver.revoked_at = utcnow()
    session.flush()
    audit.record(
        session,
        tenant_id=principal.tenant_id,
        action="waiver.revoked",
        actor_id=principal.user_id,
        resource_type="waiver",
        resource_id=waiver.id,
    )
    return {"id": waiver.id, "revoked_at": waiver.revoked_at}


@router.get("/audit")
def search_audit(
    principal: CurrentPrincipal,
    session: DbSession,
    actor_id: str | None = None,
    action: str | None = None,
    resource_id: str | None = None,
    correlation_id: str | None = None,
    limit: int = 100,
):
    """Audit search by actor/action/resource/correlation id (FR-023)."""
    authorize(principal, "audit:read", Resource(type="audit", tenant_id=principal.tenant_id))
    stmt = scoped(AuditEvent, principal.tenant_id).order_by(AuditEvent.seq.desc())
    if actor_id:
        stmt = stmt.where(AuditEvent.actor_id == actor_id)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if resource_id:
        stmt = stmt.where(AuditEvent.resource_id == resource_id)
    if correlation_id:
        stmt = stmt.where(AuditEvent.correlation_id == correlation_id)

    rows = list(session.execute(stmt.limit(min(limit, 500))).scalars().all())
    return {
        "items": [
            {
                "id": e.id,
                "seq": e.seq,
                "occurred_at": e.occurred_at,
                "actor_type": e.actor_type,
                "actor_id": e.actor_id,
                "action": e.action,
                "resource_type": e.resource_type,
                "resource_id": e.resource_id,
                "decision": e.decision,
                "correlation_id": e.correlation_id,
                "payload": e.payload,
                "hash": e.hash,
            }
            for e in rows
        ]
    }


@router.get("/audit:verify")
def verify_audit(principal: CurrentPrincipal, session: DbSession):
    """Recompute the audit hash chain and report the first broken link."""
    authorize(principal, "audit:read", Resource(type="audit", tenant_id=principal.tenant_id))
    ok, broken_id = audit.verify_chain(session, principal.tenant_id)
    return {
        "intact": ok,
        "broken_event_id": broken_id,
        "events": audit.count(session, principal.tenant_id),
    }


# `select` is used by the router indirectly through the scoped() helper.
_ = select
