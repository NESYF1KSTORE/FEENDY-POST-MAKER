"""FastAPI dependencies: authentication, tenancy and idempotency."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AuthenticationError
from app.core.security import Principal, decode_access_token, hash_api_token
from app.db import get_db
from app.models.base import Role, utcnow
from app.models.identity import ApiToken, RoleBinding, User

DbSession = Annotated[Session, Depends(get_db)]


def _principal_from_user(session: Session, user: User) -> Principal:
    now = utcnow()
    bindings = (
        session.execute(select(RoleBinding).where(RoleBinding.user_id == user.id))
        .scalars()
        .all()
    )
    tenant_roles: set[Role] = set()
    project_roles: dict[str, set[Role]] = {}
    for binding in bindings:
        if binding.expires_at is not None and binding.expires_at <= now:
            continue
        try:
            role = Role(binding.role)
        except ValueError:
            continue
        if binding.project_id:
            project_roles.setdefault(binding.project_id, set()).add(role)
        else:
            tenant_roles.add(role)

    return Principal(
        user_id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        roles=frozenset(tenant_roles),
        project_roles={pid: frozenset(roles) for pid, roles in project_roles.items()},
        is_service_account=user.is_service_account,
        mfa=user.mfa_enabled,
    )


def get_principal(
    request: Request,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Accept either a bearer JWT or a service-account API token."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("missing bearer token")
    raw = authorization.split(" ", 1)[1].strip()

    if raw.startswith("fynix_pat_"):
        token = session.execute(
            select(ApiToken).where(ApiToken.token_hash == hash_api_token(raw))
        ).scalar_one_or_none()
        now = utcnow()
        if (
            token is None
            or token.revoked_at is not None
            or (token.expires_at is not None and token.expires_at <= now)
        ):
            raise AuthenticationError("invalid or revoked API token")
        user = session.get(User, token.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("token owner is inactive")
        token.last_used_at = now
        principal = _principal_from_user(session, user)
    else:
        principal = decode_access_token(raw)
        user = session.get(User, principal.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("user is inactive")
        # Roles are re-read from the database so a revoked binding takes effect
        # immediately instead of living until the token expires.
        principal = _principal_from_user(session, user)

    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]
