"""Task DAG, durable jobs, quality gates and evidence (AC-08, AC-11)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.errors import ValidationError
from app.models.base import JobStatus, TaskStatus, utcnow
from app.orchestrator import dag, events, jobs
from app.quality import gates as gates_module
from app.quality import scanners

SPECS = [
    {"key": "T1", "title": "setup", "depends_on": [], "acceptance_criteria": ["repo создан"]},
    {"key": "T2", "title": "api", "depends_on": ["T1"], "acceptance_criteria": ["эндпоинт отвечает 200"]},
    {"key": "T3", "title": "tests", "depends_on": ["T2"], "acceptance_criteria": ["покрытие >= 60%"]},
    {"key": "T4", "title": "docs", "depends_on": ["T2"], "acceptance_criteria": ["README описывает запуск"]},
]


# --- DAG ------------------------------------------------------------------


def test_only_root_tasks_are_ready_initially(session, tenant, project):
    tasks = dag.build(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        blueprint_version_id=None,
        specs=SPECS,
    )
    by_key = {t.key: t for t in tasks}
    assert by_key["T1"].status == TaskStatus.READY.value
    assert by_key["T2"].status == TaskStatus.BLOCKED.value


def test_completing_a_task_unblocks_its_dependents(session, tenant, project):
    tasks = dag.build(
        session, tenant_id=tenant.id, project_id=project.id, blueprint_version_id=None, specs=SPECS
    )
    by_key = {t.key: t for t in tasks}

    dag.complete_task(session, task=by_key["T1"])
    session.refresh(by_key["T2"])
    assert by_key["T2"].status == TaskStatus.READY.value

    promoted = dag.complete_task(session, task=by_key["T2"])
    assert {t.key for t in promoted} == {"T3", "T4"}


def test_a_cyclic_plan_is_refused_before_anything_is_written(session, tenant, project):
    cyclic = [
        {"key": "A", "title": "a", "depends_on": ["B"], "acceptance_criteria": ["x"]},
        {"key": "B", "title": "b", "depends_on": ["A"], "acceptance_criteria": ["y"]},
    ]
    with pytest.raises(ValidationError) as exc:
        dag.build(
            session,
            tenant_id=tenant.id,
            project_id=project.id,
            blueprint_version_id=None,
            specs=cyclic,
        )
    assert set(exc.value.details["tasks"]) == {"A", "B"}

    from sqlalchemy import select

    from app.models.project import Task

    assert session.execute(select(Task)).scalars().first() is None


def test_unknown_dependency_is_refused(session, tenant, project):
    with pytest.raises(ValidationError) as exc:
        dag.build(
            session,
            tenant_id=tenant.id,
            project_id=project.id,
            blueprint_version_id=None,
            specs=[{"key": "A", "title": "a", "depends_on": ["Z"], "acceptance_criteria": ["x"]}],
        )
    assert exc.value.details["unknown"] == ["Z"]


def test_failed_task_does_not_release_its_dependents(session, tenant, project):
    tasks = dag.build(
        session, tenant_id=tenant.id, project_id=project.id, blueprint_version_id=None, specs=SPECS
    )
    by_key = {t.key: t for t in tasks}
    dag.fail_task(session, task=by_key["T1"], reason="agent produced no changes")
    dag.refresh_readiness(session, project_id=project.id, tenant_id=tenant.id)
    session.refresh(by_key["T2"])
    assert by_key["T2"].status == TaskStatus.BLOCKED.value


def test_cancelling_a_task_cancels_everything_downstream(session, tenant, project):
    tasks = dag.build(
        session, tenant_id=tenant.id, project_id=project.id, blueprint_version_id=None, specs=SPECS
    )
    by_key = {t.key: t for t in tasks}
    cancelled = dag.cancel_subtree(session, task=by_key["T2"], reason="scope removed")
    assert {t.key for t in cancelled} == {"T2", "T3", "T4"}
    session.refresh(by_key["T1"])
    assert by_key["T1"].status == TaskStatus.READY.value


def test_task_ready_events_are_emitted(session, tenant, project):
    from sqlalchemy import select

    from app.models.messaging import OutboxEvent

    dag.build(
        session, tenant_id=tenant.id, project_id=project.id, blueprint_version_id=None, specs=SPECS
    )
    ready_events = list(
        session.execute(select(OutboxEvent).where(OutboxEvent.type == events.TASK_READY))
        .scalars()
        .all()
    )
    assert len(ready_events) == 1
    assert ready_events[0].payload["key"] == "T1"


# --- jobs -----------------------------------------------------------------


def test_dedupe_key_prevents_duplicate_jobs(session, tenant, project):
    """AC-11: a retried submission must not create a second unit of work."""
    first = jobs.enqueue(
        session, tenant_id=tenant.id, kind="test.kind", project_id=project.id, dedupe_key="k1"
    )
    second = jobs.enqueue(
        session, tenant_id=tenant.id, kind="test.kind", project_id=project.id, dedupe_key="k1"
    )
    assert first.id == second.id


def test_claim_leases_and_increments_attempts(session, tenant):
    jobs.enqueue(session, tenant_id=tenant.id, kind="test.kind")
    claimed = jobs.claim(session, worker_id="w1")
    assert len(claimed) == 1
    assert claimed[0].status == JobStatus.LEASED.value
    assert claimed[0].attempts == 1
    # Already leased and not yet expired — a second worker gets nothing.
    assert jobs.claim(session, worker_id="w2") == []


def test_expired_lease_is_reclaimed(session, tenant):
    jobs.enqueue(session, tenant_id=tenant.id, kind="test.kind")
    first = jobs.claim(session, worker_id="w1")[0]
    first.lease_expires_at = utcnow() - timedelta(seconds=1)
    session.flush()

    reclaimed = jobs.claim(session, worker_id="w2")
    assert len(reclaimed) == 1
    assert reclaimed[0].id == first.id
    assert reclaimed[0].leased_by == "w2"


def test_retry_schedules_a_future_run_then_dies(session, tenant):
    job = jobs.enqueue(session, tenant_id=tenant.id, kind="test.kind", max_attempts=2)
    jobs.claim(session, worker_id="w1")
    jobs.fail(session, job, "boom")
    assert job.status == JobStatus.PENDING.value
    assert job.run_after > utcnow()

    job.run_after = utcnow()
    session.flush()
    jobs.claim(session, worker_id="w1")
    jobs.fail(session, job, "boom again")
    assert job.status == JobStatus.DEAD.value


def test_permanent_failure_skips_the_retry_budget(session, tenant):
    job = jobs.enqueue(session, tenant_id=tenant.id, kind="test.kind", max_attempts=10)
    jobs.claim(session, worker_id="w1")
    jobs.fail(session, job, "policy denied", permanent=True)
    assert job.status == JobStatus.DEAD.value
    assert job.attempts == 1


def test_cancelling_a_project_stops_its_queued_work(session, tenant, project):
    jobs.enqueue(session, tenant_id=tenant.id, kind="a", project_id=project.id)
    jobs.enqueue(session, tenant_id=tenant.id, kind="b", project_id=project.id)
    jobs.enqueue(session, tenant_id=tenant.id, kind="c", project_id="other")

    cancelled = jobs.cancel_for_project(session, project_id=project.id, reason="cancelled by client")
    assert cancelled == 2
    assert jobs.stats(session)[JobStatus.CANCELLED.value] == 2


# --- quality gates --------------------------------------------------------


def test_secret_scan_blocks_a_change_set(session, tenant, project):
    """AC-08: a test secret must not reach the repository."""
    files = [
        {
            "path": "src/config.py",
            "action": "create",
            "content": 'ANTHROPIC_API_KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz012345"\n',
        }
    ]
    outcome = gates_module.gate_g1_static(files)
    assert not outcome.passed
    assert any(f["rule"].startswith("secret.") for f in outcome.findings)


def test_example_files_downgrade_secret_findings(session):
    files = [{"path": ".env.example", "action": "create", "content": "API_KEY=sk-" + "a" * 40 + "\n"}]
    outcome = gates_module.gate_g1_static(files)
    assert outcome.passed
    assert all(f["severity"] != "critical" for f in outcome.findings)


def test_destructive_sql_is_flagged_critical():
    files = [{"path": "migrations/001.sql", "content": "DROP TABLE users;\n"}]
    findings = scanners.scan_static(files)
    assert any(f.rule == "sast.destructive_sql" and f.severity == "critical" for f in findings)


def test_denied_licence_is_a_high_finding():
    files = [{"path": "requirements.txt", "content": "somelib==1.0.0\n"}]
    findings = scanners.scan_dependencies(files, {"somelib": "AGPL-3.0"})
    assert any(f.rule == "license.denied" for f in findings)


def test_unpinned_dependency_is_reported():
    files = [{"path": "requirements.txt", "content": "requests>=2.0\n"}]
    findings = scanners.scan_dependencies(files)
    assert any(f.rule == "dependency.unpinned" for f in findings)


def test_sbom_lists_components_with_policy():
    files = [{"path": "requirements.txt", "content": "fastapi==0.115.6\nrequests==2.32.3\n"}]
    sbom = scanners.build_sbom(files, {"fastapi": "MIT", "requests": "Apache-2.0"})
    assert len(sbom.components) == 2
    assert all(c["policy"] == "allow" for c in sbom.components)


def test_requirements_gate_rejects_untestable_criteria():
    outcome = gates_module.gate_g0_requirements(
        [{"key": "T1", "acceptance_criteria": ["работает хорошо"], "executor": "agent"}]
    )
    assert any(f["rule"] == "requirements.vague_criterion" for f in outcome.findings)

    blocking = gates_module.gate_g0_requirements([{"key": "T2", "acceptance_criteria": []}])
    assert not blocking.passed


def test_waiver_converts_a_failure_into_a_waived_result(session, tenant, project, admin):
    from app.models.governance import PolicyWaiver

    session.add(
        PolicyWaiver(
            tenant_id=tenant.id,
            project_id=project.id,
            gate="G1",
            justification="test fixture uses a fake key on purpose",
            issued_by=admin.id,
            expires_at=utcnow() + timedelta(hours=4),
        )
    )
    session.flush()

    outcome = gates_module.gate_g1_static(
        [{"path": "src/x.py", "content": 'KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz012345"\n'}]
    )
    result = gates_module.persist(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        subject_type="change_set",
        subject_id="chg_1",
        outcome=outcome,
    )
    assert result.status == "waived"
    assert result.waiver_id is not None
    assert gates_module.blockers(session, tenant_id=tenant.id, subject_id="chg_1") == []


def test_expired_waiver_no_longer_unblocks(session, tenant, project, admin):
    from app.models.governance import PolicyWaiver

    session.add(
        PolicyWaiver(
            tenant_id=tenant.id,
            project_id=project.id,
            gate="G1",
            justification="expired long ago, must not apply",
            issued_by=admin.id,
            expires_at=utcnow() - timedelta(hours=1),
        )
    )
    session.flush()

    outcome = gates_module.gate_g1_static(
        [{"path": "src/x.py", "content": 'KEY = "sk-ant-abcdefghijklmnopqrstuvwxyz012345"\n'}]
    )
    result = gates_module.persist(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        subject_type="change_set",
        subject_id="chg_2",
        outcome=outcome,
    )
    assert result.status == "failed"
    assert len(gates_module.blockers(session, tenant_id=tenant.id, subject_id="chg_2")) == 1


def test_expired_gate_evidence_becomes_a_blocker(session, tenant, project):
    outcome = gates_module.gate_g2_unit({"passed": 10, "failed": 0, "coverage": 90.0})
    result = gates_module.persist(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        subject_type="release",
        subject_id="rel_1",
        outcome=outcome,
    )
    assert result.status == "passed"
    assert gates_module.blockers(session, tenant_id=tenant.id, subject_id="rel_1") == []

    result.expires_at = utcnow() - timedelta(minutes=1)
    session.flush()
    assert len(gates_module.blockers(session, tenant_id=tenant.id, subject_id="rel_1")) == 1


def test_release_gate_requires_a_complete_bundle():
    incomplete = gates_module.gate_g7_release({"release_manifest": {"v": 1}})
    assert not incomplete.passed

    complete = gates_module.gate_g7_release(
        {
            "release_manifest": {"v": 1},
            "change_sets": [{}],
            "test_reports": [{}],
            "security_reports": [{}],
            "sbom": {"components": []},
            "rollback_plan": "шаги отката",
            "approvals": [{}],
            "rollback_verified": True,
        }
    )
    assert complete.passed


# --- outbox ---------------------------------------------------------------


def test_consumers_are_deduped_across_redelivery(session, tenant, project):
    seen: list[str] = []
    events.subscribe("test.event", "counter", lambda _s, e: seen.append(e.id))

    event = events.emit(
        session, tenant_id=tenant.id, project_id=project.id, event_type="test.event", payload={}
    )
    events.deliver(session, event)
    events.deliver(session, event)
    assert len(seen) == 1


def test_a_failing_consumer_dead_letters_instead_of_stalling(session, tenant, project):
    def _boom(_session, _event):
        raise RuntimeError("consumer is broken")

    events.subscribe("test.boom", "broken", _boom)
    event = events.emit(
        session, tenant_id=tenant.id, project_id=project.id, event_type="test.boom", payload={}
    )

    for _ in range(events.MAX_ATTEMPTS):
        events.dispatch_pending(session)
    session.refresh(event)
    assert event.dead_lettered_at is not None
    assert event.published_at is None
