"""Telegram bot: linking, authorisation, dialogues and approval boundaries."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.errors import PermissionDenied, ValidationError
from app.models.base import ApprovalStatus, Role, utcnow
from app.models.project import Project
from app.models.telegram import TelegramChat
from app.services import approvals as approvals_service
from app.telegram import handlers, linking
from app.telegram.client import Button, chunk, escape, keyboard
from tests.conftest import make_principal, make_user

CHAT = "555000111"


def _message(text: str, chat_id: str = CHAT, username: str = "client") -> dict:
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": chat_id},
            "from": {"username": username},
            "text": text,
        },
    }


def _callback(data: str, chat_id: str = CHAT) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb1",
            "data": data,
            "message": {"chat": {"id": chat_id}},
        },
    }


def _link(session, tenant, user) -> TelegramChat:
    code = linking.issue_code(session, tenant_id=tenant.id, user_id=user.id)
    return linking.redeem_code(session, code=code.code, chat_id=CHAT)


@pytest.fixture
def manager(session, tenant):
    return make_user(session, tenant, "pm-tg@fynix.local", [Role.PROJECT_MANAGER])


# --- linking --------------------------------------------------------------


def test_unlinked_chat_gets_only_instructions(session):
    _, reply = handlers.handle_update(session, _message("/projects"))
    assert "не привязан" in reply.text
    assert "/link" in reply.text


def test_unlinked_chat_cannot_create_a_project(session):
    _, reply = handlers.handle_update(session, _message("/new Секретный проект"))
    assert "не привязан" in reply.text
    assert session.query(Project).count() == 0


def test_link_code_binds_the_chat(session, tenant, manager):
    code = linking.issue_code(session, tenant_id=tenant.id, user_id=manager.id)
    _, reply = handlers.handle_update(session, _message(f"/link {code.code}"))
    assert "привязан" in reply.text

    chat = linking.get_chat(session, CHAT)
    assert chat is not None
    assert chat.user_id == manager.id
    session.refresh(manager)
    assert manager.telegram_chat_id == CHAT


def test_link_code_is_single_use(session, tenant, manager):
    code = linking.issue_code(session, tenant_id=tenant.id, user_id=manager.id)
    linking.redeem_code(session, code=code.code, chat_id=CHAT)
    with pytest.raises(ValidationError):
        linking.redeem_code(session, code=code.code, chat_id="999")


def test_expired_code_is_refused(session, tenant, manager):
    code = linking.issue_code(session, tenant_id=tenant.id, user_id=manager.id)
    code.expires_at = utcnow() - timedelta(minutes=1)
    session.flush()
    with pytest.raises(ValidationError):
        linking.redeem_code(session, code=code.code, chat_id=CHAT)


def test_issuing_a_code_supersedes_the_previous_one(session, tenant, manager):
    first = linking.issue_code(session, tenant_id=tenant.id, user_id=manager.id)
    linking.issue_code(session, tenant_id=tenant.id, user_id=manager.id)
    session.refresh(first)
    assert first.used_at is not None
    with pytest.raises(ValidationError):
        linking.redeem_code(session, code=first.code, chat_id=CHAT)


def test_wrong_code_does_not_reveal_whether_it_exists(session):
    _, reply = handlers.handle_update(session, _message("/link WRONGCOD"))
    assert "недействителен или истёк" in reply.text


def test_unlink_stops_further_commands(session, tenant, manager):
    _link(session, tenant, manager)
    handlers.handle_update(session, _message("/unlink"))
    _, reply = handlers.handle_update(session, _message("/projects"))
    assert "не привязан" in reply.text


def test_linking_is_audited(session, tenant, manager):
    _link(session, tenant, manager)
    from sqlalchemy import select

    from app.models.governance import AuditEvent

    actions = set(
        session.execute(select(AuditEvent.action).where(AuditEvent.tenant_id == tenant.id))
        .scalars()
        .all()
    )
    assert "telegram.link_code_issued" in actions
    assert "telegram.chat_linked" in actions


# --- authorisation --------------------------------------------------------


def test_role_without_permission_is_refused(session, tenant):
    qa = make_user(session, tenant, "qa-tg@fynix.local", [Role.QA_ENGINEER])
    _link(session, tenant, qa)
    _, reply = handlers.handle_update(session, _message("/new Проект"))
    assert "⛔️" in reply.text
    assert session.query(Project).count() == 0


def test_revoked_role_takes_effect_immediately(session, tenant, manager):
    _link(session, tenant, manager)
    _, ok = handlers.handle_update(session, _message("/new Первый"))
    assert "создан" in ok.text

    from sqlalchemy import select

    from app.models.identity import RoleBinding

    binding = session.execute(
        select(RoleBinding).where(RoleBinding.user_id == manager.id)
    ).scalar_one()
    session.delete(binding)
    session.flush()

    _, denied = handlers.handle_update(session, _message("/new Второй"))
    assert "⛔️" in denied.text


def test_a_chat_cannot_see_another_tenants_project(session, tenant, manager):
    from app.models.identity import Tenant

    other = Tenant(name="Other", slug="other-tg")
    session.add(other)
    session.flush()
    foreign = Project(tenant_id=other.id, name="Чужой проект", owner_user_id="x")
    session.add(foreign)
    session.flush()

    _link(session, tenant, manager)
    _, reply = handlers.handle_update(session, _message(f"/status {foreign.id}"))
    assert "Укажите проект" in reply.text
    assert "Чужой проект" not in reply.text


# --- dialogues ------------------------------------------------------------


def test_create_project_then_brief_end_to_end(session, tenant, manager):
    _link(session, tenant, manager)

    _, reply = handlers.handle_update(session, _message("/new"))
    assert "Как назвать проект" in reply.text

    _, reply = handlers.handle_update(session, _message("Бот для барбершопа"))
    assert "Проект создан" in reply.text

    project = session.query(Project).one()
    assert project.source == "telegram"
    assert project.name == "Бот для барбершопа"

    _, reply = handlers.handle_update(
        session,
        _message(
            "Нужен Telegram-бот для записи клиентов: выбор мастера, выбор времени, "
            "напоминание за час. Оплата не нужна, срок две недели."
        ),
    )
    assert "Бриф принят" in reply.text

    from app.models.execution import Job

    job = session.query(Job).filter(Job.kind == "intake.analyze_brief").one()
    assert job.payload["source"] == "telegram"
    assert job.project_id == project.id


def test_too_short_a_brief_is_rejected_without_spending(session, tenant, manager):
    _link(session, tenant, manager)
    handlers.handle_update(session, _message("/new Проект"))
    _, reply = handlers.handle_update(session, _message("сделай бота"))
    assert "Слишком коротко" in reply.text

    from app.models.execution import Job

    assert session.query(Job).count() == 0


def test_cancel_clears_the_dialogue(session, tenant, manager):
    _link(session, tenant, manager)
    handlers.handle_update(session, _message("/new"))
    handlers.handle_update(session, _message("/cancel"))

    chat = linking.get_chat(session, CHAT)
    assert chat.state == ""

    _, reply = handlers.handle_update(session, _message("просто текст"))
    assert "Не понял" in reply.text


def test_projects_and_costs_render(session, tenant, manager, project):
    _link(session, tenant, manager)
    _, listing = handlers.handle_update(session, _message("/projects"))
    assert project.name in listing.text
    assert project.id in listing.text

    _, costs = handlers.handle_update(session, _message(f"/costs {project.id}"))
    assert "потрачено" in costs.text


def test_status_reports_the_gate(session, tenant, manager, project):
    _link(session, tenant, manager)
    _, reply = handlers.handle_update(session, _message(f"/status {project.id}"))
    assert project.name in reply.text
    assert "gate" in reply.text.lower()


# --- approvals ------------------------------------------------------------


def test_blueprint_approval_can_be_decided_from_the_bot(session, tenant, project):
    architect = make_user(session, tenant, "arch-tg@fynix.local", [Role.SOLUTION_ARCHITECT])
    approvals_service.request_approvals(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        subject_type="blueprint",
        subject_id="bpv_tg",
        subject_checksum="c1",
    )
    _link(session, tenant, architect)

    _, listing = handlers.handle_update(session, _message("/approvals"))
    assert "blueprint" in listing.text
    assert listing.reply_markup is not None

    approval_id = listing.reply_markup["inline_keyboard"][0][0]["callback_data"].split(":")[1]
    _, decided = handlers.handle_update(session, _callback(f"ap:{approval_id}:y"))
    assert "утверждено" in decided.text

    from app.models.governance import Approval

    approval = session.get(Approval, approval_id)
    assert approval.status == ApprovalStatus.APPROVED.value
    assert approval.decided_by == architect.id


def test_production_deploy_approval_is_not_offered_in_the_bot(session, tenant, project):
    """A Telegram account is not a second factor (NFR-007)."""
    owner = make_user(session, tenant, "owner-tg@fynix.local", [Role.CLIENT_OWNER])
    created = approvals_service.request_approvals(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        subject_type="deploy_prod",
        subject_id="rel_tg",
        subject_checksum="d1",
    )
    _link(session, tenant, owner)

    _, listing = handlers.handle_update(session, _message("/approvals"))
    assert "требует второго фактора" in listing.text
    assert listing.reply_markup is None

    # A replayed callback must not slip past the boundary either.
    _, reply = handlers.handle_update(session, _callback(f"ap:{created[0].id}:y"))
    assert "⛔️" in reply.text
    session.refresh(created[0])
    assert created[0].status == ApprovalStatus.PENDING.value


def test_decide_refuses_prod_approval_without_mfa_at_the_service_level(session, tenant, project):
    """The bot is not the only guard: the service refuses it too."""
    owner = make_user(session, tenant, "owner-mfa@fynix.local", [Role.CLIENT_OWNER], mfa=False)
    created = approvals_service.request_approvals(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        subject_type="deploy_prod",
        subject_id="rel_mfa",
        subject_checksum="d2",
    )
    with pytest.raises(PermissionDenied) as exc:
        approvals_service.decide(
            session,
            principal=make_principal(owner, [Role.CLIENT_OWNER], mfa=False),
            approval_id=created[0].id,
            approved=True,
        )
    assert exc.value.details["reason"] == "mfa_required"


def test_approvals_of_other_people_are_not_listed(session, tenant, project):
    developer = make_user(session, tenant, "dev-tg@fynix.local", [Role.DEVELOPER])
    approvals_service.request_approvals(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        subject_type="blueprint",
        subject_id="bpv_other",
        subject_checksum="c2",
    )
    _link(session, tenant, developer)
    _, reply = handlers.handle_update(session, _message("/approvals"))
    assert "ожидающих вас, нет" in reply.text


# --- client helpers -------------------------------------------------------


def test_long_messages_are_split_on_line_boundaries():
    text = "\n".join(f"строка {i}" for i in range(2000))
    parts = chunk(text, limit=1000)
    assert len(parts) > 1
    assert all(len(p) <= 1000 for p in parts)
    assert "".join(parts) == text


def test_html_is_escaped():
    assert escape("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_keyboard_shape():
    markup = keyboard([[Button("да", "a:1"), Button("нет", "a:0")]])
    assert markup["inline_keyboard"][0][0] == {"text": "да", "callback_data": "a:1"}


def test_project_name_from_a_user_cannot_inject_markup(session, tenant, manager):
    _link(session, tenant, manager)
    handlers.handle_update(session, _message("/new"))
    handlers.handle_update(session, _message("<b>жирный</b> проект"))
    _, listing = handlers.handle_update(session, _message("/projects"))
    assert "&lt;b&gt;жирный&lt;/b&gt;" in listing.text
