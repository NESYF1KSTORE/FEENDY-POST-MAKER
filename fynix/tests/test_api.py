"""HTTP surface: auth, idempotency, tenant isolation and error shape."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_sessionmaker
from app.main import create_app
from app.models.base import Role
from app.models.identity import Tenant
from tests.conftest import make_user

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def api_admin(session, tenant):
    user = make_user(session, tenant, "api-admin@fynix.local", [Role.PLATFORM_ADMIN])
    session.commit()
    return user


def _login(client: TestClient, email: str) -> str:
    response = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- auth -----------------------------------------------------------------


def test_login_and_me(client, api_admin):
    token = _login(client, api_admin.email)
    response = client.get("/v1/auth/me", headers=_auth(token))
    assert response.status_code == 200
    assert response.json()["email"] == api_admin.email


def test_wrong_password_is_rejected(client, api_admin):
    response = client.post(
        "/v1/auth/login", json={"email": api_admin.email, "password": "wrong-password-here"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_unauthenticated_request_is_refused(client):
    assert client.get("/v1/projects").status_code == 401


def test_health_and_readiness(client):
    assert client.get("/health").status_code == 200
    assert client.get("/health/ready").json()["status"] == "ready"


def test_metrics_are_prometheus_shaped(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "fynix_jobs_total" in response.text


def test_correlation_id_is_echoed(client):
    response = client.get("/health", headers={"X-Correlation-Id": "abc123"})
    assert response.headers["X-Correlation-Id"] == "abc123"


# --- projects and idempotency --------------------------------------------


def test_create_project_and_read_it_back(client, api_admin):
    token = _login(client, api_admin.email)
    created = client.post(
        "/v1/projects",
        json={"name": "Барбершоп бот", "golden_path": "telegram_bot", "budget_rub": 50000},
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]

    detail = client.get(f"/v1/projects/{project_id}", headers=_auth(token))
    assert detail.status_code == 200
    body = detail.json()
    assert body["state"] == "S0_LEAD"
    assert body["budget"]["limit"] == 50000
    assert "gate" in body and "progress" in body


def test_idempotency_key_replays_the_same_project(client, api_admin):
    """AC-06/AC-11: retrying a create must not produce a second project."""
    token = _login(client, api_admin.email)
    payload = {"name": "Idempotent", "golden_path": "landing"}
    headers = {**_auth(token), "Idempotency-Key": "key-1"}

    first = client.post("/v1/projects", json=payload, headers=headers)
    second = client.post("/v1/projects", json=payload, headers=headers)

    assert first.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.headers.get("Idempotent-Replay") == "true"

    listed = client.get("/v1/projects", headers=_auth(token)).json()
    assert len([p for p in listed if p["name"] == "Idempotent"]) == 1


def test_reusing_a_key_with_a_different_body_is_a_conflict(client, api_admin):
    token = _login(client, api_admin.email)
    headers = {**_auth(token), "Idempotency-Key": "key-2"}
    client.post("/v1/projects", json={"name": "First"}, headers=headers)
    conflict = client.post("/v1/projects", json={"name": "Second"}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["details"]["reason"] == "idempotency_key_reuse"


def test_brief_submission_returns_an_operation_handle(client, api_admin):
    token = _login(client, api_admin.email)
    project_id = client.post(
        "/v1/projects", json={"name": "Brief flow"}, headers=_auth(token)
    ).json()["id"]

    accepted = client.post(
        f"/v1/projects/{project_id}/briefs",
        json={"raw_text": "Нужен бот для записи клиентов"},
        headers=_auth(token),
    )
    assert accepted.status_code == 202
    operation_id = accepted.json()["id"]

    polled = client.get(f"/v1/operations/{operation_id}", headers=_auth(token))
    assert polled.status_code == 200
    assert polled.json()["kind"] == "brief.analyze"


# --- authorisation --------------------------------------------------------


def test_role_without_permission_gets_403(client, session, tenant):
    qa = make_user(session, tenant, "qa-api@fynix.local", [Role.QA_ENGINEER])
    session.commit()
    token = _login(client, qa.email)

    response = client.post("/v1/projects", json={"name": "Nope"}, headers=_auth(token))
    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "missing_permission"


def test_mfa_is_required_for_a_budget_override(client, session, tenant):
    finance = make_user(
        session, tenant, "finance@fynix.local", [Role.FINANCE_ADMIN, Role.PROJECT_MANAGER], mfa=False
    )
    session.commit()
    token = _login(client, finance.email)

    project_id = client.post(
        "/v1/projects", json={"name": "Budget flow", "budget_rub": 1000}, headers=_auth(token)
    ).json()["id"]

    response = client.post(
        f"/v1/projects/{project_id}/budget:override",
        json={"amount": 5000, "hours": 24, "reason": "клиент подтвердил доплату письменно"},
        headers=_auth(token),
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "mfa_required"


def test_a_project_of_another_tenant_is_not_found(client, session, tenant, api_admin):
    """AC-09: cross-tenant reads are indistinguishable from a missing resource."""
    token = _login(client, api_admin.email)
    project_id = client.post(
        "/v1/projects", json={"name": "Tenant A project"}, headers=_auth(token)
    ).json()["id"]

    other_tenant = Tenant(name="Other", slug="other-api")
    session.add(other_tenant)
    session.flush()
    intruder = make_user(session, other_tenant, "intruder@other.local", [Role.PLATFORM_ADMIN])
    session.commit()

    intruder_token = _login(client, intruder.email)
    response = client.get(f"/v1/projects/{project_id}", headers=_auth(intruder_token))
    assert response.status_code == 404

    listed = client.get("/v1/projects", headers=_auth(intruder_token)).json()
    assert listed == []


def test_revoked_role_takes_effect_without_waiting_for_token_expiry(client, session, tenant):
    """Roles are re-read per request, so a revoked binding is immediate."""
    from sqlalchemy import select

    from app.models.identity import RoleBinding

    manager = make_user(session, tenant, "pm-api@fynix.local", [Role.PROJECT_MANAGER])
    session.commit()
    token = _login(client, manager.email)
    assert client.post("/v1/projects", json={"name": "Before"}, headers=_auth(token)).status_code == 201

    db = get_sessionmaker()()
    binding = db.execute(
        select(RoleBinding).where(RoleBinding.user_id == manager.id)
    ).scalar_one()
    db.delete(binding)
    db.commit()
    db.close()

    after = client.post("/v1/projects", json={"name": "After"}, headers=_auth(token))
    assert after.status_code == 403


def test_api_token_authenticates_and_can_be_revoked(client, api_admin):
    token = _login(client, api_admin.email)
    issued = client.post("/v1/admin/tokens?name=ci", headers=_auth(token))
    assert issued.status_code == 201
    pat = issued.json()["token"]
    assert pat.startswith("fynix_pat_")

    assert client.get("/v1/auth/me", headers=_auth(pat)).status_code == 200

    revoked = client.post(
        f"/v1/admin/tokens/{issued.json()['id']}:revoke", headers=_auth(token)
    )
    assert revoked.status_code == 200
    assert client.get("/v1/auth/me", headers=_auth(pat)).status_code == 401


# --- audit ----------------------------------------------------------------


def test_audit_search_and_chain_verification(client, api_admin):
    token = _login(client, api_admin.email)
    client.post("/v1/projects", json={"name": "Audited"}, headers=_auth(token))

    found = client.get("/v1/audit?action=project.created", headers=_auth(token))
    assert found.status_code == 200
    assert found.json()["items"], "project creation must be audited"

    verified = client.get("/v1/audit:verify", headers=_auth(token))
    assert verified.json()["intact"] is True


def test_agent_catalogue_is_exposed(client, api_admin):
    token = _login(client, api_admin.email)
    response = client.get("/v1/admin/agents", headers=_auth(token))
    assert response.status_code == 200
    types = {a["agent_type"] for a in response.json()["agents"]}
    assert {"code", "review", "security", "planner"} <= types


def test_dashboard_reports_slis(client, api_admin):
    token = _login(client, api_admin.email)
    client.post("/v1/projects", json={"name": "Dash"}, headers=_auth(token))
    body = client.get("/v1/dashboard", headers=_auth(token)).json()
    assert body["projects"]["active"] >= 1
    assert "workflow" in body and "ai" in body and "cost" in body


def test_webhook_secret_is_returned_once(client, api_admin):
    token = _login(client, api_admin.email)
    created = client.post(
        "/v1/webhooks",
        json={"url": "https://example.invalid/hook", "event_types": ["release.candidate.created"]},
        headers=_auth(token),
    )
    assert created.status_code == 201
    assert created.json()["secret"]

    listed = client.get("/v1/webhooks", headers=_auth(token)).json()
    assert "secret" not in listed[0]


def test_openapi_document_is_served(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for expected in (
        "/v1/projects",
        "/v1/releases",
        "/v1/deployments",
        "/v1/operations/{operation_id}",
    ):
        assert expected in paths


# --- portal ---------------------------------------------------------------


def test_portal_redirects_anonymous_users_to_login(client):
    response = client.get("/portal", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/portal/login"


def test_portal_login_sets_an_httponly_cookie(client, api_admin):
    response = client.post(
        "/portal/login",
        data={"email": api_admin.email, "password": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    cookie = response.headers.get("set-cookie", "")
    assert "fynix_session=" in cookie
    assert "HttpOnly" in cookie


def test_portal_dashboard_renders_for_a_logged_in_user(client, api_admin):
    client.post(
        "/portal/login", data={"email": api_admin.email, "password": PASSWORD}
    )
    response = client.get("/portal")
    assert response.status_code == 200
    assert "Портфель проектов" in response.text
