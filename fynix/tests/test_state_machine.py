"""Project state machine and its exit gates (spec §3, AC-02)."""

from __future__ import annotations

import pytest

from app.core.errors import StateTransitionError
from app.core.hashing import checksum
from app.models.base import ApprovalStatus, ProjectState
from app.models.project import BlueprintVersion, BriefVersion
from app.orchestrator import state_machine as fsm
from app.services import approvals as approvals_service


def _add_brief(session, project, *, completeness: int, version: int = 1) -> BriefVersion:
    payload = {"normalized": {"goal": "bot"}, "v": version}
    brief = BriefVersion(
        tenant_id=project.tenant_id,
        project_id=project.id,
        version=version,
        payload=payload,
        completeness=completeness,
        checksum=checksum(payload),
    )
    session.add(brief)
    session.flush()
    return brief


def _add_blueprint(session, project, brief, version: int = 1) -> BlueprintVersion:
    body = {"backlog": [{"key": "T1"}], "v": version}
    blueprint = BlueprintVersion(
        tenant_id=project.tenant_id,
        project_id=project.id,
        brief_version_id=brief.id,
        version=version,
        summary="demo",
        backlog=body["backlog"],
        checksum=checksum(body),
    )
    session.add(blueprint)
    session.flush()
    return blueprint


def test_s0_triage_requires_owner_and_golden_path(session, project):
    project.golden_path = ""
    session.flush()
    verdict = fsm.evaluate_gate(session, project)
    assert not verdict.passed
    assert verdict.reason == "no_golden_path_selected"

    project.golden_path = "telegram_bot"
    session.flush()
    assert fsm.evaluate_gate(session, project).passed


def test_s1_blocks_below_the_completeness_threshold(session, project):
    fsm.transition(session, project=project, target=ProjectState.S1_DISCOVERY)
    _add_brief(session, project, completeness=55)

    with pytest.raises(StateTransitionError) as exc:
        fsm.transition(session, project=project, target=ProjectState.S2_SOLUTION)
    assert exc.value.details["reason"] == "completeness_below_threshold"
    assert project.state == ProjectState.S1_DISCOVERY.value


def test_s1_passes_at_the_threshold(session, project):
    fsm.transition(session, project=project, target=ProjectState.S1_DISCOVERY)
    _add_brief(session, project, completeness=fsm.MIN_COMPLETENESS)
    fsm.transition(session, project=project, target=ProjectState.S2_SOLUTION)
    assert project.state == ProjectState.S2_SOLUTION.value


def test_transition_is_idempotent(session, project):
    fsm.transition(session, project=project, target=ProjectState.S1_DISCOVERY)
    verdict = fsm.transition(session, project=project, target=ProjectState.S1_DISCOVERY)
    assert verdict.passed
    assert verdict.reason == "already_in_state"


def test_illegal_transition_is_refused(session, project):
    with pytest.raises(StateTransitionError) as exc:
        fsm.transition(session, project=project, target=ProjectState.S7_DELIVER)
    assert exc.value.details["reason"] == "transition_not_allowed"


def test_blueprint_gate_requires_every_approval(session, project, admin_principal):
    fsm.transition(session, project=project, target=ProjectState.S1_DISCOVERY)
    brief = _add_brief(session, project, completeness=90)
    fsm.transition(session, project=project, target=ProjectState.S2_SOLUTION)
    blueprint = _add_blueprint(session, project, brief)

    created = approvals_service.request_approvals(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        subject_type="blueprint",
        subject_id=blueprint.id,
        subject_checksum=blueprint.checksum,
    )
    assert len(created) == 2

    with pytest.raises(StateTransitionError):
        fsm.transition(session, project=project, target=ProjectState.S3_CONTRACT)

    for approval in created:
        approval.status = ApprovalStatus.APPROVED.value
        approval.decided_by = f"user-{approval.required_role}"
    session.flush()

    verdict = fsm.evaluate_gate(session, project)
    assert verdict.passed, verdict.reason


def test_new_brief_marks_blueprint_and_approvals_stale(session, project):
    """AC-02: changing the brief invalidates the approval it was based on."""
    fsm.transition(session, project=project, target=ProjectState.S1_DISCOVERY)
    brief = _add_brief(session, project, completeness=90)
    blueprint = _add_blueprint(session, project, brief)

    approvals = approvals_service.request_approvals(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        subject_type="blueprint",
        subject_id=blueprint.id,
        subject_checksum=blueprint.checksum,
    )
    for approval in approvals:
        approval.status = ApprovalStatus.APPROVED.value
    session.flush()

    _add_brief(session, project, completeness=95, version=2)
    result = fsm.invalidate_downstream(session, project=project, reason="brief v2")

    assert result["blueprints_marked_stale"] == 1
    assert result["approvals_marked_stale"] == 2
    session.refresh(blueprint)
    assert blueprint.is_stale is True

    satisfied, reason = approvals_service.is_satisfied(
        session,
        tenant_id=project.tenant_id,
        subject_type="blueprint",
        subject_id=blueprint.id,
    )
    assert not satisfied
    assert reason == "no_approvals_requested"  # all signatures were voided


def test_rework_transition_skips_the_exit_gate(session, project):
    """Going back exists precisely because the gate did not pass."""
    fsm.transition(session, project=project, target=ProjectState.S1_DISCOVERY)
    _add_brief(session, project, completeness=90)
    fsm.transition(session, project=project, target=ProjectState.S2_SOLUTION)

    # No blueprint approval, yet a move back to discovery must still work.
    fsm.transition(session, project=project, target=ProjectState.S1_DISCOVERY)
    assert project.state == ProjectState.S1_DISCOVERY.value


def test_force_requires_explicit_intent(session, project):
    fsm.transition(session, project=project, target=ProjectState.S1_DISCOVERY)
    _add_brief(session, project, completeness=10)

    with pytest.raises(StateTransitionError):
        fsm.transition(session, project=project, target=ProjectState.S2_SOLUTION)

    verdict = fsm.transition(
        session, project=project, target=ProjectState.S2_SOLUTION, force=True
    )
    assert not verdict.passed
    assert project.state == ProjectState.S2_SOLUTION.value


def test_state_change_is_audited_and_emits_an_event(session, project):
    from sqlalchemy import select

    from app.models.governance import AuditEvent
    from app.models.messaging import OutboxEvent

    fsm.transition(session, project=project, target=ProjectState.S1_DISCOVERY, actor_id="u1")

    audit_row = session.execute(
        select(AuditEvent).where(AuditEvent.action == "project.state_changed")
    ).scalar_one()
    assert audit_row.payload["to"] == ProjectState.S1_DISCOVERY.value

    event = session.execute(
        select(OutboxEvent).where(OutboxEvent.type == "project.state.changed")
    ).scalar_one()
    assert event.payload["from"] == ProjectState.S0_LEAD.value
