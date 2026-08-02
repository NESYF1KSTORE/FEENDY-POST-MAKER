"""Operator CLI: `python -m app.cli <command>`.

Commands:
  bootstrap            create the first tenant + platform admin (idempotent)
  demo                 run a full brief → blueprint → PR → release pipeline offline
  worker               run the job worker in the foreground
  verify-audit         recompute the audit hash chain for a tenant
  reconcile            clean up orphan workspaces and expired idempotency keys
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys

from sqlalchemy import select

from app.config import get_settings
from app.core import audit
from app.core.logging import configure, get_logger
from app.core.security import hash_password
from app.db import session_scope
from app.models.base import ProjectState, Role
from app.models.identity import RoleBinding, Tenant, User

log = get_logger("fynix.cli")

ADMIN_ROLES = (
    Role.PLATFORM_ADMIN,
    Role.PROJECT_MANAGER,
    Role.SOLUTION_ARCHITECT,
    Role.CLIENT_OWNER,
    Role.DEVOPS_SRE,
    Role.SECURITY_COMPLIANCE,
    Role.FINANCE_ADMIN,
    Role.AI_OPERATOR,
    Role.QA_ENGINEER,
    Role.DEVELOPER,
)


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Create the first tenant and an admin who can do everything."""
    settings = get_settings()
    email = (args.email or settings.bootstrap_admin_email).lower().strip()
    password = args.password or settings.bootstrap_admin_password or secrets.token_urlsafe(18)

    with session_scope() as session:
        tenant = session.execute(
            select(Tenant).where(Tenant.slug == args.slug)
        ).scalar_one_or_none()
        if tenant is None:
            tenant = Tenant(
                name=args.tenant_name,
                slug=args.slug,
                plan="pilot",
                monthly_budget_rub=args.budget,
            )
            session.add(tenant)
            session.flush()
            print(f"tenant created: {tenant.id} ({tenant.slug})")
        else:
            print(f"tenant exists: {tenant.id} ({tenant.slug})")

        user = session.execute(
            select(User).where(User.tenant_id == tenant.id, User.email == email)
        ).scalar_one_or_none()
        if user is not None:
            print(f"admin already exists: {user.email}")
            return 0

        user = User(
            tenant_id=tenant.id,
            email=email,
            full_name=args.full_name,
            password_hash=hash_password(password),
            mfa_enabled=True,  # privileged actions require MFA (NFR-007)
        )
        session.add(user)
        session.flush()
        for role in ADMIN_ROLES:
            session.add(
                RoleBinding(tenant_id=tenant.id, user_id=user.id, role=role.value, granted_by="cli")
            )
        audit.record(
            session,
            tenant_id=tenant.id,
            action="tenant.bootstrapped",
            actor_type="system",
            actor_id="cli",
            resource_type="user",
            resource_id=user.id,
            payload={"email": email},
        )

    if args.quiet:
        # Deploy transcripts (CI logs, SSH scrollback, screenshots) outlive the
        # session they are printed in, so the password is never echoed there.
        # It is already in .env, which is mode 600 on the server.
        print(f"admin created: {email} (password is in .env — do not print it)")
        return 0

    print("\n--- admin credentials (shown once) ---")
    print(f"email:    {email}")
    print(f"password: {password}")
    print("--- store this in your password manager, then rotate ---\n")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Drive one project through the whole pipeline with the offline provider."""
    from app.models.project import Project
    from app.orchestrator import handlers, jobs
    from app.orchestrator.worker import Worker
    from app.services import budgets as budgets_service

    with session_scope() as session:
        tenant = session.execute(
            select(Tenant).where(Tenant.slug == args.slug)
        ).scalar_one_or_none()
        if tenant is None:
            print(f"tenant '{args.slug}' not found — run `bootstrap` first", file=sys.stderr)
            return 1
        owner = session.execute(
            select(User).where(User.tenant_id == tenant.id)
        ).scalars().first()

        project = Project(
            tenant_id=tenant.id,
            name=args.name,
            golden_path="telegram_bot",
            owner_user_id=owner.id if owner else None,
            budget_rub=args.budget,
            data_class="confidential",
        )
        session.add(project)
        session.flush()
        budgets_service.ensure_budget(
            session, tenant_id=tenant.id, project_id=project.id, limit=args.budget
        )
        jobs.enqueue(
            session,
            tenant_id=tenant.id,
            kind=handlers.INTAKE_ANALYZE,
            project_id=project.id,
            payload={"project_id": project.id, "raw_text": args.brief},
        )
        project_id = project.id
        print(f"project created: {project_id}")

    worker = Worker(worker_id="cli-demo")
    for cycle in range(args.cycles):
        processed = worker.tick()
        if processed:
            print(f"cycle {cycle + 1}: {processed} job(s)")
        elif args.auto_approve and _advance_demo_approvals(project_id):
            print(f"cycle {cycle + 1}: approvals granted automatically (demo mode)")
        else:
            break

    with session_scope() as session:
        project = session.get(Project, project_id)
        print(f"\nfinal state: {project.state}")
        print(
            json.dumps(
                budgets_service.summary(
                    session, tenant_id=project.tenant_id, project_id=project.id
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        if project.state_enum == ProjectState.S2_SOLUTION:
            print(
                "\nProject is waiting for blueprint approval — "
                "approve it in the portal to continue."
            )
    return 0


def _advance_demo_approvals(project_id: str) -> bool:
    """Grant the pending approvals and queue the next stage — demo mode only.

    This exists so `fynix demo` shows the whole path end to end on one machine.
    It creates real approver accounts and goes through the normal approval
    service, so the audit trail records exactly who signed what; nothing about
    the gate logic is bypassed.
    """
    from app.core.security import Principal
    from app.models.project import BlueprintVersion, Project
    from app.orchestrator import handlers, jobs
    from app.orchestrator import state_machine as fsm
    from app.services import approvals as approvals_service

    advanced = False
    with session_scope() as session:
        project = session.get(Project, project_id)
        if project is None:
            return False

        blueprint = session.execute(
            select(BlueprintVersion)
            .where(BlueprintVersion.project_id == project.id)
            .order_by(BlueprintVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()

        subjects: list[tuple[str, str, str]] = []
        if blueprint is not None:
            subjects.append(("blueprint", blueprint.id, blueprint.checksum))
        subjects.append(("contract", project.id, "contract-demo"))

        for subject_type, subject_id, expected in subjects:
            existing = approvals_service.list_for_subject(
                session,
                tenant_id=project.tenant_id,
                subject_type=subject_type,
                subject_id=subject_id,
            )
            if subject_type == "contract" and not existing:
                if project.state_enum != ProjectState.S3_CONTRACT:
                    continue
                existing = approvals_service.request_approvals(
                    session,
                    tenant_id=project.tenant_id,
                    project_id=project.id,
                    subject_type="contract",
                    subject_id=project.id,
                    subject_checksum=expected,
                )

            for _ in range(6):
                pending = approvals_service.actionable(
                    approvals_service.list_for_subject(
                        session,
                        tenant_id=project.tenant_id,
                        subject_type=subject_type,
                        subject_id=subject_id,
                    )
                )
                if not pending:
                    break
                approval = pending[0]
                role = Role(approval.required_role)
                approver = _demo_approver(session, project.tenant_id, role)
                approvals_service.decide(
                    session,
                    principal=Principal(
                        user_id=approver.id,
                        tenant_id=project.tenant_id,
                        email=approver.email,
                        roles=frozenset({role}),
                        mfa=True,
                    ),
                    approval_id=approval.id,
                    approved=True,
                    comment="auto-approved in demo mode",
                )
                advanced = True

        # Move the project to the next state the approvals have unlocked.
        session.refresh(project)
        next_state = {
            ProjectState.S2_SOLUTION: ProjectState.S3_CONTRACT,
            ProjectState.S3_CONTRACT: ProjectState.S4_BOOTSTRAP,
        }.get(project.state_enum)
        if next_state is not None:
            try:
                fsm.transition(
                    session, project=project, target=next_state, actor_type="system",
                    reason="demo mode",
                )
            except Exception:
                return advanced
            advanced = True
            if next_state == ProjectState.S4_BOOTSTRAP:
                jobs.enqueue(
                    session,
                    tenant_id=project.tenant_id,
                    kind=handlers.BOOTSTRAP_PROJECT,
                    project_id=project.id,
                    payload={"project_id": project.id},
                    dedupe_key=f"bootstrap:{project.id}",
                )
    return advanced


def _demo_approver(session, tenant_id: str, role: Role):
    """One dedicated account per role, so dual control still needs two people."""
    from app.core.security import hash_password

    email = f"demo-{role.value}@fynix.local"
    user = session.execute(
        select(User).where(User.tenant_id == tenant_id, User.email == email)
    ).scalar_one_or_none()
    if user is not None:
        return user
    user = User(
        tenant_id=tenant_id,
        email=email,
        full_name=f"Demo {role.value}",
        password_hash=hash_password(secrets.token_urlsafe(18)),
        mfa_enabled=True,
    )
    session.add(user)
    session.flush()
    session.add(
        RoleBinding(tenant_id=tenant_id, user_id=user.id, role=role.value, granted_by="cli-demo")
    )
    session.flush()
    return user


def cmd_worker(_args: argparse.Namespace) -> int:
    from app.orchestrator.worker import Worker

    Worker().run_forever()
    return 0


def cmd_verify_audit(args: argparse.Namespace) -> int:
    with session_scope() as session:
        tenant = session.execute(
            select(Tenant).where(Tenant.slug == args.slug)
        ).scalar_one_or_none()
        if tenant is None:
            print(f"tenant '{args.slug}' not found", file=sys.stderr)
            return 1
        ok, broken = audit.verify_chain(session, tenant.id)
        total = audit.count(session, tenant.id)
    print(f"events: {total}")
    print(f"chain intact: {ok}")
    if not ok:
        print(f"first broken event: {broken}", file=sys.stderr)
        return 2
    return 0


def cmd_reconcile(_args: argparse.Namespace) -> int:
    from app.core import idempotency
    from app.orchestrator import events
    from app.runners import manager as runner_manager

    with session_scope() as session:
        orphans = runner_manager.reconcile_orphans(session)
        purged = idempotency.purge_expired(session)
        dispatched = events.dispatch_pending(session)
    print(json.dumps({"orphans": orphans, "idempotency_purged": purged, **dispatched}))
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    configure(settings.log_level, json_output=settings.is_production)

    parser = argparse.ArgumentParser(prog="fynix", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bootstrap", help="create the first tenant and admin")
    p.add_argument("--slug", default="fynix")
    p.add_argument("--tenant-name", default="FYNIX STUDIO")
    p.add_argument("--email", default="")
    p.add_argument("--password", default="")
    p.add_argument("--full-name", default="Platform Admin")
    p.add_argument("--budget", type=float, default=50000.0)
    p.add_argument(
        "--quiet",
        action="store_true",
        help="do not echo the admin password (use in any automated deploy)",
    )
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("demo", help="run one project end to end offline")
    p.add_argument("--slug", default="fynix")
    p.add_argument("--name", default="Demo Telegram Bot")
    p.add_argument(
        "--brief",
        default="Нужен Telegram-бот для записи клиентов в барбершоп: выбор мастера, "
        "выбор времени, напоминание за час, оплата не нужна. Срок — две недели.",
    )
    p.add_argument("--budget", type=float, default=50000.0)
    p.add_argument("--cycles", type=int, default=40)
    p.add_argument(
        "--no-auto-approve",
        dest="auto_approve",
        action="store_false",
        help="stop at each human approval instead of granting it automatically",
    )
    p.set_defaults(func=cmd_demo, auto_approve=True)

    p = sub.add_parser("worker", help="run the job worker")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("verify-audit", help="recompute the audit hash chain")
    p.add_argument("--slug", default="fynix")
    p.set_defaults(func=cmd_verify_audit)

    p = sub.add_parser("reconcile", help="clean up orphans and dispatch events")
    p.set_defaults(func=cmd_reconcile)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
