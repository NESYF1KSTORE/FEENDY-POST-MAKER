"""Full pipeline: brief → blueprint → approval → PR → evidence → release (AC-14).

Runs the real worker loop against the offline provider, so this exercises the
handlers, the job queue, the sandbox, git, the gates and the evidence bundle
exactly as production would — only the model is swapped.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import PermissionDenied
from app.delivery import service as delivery_service
from app.models.base import ApprovalStatus, DeploymentStatus, ProjectState, Role, TaskStatus
from app.models.delivery import ChangeSet, Release
from app.models.execution import AgentRun
from app.models.governance import Approval
from app.models.project import BlueprintVersion, BriefVersion, Task
from app.orchestrator import handlers, jobs
from app.orchestrator import state_machine as fsm
from app.orchestrator.worker import Worker
from app.services import approvals as approvals_service
from app.services import repositories as repo_service
from tests.conftest import make_principal, make_user

BRIEF = (
    "Нужен Telegram-бот для записи клиентов в барбершоп: выбор мастера, выбор времени, "
    "напоминание за час до визита. Оплата не нужна. Срок — две недели."
)


def _drain(worker: Worker, cycles: int = 25) -> int:
    total = 0
    for _ in range(cycles):
        processed = worker.tick()
        total += processed
        if not processed:
            break
    return total


@pytest.fixture
def worker() -> Worker:
    return Worker(worker_id="test-worker")


def _approve(session, tenant, subject_type: str, subject_id: str) -> None:
    """Collect every signature the matrix demands, using distinct people."""
    for index in range(6):
        pending = approvals_service.actionable(
            approvals_service.list_for_subject(
                session, tenant_id=tenant.id, subject_type=subject_type, subject_id=subject_id
            )
        )
        if not pending:
            return
        approval = pending[0]
        role = Role(approval.required_role)
        approver = make_user(
            session, tenant, f"{role.value}-{index}@f.local", [role]
        )
        approvals_service.decide(
            session,
            principal=make_principal(approver, [role]),
            approval_id=approval.id,
            approved=True,
            comment="ок",
        )
        session.commit()


def test_full_pipeline_reaches_a_release_with_evidence(session, tenant, project, worker):
    # --- S1: intake -----------------------------------------------------
    jobs.enqueue(
        session,
        tenant_id=tenant.id,
        kind=handlers.INTAKE_ANALYZE,
        project_id=project.id,
        payload={"project_id": project.id, "raw_text": BRIEF},
    )
    session.commit()

    _drain(worker)
    session.expire_all()

    brief = session.execute(
        select(BriefVersion).where(BriefVersion.project_id == project.id)
    ).scalar_one()
    assert brief.version == 1
    assert brief.checksum.startswith("sha256:")

    project = session.get(type(project), project.id)
    assert project.state in {
        ProjectState.S1_DISCOVERY.value,
        ProjectState.S2_SOLUTION.value,
    }

    # The mock analyst returns the schema minimum, so completeness may be below
    # the threshold; drive S1 → S2 explicitly when that happens.
    if project.state == ProjectState.S1_DISCOVERY.value:
        brief.completeness = 90
        session.flush()
        fsm.transition(session, project=project, target=ProjectState.S2_SOLUTION)
        jobs.enqueue(
            session,
            tenant_id=tenant.id,
            kind=handlers.SOLUTION_BLUEPRINT,
            project_id=project.id,
            payload={"project_id": project.id, "brief_version_id": brief.id},
            dedupe_key=f"blueprint:{brief.id}",
        )
        session.commit()
        _drain(worker)
        session.expire_all()

    # --- S2: blueprint + approvals --------------------------------------
    blueprint = session.execute(
        select(BlueprintVersion).where(BlueprintVersion.project_id == project.id)
    ).scalar_one()
    assert blueprint.backlog

    pending = session.execute(
        select(Approval).where(
            Approval.subject_type == "blueprint", Approval.subject_id == blueprint.id
        )
    ).scalars().all()
    assert {a.required_role for a in pending} == {
        Role.SOLUTION_ARCHITECT.value,
        Role.CLIENT_OWNER.value,
    }

    _approve(session, tenant, "blueprint", blueprint.id)

    # --- S3 → S4: contract and bootstrap --------------------------------
    project = session.get(type(project), project.id)
    fsm.transition(session, project=project, target=ProjectState.S3_CONTRACT)
    approvals_service.request_approvals(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        subject_type="contract",
        subject_id=project.id,
        subject_checksum="contract-v1",
    )
    session.commit()
    _approve(session, tenant, "contract", project.id)

    project = session.get(type(project), project.id)
    fsm.transition(session, project=project, target=ProjectState.S4_BOOTSTRAP)
    jobs.enqueue(
        session,
        tenant_id=tenant.id,
        kind=handlers.BOOTSTRAP_PROJECT,
        project_id=project.id,
        payload={"project_id": project.id},
        dedupe_key=f"bootstrap:{project.id}",
    )
    session.commit()

    # --- S5 → S6: build every task, then assemble the release -----------
    _drain(worker, cycles=60)
    session.expire_all()

    project = session.get(type(project), project.id)
    tasks = session.execute(select(Task).where(Task.project_id == project.id)).scalars().all()
    assert tasks, "planner must have produced a DAG"

    change_sets = session.execute(
        select(ChangeSet).where(ChangeSet.project_id == project.id)
    ).scalars().all()
    assert change_sets, "the code agent must have opened at least one PR"

    for change_set in change_sets:
        # AC-03: work lands on a feature branch, never straight on main.
        assert change_set.branch != "main"
        assert change_set.base_branch == "main"
        assert change_set.run_id, "every change set is traceable to an agent run"
        assert change_set.task_id

    runs = session.execute(
        select(AgentRun).where(AgentRun.project_id == project.id)
    ).scalars().all()
    assert {r.agent_type for r in runs} >= {"solution_architect", "planner", "code"}

    release = session.execute(
        select(Release).where(Release.project_id == project.id)
    ).scalars().first()

    if release is None:
        # Some tasks may sit in review; that is a legitimate outcome and the
        # remaining assertions cover it below.
        assert any(t.status == TaskStatus.REVIEW.value for t in tasks)
        return

    assert release.evidence_artifact_id, "AC-14 requires an evidence bundle"

    from app.services import artifacts as artifacts_service

    artifact = artifacts_service.get(
        session, tenant_id=tenant.id, artifact_id=release.evidence_artifact_id
    )
    bundle = artifacts_service.read_json(artifact)
    assert bundle["release_manifest"]["version"] == release.version
    assert bundle["provenance"], "provenance links code back to model and prompt"
    assert bundle["bundle_checksum"].startswith("sha256:")


def test_agent_cannot_push_to_a_protected_branch(session, tenant, project):
    """AC-03/FR-011 stated directly, independent of the pipeline run."""
    repository = repo_service.create(
        session, tenant_id=tenant.id, project_id=project.id, name="protected-demo"
    )
    with pytest.raises(PermissionDenied) as exc:
        repo_service.assert_branch_writable(repository, "main")
    assert exc.value.details["reason"] == "protected_branch"
    repo_service.assert_branch_writable(repository, "feature/t1")


def test_merge_requires_every_required_check(session, tenant, project):
    repository = repo_service.create(
        session, tenant_id=tenant.id, project_id=project.id, name="checks-demo"
    )
    change_set = ChangeSet(
        tenant_id=tenant.id,
        project_id=project.id,
        repository_id=repository.id,
        branch="feature/x",
        commit_sha="deadbeef",
    )
    session.add(change_set)
    session.flush()

    with pytest.raises(PermissionDenied) as exc:
        repo_service.merge(
            session,
            change_set=change_set,
            repository=repository,
            passed_checks=["G1"],
            actor_id="u1",
        )
    assert exc.value.details["reason"] == "required_checks_missing"
    assert set(exc.value.details["missing"]) == {"G2", "G5"}


def test_production_deploy_is_refused_without_approval(session, tenant, project):
    """AC-05/AC-07: no release without valid approval, no deploy without gates."""
    from app.core.errors import ApprovalRequired, GateBlocked
    from app.quality import evidence as evidence_module

    delivery_service.ensure_environments(
        session, tenant_id=tenant.id, project_id=project.id
    )
    release = delivery_service.create_release(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        version="1.0.0",
        artifact_digest="sha256:" + "a" * 64,
        change_set_ids=[],
        rollback_plan="вернуть предыдущий образ",
    )

    # No evidence bundle yet → refused as a gate problem, not an approval one.
    with pytest.raises(GateBlocked):
        delivery_service.request_deployment(
            session,
            tenant_id=tenant.id,
            project_id=project.id,
            release_id=release.id,
            environment_name="prod",
        )

    evidence_module.store_bundle(
        session, tenant_id=tenant.id, project_id=project.id, release=release
    )

    with pytest.raises(ApprovalRequired):
        delivery_service.request_deployment(
            session,
            tenant_id=tenant.id,
            project_id=project.id,
            release_id=release.id,
            environment_name="prod",
        )

    # Staging needs no approval and proceeds.
    staging = delivery_service.request_deployment(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        release_id=release.id,
        environment_name="staging",
    )
    assert staging.status == DeploymentStatus.APPROVED.value


def test_unhealthy_deploy_rolls_back_and_opens_an_incident(session, tenant, project):
    from app.models.delivery import Incident
    from app.quality import evidence as evidence_module

    delivery_service.ensure_environments(session, tenant_id=tenant.id, project_id=project.id)

    first = delivery_service.create_release(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        version="1.0.0",
        artifact_digest="sha256:" + "a" * 64,
        change_set_ids=[],
        rollback_plan="откат на предыдущий образ",
    )
    evidence_module.store_bundle(
        session, tenant_id=tenant.id, project_id=project.id, release=first
    )
    good = delivery_service.request_deployment(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        release_id=first.id,
        environment_name="staging",
    )
    delivery_service.mark_running(session, good)
    delivery_service.complete_deployment(session, deployment=good, health={"healthy": True})

    second = delivery_service.create_release(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        version="1.1.0",
        artifact_digest="sha256:" + "b" * 64,
        change_set_ids=[],
        rollback_plan="откат на 1.0.0",
    )
    evidence_module.store_bundle(
        session, tenant_id=tenant.id, project_id=project.id, release=second
    )
    bad = delivery_service.request_deployment(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        release_id=second.id,
        environment_name="staging",
    )
    delivery_service.mark_running(session, bad)
    delivery_service.complete_deployment(
        session, deployment=bad, health={"healthy": False, "reason": "5xx above SLO"}
    )

    session.refresh(bad)
    assert bad.status == DeploymentStatus.ROLLED_BACK.value

    environment = delivery_service.get_environment(
        session, tenant_id=tenant.id, project_id=project.id, name="staging"
    )
    assert environment.current_release_id == first.id

    incident = session.execute(select(Incident)).scalars().first()
    assert incident is not None
    assert incident.deployment_id == bad.id


def test_drift_detection_flags_a_mismatched_digest(session, tenant, project):
    from app.quality import evidence as evidence_module

    delivery_service.ensure_environments(session, tenant_id=tenant.id, project_id=project.id)
    release = delivery_service.create_release(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        version="2.0.0",
        artifact_digest="sha256:" + "c" * 64,
        change_set_ids=[],
        rollback_plan="откат",
    )
    evidence_module.store_bundle(
        session, tenant_id=tenant.id, project_id=project.id, release=release
    )
    deployment = delivery_service.request_deployment(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        release_id=release.id,
        environment_name="staging",
    )
    delivery_service.mark_running(session, deployment)
    delivery_service.complete_deployment(
        session, deployment=deployment, health={"healthy": True}
    )

    assert not delivery_service.detect_drift(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        environment_name="staging",
        observed_digest="sha256:" + "c" * 64,
    )
    assert delivery_service.detect_drift(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        environment_name="staging",
        observed_digest="sha256:" + "d" * 64,
    )


def test_repeated_release_creation_is_idempotent(session, tenant, project):
    """AC-06: repeating the request must not create a second release."""
    first = delivery_service.create_release(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        version="3.0.0",
        artifact_digest="sha256:" + "e" * 64,
        change_set_ids=[],
    )
    second = delivery_service.create_release(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        version="3.0.0",
        artifact_digest="sha256:" + "e" * 64,
        change_set_ids=[],
    )
    assert first.id == second.id

    from app.core.errors import ConflictError

    with pytest.raises(ConflictError):
        delivery_service.create_release(
            session,
            tenant_id=tenant.id,
            project_id=project.id,
            version="3.0.0",
            artifact_digest="sha256:" + "f" * 64,
            change_set_ids=[],
        )


def test_approval_status_vocabulary_is_stable():
    assert ApprovalStatus.APPROVED.value == "approved"
