"""Shared fixtures. Tests run entirely offline against SQLite."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

TMP_ROOT = Path(tempfile.mkdtemp(prefix="fynix-tests-"))

os.environ.update(
    FYNIX_ENV="test",
    DATABASE_URL=f"sqlite:///{TMP_ROOT / 'test.db'}",
    JWT_SECRET="test-secret-that-is-long-enough-for-tests-0123456789",
    AI_DEFAULT_PROVIDER="mock",
    ARTIFACT_DIR=str(TMP_ROOT / "artifacts"),
    RUNNER_WORKSPACE_ROOT=str(TMP_ROOT / "workspaces"),
    RUNNER_DRIVER="local",
    TELEGRAM_BOT_TOKEN="",
    DEFAULT_PROJECT_BUDGET_RUB="50000",
    FYNIX_LOG_LEVEL="WARNING",
)

from app.config import reset_settings_cache
from app.core.security import Principal, hash_password
from app.db import get_engine, get_sessionmaker, reset_engine
from app.models import Base, Role
from app.models.identity import RoleBinding, Tenant, User
from app.models.project import Project
from app.orchestrator import events
from app.services import budgets as budgets_service

reset_settings_cache()
reset_engine()


@pytest.fixture(autouse=True)
def _clean_database():
    """Every test starts from an empty schema."""
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    events.clear_subscribers()
    from app.ai.gateway import gateway

    gateway.clear_cache()
    yield


@pytest.fixture
def session():
    db = get_sessionmaker()()
    try:
        yield db
        db.commit()
    finally:
        db.close()


@pytest.fixture
def tenant(session) -> Tenant:
    row = Tenant(name="FYNIX STUDIO", slug="fynix", max_external_data_class="internal")
    session.add(row)
    session.flush()
    return row


def make_user(
    session,
    tenant: Tenant,
    email: str,
    roles: list[Role],
    *,
    mfa: bool = True,
    project_id: str | None = None,
) -> User:
    user = User(
        tenant_id=tenant.id,
        email=email,
        full_name=email.split("@")[0],
        password_hash=hash_password("correct-horse-battery-staple"),
        mfa_enabled=mfa,
    )
    session.add(user)
    session.flush()
    for role in roles:
        session.add(
            RoleBinding(
                tenant_id=tenant.id, user_id=user.id, role=role.value, project_id=project_id
            )
        )
    session.flush()
    return user


def make_principal(user: User, roles: list[Role], *, mfa: bool = True) -> Principal:
    return Principal(
        user_id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        roles=frozenset(roles),
        mfa=mfa,
    )


@pytest.fixture
def admin(session, tenant) -> User:
    return make_user(session, tenant, "admin@fynix.local", [Role.PLATFORM_ADMIN])


@pytest.fixture
def admin_principal(admin) -> Principal:
    return make_principal(admin, [Role.PLATFORM_ADMIN])


@pytest.fixture
def project(session, tenant, admin) -> Project:
    row = Project(
        tenant_id=tenant.id,
        name="Demo bot",
        golden_path="telegram_bot",
        owner_user_id=admin.id,
        budget_rub=50000.0,
        data_class="confidential",
    )
    session.add(row)
    session.flush()
    budgets_service.ensure_budget(
        session, tenant_id=tenant.id, project_id=row.id, limit=50000.0
    )
    return row
