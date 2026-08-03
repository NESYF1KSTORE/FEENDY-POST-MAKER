"""Command handling for the Telegram bot.

Every action runs through the same policy engine, approval service and audit log
as the REST API — the bot is a front end, not a side door. Two consequences are
enforced here explicitly:

  * an unlinked chat can do nothing except link itself;
  * approvals that require a second factor (production deploy, budget increase)
    are refused with a pointer to the portal, because a Telegram account is not
    a second factor.

The intake path implements spec §23 (a checklist rather than one free-text box)
and FR-003 (the analyst's questions come back to the client, and the answers
produce a new brief version).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import FynixError, PermissionDenied, ProviderError
from app.core.logging import get_logger
from app.core.policy import APPROVAL_MFA_REQUIRED, Resource, can_decide, evaluate
from app.core.tenancy import scoped
from app.models.base import ApprovalStatus, DataClass, Role, TaskStatus
from app.models.delivery import Deployment, Incident, Release
from app.models.governance import Approval
from app.models.project import BriefVersion, Project, Task
from app.orchestrator import dag
from app.orchestrator import state_machine as fsm
from app.services import approvals as approvals_service
from app.services import artifacts as artifacts_service
from app.services import budgets as budgets_service
from app.services import projects as projects_service
from app.telegram import intake, linking
from app.telegram.client import Button, TelegramClient, escape, keyboard

log = get_logger("fynix.telegram")

STATE_AWAITING_PROJECT_NAME = "awaiting_project_name"
STATE_BRIEF_FORM = "brief_form"
STATE_BRIEF_FREE = "brief_free"
STATE_CLARIFY = "clarify"

CALLBACK_APPROVE = "ap"
CALLBACK_BRIEF_MODE = "bf"
CALLBACK_CLARIFY = "cl"

#: Attachments are accepted only while a brief is being collected.
BRIEF_STATES = frozenset({STATE_BRIEF_FORM, STATE_BRIEF_FREE})


@dataclass
class Reply:
    """What the bot should send back. Kept as data so handlers stay testable."""

    text: str
    reply_markup: dict | None = None
    #: For callback queries: the toast shown on the button.
    toast: str = ""

    @classmethod
    def plain(cls, text: str) -> Reply:
        return cls(text=text)


HELP_LINKED = """<b>FYNIX — команды</b>

<b>Проекты</b>
/new — создать проект и заполнить бриф
/projects — мои проекты
/status &lt;id&gt; — состояние, гейт, открытые вопросы
/brief &lt;id&gt; — подать новый бриф
/answer &lt;id&gt; — ответить на уточняющие вопросы

<b>Ход работ</b>
/tasks &lt;id&gt; — задачи проекта
/releases &lt;id&gt; — релизы и evidence
/incidents — открытые инциденты
/costs &lt;id&gt; — бюджет

<b>Решения</b>
/approvals — что ждёт моего решения

<b>Прочее</b>
/cancel — прервать диалог · /unlink — отвязать чат · /help"""

HELP_UNLINKED = """<b>FYNIX AI Pipeline</b>

Этот чат пока не привязан к аккаунту, поэтому команды недоступны.

Получите код у администратора платформы:
<code>python -m app.cli telegram-code --email вы@компания.ру</code>

и отправьте сюда:
<code>/link КОД</code>"""


def handle_update(session: Session, update: dict) -> tuple[str, Reply] | None:
    """Route one Telegram update. Returns `(chat_id, reply)` or None to ignore."""
    if "message" in update:
        return _handle_message(session, update["message"])
    if "callback_query" in update:
        return _handle_callback(session, update["callback_query"])
    return None


def _handle_message(session: Session, message: dict) -> tuple[str, Reply] | None:
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not chat_id:
        return None
    text = (message.get("text") or message.get("caption") or "").strip()
    username = (message.get("from") or {}).get("username", "")
    has_file = bool(message.get("document") or message.get("photo"))
    if not text and not has_file:
        return None

    chat = linking.get_chat(session, chat_id)

    command, _, argument = text.partition(" ")
    command = command.lower().split("@")[0]
    argument = argument.strip()

    # --- commands available without a linked account --------------------
    if command == "/start":
        # Deep link: t.me/<bot>?start=CODE links the chat in one tap.
        if argument:
            return chat_id, _cmd_link(session, chat_id, argument, username)
        return chat_id, Reply.plain(HELP_LINKED if chat else HELP_UNLINKED)

    if command == "/help":
        return chat_id, Reply.plain(HELP_LINKED if chat else HELP_UNLINKED)

    if command == "/link":
        return chat_id, _cmd_link(session, chat_id, argument, username)

    if chat is None:
        return chat_id, Reply.plain(HELP_UNLINKED)

    # --- everything below acts on behalf of a real user ------------------
    try:
        principal = linking.principal_for_chat(session, chat)
    except FynixError as exc:
        return chat_id, Reply.plain(f"⛔️ {escape(exc.message)}")

    try:
        if command == "/unlink":
            linking.unlink(session, chat_id=chat_id)
            return chat_id, Reply.plain("Чат отвязан. Для повторной привязки — /link КОД")
        if command == "/cancel":
            linking.clear_state(session, chat)
            return chat_id, Reply.plain("Диалог прерван.")
        if command == "/projects":
            return chat_id, _cmd_projects(session, principal)
        if command == "/new":
            return chat_id, _cmd_new(session, chat, principal, argument)
        if command == "/brief":
            return chat_id, _cmd_brief(session, chat, principal, argument)
        if command == "/answer":
            return chat_id, _cmd_answer(session, chat, principal, argument)
        if command == "/status":
            return chat_id, _cmd_status(session, principal, argument)
        if command == "/costs":
            return chat_id, _cmd_costs(session, principal, argument)
        if command == "/tasks":
            return chat_id, _cmd_tasks(session, principal, argument)
        if command == "/releases":
            return chat_id, _cmd_releases(session, principal, argument)
        if command == "/incidents":
            return chat_id, _cmd_incidents(session, principal)
        if command == "/approvals":
            return chat_id, _cmd_approvals(session, principal)

        # A file sent while collecting a brief becomes an attachment (FR-002).
        if has_file:
            if chat.state in BRIEF_STATES:
                return chat_id, _attach_file(session, chat, principal, message)
            return chat_id, Reply.plain(
                "Файл можно приложить только во время заполнения брифа. /brief — начать."
            )

        # Not a command: continue whatever dialogue the chat is in.
        if chat.state == STATE_AWAITING_PROJECT_NAME:
            return chat_id, _finish_new(session, chat, principal, text)
        if chat.state == STATE_BRIEF_FORM:
            return chat_id, _step_brief_form(session, chat, principal, text)
        if chat.state == STATE_BRIEF_FREE:
            return chat_id, _finish_brief_free(session, chat, principal, text)
        if chat.state == STATE_CLARIFY:
            return chat_id, _step_clarify(session, chat, principal, text)

        if command.startswith("/"):
            return chat_id, Reply.plain("Неизвестная команда. /help — список команд.")
        return chat_id, Reply.plain("Не понял. /help — список команд.")

    except FynixError as exc:
        return chat_id, Reply.plain(_format_error(exc))


def _format_error(exc: FynixError) -> str:
    reason = exc.details.get("reason", "")
    hints = {
        "missing_permission": "Недостаточно прав для этого действия.",
        "mfa_required": "Действие требует второго фактора — выполните его в портале.",
        "budget_hard_stop": "Бюджет проекта исчерпан. Нужен override от финансов.",
        "budget_warning": "Достигнут порог 80% бюджета: необязательные запуски остановлены.",
        "wrong_approver_role": "Ваша роль не может принимать это решение.",
        "sequence_not_reached": "Раньше в очереди есть несогласованный пункт.",
        "dual_control_violation": "Нужны две разные подписи — вы уже подписали.",
        "attachment_too_large": "Файл слишком большой — до 10 МБ.",
    }
    hint = hints.get(reason)
    body = f"⛔️ {escape(exc.message)}"
    return f"{body}\n\n{escape(hint)}" if hint else body


# --- linking --------------------------------------------------------------


def _cmd_link(session: Session, chat_id: str, argument: str, username: str) -> Reply:
    if not argument:
        return Reply.plain("Укажите код: <code>/link КОД</code>")
    try:
        linking.redeem_code(session, code=argument, chat_id=chat_id, username=username)
    except FynixError as exc:
        return Reply.plain(f"⛔️ {escape(exc.message)}")
    return Reply.plain("✅ Чат привязан.\n\n" + HELP_LINKED)


# --- projects -------------------------------------------------------------


def _readable_project(session: Session, project: Project) -> str:
    progress = dag.progress(session, project.id)
    budget = budgets_service.summary(
        session, tenant_id=project.tenant_id, project_id=project.id
    )
    return (
        f"<b>{escape(project.name)}</b>\n"
        f"<code>{project.id}</code>\n"
        f"состояние: {project.state}\n"
        f"задачи: {progress['done']}/{progress['total']}\n"
        f"бюджет: {budget['spent']:.0f} / {budget['limit']:.0f} ₽ ({budget['usage_pct']}%)"
    )


def _cmd_projects(session: Session, principal) -> Reply:
    evaluate(
        principal, "project:read", Resource(type="project", tenant_id=principal.tenant_id)
    ).raise_if_denied("project:read")

    projects = list(
        session.execute(
            scoped(Project, principal.tenant_id)
            .order_by(Project.created_at.desc())
            .limit(15)
        )
        .scalars()
        .all()
    )
    if not projects:
        return Reply.plain("Проектов пока нет. /new — создать первый.")
    blocks = [_readable_project(session, p) for p in projects]
    return Reply.plain("\n\n".join(blocks))


def _cmd_new(session: Session, chat, principal, argument: str) -> Reply:
    evaluate(
        principal, "project:create", Resource(type="project", tenant_id=principal.tenant_id)
    ).raise_if_denied("project:create")

    if argument:
        return _finish_new(session, chat, principal, argument)
    linking.set_state(session, chat, STATE_AWAITING_PROJECT_NAME)
    return Reply.plain("Как назвать проект? Отправьте название одним сообщением.\n/cancel — отмена")


def _finish_new(session: Session, chat, principal, name: str) -> Reply:
    name = name.strip()
    if len(name) < 2:
        return Reply.plain("Название слишком короткое. Попробуйте ещё раз или /cancel.")

    project = projects_service.create_project(
        session,
        principal=principal,
        name=name[:200],
        data_class=DataClass.CONFIDENTIAL.value,
        source="telegram",
    )
    linking.clear_state(session, chat)
    return _offer_brief_mode(project)


def _offer_brief_mode(project: Project) -> Reply:
    return Reply(
        text=(
            f"✅ Проект создан: <b>{escape(project.name)}</b>\n"
            f"<code>{project.id}</code>\n\n"
            "Как удобнее описать задачу?"
        ),
        reply_markup=keyboard(
            [
                [Button("📝 Ответить на вопросы", f"{CALLBACK_BRIEF_MODE}:form:{project.id}")],
                [Button("⌨️ Написать одним сообщением", f"{CALLBACK_BRIEF_MODE}:free:{project.id}")],
            ]
        ),
    )


def _cmd_brief(session: Session, chat, principal, argument: str) -> Reply:
    project = _resolve_project(session, principal, argument)
    if project is None:
        return Reply.plain(
            "Укажите проект: <code>/brief prj_...</code>\n/projects — список проектов"
        )
    return _offer_brief_mode(project)


def _start_brief_form(session: Session, chat, project: Project) -> Reply:
    progress = intake.Progress(project_id=project.id)
    linking.set_state(session, chat, STATE_BRIEF_FORM, progress.to_dict())
    question = progress.current
    return Reply.plain(
        f"Заполним бриф по проекту <b>{escape(project.name)}</b>.\n"
        "Файлы можно прикладывать в любой момент.\n\n"
        + question.render(progress.index, len(intake.CHECKLIST))
    )


def _step_brief_form(session: Session, chat, principal, text: str) -> Reply:
    progress = intake.Progress.from_dict(chat.state_data or {})
    project = _resolve_project(session, principal, progress.project_id)
    if project is None:
        linking.clear_state(session, chat)
        return Reply.plain("Проект не найден. Начните заново: /projects")

    question = progress.current
    if question is None:
        return _submit_form(session, chat, principal, project, progress)

    error = intake.validate(question, text)
    if error:
        return Reply.plain(f"{escape(error)}\n\n" + question.render(progress.index, len(intake.CHECKLIST)))

    intake.record(progress, question, text)
    if progress.finished:
        return _submit_form(session, chat, principal, project, progress)

    linking.set_state(session, chat, STATE_BRIEF_FORM, progress.to_dict())
    return Reply.plain(progress.current.render(progress.index, len(intake.CHECKLIST)))


def _submit_form(session: Session, chat, principal, project: Project, progress) -> Reply:
    attachments = (chat.state_data or {}).get("attachments") or []
    projects_service.submit_brief(
        session,
        principal=principal,
        project=project,
        raw_text=intake.compose_brief(progress.answers),
        answers=progress.answers,
        attachments=attachments,
        source="telegram",
    )
    linking.clear_state(session, chat)
    tail = f"\n\nПриложено файлов: {len(attachments)}" if attachments else ""
    return Reply.plain(
        intake.summary(progress.answers)
        + tail
        + "\n\n✅ Бриф принят и поставлен в обработку.\n"
        f"Статус: <code>/status {project.id}</code>"
    )


def _start_brief_free(session: Session, chat, project: Project) -> Reply:
    linking.set_state(session, chat, STATE_BRIEF_FREE, {"project_id": project.id})
    return Reply.plain(
        f"Опишите задачу по проекту <b>{escape(project.name)}</b> одним сообщением: "
        "что нужно, для кого, какие интеграции и сроки.\n"
        "Файлы можно приложить отдельным сообщением.\n/cancel — отмена"
    )


def _finish_brief_free(session: Session, chat, principal, text: str) -> Reply:
    state = chat.state_data or {}
    project = _resolve_project(session, principal, state.get("project_id", ""))
    if project is None:
        linking.clear_state(session, chat)
        return Reply.plain("Проект не найден. Начните заново: /brief")

    if len(text.strip()) < 20:
        return Reply.plain(
            "Слишком коротко, чтобы оценить задачу. Опишите цель, аудиторию и сроки — "
            "или /cancel."
        )

    attachments = state.get("attachments") or []
    projects_service.submit_brief(
        session,
        principal=principal,
        project=project,
        raw_text=text,
        attachments=attachments,
        source="telegram",
    )
    linking.clear_state(session, chat)
    tail = f"\nПриложено файлов: {len(attachments)}" if attachments else ""
    return Reply.plain(
        "✅ Бриф принят и поставлен в обработку." + tail + "\n\n"
        f"Статус: <code>/status {project.id}</code>\n"
        "Когда blueprint будет готов, придёт запрос на утверждение."
    )


# --- clarification loop (FR-003) -----------------------------------------


def _latest_brief(session: Session, project: Project) -> BriefVersion | None:
    return session.execute(
        select(BriefVersion)
        .where(BriefVersion.project_id == project.id)
        .order_by(BriefVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def _cmd_answer(session: Session, chat, principal, argument: str) -> Reply:
    project = _resolve_project(session, principal, argument)
    if project is None:
        return Reply.plain("Укажите проект: <code>/answer prj_...</code>")
    return _start_clarify(session, chat, principal, project)


def _start_clarify(session: Session, chat, principal, project: Project) -> Reply:
    evaluate(
        principal,
        "brief:write",
        Resource(type="brief", tenant_id=project.tenant_id, project_id=project.id),
    ).raise_if_denied("brief:write")

    brief = _latest_brief(session, project)
    if brief is None:
        return Reply.plain("По этому проекту ещё нет брифа. /brief — подать.")

    questions = intake.extract_questions(brief.open_questions)
    if not questions:
        return Reply.plain(
            f"Открытых вопросов нет — бриф v{brief.version} полон на "
            f"{brief.completeness}/100."
        )

    clarification = intake.Clarification(
        project_id=project.id, brief_version_id=brief.id, questions=questions
    )
    linking.set_state(session, chat, STATE_CLARIFY, clarification.to_dict())
    return Reply.plain(
        f"Аналитик задал {len(questions)} уточняющих вопрос(ов) по брифу v{brief.version}.\n"
        "Ответы создадут новую версию брифа.\n\n" + clarification.render_current()
    )


def _step_clarify(session: Session, chat, principal, text: str) -> Reply:
    clarification = intake.Clarification.from_dict(chat.state_data or {})
    project = _resolve_project(session, principal, clarification.project_id)
    if project is None:
        linking.clear_state(session, chat)
        return Reply.plain("Проект не найден. Начните заново: /projects")

    clarification.record(text)
    if not clarification.finished:
        linking.set_state(session, chat, STATE_CLARIFY, clarification.to_dict())
        return Reply.plain(clarification.render_current())

    if not clarification.answers:
        linking.clear_state(session, chat)
        return Reply.plain(
            "Ни на один вопрос ответа не дано — бриф остался прежним. "
            "Вернуться к вопросам: /answer " + project.id
        )

    brief = _latest_brief(session, project)
    base_text = (brief.payload or {}).get("raw_text", "") if brief else ""
    # A clarification produces a NEW brief version rather than editing the old
    # one, so the approval bound to the previous version is correctly voided.
    projects_service.submit_brief(
        session,
        principal=principal,
        project=project,
        raw_text=base_text,
        answers=clarification.answers,
        source="telegram",
    )
    linking.clear_state(session, chat)

    answered = "\n".join(
        f"• <b>{escape(q)}</b>\n{escape(a)}" for q, a in clarification.answers.items()
    )
    return Reply.plain(
        "✅ Ответы приняты, формируется новая версия брифа.\n\n"
        + answered
        + f"\n\nСтатус: <code>/status {project.id}</code>"
    )


# --- attachments (FR-002) -------------------------------------------------


def _attach_file(session: Session, chat, principal, message: dict) -> Reply:
    """Store an uploaded file as an immutable artifact linked to the brief."""
    state = dict(chat.state_data or {})
    project = _resolve_project(
        session, principal, state.get("project_id") or state.get("project_id", "")
    )
    if project is None:
        return Reply.plain("Проект не найден — начните бриф заново: /projects")

    document = message.get("document")
    if document:
        file_id = document.get("file_id")
        name = document.get("file_name") or "attachment"
    else:
        # Photos arrive as a list of sizes; the last one is the largest.
        photo = (message.get("photo") or [])[-1]
        file_id = photo.get("file_id")
        name = f"photo-{file_id[:8]}.jpg"

    client = TelegramClient()
    try:
        meta = client.get_file(file_id)
        data = client.download_file(meta["file_path"])
    except ProviderError as exc:
        return Reply.plain(_format_error(exc))
    except KeyError:
        return Reply.plain("Не удалось получить файл от Telegram, попробуйте ещё раз.")

    artifact = artifacts_service.put_bytes(
        session,
        tenant_id=principal.tenant_id,
        project_id=project.id,
        name=name[:300],
        type="brief_attachment",
        data=data,
        data_class=DataClass(project.data_class),
        producer_kind="client",
        meta={"source": "telegram", "chat_id": chat.chat_id},
    )

    attachments = list(state.get("attachments") or [])
    attachments.append(
        {"artifact_id": artifact.id, "name": artifact.name, "checksum": artifact.checksum}
    )
    state["attachments"] = attachments
    linking.set_state(session, chat, chat.state, state)

    return Reply.plain(
        f"📎 Файл принят: <b>{escape(artifact.name)}</b> "
        f"({artifact.size_bytes // 1024} КБ). Всего вложений: {len(attachments)}.\n\n"
        "Продолжайте — или /cancel."
    )


# --- read-only views ------------------------------------------------------


def _resolve_project(session: Session, principal, project_id: str) -> Project | None:
    if not project_id:
        return None
    project = session.execute(
        scoped(Project, principal.tenant_id).where(Project.id == project_id.strip())
    ).scalar_one_or_none()
    if project is None:
        return None
    decision = evaluate(
        principal,
        "project:read",
        Resource(
            type="project",
            id=project.id,
            tenant_id=project.tenant_id,
            project_id=project.id,
            data_class=DataClass(project.data_class),
        ),
    )
    return project if decision.allowed else None


def _cmd_status(session: Session, principal, argument: str) -> Reply:
    project = _resolve_project(session, principal, argument)
    if project is None:
        return Reply.plain("Укажите проект: <code>/status prj_...</code>")

    verdict = fsm.evaluate_gate(session, project)
    brief = _latest_brief(session, project)

    lines = [_readable_project(session, project), ""]
    lines.append(
        ("✅ exit gate пройден: " if verdict.passed else "⏳ gate не пройден: ") + verdict.reason
    )

    markup = None
    if brief is not None:
        lines.append(f"бриф v{brief.version}, полнота {brief.completeness}/100")
        if brief.attachments:
            lines.append(f"вложений: {len(brief.attachments)}")
        questions = intake.extract_questions(brief.open_questions)
        if questions:
            lines.append("\n<b>Открытые вопросы:</b>")
            lines += [f"• {escape(q)}" for q in questions[:5]]
            if len(questions) > 5:
                lines.append(f"<i>…ещё {len(questions) - 5}</i>")
            markup = keyboard(
                [[Button("💬 Ответить на вопросы", f"{CALLBACK_CLARIFY}:{project.id}")]]
            )

    blocked = list(
        session.execute(
            select(Task).where(
                Task.project_id == project.id, Task.status == TaskStatus.REVIEW.value
            )
        )
        .scalars()
        .all()
    )
    if blocked:
        lines.append(f"\n⚠️ задач на доработке: {len(blocked)}")
    return Reply(text="\n".join(lines), reply_markup=markup)


def _cmd_tasks(session: Session, principal, argument: str) -> Reply:
    project = _resolve_project(session, principal, argument)
    if project is None:
        return Reply.plain("Укажите проект: <code>/tasks prj_...</code>")
    evaluate(
        principal,
        "task:read",
        Resource(type="task", tenant_id=project.tenant_id, project_id=project.id),
    ).raise_if_denied("task:read")

    tasks = list(
        session.execute(
            select(Task).where(Task.project_id == project.id).order_by(Task.priority, Task.key)
        )
        .scalars()
        .all()
    )
    if not tasks:
        return Reply.plain("Задачи ещё не спланированы — нужен утверждённый blueprint.")

    icons = {
        TaskStatus.DONE.value: "✅",
        TaskStatus.RUNNING.value: "⚙️",
        TaskStatus.READY.value: "▶️",
        TaskStatus.BLOCKED.value: "⏸",
        TaskStatus.REVIEW.value: "⚠️",
        TaskStatus.FAILED.value: "❌",
        TaskStatus.CANCELLED.value: "🚫",
        TaskStatus.AWAITING_APPROVAL.value: "🔒",
    }
    progress = dag.progress(session, project.id)
    lines = [
        f"<b>{escape(project.name)}</b> — {progress['done']}/{progress['total']} готово",
        "",
    ]
    for task in tasks[:30]:
        lines.append(
            f"{icons.get(task.status, '•')} <code>{escape(task.key)}</code> "
            f"{escape(task.title)}"
        )
    if len(tasks) > 30:
        lines.append(f"<i>…ещё {len(tasks) - 30}</i>")
    return Reply.plain("\n".join(lines))


def _cmd_releases(session: Session, principal, argument: str) -> Reply:
    project = _resolve_project(session, principal, argument)
    if project is None:
        return Reply.plain("Укажите проект: <code>/releases prj_...</code>")
    evaluate(
        principal,
        "release:read",
        Resource(type="release", tenant_id=project.tenant_id, project_id=project.id),
    ).raise_if_denied("release:read")

    releases = list(
        session.execute(
            select(Release)
            .where(Release.project_id == project.id)
            .order_by(Release.created_at.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    if not releases:
        return Reply.plain("Релизов пока нет.")

    deployments = {
        d.release_id: d
        for d in session.execute(
            select(Deployment)
            .where(Deployment.project_id == project.id)
            .order_by(Deployment.created_at.asc())
        )
        .scalars()
        .all()
    }

    lines = [f"<b>{escape(project.name)}</b> — релизы", ""]
    for release in releases:
        deployment = deployments.get(release.id)
        lines.append(
            f"<b>{escape(release.version)}</b> · {escape(release.status)}\n"
            f"digest <code>{escape(release.artifact_digest[:23])}…</code>\n"
            f"evidence: {'есть' if release.evidence_artifact_id else '—'} · "
            f"SBOM: {'есть' if release.sbom_artifact_id else '—'}"
            + (f"\nдеплой: {escape(deployment.status)}" if deployment else "")
        )
    return Reply.plain("\n\n".join(lines))


def _cmd_incidents(session: Session, principal) -> Reply:
    evaluate(
        principal, "incident:read", Resource(type="incident", tenant_id=principal.tenant_id)
    ).raise_if_denied("incident:read")

    incidents = list(
        session.execute(
            scoped(Incident, principal.tenant_id)
            .where(Incident.status != "resolved")
            .order_by(Incident.created_at.desc())
            .limit(10)
        )
        .scalars()
        .all()
    )
    if not incidents:
        return Reply.plain("Открытых инцидентов нет.")
    lines = ["<b>Открытые инциденты</b>", ""]
    for incident in incidents:
        lines.append(
            f"🔥 <b>{escape(incident.severity.upper())}</b> {escape(incident.title)}\n"
            f"<code>{incident.id}</code> · {incident.created_at.strftime('%d.%m %H:%M')}"
        )
    return Reply.plain("\n\n".join(lines))


def _cmd_costs(session: Session, principal, argument: str) -> Reply:
    project = _resolve_project(session, principal, argument)
    if project is None:
        return Reply.plain("Укажите проект: <code>/costs prj_...</code>")
    evaluate(
        principal,
        "cost:read",
        Resource(type="budget", tenant_id=project.tenant_id, project_id=project.id),
    ).raise_if_denied("cost:read")

    summary = budgets_service.summary(
        session, tenant_id=principal.tenant_id, project_id=project.id
    )
    level = {
        "ok": "в пределах",
        "soft": "50% — стоит следить за контекстом",
        "warning": "80% — необязательные запуски остановлены",
        "hard": "100% — новые запуски заблокированы",
    }.get(summary["level"], summary["level"])

    lines = [
        f"<b>{escape(project.name)}</b>",
        f"потрачено: {summary['spent']:.2f} из {summary['limit']:.2f} ₽ ({summary['usage_pct']}%)",
        f"статус: {level}",
    ]
    if summary["by_provider"]:
        lines.append("\n<b>По провайдерам:</b>")
        for row in summary["by_provider"][:6]:
            lines.append(
                f"• {escape(row['provider'])}: {row['amount']:.2f} ₽ "
                f"({row['quantity']:.0f} {escape(row['unit'])})"
            )
    return Reply.plain("\n".join(lines))


# --- approvals ------------------------------------------------------------


def actionable_for(session: Session, principal) -> list[Approval]:
    """Approvals this principal can decide right now, deduplicated by subject."""
    pending = list(
        session.execute(
            scoped(Approval, principal.tenant_id)
            .where(Approval.status == ApprovalStatus.PENDING.value)
            .order_by(Approval.created_at.asc())
        )
        .scalars()
        .all()
    )
    mine: list[Approval] = []
    seen: set[tuple[str, str]] = set()
    for approval in pending:
        key = (approval.subject_type, approval.subject_id)
        if key in seen:
            continue
        seen.add(key)
        siblings = approvals_service.list_for_subject(
            session,
            tenant_id=principal.tenant_id,
            subject_type=approval.subject_type,
            subject_id=approval.subject_id,
        )
        for candidate in approvals_service.actionable(siblings):
            if can_decide(principal, Role(candidate.required_role), candidate.project_id):
                mine.append(candidate)
    return mine


def approval_buttons(approval: Approval) -> list[list[Button]]:
    """Keyboard for one approval, or nothing when a second factor is required."""
    if approval.subject_type in APPROVAL_MFA_REQUIRED:
        return []
    return [
        [
            Button("✅ Утвердить", f"{CALLBACK_APPROVE}:{approval.id}:y"),
            Button("❌ Отклонить", f"{CALLBACK_APPROVE}:{approval.id}:n"),
        ]
    ]


def _cmd_approvals(session: Session, principal) -> Reply:
    mine = actionable_for(session, principal)
    if not mine:
        return Reply.plain("Решений, ожидающих вас, нет.")

    blocks = []
    buttons: list[list[Button]] = []
    for approval in mine[:8]:
        needs_portal = approval.subject_type in APPROVAL_MFA_REQUIRED
        project = session.get(Project, approval.project_id)
        title = project.name if project is not None else approval.project_id
        note = "\n🔒 требует второго фактора — решается в портале" if needs_portal else ""
        blocks.append(
            f"<b>{escape(approval.subject_type)}</b> · {escape(title)}\n"
            f"роль: {approval.required_role}\n"
            f"<code>{approval.subject_id}</code>{note}"
        )
        buttons.extend(approval_buttons(approval))

    return Reply(
        text="<b>Ожидают вашего решения</b>\n\n" + "\n\n".join(blocks),
        reply_markup=keyboard(buttons) if buttons else None,
    )


def _handle_callback(session: Session, callback: dict) -> tuple[str, Reply] | None:
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not chat_id or not data:
        return None

    chat = linking.get_chat(session, chat_id)
    if chat is None:
        return chat_id, Reply(text=HELP_UNLINKED, toast="чат не привязан")

    try:
        principal = linking.principal_for_chat(session, chat)
    except FynixError as exc:
        return chat_id, Reply(text=f"⛔️ {escape(exc.message)}", toast="отказано")

    try:
        if data.startswith(f"{CALLBACK_BRIEF_MODE}:"):
            _, mode, project_id = data.split(":", 2)
            project = _resolve_project(session, principal, project_id)
            if project is None:
                return chat_id, Reply(text="Проект не найден.", toast="не найдено")
            reply = (
                _start_brief_form(session, chat, project)
                if mode == "form"
                else _start_brief_free(session, chat, project)
            )
            reply.toast = "начали"
            return chat_id, reply

        if data.startswith(f"{CALLBACK_CLARIFY}:"):
            _, project_id = data.split(":", 1)
            project = _resolve_project(session, principal, project_id)
            if project is None:
                return chat_id, Reply(text="Проект не найден.", toast="не найдено")
            reply = _start_clarify(session, chat, principal, project)
            reply.toast = "уточнения"
            return chat_id, reply

        if data.startswith(f"{CALLBACK_APPROVE}:"):
            return chat_id, _decide_callback(session, principal, data)

    except FynixError as exc:
        return chat_id, Reply(text=_format_error(exc), toast="отказано")

    return chat_id, Reply(text="Некорректная кнопка.", toast="ошибка")


def _decide_callback(session: Session, principal, data: str) -> Reply:
    try:
        _, approval_id, verdict = data.split(":", 2)
    except ValueError:
        return Reply(text="Некорректная кнопка.", toast="ошибка")

    approval = session.get(Approval, approval_id)
    if approval is None or approval.tenant_id != principal.tenant_id:
        return Reply(text="Согласование не найдено.", toast="не найдено")

    if approval.subject_type in APPROVAL_MFA_REQUIRED:
        # Belt and braces: the keyboard never offers these, but a replayed
        # callback must not slip past the second-factor requirement.
        raise PermissionDenied(
            "это согласование требует второго фактора",
            details={"reason": "mfa_required"},
        )

    approved = verdict == "y"
    approvals_service.decide(
        session,
        principal=principal,
        approval_id=approval_id,
        approved=approved,
        comment="решение принято в Telegram",
    )
    word = "утверждено" if approved else "отклонено"
    return Reply(
        text=f"{'✅' if approved else '❌'} <b>{escape(approval.subject_type)}</b> {word}.",
        toast=word,
    )
