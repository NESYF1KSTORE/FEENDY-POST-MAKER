"""Tenant, user and role administration (FR-001)."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentPrincipal, DbSession
from app.api.schemas import RoleGrant, TenantCreate, UserCreate, UserOut
from app.core import audit
from app.core.errors import ConflictError, ValidationError
from app.core.policy import Resource, authorize
from app.core.security import generate_api_token, hash_password
from app.core.tenancy import scoped
from app.models.base import Role
from app.models.identity import ApiToken, RoleBinding, Tenant, User

router = APIRouter(prefix="/v1/admin", tags=["admin"])


@router.post("/tenants", status_code=201)
def create_tenant(payload: TenantCreate, principal: CurrentPrincipal, session: DbSession):
    authorize(principal, "tenant:manage", Resource(type="tenant"))
    existing = session.execute(
        select(Tenant).where(Tenant.slug == payload.slug)
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"tenant slug '{payload.slug}' is taken")

    tenant = Tenant(
        name=payload.name,
        slug=payload.slug,
        plan=payload.plan,
        max_external_data_class=payload.max_external_data_class,
        monthly_budget_rub=payload.monthly_budget_rub,
    )
    session.add(tenant)
    session.flush()
    audit.record(
        session,
        tenant_id=tenant.id,
        action="tenant.created",
        actor_id=principal.user_id,
        resource_type="tenant",
        resource_id=tenant.id,
        payload={"slug": tenant.slug, "plan": tenant.plan},
    )
    return {"id": tenant.id, "slug": tenant.slug, "name": tenant.name}


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(payload: UserCreate, principal: CurrentPrincipal, session: DbSession):
    """Create a user in the caller's tenant and grant the requested roles."""
    authorize(
        principal, "user:grant_role", Resource(type="user", tenant_id=principal.tenant_id)
    )
    email = payload.email.lower().strip()
    existing = session.execute(
        select(User).where(User.tenant_id == principal.tenant_id, User.email == email)
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"user '{email}' already exists in this tenant")

    roles = []
    for name in payload.roles:
        try:
            roles.append(Role(name))
        except ValueError as exc:
            raise ValidationError(f"unknown role '{name}'") from exc

    user = User(
        tenant_id=principal.tenant_id,
        email=email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        mfa_enabled=payload.mfa_enabled,
    )
    session.add(user)
    session.flush()

    for role in roles:
        session.add(
            RoleBinding(
                tenant_id=principal.tenant_id,
                user_id=user.id,
                role=role.value,
                granted_by=principal.user_id,
            )
        )
    session.flush()

    audit.record(
        session,
        tenant_id=principal.tenant_id,
        action="user.created",
        actor_id=principal.user_id,
        resource_type="user",
        resource_id=user.id,
        payload={"email": email, "roles": [r.value for r in roles]},
    )
    return user


@router.get("/users", response_model=list[UserOut])
def list_users(principal: CurrentPrincipal, session: DbSession):
    authorize(principal, "project:read", Resource(type="user", tenant_id=principal.tenant_id))
    return list(session.execute(scoped(User, principal.tenant_id)).scalars().all())


@router.post("/roles:grant", status_code=201)
def grant_role(payload: RoleGrant, principal: CurrentPrincipal, session: DbSession):
    authorize(
        principal,
        "user:grant_role",
        Resource(type="user", tenant_id=principal.tenant_id, project_id=payload.project_id),
    )
    try:
        role = Role(payload.role)
    except ValueError as exc:
        raise ValidationError(f"unknown role '{payload.role}'") from exc

    user = session.execute(
        select(User).where(
            User.id == payload.user_id, User.tenant_id == principal.tenant_id
        )
    ).scalar_one_or_none()
    if user is None:
        raise ValidationError("user not found in this tenant")

    existing = session.execute(
        select(RoleBinding).where(
            RoleBinding.user_id == user.id,
            RoleBinding.role == role.value,
            RoleBinding.project_id == payload.project_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"id": existing.id, "role": role.value, "project_id": payload.project_id}

    binding = RoleBinding(
        tenant_id=principal.tenant_id,
        user_id=user.id,
        role=role.value,
        project_id=payload.project_id,
        expires_at=payload.expires_at,
        granted_by=principal.user_id,
    )
    session.add(binding)
    session.flush()
    audit.record(
        session,
        tenant_id=principal.tenant_id,
        action="user.role_granted",
        actor_id=principal.user_id,
        resource_type="user",
        resource_id=user.id,
        payload={
            "role": role.value,
            "project_id": payload.project_id,
            "expires_at": payload.expires_at.isoformat() if payload.expires_at else None,
        },
    )
    return {"id": binding.id, "role": role.value, "project_id": payload.project_id}


@router.post("/tokens", status_code=201)
def create_api_token(
    name: str, principal: CurrentPrincipal, session: DbSession, expires_days: int = 90
):
    """Issue a service-account token. The plaintext is shown exactly once."""
    from datetime import timedelta

    from app.models.base import utcnow

    authorize(
        principal, "user:grant_role", Resource(type="api_token", tenant_id=principal.tenant_id)
    )
    raw, token_hash = generate_api_token()
    token = ApiToken(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        name=name,
        token_hash=token_hash,
        expires_at=utcnow() + timedelta(days=expires_days),
    )
    session.add(token)
    session.flush()
    audit.record(
        session,
        tenant_id=principal.tenant_id,
        action="api_token.created",
        actor_id=principal.user_id,
        resource_type="api_token",
        resource_id=token.id,
        payload={"name": name, "expires_at": token.expires_at.isoformat()},
    )
    return {"id": token.id, "name": name, "token": raw, "expires_at": token.expires_at}


@router.post("/tokens/{token_id}:revoke")
def revoke_api_token(token_id: str, principal: CurrentPrincipal, session: DbSession):
    from app.core.tenancy import get_scoped
    from app.models.base import utcnow

    authorize(
        principal, "user:grant_role", Resource(type="api_token", tenant_id=principal.tenant_id)
    )
    token = get_scoped(session, ApiToken, principal.tenant_id, token_id)
    token.revoked_at = utcnow()
    session.flush()
    audit.record(
        session,
        tenant_id=principal.tenant_id,
        action="api_token.revoked",
        actor_id=principal.user_id,
        resource_type="api_token",
        resource_id=token.id,
    )
    return {"id": token.id, "revoked_at": token.revoked_at}


@router.get("/agents")
def list_agents(principal: CurrentPrincipal):
    """Catalogue of agent types and their contracts."""
    from app.agents import registry

    authorize(principal, "run:read", Resource(type="agent", tenant_id=principal.tenant_id))
    return {"agents": registry.describe()}
