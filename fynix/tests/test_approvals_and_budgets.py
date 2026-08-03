"""Approval sequencing, dual control and budget thresholds (AC-05, AC-07, AC-10)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.errors import BudgetExceeded, ConflictError, PermissionDenied
from app.models.base import ApprovalStatus, Role, utcnow
from app.services import approvals as approvals_service
from app.services import budgets as budgets_service
from tests.conftest import make_principal, make_user


def _request(session, project, subject_type="blueprint", subject_id="bpv_1", checksum="c1"):
    return approvals_service.request_approvals(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_checksum=checksum,
    )


# --- approvals -----------------------------------------------------------


def test_sequential_approvals_are_offered_in_order(session, tenant, project):
    created = _request(session, project)
    actionable = approvals_service.actionable(created)
    assert len(actionable) == 1
    assert actionable[0].required_role == Role.SOLUTION_ARCHITECT.value

    architect = make_user(session, tenant, "arch@f.local", [Role.SOLUTION_ARCHITECT])
    approvals_service.decide(
        session,
        principal=make_principal(architect, [Role.SOLUTION_ARCHITECT]),
        approval_id=actionable[0].id,
        approved=True,
    )

    remaining = approvals_service.actionable(
        approvals_service.list_for_subject(
            session, tenant_id=tenant.id, subject_type="blueprint", subject_id="bpv_1"
        )
    )
    assert [a.required_role for a in remaining] == [Role.CLIENT_OWNER.value]


def test_out_of_sequence_decision_is_refused(session, tenant, project):
    created = _request(session, project)
    owner_approval = next(a for a in created if a.required_role == Role.CLIENT_OWNER.value)
    owner = make_user(session, tenant, "owner@f.local", [Role.CLIENT_OWNER])

    with pytest.raises(ConflictError) as exc:
        approvals_service.decide(
            session,
            principal=make_principal(owner, [Role.CLIENT_OWNER]),
            approval_id=owner_approval.id,
            approved=True,
        )
    assert exc.value.details["reason"] == "sequence_not_reached"


def test_wrong_role_cannot_decide(session, tenant, project):
    created = _request(session, project)
    qa = make_user(session, tenant, "qa@f.local", [Role.QA_ENGINEER])
    with pytest.raises(PermissionDenied) as exc:
        approvals_service.decide(
            session,
            principal=make_principal(qa, [Role.QA_ENGINEER]),
            approval_id=created[0].id,
            approved=True,
        )
    assert exc.value.details["reason"] == "wrong_approver_role"


def test_dual_control_needs_two_distinct_people(session, tenant, project):
    """AC-07: one person holding both roles cannot sign a prod deploy alone."""
    created = approvals_service.request_approvals(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        subject_type="deploy_prod",
        subject_id="rel_1",
        subject_checksum="d1",
    )
    assert len(created) == 2

    omni = make_user(session, tenant, "omni@f.local", [Role.CLIENT_OWNER, Role.DEVOPS_SRE])
    principal = make_principal(omni, [Role.CLIENT_OWNER, Role.DEVOPS_SRE])

    approvals_service.decide(
        session, principal=principal, approval_id=created[0].id, approved=True
    )
    with pytest.raises(ConflictError) as exc:
        approvals_service.decide(
            session, principal=principal, approval_id=created[1].id, approved=True
        )
    assert exc.value.details["reason"] == "dual_control_violation"


def test_expired_approval_does_not_satisfy_the_gate(session, tenant, project):
    created = _request(session, project)
    for approval in created:
        approval.status = ApprovalStatus.APPROVED.value
        approval.decided_by = f"u-{approval.required_role}"
        approval.expires_at = utcnow() - timedelta(minutes=1)
    session.flush()

    satisfied, reason = approvals_service.is_satisfied(
        session, tenant_id=tenant.id, subject_type="blueprint", subject_id="bpv_1"
    )
    assert not satisfied
    assert reason == "expired"


def test_checksum_mismatch_marks_the_approval_stale(session, tenant, project):
    created = _request(session, project, checksum="original")
    for approval in created:
        approval.status = ApprovalStatus.APPROVED.value
        approval.decided_by = f"u-{approval.required_role}"
    session.flush()

    satisfied, reason = approvals_service.is_satisfied(
        session,
        tenant_id=tenant.id,
        subject_type="blueprint",
        subject_id="bpv_1",
        expected_checksum="changed",
    )
    assert not satisfied
    assert reason == "stale_subject"


def test_rerequesting_supersedes_outstanding_approvals(session, tenant, project):
    first = _request(session, project)
    second = _request(session, project, checksum="c2")

    session.refresh(first[0])
    assert first[0].status == ApprovalStatus.STALE.value
    assert all(a.status == ApprovalStatus.PENDING.value for a in second)


def test_a_rejection_blocks_the_subject(session, tenant, project):
    created = _request(session, project)
    architect = make_user(session, tenant, "arch2@f.local", [Role.SOLUTION_ARCHITECT])
    approvals_service.decide(
        session,
        principal=make_principal(architect, [Role.SOLUTION_ARCHITECT]),
        approval_id=created[0].id,
        approved=False,
        comment="архитектура не покрывает нагрузку",
    )
    satisfied, reason = approvals_service.is_satisfied(
        session, tenant_id=tenant.id, subject_type="blueprint", subject_id="bpv_1"
    )
    assert not satisfied
    assert reason == "rejected"


# --- budgets -------------------------------------------------------------


def test_thresholds_fire_once_each(session, tenant, project):
    for _ in range(5):
        budgets_service.record_cost(
            session,
            tenant_id=tenant.id,
            project_id=project.id,
            provider="anthropic",
            sku="claude",
            quantity=1000,
            unit="token",
            effective_rate=0,
            amount=10_000.0,
        )

    from sqlalchemy import select

    from app.models.messaging import OutboxEvent

    events = list(
        session.execute(
            select(OutboxEvent).where(OutboxEvent.type == "budget.threshold.reached")
        )
        .scalars()
        .all()
    )
    thresholds = sorted(e.payload["threshold_pct"] for e in events)
    assert thresholds == [50, 80, 100]


def test_hard_stop_blocks_new_billable_runs(session, tenant, project):
    budgets_service.record_cost(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        provider="anthropic",
        sku="claude",
        quantity=1,
        unit="token",
        effective_rate=0,
        amount=50_000.0,
    )
    state = budgets_service.state(session, tenant_id=tenant.id, project_id=project.id)
    assert state.hard_stopped

    with pytest.raises(BudgetExceeded) as exc:
        budgets_service.assert_can_spend(
            session, tenant_id=tenant.id, project_id=project.id
        )
    assert exc.value.details["reason"] == "budget_hard_stop"


def test_warning_threshold_only_suspends_optional_work(session, tenant, project):
    budgets_service.record_cost(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        provider="deepseek",
        sku="chat",
        quantity=1,
        unit="token",
        effective_rate=0,
        amount=42_000.0,  # 84%
    )
    # Mandatory work still proceeds…
    budgets_service.assert_can_spend(session, tenant_id=tenant.id, project_id=project.id)
    # …optional work does not.
    with pytest.raises(BudgetExceeded) as exc:
        budgets_service.assert_can_spend(
            session, tenant_id=tenant.id, project_id=project.id, optional=True
        )
    assert exc.value.details["reason"] == "budget_warning"


def test_forecast_refuses_a_run_that_would_overshoot(session, tenant, project):
    budgets_service.record_cost(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        provider="deepseek",
        sku="chat",
        quantity=1,
        unit="token",
        effective_rate=0,
        amount=45_000.0,
    )
    with pytest.raises(BudgetExceeded) as exc:
        budgets_service.assert_can_spend(
            session, tenant_id=tenant.id, project_id=project.id, estimated_amount=10_000.0
        )
    assert exc.value.details["reason"] == "budget_forecast_exceeded"


def test_override_is_time_bound_and_audited(session, tenant, project, admin, admin_principal):
    budgets_service.record_cost(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        provider="anthropic",
        sku="claude",
        quantity=1,
        unit="token",
        effective_rate=0,
        amount=50_000.0,
    )
    budgets_service.grant_override(
        session,
        principal=admin_principal,
        project_id=project.id,
        amount=20_000.0,
        hours=4,
        reason="pilot demo must ship today",
    )
    state = budgets_service.state(session, tenant_id=tenant.id, project_id=project.id)
    assert state.limit == 70_000.0
    assert not state.hard_stopped
    budgets_service.assert_can_spend(session, tenant_id=tenant.id, project_id=project.id)

    from sqlalchemy import select

    from app.models.governance import AuditEvent

    row = session.execute(
        select(AuditEvent).where(AuditEvent.action == "budget.override_granted")
    ).scalar_one()
    assert row.payload["amount"] == 20_000.0


def test_expired_override_reinstates_the_hard_stop(session, tenant, project, admin_principal):
    budgets_service.record_cost(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        provider="anthropic",
        sku="claude",
        quantity=1,
        unit="token",
        effective_rate=0,
        amount=50_000.0,
    )
    budget = budgets_service.grant_override(
        session,
        principal=admin_principal,
        project_id=project.id,
        amount=20_000.0,
        hours=1,
        reason="temporary extension for the pilot",
    )
    budget.override_until = utcnow() - timedelta(minutes=1)
    session.flush()

    state = budgets_service.state(session, tenant_id=tenant.id, project_id=project.id)
    assert state.limit == 50_000.0
    assert state.hard_stopped


def test_non_billable_cost_does_not_consume_the_budget(session, tenant, project):
    budgets_service.record_cost(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        provider="human",
        sku="review-hours",
        quantity=3,
        unit="hour",
        effective_rate=0,
        amount=9_000.0,
        billable=False,
    )
    state = budgets_service.state(session, tenant_id=tenant.id, project_id=project.id)
    assert state.spent == 0.0
    summary = budgets_service.summary(session, tenant_id=tenant.id, project_id=project.id)
    assert any(row["provider"] == "human" for row in summary["by_provider"])
