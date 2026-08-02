"""Append-only, tamper-evident audit log (NFR-009, spec §10.2).

Each event stores the hash of the previous event for the same tenant. Editing
or removing a row breaks every hash downstream of it, which `verify_chain`
detects. This is *tamper-evident*, not tamper-proof: it makes silent history
rewriting detectable without requiring a separate WORM store in the MVP.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.hashing import canonical_json, sha256_hex
from app.core.redaction import redact
from app.models.base import utcnow
from app.models.governance import AuditEvent

GENESIS_HASH = "0" * 64


def _stamp(value: datetime) -> str:
    """Timestamp rendering that survives a round trip through any driver.

    Some drivers (SQLite) hand back a naive datetime for a column declared with
    a timezone. Normalising to UTC here keeps the recomputed hash identical to
    the stored one, so a verification failure means real tampering rather than
    a storage detail.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _event_hash(
    *,
    prev_hash: str,
    seq: int,
    tenant_id: str,
    occurred_at: datetime,
    actor_type: str,
    actor_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    decision: str,
    payload: dict,
) -> str:
    material = canonical_json(
        {
            "prev": prev_hash,
            "seq": seq,
            "tenant": tenant_id,
            "at": _stamp(occurred_at),
            "actor_type": actor_type,
            "actor": actor_id,
            "action": action,
            "res_type": resource_type,
            "res_id": resource_id,
            "decision": decision,
            "payload": payload,
        }
    )
    return sha256_hex(material)


def record(
    session: Session,
    *,
    tenant_id: str,
    action: str,
    actor_id: str = "",
    actor_type: str = "user",
    resource_type: str = "",
    resource_id: str = "",
    decision: str = "allow",
    correlation_id: str = "",
    payload: dict | None = None,
) -> AuditEvent:
    """Append one event. Must be called inside the caller's transaction so the
    audit entry commits atomically with the change it describes."""
    safe_payload = redact(payload or {})
    last = session.execute(
        select(AuditEvent)
        .where(AuditEvent.tenant_id == tenant_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    ).scalar_one_or_none()

    prev_hash = last.hash if last else GENESIS_HASH
    seq = (last.seq + 1) if last else 1
    occurred_at = utcnow()

    event = AuditEvent(
        tenant_id=tenant_id,
        seq=seq,
        occurred_at=occurred_at,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=decision,
        correlation_id=correlation_id,
        payload=safe_payload,
        prev_hash=prev_hash,
        hash=_event_hash(
            prev_hash=prev_hash,
            seq=seq,
            tenant_id=tenant_id,
            occurred_at=occurred_at,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            decision=decision,
            payload=safe_payload,
        ),
    )
    session.add(event)
    session.flush()
    return event


def verify_chain(session: Session, tenant_id: str) -> tuple[bool, str | None]:
    """Recompute the chain. Returns `(ok, first_broken_event_id)`."""
    events = (
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.tenant_id == tenant_id)
            .order_by(AuditEvent.seq.asc())
        )
        .scalars()
        .all()
    )
    prev_hash = GENESIS_HASH
    expected_seq = 1
    for event in events:
        if event.seq != expected_seq or event.prev_hash != prev_hash:
            return False, event.id
        recomputed = _event_hash(
            prev_hash=event.prev_hash,
            seq=event.seq,
            tenant_id=event.tenant_id,
            occurred_at=event.occurred_at,
            actor_type=event.actor_type,
            actor_id=event.actor_id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            decision=event.decision,
            payload=event.payload,
        )
        if recomputed != event.hash:
            return False, event.id
        prev_hash = event.hash
        expected_seq += 1
    return True, None


def count(session: Session, tenant_id: str) -> int:
    return int(
        session.execute(
            select(func.count(AuditEvent.id)).where(AuditEvent.tenant_id == tenant_id)
        ).scalar_one()
    )
