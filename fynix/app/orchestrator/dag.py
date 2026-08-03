"""Task DAG: dependency resolution, readiness and cancellation (FR-008)."""

from __future__ import annotations

from collections import defaultdict, deque

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.models.base import TaskStatus, utcnow
from app.models.project import Task, TaskDependency
from app.orchestrator import events


def build(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    blueprint_version_id: str | None,
    specs: list[dict],
) -> list[Task]:
    """Materialise a backlog into tasks + edges.

    `specs` entries look like::

        {"key": "T1", "title": "...", "depends_on": ["T0"], "executor": "agent",
         "agent_type": "code", "acceptance_criteria": [...]}

    The graph is validated for unknown references and cycles before anything is
    written, so a bad plan never leaves a half-built DAG behind.
    """
    keys = [s["key"] for s in specs]
    if len(set(keys)) != len(keys):
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        raise ValidationError("duplicate task keys in plan", details={"keys": duplicates})

    known = set(keys)
    edges: dict[str, list[str]] = {}
    for spec in specs:
        deps = list(spec.get("depends_on") or [])
        unknown = [d for d in deps if d not in known]
        if unknown:
            raise ValidationError(
                f"task '{spec['key']}' depends on unknown tasks",
                details={"unknown": unknown},
            )
        edges[spec["key"]] = deps

    _assert_acyclic(edges)

    created: dict[str, Task] = {}
    for spec in specs:
        task = Task(
            tenant_id=tenant_id,
            project_id=project_id,
            blueprint_version_id=blueprint_version_id,
            key=spec["key"],
            title=spec.get("title", spec["key"]),
            description=spec.get("description", ""),
            kind=spec.get("kind", "code"),
            executor=spec.get("executor", "agent"),
            agent_type=spec.get("agent_type"),
            priority=int(spec.get("priority", 100)),
            acceptance_criteria=list(spec.get("acceptance_criteria") or []),
            estimate_tokens=int(spec.get("estimate_tokens", 0)),
            estimate_hours=float(spec.get("estimate_hours", 0.0)),
            status=TaskStatus.BLOCKED.value,
        )
        session.add(task)
        created[spec["key"]] = task
    session.flush()

    for key, deps in edges.items():
        for dep_key in deps:
            session.add(
                TaskDependency(task_id=created[key].id, depends_on_id=created[dep_key].id)
            )
    session.flush()

    refresh_readiness(session, project_id=project_id, tenant_id=tenant_id)
    return list(created.values())


def _assert_acyclic(edges: dict[str, list[str]]) -> None:
    """Kahn's algorithm; reports the nodes involved in the cycle."""
    indegree = {node: 0 for node in edges}
    dependents: dict[str, list[str]] = defaultdict(list)
    for node, deps in edges.items():
        indegree[node] = len(deps)
        for dep in deps:
            dependents[dep].append(node)

    queue = deque([n for n, d in indegree.items() if d == 0])
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for child in dependents[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if visited != len(edges):
        cyclic = sorted(n for n, d in indegree.items() if d > 0)
        raise ValidationError("task graph contains a cycle", details={"tasks": cyclic})


def dependencies_of(session: Session, task_id: str) -> list[Task]:
    return list(
        session.execute(
            select(Task)
            .join(TaskDependency, TaskDependency.depends_on_id == Task.id)
            .where(TaskDependency.task_id == task_id)
        )
        .scalars()
        .all()
    )


def dependents_of(session: Session, task_id: str) -> list[Task]:
    return list(
        session.execute(
            select(Task)
            .join(TaskDependency, TaskDependency.task_id == Task.id)
            .where(TaskDependency.depends_on_id == task_id)
        )
        .scalars()
        .all()
    )


def refresh_readiness(session: Session, *, project_id: str, tenant_id: str) -> list[Task]:
    """Promote BLOCKED tasks whose dependencies are all DONE to READY.

    Emits `task.ready` for each promotion so the agent runtime / human queue can
    pick the work up (spec §9.2).
    """
    tasks = list(
        session.execute(select(Task).where(Task.project_id == project_id)).scalars().all()
    )
    by_id = {t.id: t for t in tasks}

    edges = list(
        session.execute(
            select(TaskDependency).where(TaskDependency.task_id.in_(list(by_id)))
        )
        .scalars()
        .all()
    )
    deps_by_task: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        deps_by_task[edge.task_id].append(edge.depends_on_id)

    promoted: list[Task] = []
    for task in tasks:
        if task.status != TaskStatus.BLOCKED.value:
            continue
        deps = deps_by_task.get(task.id, [])
        if all(
            by_id[d].status == TaskStatus.DONE.value for d in deps if d in by_id
        ):
            task.status = TaskStatus.READY.value
            promoted.append(task)

    if promoted:
        session.flush()
        for task in promoted:
            events.emit(
                session,
                tenant_id=tenant_id,
                project_id=project_id,
                event_type=events.TASK_READY,
                payload={
                    "task_id": task.id,
                    "key": task.key,
                    "executor": task.executor,
                    "agent_type": task.agent_type,
                },
            )
    return promoted


def complete_task(
    session: Session, *, task: Task, result: dict | None = None
) -> list[Task]:
    """Mark a task done and unblock whatever it was gating."""
    task.status = TaskStatus.DONE.value
    task.result = result or task.result
    session.flush()
    return refresh_readiness(
        session, project_id=task.project_id, tenant_id=task.tenant_id
    )


def fail_task(session: Session, *, task: Task, reason: str) -> Task:
    """A failed task blocks its dependents; they stay BLOCKED, never silently ready."""
    task.status = TaskStatus.FAILED.value
    task.result = {**(task.result or {}), "failure_reason": reason, "at": utcnow().isoformat()}
    session.flush()
    return task


def cancel_subtree(session: Session, *, task: Task, reason: str) -> list[Task]:
    """Cancel a task and everything downstream of it (compensation, §15)."""
    cancelled: list[Task] = []
    queue = deque([task])
    seen = {task.id}
    while queue:
        current = queue.popleft()
        if current.status not in {TaskStatus.DONE.value, TaskStatus.CANCELLED.value}:
            current.status = TaskStatus.CANCELLED.value
            current.result = {**(current.result or {}), "cancel_reason": reason}
            cancelled.append(current)
        for child in dependents_of(session, current.id):
            if child.id not in seen:
                seen.add(child.id)
                queue.append(child)
    session.flush()
    return cancelled


def progress(session: Session, project_id: str) -> dict:
    tasks = list(
        session.execute(select(Task).where(Task.project_id == project_id)).scalars().all()
    )
    counts: dict[str, int] = defaultdict(int)
    for task in tasks:
        counts[task.status] += 1
    total = len(tasks)
    done = counts.get(TaskStatus.DONE.value, 0)
    return {
        "total": total,
        "done": done,
        "percent": round(done / total * 100, 1) if total else 0.0,
        "by_status": dict(counts),
    }
