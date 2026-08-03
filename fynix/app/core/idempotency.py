"""Idempotency-Key handling for mutating endpoints (spec §9.1, AC-06/AC-11)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.core.hashing import checksum
from app.models.base import utcnow
from app.models.execution import IdempotencyKey

DEFAULT_TTL = timedelta(hours=24)


def lookup(
    session: Session, *, tenant_id: str, key: str, endpoint: str, request_body: dict
) -> IdempotencyKey | None:
    """Return the stored response for a replayed request.

    Same key + same body → the previous response is replayed.
    Same key + different body → 409, because the key would otherwise mask a
    genuinely different request.
    """
    if not key:
        return None
    now = utcnow()
    record = session.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.tenant_id == tenant_id,
            IdempotencyKey.key == key,
            IdempotencyKey.endpoint == endpoint,
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    if record.expires_at <= now:
        session.delete(record)
        session.flush()
        return None
    if record.request_hash != checksum(request_body):
        raise ConflictError(
            "Idempotency-Key was already used with a different request body",
            details={"reason": "idempotency_key_reuse", "key": key},
        )
    return record


def store(
    session: Session,
    *,
    tenant_id: str,
    key: str,
    endpoint: str,
    request_body: dict,
    response_status: int,
    response_body: dict,
    ttl: timedelta = DEFAULT_TTL,
) -> IdempotencyKey | None:
    if not key:
        return None
    record = IdempotencyKey(
        tenant_id=tenant_id,
        key=key,
        endpoint=endpoint,
        request_hash=checksum(request_body),
        response_status=response_status,
        response_body=response_body,
        expires_at=utcnow() + ttl,
    )
    session.add(record)
    try:
        session.flush()
    except IntegrityError:
        # A concurrent request won the race; replay its stored response.
        session.rollback()
        return lookup(
            session,
            tenant_id=tenant_id,
            key=key,
            endpoint=endpoint,
            request_body=request_body,
        )
    return record


def purge_expired(session: Session) -> int:
    now = utcnow()
    rows = (
        session.execute(select(IdempotencyKey).where(IdempotencyKey.expires_at <= now))
        .scalars()
        .all()
    )
    for row in rows:
        session.delete(row)
    return len(rows)
