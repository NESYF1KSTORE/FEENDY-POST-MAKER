"""Notifications and webhooks (FR-021, spec §9.2).

Subscribes to domain events and turns the ones a human needs to act on into
Telegram messages and signed webhooks. Delivery is recorded so an unnoticed
escalation is itself detectable.
"""

from __future__ import annotations

import json

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.logging import get_logger
from app.core.redaction import redact_text
from app.core.security import sign_webhook
from app.models.base import Role, utcnow
from app.models.governance import Approval
from app.models.identity import RoleBinding
from app.models.messaging import NotificationLog, OutboxEvent, WebhookSubscription
from app.models.project import BriefVersion
from app.models.telegram import TelegramChat
from app.orchestrator import events

log = get_logger("fynix.notify")

#: Events that reach a human, with the message template used for them.
HUMAN_FACING: dict[str, str] = {
    events.BLUEPRINT_APPROVAL_REQUESTED: "🧭 Требуется утверждение Blueprint по проекту {project_id}",
    events.QUALITY_GATE_FAILED: "🚦 Gate {gate} не пройден по {subject_type} {subject_id}",
    events.BUDGET_THRESHOLD_REACHED: "💰 Бюджет проекта {project_id}: {usage_pct}% ({threshold_pct}% порог)",
    events.RELEASE_CANDIDATE_CREATED: "📦 Собран release candidate {version}",
    events.DEPLOYMENT_COMPLETED: "🚀 Деплой в {environment} завершён",
    events.DEPLOYMENT_ROLLED_BACK: "↩️ Откат в {environment}: {reason}",
    events.INCIDENT_OPENED: "🔥 Инцидент {severity}: {title}",
    events.APPROVAL_DECIDED: "✅ Решение по {subject_type}: approved={approved}",
}


def render(event: OutboxEvent) -> str | None:
    template = HUMAN_FACING.get(event.type)
    if template is None:
        return None
    fields = {"project_id": event.project_id or "—", **(event.payload or {})}
    try:
        return template.format(**fields)
    except KeyError:
        return f"{event.type}: {json.dumps(event.payload, ensure_ascii=False)[:400]}"


def send_telegram(
    session: Session,
    *,
    tenant_id: str,
    text: str,
    chat_id: str = "",
    project_id: str | None = None,
    event_type: str = "",
    reply_markup: dict | None = None,
    client: httpx.Client | None = None,
) -> NotificationLog:
    settings = get_settings()
    target = chat_id or settings.telegram_chat_id
    record = NotificationLog(
        tenant_id=tenant_id,
        project_id=project_id,
        channel="telegram",
        target=target,
        event_type=event_type,
        body=redact_text(text),
    )

    if not settings.telegram_bot_token or not target:
        record.status = "skipped"
        record.error = "telegram is not configured"
        session.add(record)
        session.flush()
        return record

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    payload: dict = {
        "chat_id": target,
        "text": record.body,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    http = client or httpx.Client(timeout=15)
    try:
        response = http.post(url, json=payload)
        if response.status_code >= 400:
            record.status = "failed"
            record.error = f"{response.status_code}: {response.text[:300]}"
        else:
            record.status = "sent"
    except httpx.HTTPError as exc:
        record.status = "failed"
        record.error = str(exc)[:300]
    finally:
        if client is None:
            http.close()

    session.add(record)
    session.flush()
    return record


def deliver_webhooks(
    session: Session, event: OutboxEvent, *, client: httpx.Client | None = None
) -> list[NotificationLog]:
    """Signed, at-least-once webhook delivery for subscribers of this event type."""
    subscriptions = (
        session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.tenant_id == event.tenant_id,
                WebhookSubscription.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    delivered: list[NotificationLog] = []
    http = client or httpx.Client(timeout=15)
    try:
        for subscription in subscriptions:
            if subscription.event_types and event.type not in subscription.event_types:
                continue
            if subscription.project_id and subscription.project_id != event.project_id:
                continue

            body = json.dumps(
                {
                    "event_id": event.id,
                    "type": event.type,
                    "schema_version": event.schema_version,
                    "occurred_at": event.occurred_at.isoformat(),
                    "tenant_id": event.tenant_id,
                    "project_id": event.project_id,
                    "correlation_id": event.correlation_id,
                    "causation_id": event.causation_id,
                    "payload": event.payload,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            timestamp = str(int(utcnow().timestamp()))
            record = NotificationLog(
                tenant_id=event.tenant_id,
                project_id=event.project_id,
                channel="webhook",
                target=subscription.url,
                event_type=event.type,
                body=body.decode("utf-8")[:4000],
            )
            try:
                response = http.post(
                    subscription.url,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-Fynix-Timestamp": timestamp,
                        "X-Fynix-Signature": sign_webhook(subscription.secret, body, timestamp),
                        "X-Fynix-Event-Id": event.id,
                    },
                )
                if response.status_code >= 400:
                    record.status = "failed"
                    record.error = f"{response.status_code}"
                    subscription.failure_count += 1
                    # Park a persistently broken endpoint rather than retrying forever.
                    if subscription.failure_count >= 20:
                        subscription.is_active = False
                else:
                    record.status = "sent"
                    subscription.failure_count = 0
                    subscription.last_delivery_at = utcnow()
            except httpx.HTTPError as exc:
                record.status = "failed"
                record.error = str(exc)[:300]
                subscription.failure_count += 1

            session.add(record)
            delivered.append(record)
    finally:
        if client is None:
            http.close()

    session.flush()
    return delivered


#: Which roles care about which event. An approval request is routed to the
#: people who can actually decide it, rather than broadcast to one shared chat.
EVENT_AUDIENCE: dict[str, tuple[Role, ...]] = {
    events.BLUEPRINT_APPROVAL_REQUESTED: (Role.SOLUTION_ARCHITECT, Role.CLIENT_OWNER),
    "approval.requested": (),  # resolved from the approval rows themselves
    events.BUDGET_THRESHOLD_REACHED: (Role.FINANCE_ADMIN, Role.PROJECT_MANAGER),
    events.QUALITY_GATE_FAILED: (Role.PROJECT_MANAGER, Role.DEVELOPER, Role.QA_ENGINEER),
    events.INCIDENT_OPENED: (Role.DEVOPS_SRE, Role.PROJECT_MANAGER),
    events.DEPLOYMENT_ROLLED_BACK: (Role.DEVOPS_SRE, Role.PROJECT_MANAGER),
    events.RELEASE_CANDIDATE_CREATED: (Role.QA_ENGINEER, Role.PROJECT_MANAGER),
}


def _roles_for_event(session: Session, event: OutboxEvent) -> tuple[Role, ...]:
    """Roles to notify. For approval requests, read them off the approvals."""
    approval_ids = (event.payload or {}).get("approval_ids") or []
    if approval_ids:
        rows = (
            session.execute(select(Approval).where(Approval.id.in_(approval_ids)))
            .scalars()
            .all()
        )
        roles = []
        for row in rows:
            try:
                roles.append(Role(row.required_role))
            except ValueError:
                continue
        if roles:
            return tuple(dict.fromkeys(roles))
    return EVENT_AUDIENCE.get(event.type, ())


def _chats_for_roles(
    session: Session, tenant_id: str, roles: tuple[Role, ...], project_id: str | None
) -> list[str]:
    """Linked Telegram chats of users holding any of `roles` in this tenant."""
    if not roles:
        return []
    now = utcnow()
    bindings = (
        session.execute(
            select(RoleBinding).where(
                RoleBinding.tenant_id == tenant_id,
                RoleBinding.role.in_([r.value for r in roles]),
            )
        )
        .scalars()
        .all()
    )
    user_ids = {
        b.user_id
        for b in bindings
        if (b.expires_at is None or b.expires_at > now)
        # A project-scoped binding only counts for that project.
        and (b.project_id is None or b.project_id == project_id)
    }
    if not user_ids:
        return []

    chats = (
        session.execute(
            select(TelegramChat).where(
                TelegramChat.tenant_id == tenant_id,
                TelegramChat.user_id.in_(user_ids),
                TelegramChat.unlinked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    return [chat.chat_id for chat in chats]


def _approval_notifications(session: Session, event: OutboxEvent) -> int:
    """Send each approver the decision they can act on, with buttons attached.

    An approval that only says "something needs approving" costs a round trip
    through /approvals. Sending the actionable keyboard straight to the person
    who holds the role is what actually shortens time-to-decision — except for
    the ones that need a second factor, which carry no buttons by design.
    """
    from app.telegram.client import keyboard
    from app.telegram.handlers import approval_buttons  # local: avoids a cycle

    approval_ids = (event.payload or {}).get("approval_ids") or []
    if not approval_ids:
        return 0

    approvals = (
        session.execute(select(Approval).where(Approval.id.in_(approval_ids))).scalars().all()
    )
    sent = 0
    for approval in approvals:
        try:
            role = Role(approval.required_role)
        except ValueError:
            continue
        chats = _chats_for_roles(session, event.tenant_id, (role,), approval.project_id)
        if not chats:
            continue

        buttons = approval_buttons(approval)
        needs_portal = not buttons
        text = (
            f"🧭 Требуется решение: {approval.subject_type}\n"
            f"проект {approval.project_id}\n"
            f"роль {approval.required_role}"
            + ("\n🔒 требует второго фактора — решается в портале" if needs_portal else "")
        )
        for chat_id in chats:
            send_telegram(
                session,
                tenant_id=event.tenant_id,
                text=text,
                chat_id=chat_id,
                project_id=approval.project_id,
                event_type=event.type,
                reply_markup=keyboard(buttons) if buttons else None,
            )
            sent += 1
    return sent


def _clarification_notification(session: Session, event: OutboxEvent) -> int:
    """Tell the client that the analyst has questions, with a button to answer.

    This closes the FR-003 loop inside Telegram: brief in, questions out,
    answers back, new brief version.
    """
    from app.telegram.client import Button, keyboard
    from app.telegram.handlers import CALLBACK_CLARIFY

    payload = event.payload or {}
    if not payload.get("open_questions"):
        return 0

    brief = session.get(BriefVersion, payload.get("brief_version_id", ""))
    if brief is None or not brief.created_by:
        return 0

    chat = session.execute(
        select(TelegramChat).where(
            TelegramChat.tenant_id == event.tenant_id,
            TelegramChat.user_id == brief.created_by,
            TelegramChat.unlinked_at.is_(None),
        )
    ).scalar_one_or_none()
    if chat is None:
        return 0

    send_telegram(
        session,
        tenant_id=event.tenant_id,
        text=(
            f"💬 По брифу v{payload.get('version')} есть "
            f"{payload['open_questions']} уточняющих вопрос(ов). "
            f"Полнота: {payload.get('completeness')}/100 — ответы поднимут её."
        ),
        chat_id=chat.chat_id,
        project_id=event.project_id,
        event_type=event.type,
        reply_markup=keyboard(
            [[Button("💬 Ответить на вопросы", f"{CALLBACK_CLARIFY}:{event.project_id}")]]
        ),
    )
    return 1


def _on_event(session: Session, event: OutboxEvent) -> None:
    if event.type == events.BRIEF_VERSION_CREATED:
        _clarification_notification(session, event)
        deliver_webhooks(session, event)
        return

    targeted = _approval_notifications(session, event)

    text = render(event)
    if text:
        settings = get_settings()
        targets: list[str] = []

        # Approvals were already delivered individually with their keyboards.
        if not targeted:
            roles = _roles_for_event(session, event)
            targets.extend(_chats_for_roles(session, event.tenant_id, roles, event.project_id))

        # The shared operations chat still sees everything, if configured.
        if settings.telegram_chat_id:
            targets.append(settings.telegram_chat_id)

        # No linked chats and no shared chat: record the skip so a missed
        # escalation is visible rather than silent.
        for chat_id in dict.fromkeys(targets) or ([] if targeted else [""]):
            send_telegram(
                session,
                tenant_id=event.tenant_id,
                text=text,
                chat_id=chat_id,
                project_id=event.project_id,
                event_type=event.type,
            )
    deliver_webhooks(session, event)


def register() -> None:
    """Wire the notifier into the event bus. Called once at startup."""
    for event_type in events.KNOWN_EVENT_TYPES:
        events.subscribe(event_type, "notifications", _on_event)
