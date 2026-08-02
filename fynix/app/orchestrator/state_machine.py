"""Project state machine S0..S8 (spec §3).

Invariants enforced here (spec §3.1):
  * a transition is atomic — guard, state write, audit entry and event all
    happen in one transaction;
  * a transition is idempotent — re-running it when already in the target state
    is a no-op that returns the same result instead of duplicating side effects;
  * every gate returns a machine-readable verdict with evidence, an author and
    a timestamp;
  * changing the brief marks downstream blueprints and approvals stale.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import StateTransitionError
from app.models.base import ApprovalStatus, GateStatus, ProjectState, TaskStatus, utcnow
from app.models.delivery import (
    Deployment,
    Environment,
    Incident,
    QualityGateResult,
    Release,
    Repository,
)
from app.models.governance import Approval
from app.models.project import BlueprintVersion, BriefVersion, Project, Task
from app.orchestrator import events
from app.services import approvals as approvals_service

MIN_COMPLETENESS = 80


@dataclass(frozen=True)
class GateVerdict:
    """Machine result of an exit gate, with the evidence that produced it."""

    passed: bool
    reason: str
    evidence: dict = field(default_factory=dict)


GateFn = Callable[[Session, Project, datetime], GateVerdict]


# --------------------------------------------------------------------------
# Exit gates — one per state, mirroring the "Exit gate" column of spec §3
# --------------------------------------------------------------------------


def _gate_s0_triage(session: Session, project: Project, now: datetime) -> GateVerdict:
    """PM triage: the lead must have an owner and a chosen golden path."""
    if not project.owner_user_id:
        return GateVerdict(False, "no_owner_assigned")
    if not project.golden_path:
        return GateVerdict(False, "no_golden_path_selected")
    _ = session, now
    return GateVerdict(True, "triaged", {"golden_path": project.golden_path})


def _latest_brief(session: Session, project: Project) -> BriefVersion | None:
    return session.execute(
        select(BriefVersion)
        .where(BriefVersion.project_id == project.id)
        .order_by(BriefVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def _latest_blueprint(session: Session, project: Project) -> BlueprintVersion | None:
    return session.execute(
        select(BlueprintVersion)
        .where(BlueprintVersion.project_id == project.id)
        .order_by(BlueprintVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def _gate_s1_completeness(session: Session, project: Project, now: datetime) -> GateVerdict:
    brief = _latest_brief(session, project)
    if brief is None:
        return GateVerdict(False, "no_brief")
    if brief.completeness < MIN_COMPLETENESS:
        return GateVerdict(
            False,
            "completeness_below_threshold",
            {
                "completeness": brief.completeness,
                "required": MIN_COMPLETENESS,
                "open_questions": brief.open_questions,
            },
        )
    _ = now
    return GateVerdict(
        True,
        "brief_complete",
        {"brief_version_id": brief.id, "completeness": brief.completeness},
    )


def _gate_s2_architect_approval(session: Session, project: Project, now: datetime) -> GateVerdict:
    blueprint = _latest_blueprint(session, project)
    if blueprint is None:
        return GateVerdict(False, "no_blueprint")
    if blueprint.is_stale:
        return GateVerdict(False, "blueprint_stale", {"blueprint_id": blueprint.id})
    satisfied, reason = approvals_service.is_satisfied(
        session,
        tenant_id=project.tenant_id,
        subject_type="blueprint",
        subject_id=blueprint.id,
        expected_checksum=blueprint.checksum,
        now=now,
    )
    return GateVerdict(
        satisfied,
        "blueprint_approved" if satisfied else f"approval_{reason}",
        {"blueprint_id": blueprint.id},
    )


def _gate_s3_contract(session: Session, project: Project, now: datetime) -> GateVerdict:
    satisfied, reason = approvals_service.is_satisfied(
        session,
        tenant_id=project.tenant_id,
        subject_type="contract",
        subject_id=project.id,
        now=now,
    )
    if not satisfied:
        return GateVerdict(False, f"contract_{reason}")
    if project.budget_rub <= 0:
        return GateVerdict(False, "no_budget_set")
    return GateVerdict(True, "contract_signed", {"budget_rub": project.budget_rub})


def _gate_s4_bootstrap(session: Session, project: Project, now: datetime) -> GateVerdict:
    """Bootstrap checks: repository, protected branch policy and environments."""
    repo = session.execute(
        select(Repository).where(Repository.project_id == project.id)
    ).scalar_one_or_none()
    if repo is None:
        return GateVerdict(False, "no_repository")
    if not repo.protected_branches:
        return GateVerdict(False, "default_branch_not_protected")

    env_names = set(
        session.execute(
            select(Environment.name).where(Environment.project_id == project.id)
        ).scalars()
    )
    missing = {"dev", "staging", "prod"} - env_names
    if missing:
        return GateVerdict(False, "environments_missing", {"missing": sorted(missing)})
    _ = now
    return GateVerdict(
        True, "bootstrap_complete", {"repository": repo.full_name, "environments": sorted(env_names)}
    )


def _gate_s5_tasks_done(session: Session, project: Project, now: datetime) -> GateVerdict:
    tasks = list(
        session.execute(select(Task).where(Task.project_id == project.id)).scalars().all()
    )
    if not tasks:
        return GateVerdict(False, "no_tasks_planned")
    unfinished = [
        t
        for t in tasks
        if t.status not in {TaskStatus.DONE.value, TaskStatus.CANCELLED.value}
    ]
    if unfinished:
        return GateVerdict(
            False,
            "tasks_incomplete",
            {"remaining": len(unfinished), "sample": [t.key for t in unfinished[:10]]},
        )
    _ = now
    return GateVerdict(True, "all_tasks_done", {"tasks": len(tasks)})


def _gate_s6_verify(session: Session, project: Project, now: datetime) -> GateVerdict:
    """QA + security verdict on the newest release candidate (spec §11.1 G0..G7)."""
    release = session.execute(
        select(Release)
        .where(Release.project_id == project.id)
        .order_by(Release.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if release is None:
        return GateVerdict(False, "no_release_candidate")

    gate_rows = list(
        session.execute(
            select(QualityGateResult).where(
                QualityGateResult.project_id == project.id,
                QualityGateResult.subject_id == release.id,
            )
        )
        .scalars()
        .all()
    )
    if not gate_rows:
        return GateVerdict(False, "no_gate_results", {"release_id": release.id})

    blockers = [
        {"gate": g.gate, "status": g.status, "findings": len(g.findings or [])}
        for g in gate_rows
        if g.blocking and g.status == GateStatus.FAILED.value
    ]
    expired = [
        g.gate for g in gate_rows if g.expires_at is not None and g.expires_at <= now
    ]
    if blockers:
        return GateVerdict(False, "blocking_gate_failed", {"blockers": blockers})
    if expired:
        return GateVerdict(False, "gate_evidence_expired", {"gates": expired})
    if not release.evidence_artifact_id:
        return GateVerdict(False, "evidence_bundle_missing", {"release_id": release.id})
    return GateVerdict(
        True,
        "release_verified",
        {"release_id": release.id, "gates": [g.gate for g in gate_rows]},
    )


def _gate_s7_deploy_approved(session: Session, project: Project, now: datetime) -> GateVerdict:
    deployment = session.execute(
        select(Deployment)
        .where(Deployment.project_id == project.id)
        .order_by(Deployment.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if deployment is None:
        return GateVerdict(False, "no_deployment")
    if deployment.status != "healthy":
        return GateVerdict(
            False, "deployment_not_healthy", {"status": deployment.status}
        )
    _ = now
    return GateVerdict(True, "deployed", {"deployment_id": deployment.id})


def _gate_s8_close(session: Session, project: Project, now: datetime) -> GateVerdict:
    """A project only closes once its operational obligations are discharged."""
    open_incidents = list(
        session.execute(
            select(Incident).where(
                Incident.project_id == project.id, Incident.status != "resolved"
            )
        )
        .scalars()
        .all()
    )
    if open_incidents:
        return GateVerdict(
            False,
            "open_incidents",
            {"incidents": [{"id": i.id, "severity": i.severity} for i in open_incidents]},
        )
    # SEV-1/SEV-2 require a postmortem before the project can be closed (§13).
    missing_postmortem = list(
        session.execute(
            select(Incident).where(
                Incident.project_id == project.id,
                Incident.severity.in_(["sev1", "sev2"]),
                Incident.postmortem_url == "",
            )
        )
        .scalars()
        .all()
    )
    if missing_postmortem:
        return GateVerdict(
            False, "postmortem_missing", {"incidents": [i.id for i in missing_postmortem]}
        )
    _ = now
    return GateVerdict(True, "closed")


#: state -> (allowed target states, exit gate)
TRANSITIONS: dict[ProjectState, tuple[tuple[ProjectState, ...], GateFn | None]] = {
    ProjectState.S0_LEAD: ((ProjectState.S1_DISCOVERY, ProjectState.REJECTED), _gate_s0_triage),
    ProjectState.S1_DISCOVERY: (
        (ProjectState.S2_SOLUTION, ProjectState.CANCELLED),
        _gate_s1_completeness,
    ),
    ProjectState.S2_SOLUTION: (
        (ProjectState.S3_CONTRACT, ProjectState.S1_DISCOVERY, ProjectState.CANCELLED),
        _gate_s2_architect_approval,
    ),
    ProjectState.S3_CONTRACT: (
        (ProjectState.S4_BOOTSTRAP, ProjectState.CANCELLED),
        _gate_s3_contract,
    ),
    ProjectState.S4_BOOTSTRAP: ((ProjectState.S5_BUILD, ProjectState.CANCELLED), _gate_s4_bootstrap),
    ProjectState.S5_BUILD: (
        (ProjectState.S6_VERIFY, ProjectState.CANCELLED),
        _gate_s5_tasks_done,
    ),
    ProjectState.S6_VERIFY: (
        (ProjectState.S7_DELIVER, ProjectState.S5_BUILD),
        _gate_s6_verify,
    ),
    ProjectState.S7_DELIVER: (
        (ProjectState.S8_OPERATE, ProjectState.S5_BUILD),
        _gate_s7_deploy_approved,
    ),
    ProjectState.S8_OPERATE: (
        (ProjectState.CLOSED, ProjectState.S1_DISCOVERY, ProjectState.S5_BUILD),
        _gate_s8_close,
    ),
    ProjectState.CLOSED: ((), None),
    ProjectState.CANCELLED: ((), None),
    ProjectState.REJECTED: ((), None),
}

#: Backwards moves (rework, change request, cancellation) skip the exit gate:
#: they exist precisely because the gate did not pass.
_REWORK_TARGETS = {
    ProjectState.S1_DISCOVERY,
    ProjectState.S5_BUILD,
    ProjectState.CANCELLED,
    ProjectState.REJECTED,
}


def evaluate_gate(session: Session, project: Project, now: datetime | None = None) -> GateVerdict:
    """Run the exit gate for the project's current state without transitioning."""
    now = now or utcnow()
    _, gate = TRANSITIONS.get(project.state_enum, ((), None))
    if gate is None:
        return GateVerdict(False, "terminal_state")
    return gate(session, project, now)


def _is_forward(current: ProjectState, target: ProjectState) -> bool:
    order = [
        ProjectState.S0_LEAD,
        ProjectState.S1_DISCOVERY,
        ProjectState.S2_SOLUTION,
        ProjectState.S3_CONTRACT,
        ProjectState.S4_BOOTSTRAP,
        ProjectState.S5_BUILD,
        ProjectState.S6_VERIFY,
        ProjectState.S7_DELIVER,
        ProjectState.S8_OPERATE,
        ProjectState.CLOSED,
    ]
    if current not in order or target not in order:
        return False
    return order.index(target) > order.index(current)


def transition(
    session: Session,
    *,
    project: Project,
    target: ProjectState,
    actor_id: str = "",
    actor_type: str = "user",
    reason: str = "",
    force: bool = False,
    now: datetime | None = None,
) -> GateVerdict:
    """Move the project to `target`, enforcing the exit gate.

    Idempotent: transitioning to the state the project is already in returns a
    passing verdict without emitting a second event.
    """
    now = now or utcnow()
    current = project.state_enum

    if current == target:
        return GateVerdict(True, "already_in_state", {"state": target.value})

    allowed, gate = TRANSITIONS.get(current, ((), None))
    if target not in allowed:
        raise StateTransitionError(
            f"cannot move from {current.value} to {target.value}",
            details={
                "reason": "transition_not_allowed",
                "from": current.value,
                "to": target.value,
                "allowed": [s.value for s in allowed],
            },
        )

    needs_gate = _is_forward(current, target) and target not in _REWORK_TARGETS
    verdict = GateVerdict(True, "gate_skipped_rework")
    if needs_gate and gate is not None:
        verdict = gate(session, project, now)
        if not verdict.passed and not force:
            raise StateTransitionError(
                f"exit gate for {current.value} did not pass: {verdict.reason}",
                details={
                    "reason": verdict.reason,
                    "evidence": verdict.evidence,
                    "from": current.value,
                    "to": target.value,
                },
            )

    project.state = target.value
    if target in {ProjectState.CLOSED, ProjectState.CANCELLED, ProjectState.REJECTED}:
        project.closed_at = now
    session.flush()

    audit.record(
        session,
        tenant_id=project.tenant_id,
        action="project.state_changed",
        actor_id=actor_id,
        actor_type=actor_type,
        resource_type="project",
        resource_id=project.id,
        payload={
            "from": current.value,
            "to": target.value,
            "gate_reason": verdict.reason,
            "gate_evidence": verdict.evidence,
            "forced": force,
            "reason": reason,
        },
    )
    events.emit(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        event_type=events.PROJECT_STATE_CHANGED,
        payload={"from": current.value, "to": target.value, "gate_reason": verdict.reason},
    )
    return verdict


def invalidate_downstream(
    session: Session, *, project: Project, reason: str, actor_id: str = ""
) -> dict:
    """Spec §3.1: a scope change marks downstream estimates and approvals stale."""
    blueprints = list(
        session.execute(
            select(BlueprintVersion).where(
                BlueprintVersion.project_id == project.id,
                BlueprintVersion.is_stale.is_(False),
            )
        )
        .scalars()
        .all()
    )
    stale_approvals = 0
    for blueprint in blueprints:
        blueprint.is_stale = True
        stale_approvals += approvals_service.invalidate_for_subject(
            session,
            tenant_id=project.tenant_id,
            subject_type="blueprint",
            subject_id=blueprint.id,
            reason=reason,
        )

    contract_approvals = list(
        session.execute(
            select(Approval).where(
                Approval.project_id == project.id,
                Approval.subject_type == "contract",
                Approval.status.in_(
                    [ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value]
                ),
            )
        )
        .scalars()
        .all()
    )
    for approval in contract_approvals:
        approval.status = ApprovalStatus.STALE.value
        stale_approvals += 1

    session.flush()
    audit.record(
        session,
        tenant_id=project.tenant_id,
        action="project.downstream_invalidated",
        actor_id=actor_id,
        actor_type="system" if not actor_id else "user",
        resource_type="project",
        resource_id=project.id,
        payload={
            "reason": reason,
            "blueprints": len(blueprints),
            "approvals": stale_approvals,
        },
    )
    return {"blueprints_marked_stale": len(blueprints), "approvals_marked_stale": stale_approvals}
