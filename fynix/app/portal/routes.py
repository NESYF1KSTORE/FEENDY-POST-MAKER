"""Client portal and admin console (FR-022).

Server-rendered with Jinja2 + a small amount of HTMX. There is no build step,
which is the right trade for a single-VPS install: the portal ships with the
API image and cannot drift from it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.core.errors import AuthenticationError
from app.core.security import Principal, create_access_token, decode_access_token, verify_password
from app.core.tenancy import scoped
from app.db import get_sessionmaker
from app.models.base import ApprovalStatus, utcnow
from app.models.delivery import Deployment, Incident, Release
from app.models.execution import AgentRun
from app.models.governance import Approval
from app.models.identity import User
from app.models.project import BlueprintVersion, BriefVersion, Project, Task
from app.orchestrator import dag
from app.orchestrator import state_machine as fsm
from app.services import approvals as approvals_service
from app.services import budgets as budgets_service

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["portal"], include_in_schema=False)

SESSION_COOKIE = "fynix_session"


def _principal_from_cookie(request: Request) -> Principal | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        return decode_access_token(token)
    except AuthenticationError:
        return None


def _redirect_to_login() -> RedirectResponse:
    return RedirectResponse("/portal/login", status_code=303)


@router.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/portal", status_code=307)


@router.get("/portal/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/portal/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    from app.api.deps import _principal_from_user

    session = get_sessionmaker()()
    try:
        user = session.execute(
            select(User).where(User.email == email.lower().strip())
        ).scalars().first()
        if user is None or not verify_password(password, user.password_hash) or not user.is_active:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Неверный email или пароль"},
                status_code=401,
            )
        principal = _principal_from_user(session, user)
        user.last_login_at = utcnow()
        session.commit()
    finally:
        session.close()

    token, expires = create_access_token(principal)
    response = RedirectResponse("/portal", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=int((expires - utcnow()).total_seconds()),
    )
    return response


@router.get("/portal/logout")
def logout():
    response = RedirectResponse("/portal/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/portal", response_class=HTMLResponse)
def dashboard(request: Request):
    principal = _principal_from_cookie(request)
    if principal is None:
        return _redirect_to_login()

    session = get_sessionmaker()()
    try:
        projects = list(
            session.execute(
                scoped(Project, principal.tenant_id).order_by(Project.created_at.desc()).limit(50)
            )
            .scalars()
            .all()
        )
        pending_approvals = list(
            session.execute(
                scoped(Approval, principal.tenant_id)
                .where(Approval.status == ApprovalStatus.PENDING.value)
                .order_by(Approval.created_at.desc())
                .limit(20)
            )
            .scalars()
            .all()
        )
        incidents = list(
            session.execute(
                scoped(Incident, principal.tenant_id)
                .where(Incident.status != "resolved")
                .limit(10)
            )
            .scalars()
            .all()
        )
        rows = []
        for project in projects:
            rows.append(
                {
                    "project": project,
                    "progress": dag.progress(session, project.id),
                    "budget": budgets_service.summary(
                        session, tenant_id=principal.tenant_id, project_id=project.id
                    ),
                }
            )
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "principal": principal,
                "rows": rows,
                "approvals": pending_approvals,
                "incidents": incidents,
            },
        )
    finally:
        session.close()


@router.get("/portal/projects/{project_id}", response_class=HTMLResponse)
def project_detail(request: Request, project_id: str):
    principal = _principal_from_cookie(request)
    if principal is None:
        return _redirect_to_login()

    session = get_sessionmaker()()
    try:
        project = session.execute(
            scoped(Project, principal.tenant_id).where(Project.id == project_id)
        ).scalar_one_or_none()
        if project is None:
            return HTMLResponse("<h1>404</h1><p>Проект не найден</p>", status_code=404)

        briefs = list(
            session.execute(
                select(BriefVersion)
                .where(BriefVersion.project_id == project.id)
                .order_by(BriefVersion.version.desc())
            )
            .scalars()
            .all()
        )
        blueprints = list(
            session.execute(
                select(BlueprintVersion)
                .where(BlueprintVersion.project_id == project.id)
                .order_by(BlueprintVersion.version.desc())
            )
            .scalars()
            .all()
        )
        tasks = list(
            session.execute(
                select(Task).where(Task.project_id == project.id).order_by(Task.priority, Task.key)
            )
            .scalars()
            .all()
        )
        runs = list(
            session.execute(
                select(AgentRun)
                .where(AgentRun.project_id == project.id)
                .order_by(AgentRun.created_at.desc())
                .limit(25)
            )
            .scalars()
            .all()
        )
        releases = list(
            session.execute(
                select(Release)
                .where(Release.project_id == project.id)
                .order_by(Release.created_at.desc())
            )
            .scalars()
            .all()
        )
        deployments = list(
            session.execute(
                select(Deployment)
                .where(Deployment.project_id == project.id)
                .order_by(Deployment.created_at.desc())
                .limit(10)
            )
            .scalars()
            .all()
        )
        project_approvals = list(
            session.execute(
                scoped(Approval, principal.tenant_id)
                .where(Approval.project_id == project.id)
                .order_by(Approval.created_at.desc())
            )
            .scalars()
            .all()
        )
        verdict = fsm.evaluate_gate(session, project)

        return templates.TemplateResponse(
            request,
            "project.html",
            {
                "principal": principal,
                "project": project,
                "briefs": briefs,
                "blueprints": blueprints,
                "tasks": tasks,
                "runs": runs,
                "releases": releases,
                "deployments": deployments,
                "approvals": project_approvals,
                "gate": verdict,
                "progress": dag.progress(session, project.id),
                "budget": budgets_service.summary(
                    session, tenant_id=principal.tenant_id, project_id=project.id
                ),
            },
        )
    finally:
        session.close()


@router.post("/portal/approvals/{approval_id}/decide")
def decide(request: Request, approval_id: str, decision: str = Form(...), comment: str = Form("")):
    principal = _principal_from_cookie(request)
    if principal is None:
        return _redirect_to_login()

    session = get_sessionmaker()()
    try:
        approval = session.get(Approval, approval_id)
        redirect_to = (
            f"/portal/projects/{approval.project_id}" if approval is not None else "/portal"
        )
        approvals_service.decide(
            session,
            principal=principal,
            approval_id=approval_id,
            approved=decision == "approve",
            comment=comment,
        )
        session.commit()
        return RedirectResponse(redirect_to, status_code=303)
    except Exception as exc:
        session.rollback()
        return HTMLResponse(
            f"<h1>Не удалось применить решение</h1><pre>{exc}</pre>"
            '<p><a href="/portal">Назад</a></p>',
            status_code=409,
        )
    finally:
        session.close()
