"""Binding a Telegram chat to a platform user.

A chat id proves only that someone is talking to the bot. To act on a user's
behalf the chat must present a code that was issued inside the platform to that
user, is single-use and expires quickly. Until then the bot answers with help
text and nothing else.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import _principal_from_user
from app.core import audit
from app.core.errors import NotFoundError, ValidationError
from app.core.security import Principal
from app.models.base import utcnow
from app.models.identity import User
from app.models.telegram import TelegramChat, TelegramLinkCode

CODE_TTL = timedelta(minutes=15)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters
CODE_LENGTH = 8


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def issue_code(
    session: Session, *, tenant_id: str, user_id: str, issued_by: str = "", now: datetime | None = None
) -> TelegramLinkCode:
    """Create a single-use link code for a user."""
    now = now or utcnow()
    user = session.execute(
        select(User).where(User.id == user_id, User.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError(f"user '{user_id}' not found in this tenant")

    # Any outstanding code is superseded — a user should never have two valid.
    outstanding = (
        session.execute(
            select(TelegramLinkCode).where(
                TelegramLinkCode.tenant_id == tenant_id,
                TelegramLinkCode.user_id == user_id,
                TelegramLinkCode.used_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    for row in outstanding:
        row.used_at = now
        row.used_by_chat_id = "superseded"

    code = TelegramLinkCode(
        tenant_id=tenant_id,
        user_id=user_id,
        code=generate_code(),
        expires_at=now + CODE_TTL,
        issued_by=issued_by or user_id,
    )
    session.add(code)
    session.flush()

    audit.record(
        session,
        tenant_id=tenant_id,
        action="telegram.link_code_issued",
        actor_id=issued_by or user_id,
        resource_type="user",
        resource_id=user_id,
        payload={"expires_at": code.expires_at.isoformat()},
    )
    return code


def redeem_code(
    session: Session,
    *,
    code: str,
    chat_id: str,
    username: str = "",
    now: datetime | None = None,
) -> TelegramChat:
    """Bind `chat_id` to the user the code belongs to."""
    now = now or utcnow()
    normalized = (code or "").strip().upper()
    if not normalized:
        raise ValidationError("code is required")

    record = session.execute(
        select(TelegramLinkCode).where(TelegramLinkCode.code == normalized)
    ).scalar_one_or_none()
    if record is None or not record.is_usable(now):
        # One message for "wrong" and "expired" so the bot cannot be used to
        # probe which codes exist.
        raise ValidationError("код недействителен или истёк")

    existing = session.execute(
        select(TelegramChat).where(TelegramChat.chat_id == str(chat_id))
    ).scalar_one_or_none()

    if existing is not None:
        existing.user_id = record.user_id
        existing.tenant_id = record.tenant_id
        existing.username = username
        existing.unlinked_at = None
        existing.state = ""
        existing.state_data = {}
        chat = existing
    else:
        chat = TelegramChat(
            tenant_id=record.tenant_id,
            chat_id=str(chat_id),
            user_id=record.user_id,
            username=username,
        )
        session.add(chat)

    record.used_at = now
    record.used_by_chat_id = str(chat_id)
    session.flush()

    user = session.get(User, record.user_id)
    if user is not None:
        user.telegram_chat_id = str(chat_id)

    audit.record(
        session,
        tenant_id=record.tenant_id,
        action="telegram.chat_linked",
        actor_id=record.user_id,
        resource_type="user",
        resource_id=record.user_id,
        payload={"chat_id": str(chat_id), "username": username},
    )
    return chat


def unlink(session: Session, *, chat_id: str, now: datetime | None = None) -> bool:
    now = now or utcnow()
    chat = session.execute(
        select(TelegramChat).where(TelegramChat.chat_id == str(chat_id))
    ).scalar_one_or_none()
    if chat is None or not chat.is_active:
        return False

    chat.unlinked_at = now
    chat.state = ""
    chat.state_data = {}
    user = session.get(User, chat.user_id)
    if user is not None and user.telegram_chat_id == str(chat_id):
        user.telegram_chat_id = ""
    session.flush()

    audit.record(
        session,
        tenant_id=chat.tenant_id,
        action="telegram.chat_unlinked",
        actor_id=chat.user_id,
        resource_type="user",
        resource_id=chat.user_id,
        payload={"chat_id": str(chat_id)},
    )
    return True


def get_chat(session: Session, chat_id: str) -> TelegramChat | None:
    chat = session.execute(
        select(TelegramChat).where(TelegramChat.chat_id == str(chat_id))
    ).scalar_one_or_none()
    if chat is None or not chat.is_active:
        return None
    return chat


def principal_for_chat(session: Session, chat: TelegramChat) -> Principal:
    """Build the caller identity, re-reading roles so revocation is immediate."""
    user = session.get(User, chat.user_id)
    if user is None or not user.is_active:
        raise ValidationError("аккаунт отключён — обратитесь к администратору")
    return _principal_from_user(session, user)


def set_state(session: Session, chat: TelegramChat, state: str, data: dict | None = None) -> None:
    chat.state = state
    chat.state_data = data or {}
    chat.last_message_at = utcnow()
    session.flush()


def clear_state(session: Session, chat: TelegramChat) -> None:
    set_state(session, chat, "", {})


def purge_expired_codes(session: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    rows = (
        session.execute(
            select(TelegramLinkCode).where(
                TelegramLinkCode.used_at.is_(None), TelegramLinkCode.expires_at <= now
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        session.delete(row)
    session.flush()
    return len(rows)
