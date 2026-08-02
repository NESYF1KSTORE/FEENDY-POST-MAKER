"""Tenant isolation helpers (NFR-006, AC-09).

Every read of a tenant-scoped table goes through `scoped()` so the tenant filter
cannot be forgotten. Cache and storage keys are namespaced the same way, because
a shared cache key is just as much a cross-tenant leak as a missing WHERE clause.
"""

from __future__ import annotations

from typing import TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError

T = TypeVar("T")


def scoped(model: type[T], tenant_id: str) -> Select:
    """`SELECT * FROM model WHERE tenant_id = :tenant_id`."""
    if not tenant_id:
        raise ValueError("tenant_id is required for a tenant-scoped query")
    if not hasattr(model, "tenant_id"):
        raise TypeError(f"{model.__name__} is not tenant-scoped")
    return select(model).where(model.tenant_id == tenant_id)


def get_scoped(session: Session, model: type[T], tenant_id: str, obj_id: str) -> T:
    """Fetch by id within the tenant, or raise 404.

    A row belonging to another tenant is reported as *not found* rather than
    *forbidden*, so the API does not confirm the existence of other tenants'
    resources.
    """
    obj = session.execute(scoped(model, tenant_id).where(model.id == obj_id)).scalar_one_or_none()
    if obj is None:
        raise NotFoundError(f"{model.__name__.lower()} '{obj_id}' not found")
    return obj


def cache_key(tenant_id: str, *parts: str) -> str:
    """Namespaced cache key. Prompt/context caches must never be shared (§5.3)."""
    if not tenant_id:
        raise ValueError("tenant_id is required for a cache key")
    return ":".join(["fynix", tenant_id, *[str(p) for p in parts]])


def storage_prefix(tenant_id: str, project_id: str = "") -> str:
    """Object-storage prefix. Mirrors the cache namespacing rule."""
    if not tenant_id:
        raise ValueError("tenant_id is required for a storage prefix")
    return f"{tenant_id}/{project_id}/" if project_id else f"{tenant_id}/"
