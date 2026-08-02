"""Policy engine, tenant isolation and audit integrity (AC-01, AC-09, NFR-006/009)."""

from __future__ import annotations

import pytest

from app.core import audit
from app.core.errors import NotFoundError, PermissionDenied
from app.core.policy import Resource, authorize, evaluate, resolve_tool_grants
from app.core.security import Principal
from app.core.tenancy import cache_key, get_scoped, storage_prefix
from app.models.base import DataClass, Role
from app.models.identity import Tenant
from app.models.project import Project
from tests.conftest import make_principal, make_user


def test_deny_by_default_without_role_binding(tenant):
    stranger = Principal(user_id="u1", tenant_id=tenant.id, roles=frozenset())
    decision = evaluate(stranger, "project:read", Resource(type="project", tenant_id=tenant.id))
    assert not decision.allowed
    assert decision.reason == "no_role_binding"


def test_cross_tenant_access_is_refused_before_any_role_check(tenant):
    principal = Principal(
        user_id="u1", tenant_id="other-tenant", roles=frozenset({Role.PLATFORM_ADMIN})
    )
    decision = evaluate(principal, "project:read", Resource(type="project", tenant_id=tenant.id))
    assert not decision.allowed
    assert decision.reason == "cross_tenant_access"


def test_privileged_action_requires_mfa(session, tenant, admin):
    no_mfa = make_principal(admin, [Role.DEVOPS_SRE], mfa=False)
    decision = evaluate(
        no_mfa,
        "deployment:approve_prod",
        Resource(type="deployment", tenant_id=tenant.id, environment="prod"),
    )
    assert not decision.allowed
    assert decision.reason == "mfa_required"

    with_mfa = make_principal(admin, [Role.DEVOPS_SRE], mfa=True)
    assert evaluate(
        with_mfa,
        "deployment:approve_prod",
        Resource(type="deployment", tenant_id=tenant.id, environment="prod"),
    ).allowed


def test_restricted_data_is_not_readable_by_a_broad_role(session, tenant, admin):
    developer = make_principal(admin, [Role.DEVELOPER])
    decision = evaluate(
        developer,
        "project:read",
        Resource(type="project", tenant_id=tenant.id, data_class=DataClass.RESTRICTED),
    )
    assert not decision.allowed
    assert decision.reason == "restricted_data_class"


def test_project_scoped_role_does_not_leak_to_other_projects(tenant):
    principal = Principal(
        user_id="u1",
        tenant_id=tenant.id,
        roles=frozenset(),
        project_roles={"prj_a": frozenset({Role.DEVELOPER})},
    )
    allowed = evaluate(
        principal,
        "task:read",
        Resource(type="task", tenant_id=tenant.id, project_id="prj_a"),
    )
    denied = evaluate(
        principal,
        "task:read",
        Resource(type="task", tenant_id=tenant.id, project_id="prj_b"),
    )
    assert allowed.allowed
    assert not denied.allowed


def test_forbidden_tools_are_never_grantable():
    with pytest.raises(PermissionDenied) as exc:
        resolve_tool_grants("code", ["read_repo", "read_secret_value"])
    assert exc.value.details["reason"] == "forbidden_tool"

    # Anything outside the agent's own allowlist is silently dropped.
    assert resolve_tool_grants("code", ["read_repo", "read_evidence"]) == ["read_repo"]


def test_get_scoped_reports_other_tenants_rows_as_not_found(session, tenant, project):
    other = Tenant(name="Other", slug="other")
    session.add(other)
    session.flush()

    with pytest.raises(NotFoundError):
        get_scoped(session, Project, other.id, project.id)

    assert get_scoped(session, Project, tenant.id, project.id).id == project.id


def test_cache_and_storage_keys_are_tenant_namespaced(tenant):
    assert cache_key(tenant.id, "prompt", "abc").startswith(f"fynix:{tenant.id}:")
    assert storage_prefix(tenant.id, "prj_1") == f"{tenant.id}/prj_1/"
    with pytest.raises(ValueError):
        cache_key("", "prompt")


def test_authorize_raises_with_a_machine_readable_reason(tenant):
    principal = Principal(user_id="u1", tenant_id=tenant.id, roles=frozenset({Role.QA_ENGINEER}))
    with pytest.raises(PermissionDenied) as exc:
        authorize(principal, "budget:override", Resource(type="budget", tenant_id=tenant.id))
    assert exc.value.details["reason"] == "missing_permission"


# --- audit chain ---------------------------------------------------------


def test_audit_chain_is_verifiable(session, tenant):
    for i in range(5):
        audit.record(
            session,
            tenant_id=tenant.id,
            action="test.event",
            actor_id="u1",
            resource_type="thing",
            resource_id=f"t{i}",
        )
    ok, broken = audit.verify_chain(session, tenant.id)
    assert ok and broken is None
    assert audit.count(session, tenant.id) == 5


def test_tampering_with_an_audit_row_breaks_the_chain(session, tenant):
    for i in range(3):
        audit.record(session, tenant_id=tenant.id, action="test.event", resource_id=f"t{i}")
    session.flush()

    from sqlalchemy import select

    from app.models.governance import AuditEvent

    victim = session.execute(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant.id, AuditEvent.seq == 2)
    ).scalar_one()
    victim.action = "test.rewritten"
    session.flush()

    ok, broken = audit.verify_chain(session, tenant.id)
    assert not ok
    assert broken == victim.id


def test_audit_payload_is_redacted(session, tenant):
    event = audit.record(
        session,
        tenant_id=tenant.id,
        action="test.secret",
        payload={"api_key": "sk-ant-abcdefghijklmnopqrstuvwxyz123456", "note": "ok"},
    )
    assert event.payload["api_key"] == "«redacted»"
    assert event.payload["note"] == "ok"


def test_users_from_different_tenants_do_not_share_an_audit_sequence(session, tenant):
    other = Tenant(name="Other", slug="other2")
    session.add(other)
    session.flush()

    audit.record(session, tenant_id=tenant.id, action="a")
    audit.record(session, tenant_id=other.id, action="b")
    audit.record(session, tenant_id=tenant.id, action="c")

    assert audit.count(session, tenant.id) == 2
    assert audit.count(session, other.id) == 1
    assert audit.verify_chain(session, tenant.id)[0]
    assert audit.verify_chain(session, other.id)[0]


def test_make_user_helper_grants_expected_roles(session, tenant):
    user = make_user(session, tenant, "qa@fynix.local", [Role.QA_ENGINEER])
    assert user.tenant_id == tenant.id
