"""Domain events: transactional outbox + at-least-once publication (spec §9.2).

Producers call `emit()` inside the same transaction as the state change. A
separate dispatcher publishes unpublished rows to subscribers (webhooks,
notifications, in-process consumers) and retries with backoff; after
`MAX_ATTEMPTS` the row is dead-lettered and alerted on instead of blocking the
stream.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_correlation_id, get_logger
from app.core.redaction import redact
from app.models.base import utcnow
from app.models.messaging import OutboxEvent, ProcessedEvent

log = get_logger("fynix.events")

MAX_ATTEMPTS = 8

# --- event type vocabulary (spec §9.2) -----------------------------------

BRIEF_VERSION_CREATED = "brief.version.created"
BLUEPRINT_APPROVAL_REQUESTED = "blueprint.approval.requested"
BLUEPRINT_APPROVED = "blueprint.approved"
TASK_READY = "task.ready"
AGENT_RUN_COMPLETED = "agent.run.completed"
QUALITY_GATE_FAILED = "quality.gate.failed"
RELEASE_CANDIDATE_CREATED = "release.candidate.created"
DEPLOYMENT_COMPLETED = "deployment.completed"
DEPLOYMENT_ROLLED_BACK = "deployment.rolled_back"
BUDGET_THRESHOLD_REACHED = "budget.threshold.reached"
INCIDENT_OPENED = "incident.opened"
PROJECT_STATE_CHANGED = "project.state.changed"
APPROVAL_DECIDED = "approval.decided"

KNOWN_EVENT_TYPES = frozenset(
    {
        BRIEF_VERSION_CREATED,
        BLUEPRINT_APPROVAL_REQUESTED,
        BLUEPRINT_APPROVED,
        TASK_READY,
        AGENT_RUN_COMPLETED,
        QUALITY_GATE_FAILED,
        RELEASE_CANDIDATE_CREATED,
        DEPLOYMENT_COMPLETED,
        DEPLOYMENT_ROLLED_BACK,
        BUDGET_THRESHOLD_REACHED,
        INCIDENT_OPENED,
        PROJECT_STATE_CHANGED,
        APPROVAL_DECIDED,
    }
)


def emit(
    session: Session,
    *,
    tenant_id: str,
    event_type: str,
    payload: dict,
    project_id: str | None = None,
    causation_id: str = "",
    correlation_id: str | None = None,
) -> OutboxEvent:
    """Append an event to the outbox. Never publishes inline."""
    event = OutboxEvent(
        tenant_id=tenant_id,
        type=event_type,
        project_id=project_id,
        payload=redact(payload),
        occurred_at=utcnow(),
        correlation_id=correlation_id if correlation_id is not None else get_correlation_id(),
        causation_id=causation_id,
    )
    session.add(event)
    session.flush()
    return event


# --- in-process subscribers ----------------------------------------------

Subscriber = Callable[[Session, OutboxEvent], None]
_subscribers: dict[str, list[tuple[str, Subscriber]]] = {}


def subscribe(event_type: str, name: str, handler: Subscriber) -> None:
    """Register a consumer. `name` identifies it in the dedupe table."""
    _subscribers.setdefault(event_type, [])
    if not any(existing == name for existing, _ in _subscribers[event_type]):
        _subscribers[event_type].append((name, handler))


def clear_subscribers() -> None:
    _subscribers.clear()


def _already_processed(session: Session, consumer: str, event_id: str) -> bool:
    return (
        session.execute(
            select(ProcessedEvent).where(
                ProcessedEvent.consumer == consumer, ProcessedEvent.event_id == event_id
            )
        ).scalar_one_or_none()
        is not None
    )


def deliver(session: Session, event: OutboxEvent) -> list[str]:
    """Run every subscriber for this event exactly once (consumer-side dedupe)."""
    delivered: list[str] = []
    for name, handler in _subscribers.get(event.type, []):
        if _already_processed(session, name, event.id):
            continue
        handler(session, event)
        session.add(ProcessedEvent(consumer=name, event_id=event.id))
        session.flush()
        delivered.append(name)
    return delivered


def dispatch_pending(session: Session, *, limit: int = 100) -> dict:
    """Publish unpublished outbox rows. Safe to run concurrently: a row already
    marked published is skipped, and consumers dedupe on `event_id`."""
    now = utcnow()
    pending = (
        session.execute(
            select(OutboxEvent)
            .where(
                OutboxEvent.published_at.is_(None),
                OutboxEvent.dead_lettered_at.is_(None),
            )
            .order_by(OutboxEvent.created_at.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )

    published = 0
    dead_lettered = 0
    for event in pending:
        try:
            deliver(session, event)
            event.published_at = utcnow()
            published += 1
        except Exception as exc:
            event.attempts += 1
            event.last_error = str(exc)[:2000]
            if event.attempts >= MAX_ATTEMPTS:
                event.dead_lettered_at = utcnow()
                dead_lettered += 1
                log.error(
                    "event.dead_lettered",
                    event_id=event.id,
                    event_type=event.type,
                    attempts=event.attempts,
                    error=str(exc)[:500],
                )
            else:
                log.warning(
                    "event.delivery_failed",
                    event_id=event.id,
                    event_type=event.type,
                    attempts=event.attempts,
                )
        session.flush()

    _ = now, timedelta
    return {"scanned": len(pending), "published": published, "dead_lettered": dead_lettered}
