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


def _create_project(session, name: str = "Бот для барбершопа"):
    _, reply = handlers.handle_update(session, _message("/new"))
    assert "Как назвать проект" in reply.text
    _, reply = handlers.handle_update(session, _message(name))
    assert "Проект создан" in reply.text
    return session.query(Project).filter(Project.name == name).one(), reply


def test_free_text_brief_end_to_end(session, tenant, manager):
    _link(session, tenant, manager)
    project, offer = _create_project(session)
    assert project.source == "telegram"

    # The client picks how to describe the task.
    _, reply = handlers.handle_update(session, _callback(f"bf:free:{project.id}"))
    assert "одним сообщением" in reply.text

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
    project, _ = _create_project(session, "Проект")
    handlers.handle_update(session, _callback(f"bf:free:{project.id}"))
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


# --- guided checklist and clarification loop (FR-002, FR-003, §23) --------


def _run_checklist(session, answers: list[str]) -> object:
    """Walk the checklist, returning the final reply."""
    reply = None
    for answer in answers:
        _, reply = handlers.handle_update(session, _message(answer))
    return reply


def test_new_project_offers_both_intake_modes(session, tenant, manager):
    _link(session, tenant, manager)
    project, offer = _create_project(session)
    labels = [b["text"] for row in offer.reply_markup["inline_keyboard"] for b in row]
    assert any("вопросы" in label for label in labels)
    assert any("одним сообщением" in label for label in labels)


def test_guided_checklist_collects_a_full_brief(session, tenant, manager):
    _link(session, tenant, manager)
    project, _ = _create_project(session)

    _, reply = handlers.handle_update(session, _callback(f"bf:form:{project.id}"))
    assert "Вопрос 1 из" in reply.text

    reply = _run_checklist(
        session,
        [
            "Нужен бот для записи клиентов в барбершоп",
            "Клиенты салона, примерно 300 человек в месяц",
            "Выбор мастера, выбор времени, напоминание за час",
            "-",  # integrations are optional
            "Две недели",
            "-",  # constraints are optional
        ],
    )
    assert "Бриф собран" in reply.text
    assert "Бриф принят" in reply.text

    from app.models.execution import Job

    job = session.query(Job).filter(Job.kind == "intake.analyze_brief").one()
    answers = job.payload["answers"]
    assert answers["goal"].startswith("Нужен бот")
    assert "integrations" not in answers  # skipped, not invented
    assert "Кто будет этим пользоваться" in job.payload["raw_text"]


def test_required_checklist_question_cannot_be_skipped(session, tenant, manager):
    _link(session, tenant, manager)
    project, _ = _create_project(session)
    handlers.handle_update(session, _callback(f"bf:form:{project.id}"))

    _, reply = handlers.handle_update(session, _message("-"))
    assert "нужен для оценки" in reply.text
    assert "Вопрос 1 из" in reply.text  # still on the same question


def test_checklist_rejects_a_too_short_answer(session, tenant, manager):
    _link(session, tenant, manager)
    project, _ = _create_project(session)
    handlers.handle_update(session, _callback(f"bf:form:{project.id}"))

    _, reply = handlers.handle_update(session, _message("бот"))
    assert "Слишком коротко" in reply.text


def _brief_with_questions(session, tenant, project, user, questions: list[dict]):
    from app.core.hashing import checksum
    from app.models.project import BriefVersion

    payload = {"raw_text": "исходный бриф клиента", "normalized": {}}
    brief = BriefVersion(
        tenant_id=tenant.id,
        project_id=project.id,
        version=1,
        payload=payload,
        completeness=55,
        open_questions=questions,
        checksum=checksum(payload),
        created_by=user.id,
    )
    session.add(brief)
    session.flush()
    return brief


def test_clarification_answers_create_a_new_brief_version(session, tenant, manager, project):
    """FR-003: the analyst's questions come back and the answers make a new version."""
    _link(session, tenant, manager)
    _brief_with_questions(
        session,
        tenant,
        project,
        manager,
        [
            {"question": "Нужна ли онлайн-оплата?", "blocking": True},
            {"question": "Сколько мастеров в салоне?", "blocking": False},
        ],
    )

    _, reply = handlers.handle_update(session, _message(f"/answer {project.id}"))
    assert "Уточнение 1 из 2" in reply.text
    assert "Нужна ли онлайн-оплата?" in reply.text

    _, reply = handlers.handle_update(session, _message("Нет, оплата не нужна"))
    assert "Уточнение 2 из 2" in reply.text

    _, reply = handlers.handle_update(session, _message("Четыре мастера"))
    assert "Ответы приняты" in reply.text

    from app.models.execution import Job

    job = session.query(Job).filter(Job.kind == "intake.analyze_brief").one()
    assert job.payload["answers"]["Нужна ли онлайн-оплата?"] == "Нет, оплата не нужна"
    # The original request is carried forward, not lost.
    assert job.payload["raw_text"] == "исходный бриф клиента"


def test_blocking_questions_are_asked_first(session, tenant, manager, project):
    _link(session, tenant, manager)
    _brief_with_questions(
        session,
        tenant,
        project,
        manager,
        [
            {"question": "Второстепенный вопрос", "blocking": False},
            {"question": "Блокирующий вопрос", "blocking": True},
        ],
    )
    _, reply = handlers.handle_update(session, _message(f"/answer {project.id}"))
    assert "Блокирующий вопрос" in reply.text


def test_skipping_every_clarification_creates_no_new_version(session, tenant, manager, project):
    _link(session, tenant, manager)
    _brief_with_questions(
        session, tenant, project, manager, [{"question": "Вопрос?", "blocking": False}]
    )
    handlers.handle_update(session, _message(f"/answer {project.id}"))
    _, reply = handlers.handle_update(session, _message("-"))
    assert "Ни на один вопрос ответа не дано" in reply.text

    from app.models.execution import Job

    assert session.query(Job).count() == 0


def test_answer_without_open_questions_says_so(session, tenant, manager, project):
    _link(session, tenant, manager)
    _brief_with_questions(session, tenant, project, manager, [])
    _, reply = handlers.handle_update(session, _message(f"/answer {project.id}"))
    assert "Открытых вопросов нет" in reply.text


def test_status_offers_a_button_to_answer_questions(session, tenant, manager, project):
    _link(session, tenant, manager)
    _brief_with_questions(
        session, tenant, project, manager, [{"question": "Нужна ли оплата?", "blocking": True}]
    )
    _, reply = handlers.handle_update(session, _message(f"/status {project.id}"))
    assert "Открытые вопросы" in reply.text
    assert reply.reply_markup is not None

    data = reply.reply_markup["inline_keyboard"][0][0]["callback_data"]
    _, started = handlers.handle_update(session, _callback(data))
    assert "Уточнение 1 из 1" in started.text


# --- portal parity: tasks, releases, incidents (FR-022) -------------------


def test_tasks_command_lists_the_dag(session, tenant, manager, project):
    from app.orchestrator import dag

    _link(session, tenant, manager)
    dag.build(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        blueprint_version_id=None,
        specs=[
            {"key": "T1", "title": "Каркас", "depends_on": [], "acceptance_criteria": ["ok"]},
            {"key": "T2", "title": "Тесты", "depends_on": ["T1"], "acceptance_criteria": ["ok"]},
        ],
    )
    _, reply = handlers.handle_update(session, _message(f"/tasks {project.id}"))
    assert "T1" in reply.text
    assert "Каркас" in reply.text
    assert "0/2 готово" in reply.text


def test_tasks_before_planning_explains_why(session, tenant, manager, project):
    _link(session, tenant, manager)
    _, reply = handlers.handle_update(session, _message(f"/tasks {project.id}"))
    assert "не спланированы" in reply.text


def test_releases_command_shows_evidence_state(session, tenant, manager, project):
    from app.delivery import service as delivery_service

    _link(session, tenant, manager)
    delivery_service.create_release(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        version="1.0.0",
        artifact_digest="sha256:" + "a" * 64,
        change_set_ids=[],
    )
    _, reply = handlers.handle_update(session, _message(f"/releases {project.id}"))
    assert "1.0.0" in reply.text
    assert "evidence" in reply.text


def test_incidents_command(session, tenant, manager, project):
    from app.models.delivery import Incident

    _link(session, tenant, manager)
    _, empty = handlers.handle_update(session, _message("/incidents"))
    assert "Открытых инцидентов нет" in empty.text

    session.add(
        Incident(
            tenant_id=tenant.id,
            project_id=project.id,
            severity="sev2",
            title="Откат в staging",
            status="open",
        )
    )
    session.flush()
    _, reply = handlers.handle_update(session, _message("/incidents"))
    assert "Откат в staging" in reply.text
    assert "SEV2" in reply.text


# --- deep link and attachments -------------------------------------------


def test_start_with_a_code_links_in_one_tap(session, tenant, manager):
    code = linking.issue_code(session, tenant_id=tenant.id, user_id=manager.id)
    _, reply = handlers.handle_update(session, _message(f"/start {code.code}"))
    assert "Чат привязан" in reply.text
    assert linking.get_chat(session, CHAT) is not None


def test_start_without_a_code_shows_help(session):
    _, reply = handlers.handle_update(session, _message("/start"))
    assert "не привязан" in reply.text


def test_attachment_outside_a_brief_is_refused(session, tenant, manager):
    _link(session, tenant, manager)
    update = _message("")
    update["message"]["text"] = ""
    update["message"]["document"] = {"file_id": "f1", "file_name": "тз.pdf"}
    _, reply = handlers.handle_update(session, update)
    assert "только во время заполнения брифа" in reply.text


def test_attachment_is_stored_as_an_artifact_and_linked_to_the_brief(
    session, tenant, manager, monkeypatch
):
    """FR-002: a brief may carry attachments."""
    from app.telegram import handlers as handlers_module

    class _FakeClient:
        def get_file(self, file_id):
            return {"file_path": f"documents/{file_id}"}

        def download_file(self, file_path, max_bytes=None):
            return b"%PDF-1.4 fake requirements document"

    monkeypatch.setattr(handlers_module, "TelegramClient", lambda *a, **k: _FakeClient())

    _link(session, tenant, manager)
    project, _ = _create_project(session)
    handlers.handle_update(session, _callback(f"bf:free:{project.id}"))

    update = _message("")
    update["message"]["text"] = ""
    update["message"]["document"] = {"file_id": "abc123", "file_name": "тз.pdf"}
    _, reply = handlers.handle_update(session, update)
    assert "Файл принят" in reply.text

    from app.models.execution import Artifact

    artifact = session.query(Artifact).one()
    assert artifact.type == "brief_attachment"
    assert artifact.name == "тз.pdf"
    assert artifact.checksum.startswith("sha256:")

    _, done = handlers.handle_update(
        session,
        _message(
            "Нужен бот для записи клиентов, детали во вложенном документе, срок две недели."
        ),
    )
    assert "Приложено файлов: 1" in done.text

    from app.models.execution import Job

    job = session.query(Job).filter(Job.kind == "intake.analyze_brief").one()
    assert job.payload["attachments"][0]["artifact_id"] == artifact.id


# --- notifications carry the decision, not just news ---------------------


def test_approval_notification_goes_to_the_role_with_buttons(session, tenant, project, monkeypatch):
    """The approver gets the keyboard, not an instruction to run /approvals."""
    import app.notifications.service as notify

    architect = make_user(session, tenant, "arch-notify@fynix.local", [Role.SOLUTION_ARCHITECT])
    _link(session, tenant, architect)

    sent: list[dict] = []
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda session, **kw: sent.append(kw) or object(),
    )

    approvals_service.request_approvals(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        subject_type="blueprint",
        subject_id="bpv_notify",
        subject_checksum="c1",
    )
    from app.models.messaging import OutboxEvent

    outbox = (
        session.query(OutboxEvent)
        .filter(OutboxEvent.type == "blueprint.approval.requested")
        .one()
    )
    notify._on_event(session, outbox)

    architect_messages = [m for m in sent if m.get("chat_id") == CHAT]
    assert architect_messages, "the architect's linked chat must be notified"
    assert architect_messages[0]["reply_markup"] is not None


def test_prod_approval_notification_carries_no_buttons(session, tenant, project, monkeypatch):
    import app.notifications.service as notify

    owner = make_user(session, tenant, "owner-notify@fynix.local", [Role.CLIENT_OWNER])
    _link(session, tenant, owner)

    sent: list[dict] = []
    monkeypatch.setattr(notify, "send_telegram", lambda session, **kw: sent.append(kw) or object())

    approvals_service.request_approvals(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        subject_type="deploy_prod",
        subject_id="rel_notify",
        subject_checksum="d1",
    )

    from app.models.messaging import OutboxEvent

    outbox = (
        session.query(OutboxEvent).filter(OutboxEvent.type == "approval.requested").one()
    )
    notify._on_event(session, outbox)

    owner_messages = [m for m in sent if m.get("chat_id") == CHAT]
    assert owner_messages
    assert owner_messages[0]["reply_markup"] is None
    assert "второго фактора" in owner_messages[0]["text"]


def test_open_questions_notify_the_client_with_an_answer_button(
    session, tenant, manager, project, monkeypatch
):
    import app.notifications.service as notify

    _link(session, tenant, manager)
    brief = _brief_with_questions(
        session, tenant, project, manager, [{"question": "Нужна ли оплата?", "blocking": True}]
    )

    sent: list[dict] = []
    monkeypatch.setattr(notify, "send_telegram", lambda session, **kw: sent.append(kw) or object())

    from app.orchestrator import events as events_module

    event = events_module.emit(
        session,
        tenant_id=tenant.id,
        project_id=project.id,
        event_type=events_module.BRIEF_VERSION_CREATED,
        payload={
            "brief_version_id": brief.id,
            "version": 1,
            "completeness": 55,
            "open_questions": 1,
        },
    )
    notify._on_event(session, event)

    assert sent, "the client who submitted the brief must be told about the questions"
    assert sent[0]["chat_id"] == CHAT
    assert sent[0]["reply_markup"] is not None
