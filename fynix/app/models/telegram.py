"""Telegram binding and conversation state (spec §4.2 "Telegram-интерфейс").

A Telegram chat is not an identity. Before the bot will do anything on a user's
behalf, the chat must be bound to a real platform user through a single-use,
short-lived code issued inside the platform. Everything the bot then does runs
through the same policy engine as the REST API.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import (
    Base,
    TenantScopedMixin,
    TimestampMixin,
    TZDateTime,
    new_id,
)


class TelegramLinkCode(Base, TimestampMixin, TenantScopedMixin):
    """Single-use code that binds a Telegram chat to a platform user."""

    __tablename__ = "telegram_link_codes"
    __table_args__ = (UniqueConstraint("code", name="uq_telegram_link_code"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("tlc"))
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    used_by_chat_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    issued_by: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    def is_usable(self, now: datetime) -> bool:
        return self.used_at is None and self.expires_at > now


class TelegramChat(Base, TimestampMixin, TenantScopedMixin):
    """A bound chat plus its conversation state.

    Conversation state lives in the database rather than in memory so a restart
    or a second worker does not lose a half-entered brief.
    """

    __tablename__ = "telegram_chats"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_telegram_chat"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("tgc"))
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    # Conversation step: "" (idle) | "awaiting_project_name" | "awaiting_brief"
    state: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    state_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    unlinked_at: Mapped[datetime | None] = mapped_column(TZDateTime)

    @property
    def is_active(self) -> bool:
        return self.unlinked_at is None


class TelegramUpdateCursor(Base, TimestampMixin):
    """Long-polling offset, so a restart does not replay old updates.

    Only one row exists (`id="singleton"`); Telegram's getUpdates offset is
    global to the bot token, not per tenant.
    """

    __tablename__ = "telegram_update_cursor"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="singleton")
    last_update_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
