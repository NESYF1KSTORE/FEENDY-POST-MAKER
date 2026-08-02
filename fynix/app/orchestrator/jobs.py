"""Durable job queue with leases (spec §4.3 "durable timers, retries, signals").

This is the workflow-engine *port*. It provides what the spec actually demands —
work survives a restart, is executed at-least-once, retries with exponential
backoff, honours durable timers and never runs the same logical job twice
thanks to `dedupe_key`. Swapping in Temporal later means reimplementing this
module, not the callers.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.logging import get_logger
from app.models.base import JobStatus, utcnow
from app.models.execution import Job

log = get_logger("fynix.jobs")

#: kind -> handler. Handlers receive `(session, job)` and may raise to retry.
HANDLERS: dict[str, Callable[[Session, Job], dict | None]] = {}

BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 3600


def handler(kind: str):
    """Decorator registering a job handler."""

    def wrapper(fn: Callable[[Session, Job], dict | None]):
        HANDLERS[kind] = fn
        return fn

    return wrapper


def enqueue(
    session: Session,
    *,
    tenant_id: str,
    kind: str,
    payload: dict | None = None,
    project_id: str | None = None,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
    delay_seconds: float = 0,
    max_attempts: int = 5,
    priority: int = 100,
    correlation_id: str = "",
) -> Job:
    """Add a job. With `dedupe_key`, a duplicate submission returns the existing
    job rather than creating a second one (AC-11: retries create no duplicates).
    """
    now = utcnow()
    scheduled = run_after or (now + timedelta(seconds=delay_seconds))

    if dedupe_key:
        existing = session.execute(
            select(Job).where(Job.dedupe_key == dedupe_key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    job = Job(
        tenant_id=tenant_id,
        kind=kind,
        payload=payload or {},
        project_id=project_id,
        dedupe_key=dedupe_key,
        run_after=scheduled,
        max_attempts=max_attempts,
        priority=priority,
        correlation_id=correlation_id,
    )
    session.add(job)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        if dedupe_key:
            existing = session.execute(
                select(Job).where(Job.dedupe_key == dedupe_key)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
        raise
    return job


def claim(session: Session, *, worker_id: str, limit: int = 1) -> list[Job]:
    """Lease due jobs for this worker.

    Uses `SELECT ... FOR UPDATE SKIP LOCKED` on PostgreSQL so several workers can
    poll the same table without contending. On SQLite (tests) the lock hint is
    dropped — single-writer semantics make it unnecessary.
    """
    now = utcnow()
    settings = get_settings()
    stmt = (
        select(Job)
        .where(
            Job.run_after <= now,
            or_(
                Job.status == JobStatus.PENDING.value,
                # Reclaim jobs whose worker died mid-flight.
                (Job.status == JobStatus.LEASED.value) & (Job.lease_expires_at <= now),
            ),
        )
        .order_by(Job.priority.asc(), Job.run_after.asc())
        .limit(limit)
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)

    jobs = list(session.execute(stmt).scalars().all())
    for job in jobs:
        job.status = JobStatus.LEASED.value
        job.leased_by = worker_id
        job.lease_expires_at = now + timedelta(seconds=settings.job_lease_seconds)
        job.attempts += 1
    session.flush()
    return jobs


def _backoff(attempts: int) -> float:
    """Exponential backoff with full jitter — avoids retry storms (§15)."""
    ceiling = min(MAX_BACKOFF_SECONDS, BASE_BACKOFF_SECONDS * (2 ** max(0, attempts - 1)))
    return random.uniform(BASE_BACKOFF_SECONDS, max(BASE_BACKOFF_SECONDS, ceiling))


def complete(session: Session, job: Job) -> None:
    job.status = JobStatus.SUCCEEDED.value
    job.finished_at = utcnow()
    job.leased_by = None
    job.lease_expires_at = None
    session.flush()


def fail(session: Session, job: Job, error: str, *, permanent: bool = False) -> None:
    """Record a failure and either schedule a retry or mark the job dead."""
    job.last_error = error[:4000]
    job.leased_by = None
    job.lease_expires_at = None
    if permanent or job.attempts >= job.max_attempts:
        job.status = JobStatus.DEAD.value
        job.finished_at = utcnow()
        log.error(
            "job.dead", job_id=job.id, kind=job.kind, attempts=job.attempts, error=error[:300]
        )
    else:
        job.status = JobStatus.PENDING.value
        job.run_after = utcnow() + timedelta(seconds=_backoff(job.attempts))
        log.warning(
            "job.retry_scheduled",
            job_id=job.id,
            kind=job.kind,
            attempts=job.attempts,
            next_at=job.run_after.isoformat(),
        )
    session.flush()


def cancel(session: Session, job: Job, reason: str = "") -> None:
    if job.status in {JobStatus.SUCCEEDED.value, JobStatus.DEAD.value}:
        return
    job.status = JobStatus.CANCELLED.value
    job.last_error = reason
    job.finished_at = utcnow()
    job.leased_by = None
    job.lease_expires_at = None
    session.flush()


def cancel_for_project(session: Session, *, project_id: str, reason: str) -> int:
    """Spec §3.1: cancelling a project run stops new jobs from starting."""
    jobs = list(
        session.execute(
            select(Job).where(
                Job.project_id == project_id,
                Job.status.in_([JobStatus.PENDING.value, JobStatus.LEASED.value]),
            )
        )
        .scalars()
        .all()
    )
    for job in jobs:
        cancel(session, job, reason)
    return len(jobs)


def execute(session: Session, job: Job) -> dict | None:
    """Run one job's handler. Errors are the caller's to translate into retries."""
    fn = HANDLERS.get(job.kind)
    if fn is None:
        raise LookupError(f"no handler registered for job kind '{job.kind}'")
    return fn(session, job)


def stats(session: Session) -> dict:
    from sqlalchemy import func

    rows = session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
    return {status: int(count) for status, count in rows}
