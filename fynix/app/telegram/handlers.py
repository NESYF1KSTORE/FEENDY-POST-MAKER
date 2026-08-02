"""Command handling for the Telegram bot.

Every action runs through the same policy engine, approval service and audit log
as the REST API — the bot is a front end, not a side door. Two consequences are
enforced here explicitly:

  * an unlinked chat can do nothing except link itself;
  * approvals that require a second factor (production deploy, budget increase)
    are refused with a pointer to the portal, because a Telegram account is not
    a second factor.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import FynixError, PermissionDenied
from app.core.logging import get_logger
from app.core.policy import APPROVAL_MFA_REQUIRED, Resource, can_decide, evaluate
from app.core.tenancy import scoped
from app.models.base import ApprovalStatus, DataClass, Role, TaskStatus
from app.models.governance import Approval
from app.models.project import BriefVersion, Project, Task
from app.orchestrator import dag
from app.orchestrator import state_machine as fsm
from app.services import approvals as approvals_service
from app.services import budgets as budgets_service
from app.services import projects as projects_service
from app.telegram import linking
from app.telegram.client import Button, escape, keyboard

log = get_logger("fynix.telegram")

STATE_AWAITING_PROJECT_NAME = "awaiting_project_name"
STATE_AWAITING_BRIEF = "awaiting_brief"

CALLBACK_APPROVE = "ap"


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

/projects — мои проекты
/new — создать проект
/brief — подать бриф по проекту
/status &lt;id&gt; — состояние проекта и гейта
/costs &lt;id&gt; — бюджет проекта
/approvals — что ждёт моего решения
/cancel — прервать текущий диалог
/unlink — отвязать этот чат
/help — эта справка"""

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
    text = (message.get("text") or "").strip()
    username = (message.get("from") or {}).get("username", "")
    if not chat_id or not text:
        return None

    chat = linking.get_chat(session, chat_id)

    command, _, argument = text.partition(" ")
    command = command.lower().split("@")[0]
    argument = argument.strip()

    # --- commands available without a linked account --------------------
    if command in {"/start", "/help"}:
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
        if command == "/status":
            return chat_id, _cmd_status(session, principal, argument)
        if command == "/costs":
            return chat_id, _cmd_costs(session, principal, argument)
        if command == "/approvals":
            return chat_id, _cmd_approvals(session, principal)

        # Not a command: continue whatever dialogue the chat is in.
        if chat.state == STATE_AWAITING_PROJECT_NAME:
            return chat_id, _finish_new(session, chat, principal, text)
        if chat.state == STATE_AWAITING_BRIEF:
            return chat_id, _finish_brief(session, chat, principal, text)

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
    linking.set_state(session, chat, STATE_AWAITING_BRIEF, {"project_id": project.id})
    return Reply.plain(
        f"✅ Проект создан: <b>{escape(project.name)}</b>\n<code>{project.id}</code>\n\n"
        "Теперь опишите задачу одним сообщением — что нужно сделать, для кого, "
        "какие интеграции и сроки.\n/cancel — отложить"
    )


def _cmd_brief(session: Session, chat, principal, argument: str) -> Reply:
    project = _resolve_project(session, principal, argument)
    if project is None:
        return Reply.plain(
            "Укажите проект: <code>/brief prj_...</code>\n/projects — список проектов"
        )
    linking.set_state(session, chat, STATE_AWAITING_BRIEF, {"project_id": project.id})
    return Reply.plain(
        f"Опишите задачу по проекту <b>{escape(project.name)}</b> одним сообщением.\n"
        "/cancel — отмена"
    )


def _finish_brief(session: Session, chat, principal, text: str) -> Reply:
    project_id = (chat.state_data or {}).get("project_id", "")
    project = _resolve_project(session, principal, project_id)
    if project is None:
        linking.clear_state(session, chat)
        return Reply.plain("Проект не найден. Начните заново: /brief")

    if len(text.strip()) < 20:
        return Reply.plain(
            "Слишком коротко, чтобы оценить задачу. Опишите цель, аудиторию и сроки — "
            "или /cancel."
        )

    # The text goes to the analyst agent fenced as untrusted data (§5.4).
    projects_service.submit_brief(
        session,
        principal=principal,
        project=project,
        raw_text=text,
        source="telegram",
    )
    linking.clear_state(session, chat)
    return Reply.plain(
        "✅ Бриф принят и поставлен в обработку.\n\n"
        f"Статус: <code>/status {project.id}</code>\n"
        "Когда blueprint будет готов, придёт запрос на утверждение."
    )


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
    brief = session.execute(
        select(BriefVersion)
        .where(BriefVersion.project_id == project.id)
        .order_by(BriefVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()

    lines = [_readable_project(session, project), ""]
    lines.append(
        ("✅ exit gate пройден: " if verdict.passed else "⏳ gate не пройден: ") + verdict.reason
    )
    if brief is not None:
        lines.append(f"бриф v{brief.version}, полнота {brief.completeness}/100")
        open_questions = [
            q.get("question") if isinstance(q, dict) else str(q)
            for q in (brief.open_questions or [])
        ][:5]
        if open_questions:
            lines.append("\n<b>Открытые вопросы:</b>")
            lines += [f"• {escape(q)}" for q in open_questions]

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
    return Reply.plain("\n".join(lines))


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


def _cmd_approvals(session: Session, principal) -> Reply:
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
    seen_subjects: set[tuple[str, str]] = set()
    for approval in pending:
        key = (approval.subject_type, approval.subject_id)
        if key in seen_subjects:
            continue
        seen_subjects.add(key)
        siblings = approvals_service.list_for_subject(
            session,
            tenant_id=principal.tenant_id,
            subject_type=approval.subject_type,
            subject_id=approval.subject_id,
        )
        for candidate in approvals_service.actionable(siblings):
            if can_decide(principal, Role(candidate.required_role), candidate.project_id):
                mine.append(candidate)

    if not mine:
        return Reply.plain("Решений, ожидающих вас, нет.")

    blocks = []
    buttons: list[list[Button]] = []
    for approval in mine[:8]:
        needs_portal = approval.subject_type in APPROVAL_MFA_REQUIRED
        project = session.get(Project, approval.project_id)
        title = project.name if project is not None else approval.project_id
        note = (
            "\n🔒 требует второго фактора — решается в портале"
            if needs_portal
            else ""
        )
        blocks.append(
            f"<b>{escape(approval.subject_type)}</b> · {escape(title)}\n"
            f"роль: {approval.required_role}\n"
            f"<code>{approval.subject_id}</code>{note}"
        )
        if not needs_portal:
            buttons.append(
                [
                    Button(f"✅ {approval.subject_type}", f"{CALLBACK_APPROVE}:{approval.id}:y"),
                    Button("❌ отклонить", f"{CALLBACK_APPROVE}:{approval.id}:n"),
                ]
            )

    return Reply(
        text="<b>Ожидают вашего решения</b>\n\n" + "\n\n".join(blocks),
        reply_markup=keyboard(buttons) if buttons else None,
    )


def _handle_callback(session: Session, callback: dict) -> tuple[str, Reply] | None:
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not chat_id or not data.startswith(f"{CALLBACK_APPROVE}:"):
        return None

    chat = linking.get_chat(session, chat_id)
    if chat is None:
        return chat_id, Reply(text=HELP_UNLINKED, toast="чат не привязан")

    try:
        _, approval_id, verdict = data.split(":", 2)
    except ValueError:
        return chat_id, Reply(text="Некорректная кнопка.", toast="ошибка")

    try:
        principal = linking.principal_for_chat(session, chat)
        approval = session.get(Approval, approval_id)
        if approval is None or approval.tenant_id != principal.tenant_id:
            return chat_id, Reply(text="Согласование не найдено.", toast="не найдено")

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
    except FynixError as exc:
        return chat_id, Reply(text=_format_error(exc), toast="отказано")

    word = "утверждено" if approved else "отклонено"
    return chat_id, Reply(
        text=f"{'✅' if approved else '❌'} <b>{escape(approval.subject_type)}</b> {word}.",
        toast=word,
    )
