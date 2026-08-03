"""Domain events, transactional outbox, webhooks and notifications (spec §9.2)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScopedMixin, TimestampMixin, TZDateTime, new_id


class OutboxEvent(Base, TimestampMixin, TenantScopedMixin):
    """Transactional outbox: events are written in the same transaction as the
    state change that produced them, then published at-least-once."""

    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("evt"))
    schema_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    causation_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)

    published_at: Mapped[datetime | None] = mapped_column(TZDateTime, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Poison messages land here rather than blocking the stream (spec §9.2).
    dead_lettered_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class ProcessedEvent(Base, TimestampMixin):
    """Consumer-side dedupe table — makes at-least-once delivery safe."""

    __tablename__ = "processed_events"
    __table_args__ = (
        UniqueConstraint("consumer", "event_id", name="uq_processed_event"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("pev"))
    consumer: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class WebhookSubscription(Base, TimestampMixin, TenantScopedMixin):
    """Signed outbound webhook (FR-025). The secret is stored hashed for lookup
    and encrypted-at-rest by the database; signatures use HMAC-SHA256."""

    __tablename__ = "webhook_subscriptions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("whk"))
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    event_types: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    secret: Mapped[str] = mapped_column(String(120), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_delivery_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class NotificationLog(Base, TimestampMixin, TenantScopedMixin):
    """Delivery record for Telegram/email/webhook notifications (FR-021)."""

    __tablename__ = "notification_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ntf"))
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    target: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="sent", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)
