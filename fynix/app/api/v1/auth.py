"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentPrincipal, DbSession, _principal_from_user
from app.api.schemas import LoginRequest, TokenResponse, UserOut
from app.core import audit
from app.core.errors import AuthenticationError
from app.core.security import create_access_token, hash_password, needs_rehash, verify_password
from app.models.base import utcnow
from app.models.identity import User

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, session: DbSession) -> TokenResponse:
    user = session.execute(
        select(User).where(User.email == payload.email.lower().strip())
    ).scalars().first()

    # Always run a verification so a missing user and a wrong password take a
    # comparable amount of time.
    stored_hash = user.password_hash if user else ""
    ok = verify_password(payload.password, stored_hash)
    if user is None or not ok or not user.is_active:
        if user is not None:
            audit.record(
                session,
                tenant_id=user.tenant_id,
                action="auth.login_failed",
                actor_id=user.id,
                resource_type="user",
                resource_id=user.id,
                decision="deny",
                payload={"email": payload.email},
            )
        raise AuthenticationError("invalid credentials")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password)

    user.last_login_at = utcnow()
    principal = _principal_from_user(session, user)
    token, expires = create_access_token(principal)

    audit.record(
        session,
        tenant_id=user.tenant_id,
        action="auth.login",
        actor_id=user.id,
        resource_type="user",
        resource_id=user.id,
    )
    return TokenResponse(
        access_token=token,
        expires_at=expires,
        tenant_id=user.tenant_id,
        roles=sorted(r.value for r in principal.roles),
    )


@router.get("/me", response_model=UserOut)
def me(principal: CurrentPrincipal, session: DbSession) -> User:
    user = session.get(User, principal.user_id)
    if user is None:
        raise AuthenticationError("user not found")
    return user
