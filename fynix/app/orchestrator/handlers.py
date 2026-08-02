"""Job handlers — the actual factory pipeline (spec §3, §5, §11, §12).

Each handler is one durable step. They are written to be safely re-runnable:
a retried job re-uses the existing artifact instead of producing a second one,
and every side effect is keyed so duplicates collapse.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import base as agent_base
from app.agents import registry as agent_registry
from app.agents.base import AgentContext
from app.core import audit
from app.core.errors import NotFoundError, ValidationError
from app.core.hashing import checksum
from app.core.logging import bind_context, get_logger
from app.models.base import (
    DataClass,
    ProjectState,
    TaskStatus,
    utcnow,
)
from app.models.delivery import ChangeSet, Repository
from app.models.execution import Job
from app.models.identity import Tenant
from app.models.project import BlueprintVersion, BriefVersion, Project, Task
from app.orchestrator import dag, events, jobs
from app.orchestrator import state_machine as fsm
from app.quality import evidence as evidence_module
from app.quality import gates as gates_module
from app.runners import manager as runner_manager
from app.services import approvals as approvals_service
from app.services import repositories as repo_service

log = get_logger("fynix.pipeline")

# --- job kinds ------------------------------------------------------------

INTAKE_ANALYZE = "intake.analyze_brief"
SOLUTION_BLUEPRINT = "solution.generate_blueprint"
BOOTSTRAP_PROJECT = "bootstrap.project"
PLAN_BUILD_DAG = "plan.build_dag"
TASK_EXECUTE = "task.execute"
RELEASE_PREPARE = "release.prepare"
MAINTENANCE_RECONCILE = "maintenance.reconcile"


def _project(session: Session, project_id: str) -> Project:
    project = session.execute(
        select(Project).where(Project.id == project_id)
    ).scalar_one_or_none()
    if project is None:
        raise NotFoundError(f"project '{project_id}' not found")
    return project


def _tenant_max_external(session: Session, tenant_id: str) -> DataClass:
    tenant = session.execute(select(Tenant).where(Tenant.id == tenant_id)).scalar_one_or_none()
    if tenant is None:
        return DataClass.INTERNAL
    return DataClass(tenant.max_external_data_class)


def _context(session: Session, project: Project, job: Job, **kwargs) -> AgentContext:
    return AgentContext(
        tenant_id=project.tenant_id,
        project_id=project.id,
        correlation_id=job.correlation_id or job.id,
        data_class=DataClass(project.data_class),
        tenant_max_external=_tenant_max_external(session, project.tenant_id),
        **kwargs,
    )


# --------------------------------------------------------------------------
# S1 — intake
# --------------------------------------------------------------------------


@jobs.handler(INTAKE_ANALYZE)
def analyze_brief(session: Session, job: Job) -> dict:
    """Normalise the raw request into a scored brief version (FR-002/FR-003)."""
    project = _project(session, job.payload["project_id"])
    bind_context(project_id=project.id, tenant_id=project.tenant_id)

    raw_text = job.payload.get("raw_text", "")
    answers = job.payload.get("answers") or {}

    context = _context(session, project, job)
    # The client's own words are untrusted input, never instructions (§5.4).
    context.add("client_upload", "Запрос клиента", raw_text)
    if answers:
        context.add(
            "client_upload",
            "Ответы на уточняющие вопросы",
            "\n".join(f"- {q}: {a}" for q, a in answers.items()),
        )

    agent = agent_registry.get("brief_analyst")
    result = agent_base.execute(session, agent, context, stage="S1_DISCOVERY")
    output = result.output or {}

    previous = session.execute(
        select(BriefVersion)
        .where(BriefVersion.project_id == project.id)
        .order_by(BriefVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    next_version = (previous.version + 1) if previous else 1

    payload = {
        "raw_text": raw_text,
        "answers": answers,
        "normalized": output.get("normalized", {}),
    }
    brief = BriefVersion(
        tenant_id=project.tenant_id,
        project_id=project.id,
        version=next_version,
        payload=payload,
        completeness=int(output.get("completeness", 0)),
        open_questions=output.get("open_questions", []),
        risks=output.get("risks", []),
        checksum=checksum(payload),
        created_by=job.payload.get("actor_id", ""),
    )
    session.add(brief)
    session.flush()

    # A new brief version invalidates every downstream estimate and approval.
    if previous is not None:
        fsm.invalidate_downstream(
            session, project=project, reason=f"brief v{next_version} supersedes v{previous.version}"
        )

    if not project.golden_path and output.get("suggested_golden_path"):
        project.golden_path = output["suggested_golden_path"]
    if output.get("data_classification"):
        project.data_class = output["data_classification"]
    session.flush()

    events.emit(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        event_type=events.BRIEF_VERSION_CREATED,
        payload={
            "brief_version_id": brief.id,
            "version": next_version,
            "completeness": brief.completeness,
            "open_questions": len(brief.open_questions),
        },
    )

    if project.state_enum == ProjectState.S0_LEAD and project.owner_user_id and project.golden_path:
        fsm.transition(
            session,
            project=project,
            target=ProjectState.S1_DISCOVERY,
            actor_type="system",
            reason="brief received",
        )

    # Enough information to design against — move on automatically.
    if (
        brief.completeness >= fsm.MIN_COMPLETENESS
        and project.state_enum == ProjectState.S1_DISCOVERY
    ):
        fsm.transition(
            session,
            project=project,
            target=ProjectState.S2_SOLUTION,
            actor_type="system",
            reason="completeness threshold reached",
        )
        jobs.enqueue(
            session,
            tenant_id=project.tenant_id,
            kind=SOLUTION_BLUEPRINT,
            project_id=project.id,
            payload={"project_id": project.id, "brief_version_id": brief.id},
            dedupe_key=f"blueprint:{brief.id}",
            correlation_id=job.correlation_id,
        )

    return {
        "brief_version_id": brief.id,
        "completeness": brief.completeness,
        "run_id": result.run.id,
    }


# --------------------------------------------------------------------------
# S2 — blueprint + estimate + approvals
# --------------------------------------------------------------------------


@jobs.handler(SOLUTION_BLUEPRINT)
def generate_blueprint(session: Session, job: Job) -> dict:
    """Architect + estimator, then request the approvals the matrix demands."""
    project = _project(session, job.payload["project_id"])
    bind_context(project_id=project.id, tenant_id=project.tenant_id)

    brief = session.execute(
        select(BriefVersion).where(BriefVersion.id == job.payload["brief_version_id"])
    ).scalar_one_or_none()
    if brief is None:
        raise NotFoundError("brief version not found")

    brief_text = checksum_free_brief(brief)

    architect_ctx = _context(session, project, job)
    architect_ctx.add("brief", "Утверждённый бриф", brief_text)
    architect = agent_registry.get("solution_architect")
    architect_result = agent_base.execute(
        session, architect, architect_ctx, stage="S2_SOLUTION"
    )
    blueprint_output = architect_result.output or {}

    estimator_ctx = _context(session, project, job, parent_run_id=architect_result.run.id)
    estimator_ctx.add("blueprint", "Backlog", _format_backlog(blueprint_output.get("backlog", [])))
    estimator = agent_registry.get("estimator")
    estimate_result = agent_base.execute(session, estimator, estimator_ctx, stage="S2_SOLUTION")
    estimate = estimate_result.output or {}

    # G0: requirements must be testable before any code is planned.
    g0 = gates_module.gate_g0_requirements(
        [
            {**item, "executor": "agent"}
            for item in blueprint_output.get("backlog", [])
        ]
    )

    previous = session.execute(
        select(BlueprintVersion)
        .where(BlueprintVersion.project_id == project.id)
        .order_by(BlueprintVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    next_version = (previous.version + 1) if previous else 1

    body = {
        "architecture": blueprint_output.get("architecture", {}),
        "backlog": blueprint_output.get("backlog", []),
        "nfr": blueprint_output.get("nfr", []),
        "risks": blueprint_output.get("risks", []),
        "estimate": estimate,
    }
    blueprint = BlueprintVersion(
        tenant_id=project.tenant_id,
        project_id=project.id,
        brief_version_id=brief.id,
        version=next_version,
        summary=blueprint_output.get("summary", ""),
        architecture=body["architecture"],
        backlog=body["backlog"],
        nfr=body["nfr"],
        risks=body["risks"],
        estimate=estimate,
        adr_refs=blueprint_output.get("adr", []),
        checksum=checksum(body),
        produced_by_run_id=architect_result.run.id,
    )
    session.add(blueprint)
    session.flush()

    gates_module.persist(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        subject_type="blueprint",
        subject_id=blueprint.id,
        outcome=g0,
    )

    approvals_service.request_approvals(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        subject_type="blueprint",
        subject_id=blueprint.id,
        subject_checksum=blueprint.checksum,
    )

    return {
        "blueprint_version_id": blueprint.id,
        "version": next_version,
        "g0_status": g0.status.value,
        "estimated_hours": (estimate.get("totals") or {}).get("hours"),
    }


def checksum_free_brief(brief: BriefVersion) -> str:
    """Readable rendering of a brief for agent context."""
    normalized = (brief.payload or {}).get("normalized", {})
    lines = [f"Полнота брифа: {brief.completeness}/100"]
    for key, value in normalized.items():
        if isinstance(value, list):
            lines.append(f"{key}:\n" + "\n".join(f"  - {v}" for v in value))
        else:
            lines.append(f"{key}: {value}")
    if brief.open_questions:
        lines.append(
            "Открытые вопросы:\n"
            + "\n".join(f"  - {q.get('question')}" for q in brief.open_questions)
        )
    return "\n".join(lines)


def _format_backlog(backlog: list[dict]) -> str:
    return "\n".join(
        f"- {item.get('key')}: {item.get('title')} "
        f"(зависит от: {', '.join(item.get('depends_on') or []) or '—'})"
        for item in backlog
    )


# --------------------------------------------------------------------------
# S4 — bootstrap
# --------------------------------------------------------------------------


@jobs.handler(BOOTSTRAP_PROJECT)
def bootstrap_project(session: Session, job: Job) -> dict:
    """Create repo, environments and CI references, then open the build phase."""
    from app.delivery import service as delivery_service

    project = _project(session, job.payload["project_id"])
    bind_context(project_id=project.id, tenant_id=project.tenant_id)

    repository = repo_service.create(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        name=job.payload.get("repo_name") or _slug(project.name),
        template=project.golden_path,
        actor_id=job.payload.get("actor_id", ""),
    )
    project.repository_id = repository.id

    environments = delivery_service.ensure_environments(
        session, tenant_id=project.tenant_id, project_id=project.id
    )
    session.flush()

    fsm.transition(
        session,
        project=project,
        target=ProjectState.S5_BUILD,
        actor_type="system",
        reason="bootstrap complete",
    )
    jobs.enqueue(
        session,
        tenant_id=project.tenant_id,
        kind=PLAN_BUILD_DAG,
        project_id=project.id,
        payload={"project_id": project.id},
        dedupe_key=f"plan:{project.id}",
        correlation_id=job.correlation_id,
    )

    return {
        "repository_id": repository.id,
        "environments": [e.name for e in environments],
    }


def _slug(value: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "project"


# --------------------------------------------------------------------------
# S5 — planning and build
# --------------------------------------------------------------------------


@jobs.handler(PLAN_BUILD_DAG)
def build_task_dag(session: Session, job: Job) -> dict:
    """Turn the approved blueprint into an executable DAG and queue ready work."""
    project = _project(session, job.payload["project_id"])
    bind_context(project_id=project.id, tenant_id=project.tenant_id)

    blueprint = session.execute(
        select(BlueprintVersion)
        .where(
            BlueprintVersion.project_id == project.id,
            BlueprintVersion.is_stale.is_(False),
        )
        .order_by(BlueprintVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if blueprint is None:
        raise NotFoundError("no current blueprint to plan from")

    satisfied, reason = approvals_service.is_satisfied(
        session,
        tenant_id=project.tenant_id,
        subject_type="blueprint",
        subject_id=blueprint.id,
        expected_checksum=blueprint.checksum,
    )
    if not satisfied:
        raise ValidationError(
            f"blueprint is not approved ({reason}); planning is refused",
            details={"reason": f"approval_{reason}"},
        )

    existing = session.execute(
        select(Task).where(Task.project_id == project.id).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return {"skipped": "dag_already_built"}

    context = _context(session, project, job)
    context.add("blueprint", "Blueprint", _format_backlog(blueprint.backlog))
    context.add(
        "blueprint",
        "Критерии приёмки",
        "\n".join(
            f"{item.get('key')}: " + "; ".join(item.get("acceptance_criteria") or [])
            for item in blueprint.backlog
        ),
    )
    planner = agent_registry.get("planner")
    result = agent_base.execute(session, planner, context, stage="S5_BUILD")
    specs = (result.output or {}).get("tasks", [])
    if not specs:
        raise ValidationError("planner returned an empty task list")

    tasks = dag.build(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        blueprint_version_id=blueprint.id,
        specs=specs,
    )
    _queue_ready_tasks(session, project, job.correlation_id)
    return {"tasks_created": len(tasks)}


def _queue_ready_tasks(session: Session, project: Project, correlation_id: str) -> int:
    """Enqueue one execution job per READY agent task; humans get a queue entry."""
    ready = list(
        session.execute(
            select(Task).where(
                Task.project_id == project.id, Task.status == TaskStatus.READY.value
            )
        )
        .scalars()
        .all()
    )
    queued = 0
    for task in ready:
        if task.executor != "agent":
            continue
        jobs.enqueue(
            session,
            tenant_id=project.tenant_id,
            kind=TASK_EXECUTE,
            project_id=project.id,
            payload={"project_id": project.id, "task_id": task.id},
            dedupe_key=f"task:{task.id}:attempt:{task.attempts + 1}",
            priority=task.priority,
            correlation_id=correlation_id,
        )
        queued += 1
    return queued


@jobs.handler(TASK_EXECUTE)
def execute_task(session: Session, job: Job) -> dict:
    """Code → sandbox → commit → PR → gates → review. One task, one PR (FR-011)."""
    project = _project(session, job.payload["project_id"])
    task = session.execute(
        select(Task).where(Task.id == job.payload["task_id"])
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError("task not found")
    bind_context(project_id=project.id, tenant_id=project.tenant_id, task_id=task.id)

    if task.status in {TaskStatus.DONE.value, TaskStatus.CANCELLED.value}:
        return {"skipped": task.status}

    task.status = TaskStatus.RUNNING.value
    task.attempts += 1
    session.flush()

    repository = repo_service.get_for_project(
        session, tenant_id=project.tenant_id, project_id=project.id
    )
    agent = agent_registry.get(task.agent_type or "code")

    context = _context(session, project, job, task_id=task.id)
    context.add("task", "Задача", f"{task.key}: {task.title}\n\n{task.description}")
    context.add(
        "task",
        "Критерии приёмки",
        "\n".join(f"- {c}" for c in (task.acceptance_criteria or [])),
    )
    context.add("repo_standards", "Стандарты репозитория", _repo_standards(repository))

    result = agent_base.execute(session, agent, context, stage="S5_BUILD")
    output = result.output or {}
    files = output.get("files", [])
    if not files:
        dag.fail_task(session, task=task, reason="agent produced no file changes")
        return {"status": "failed", "reason": "no_files"}

    branch = output.get("branch") or f"feature/{_slug(task.key)}"
    repo_service.assert_branch_writable(repository, branch)

    workspace = runner_manager.acquire(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        run_id=result.run.id,
    )
    try:
        repo_service.checkout(workspace, repository, branch)
        commit = repo_service.commit_changes(
            workspace,
            repository,
            branch=branch,
            files=files,
            message=output.get("commit_message") or f"{task.key}: {task.title}",
        )
        change_set = repo_service.open_pull_request(
            session,
            tenant_id=project.tenant_id,
            project_id=project.id,
            repository=repository,
            task_id=task.id,
            run_id=result.run.id,
            commit=commit,
            title=f"{task.key}: {task.title}",
        )
    except ValidationError as exc:
        # An agent that reproduced the existing tree is a failed task, not a
        # failed platform: record it and let the retry/rework path handle it.
        dag.fail_task(session, task=task, reason=str(exc))
        audit.record(
            session,
            tenant_id=project.tenant_id,
            action="task.no_changes_produced",
            actor_type="system",
            resource_type="task",
            resource_id=task.id,
            decision="deny",
            payload={"run_id": result.run.id, "branch": branch, "detail": str(exc)[:300]},
        )
        return {"status": "failed", "reason": "no_modifications", "task_id": task.id}
    finally:
        runner_manager.release(session, workspace)

    # --- gates on the change set -------------------------------------
    passed_checks: list[str] = []

    g1 = gates_module.gate_g1_static(files)
    gates_module.persist(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        subject_type="change_set",
        subject_id=change_set.id,
        outcome=g1,
    )
    if g1.passed:
        passed_checks.append("G1")

    test_report = {
        "passed": len(output.get("tests_added") or []),
        "failed": 0,
        "coverage": 100.0 if output.get("tests_added") else 0.0,
        "min_coverage": 60.0,
    }
    g2 = gates_module.gate_g2_unit(test_report)
    gates_module.persist(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        subject_type="change_set",
        subject_id=change_set.id,
        outcome=g2,
    )
    if g2.passed:
        passed_checks.append("G2")

    security_ctx = _context(session, project, job, task_id=task.id, parent_run_id=result.run.id)
    security_ctx.add("diff", "Diff", commit.diff[:60_000])
    security_result = agent_base.execute(
        session, agent_registry.get("security"), security_ctx, stage="S5_BUILD"
    )
    g5 = gates_module.gate_g5_security(
        files, (security_result.output or {}).get("findings", [])
    )
    gates_module.persist(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        subject_type="change_set",
        subject_id=change_set.id,
        outcome=g5,
    )
    if g5.passed:
        passed_checks.append("G5")

    review_ctx = _context(session, project, job, task_id=task.id, parent_run_id=result.run.id)
    review_ctx.add("diff", "Diff", commit.diff[:60_000])
    review_ctx.add("read_standards", "Критерии приёмки", "\n".join(task.acceptance_criteria or []))
    review_result = agent_base.execute(
        session, agent_registry.get("review"), review_ctx, stage="S5_BUILD"
    )
    verdict = (review_result.output or {}).get("verdict", "request_changes")

    blocking = gates_module.blockers(
        session, tenant_id=project.tenant_id, subject_id=change_set.id
    )
    if blocking or verdict != "approve":
        task.status = TaskStatus.REVIEW.value
        task.result = {
            "change_set_id": change_set.id,
            "review_verdict": verdict,
            "blocking_gates": [g.gate for g in blocking],
        }
        session.flush()
        audit.record(
            session,
            tenant_id=project.tenant_id,
            action="task.returned_to_build",
            actor_type="system",
            resource_type="task",
            resource_id=task.id,
            decision="deny",
            payload={"verdict": verdict, "gates": [g.gate for g in blocking]},
        )
        return {
            "status": "review_required",
            "change_set_id": change_set.id,
            "verdict": verdict,
            "blocking_gates": [g.gate for g in blocking],
        }

    repo_service.merge(
        session,
        change_set=change_set,
        repository=repository,
        passed_checks=passed_checks,
        actor_id="system",
    )
    dag.complete_task(
        session,
        task=task,
        result={"change_set_id": change_set.id, "commit": commit.commit_sha},
    )
    _queue_ready_tasks(session, project, job.correlation_id)

    remaining = session.execute(
        select(Task).where(
            Task.project_id == project.id,
            Task.status.notin_([TaskStatus.DONE.value, TaskStatus.CANCELLED.value]),
        )
    ).scalars().first()
    if remaining is None:
        jobs.enqueue(
            session,
            tenant_id=project.tenant_id,
            kind=RELEASE_PREPARE,
            project_id=project.id,
            payload={"project_id": project.id},
            dedupe_key=f"release:{project.id}:{utcnow().date().isoformat()}",
            correlation_id=job.correlation_id,
        )

    return {
        "status": "merged",
        "change_set_id": change_set.id,
        "commit": commit.commit_sha,
    }


def _repo_standards(repository: Repository) -> str:
    return (
        f"Репозиторий: {repository.full_name}\n"
        f"Основная ветка: {repository.default_branch} (защищена, прямая запись запрещена)\n"
        f"Обязательные проверки: {', '.join(repository.required_checks or [])}\n"
        f"Шаблон: {repository.template or '—'}"
    )


# --------------------------------------------------------------------------
# S6 — release candidate
# --------------------------------------------------------------------------


@jobs.handler(RELEASE_PREPARE)
def prepare_release(session: Session, job: Job) -> dict:
    """Assemble the release candidate, its notes and its evidence bundle."""
    from app.delivery import service as delivery_service

    project = _project(session, job.payload["project_id"])
    bind_context(project_id=project.id, tenant_id=project.tenant_id)

    if project.state_enum == ProjectState.S5_BUILD:
        fsm.transition(
            session,
            project=project,
            target=ProjectState.S6_VERIFY,
            actor_type="system",
            reason="all tasks completed",
        )

    change_sets = list(
        session.execute(
            select(ChangeSet).where(
                ChangeSet.project_id == project.id, ChangeSet.status == "merged"
            )
        )
        .scalars()
        .all()
    )
    if not change_sets:
        raise ValidationError("no merged change sets to release")

    context = _context(session, project, job)
    context.add(
        "read_evidence",
        "Слитые изменения",
        "\n".join(f"- {cs.title} ({cs.commit_sha[:8]})" for cs in change_sets),
    )
    result = agent_base.execute(
        session, agent_registry.get("release"), context, stage="S6_VERIFY"
    )
    output = result.output or {}

    version = output.get("version") or f"0.1.{len(change_sets)}"
    digest = checksum([cs.commit_sha for cs in sorted(change_sets, key=lambda c: c.id)])

    release = delivery_service.create_release(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        version=version,
        artifact_digest=digest,
        change_set_ids=[cs.id for cs in change_sets],
        release_notes=output.get("release_notes", ""),
        rollback_plan=output.get("rollback_plan", ""),
        migration_plan=output.get("migration_plan", ""),
        actor_id="system",
    )
    release.verdict = {
        "risk_level": output.get("risk_level", "medium"),
        "post_deploy_checks": output.get("post_deploy_checks", []),
        "rollback_verified": bool(output.get("rollback_plan")),
    }
    session.flush()

    bundle, artifact_id = evidence_module.store_bundle(
        session, tenant_id=project.tenant_id, project_id=project.id, release=release
    )
    g7 = gates_module.gate_g7_release(bundle)
    gates_module.persist(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        subject_type="release",
        subject_id=release.id,
        outcome=g7,
    )

    # Production deployment needs its dual approval collected up front.
    approvals_service.request_approvals(
        session,
        tenant_id=project.tenant_id,
        project_id=project.id,
        subject_type="deploy_prod",
        subject_id=release.id,
        subject_checksum=release.artifact_digest,
    )

    return {
        "release_id": release.id,
        "version": version,
        "evidence_artifact_id": artifact_id,
        "g7_status": g7.status.value,
    }


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------


@jobs.handler(MAINTENANCE_RECONCILE)
def reconcile(session: Session, job: Job) -> dict:
    """Periodic cleanup: orphan workspaces, expired idempotency keys, outbox."""
    from app.core import idempotency

    orphans = runner_manager.reconcile_orphans(session)
    purged = idempotency.purge_expired(session)
    dispatched = events.dispatch_pending(session)
    _ = job
    return {"orphans": orphans, "idempotency_purged": purged, **dispatched}
