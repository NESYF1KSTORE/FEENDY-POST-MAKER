"""Tenants, users, role bindings and service accounts (spec §8, FR-001)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    DataClass,
    Role,
    TenantScopedMixin,
    TimestampMixin,
    TZDateTime,
    new_id,
)


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ten"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(40), default="pilot", nullable=False)
    data_region: Mapped[str] = mapped_column(String(40), default="ru-central", nullable=False)
    # Highest classification this tenant permits to leave the perimeter towards
    # an external model provider (spec §8.1).
    max_external_data_class: Mapped[str] = mapped_column(
        String(20), default=DataClass.INTERNAL.value, nullable=False
    )
    quotas: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    policy_set: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    monthly_budget_rub: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    users: Mapped[list[User]] = relationship(back_populates="tenant")


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("usr"))
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_service_account: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # MFA is mandatory for privileged roles (NFR-007); enforcement lives in
    # app.core.policy so the flag alone is not a security decision.
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    telegram_chat_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    tenant: Mapped[Tenant] = relationship(back_populates="users")
    role_bindings: Mapped[list[RoleBinding]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RoleBinding(Base, TimestampMixin, TenantScopedMixin):
    """A role granted either tenant-wide (project_id NULL) or per project."""

    __tablename__ = "role_bindings"
    __table_args__ = (
        UniqueConstraint("user_id", "role", "project_id", name="uq_binding_scope"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("rb"))
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    granted_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    user: Mapped[User] = relationship(back_populates="role_bindings")

    @property
    def role_enum(self) -> Role:
        return Role(self.role)


class ApiToken(Base, TimestampMixin, TenantScopedMixin):
    """Long-lived credential for service accounts (FR-025). Only the hash is kept."""

    __tablename__ = "api_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("tok"))
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(TZDateTime)
