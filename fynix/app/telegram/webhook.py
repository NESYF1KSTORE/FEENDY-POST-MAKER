"""Telegram webhook endpoint.

The URL is effectively public, so the secret token Telegram echoes back is the
only thing standing between the bot and anyone who guesses the path. It is
compared in constant time and a mismatch is refused before the body is parsed.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, Response

from app.api.deps import DbSession
from app.config import get_settings
from app.core.errors import AuthenticationError, ProviderError
from app.core.logging import get_logger
from app.telegram import handlers
from app.telegram.client import TelegramClient

log = get_logger("fynix.telegram")

router = APIRouter(prefix="/v1/telegram", tags=["telegram"], include_in_schema=False)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


@router.post("/webhook")
def telegram_webhook(
    request: Request,
    update: dict,
    session: DbSession,
    secret_token: Annotated[str | None, Header(alias=SECRET_HEADER)] = None,
):
    """Receive one update. Always answers 200 once authenticated.

    Telegram retries any non-2xx response, so an application bug would turn into
    an endless redelivery loop. Errors are logged and swallowed instead; the
    user gets a message, not a retry storm.
    """
    import hmac

    settings = get_settings()
    expected = settings.telegram_webhook_secret
    if not expected:
        raise AuthenticationError("telegram webhook is not configured")
    if not secret_token or not hmac.compare_digest(secret_token, expected):
        log.warning("telegram.webhook_bad_secret", client=request.client.host if request.client else "")
        raise AuthenticationError("invalid webhook secret")

    try:
        result = handlers.handle_update(session, update)
    except Exception:
        log.exception("telegram.handler_failed", update_id=update.get("update_id"))
        return Response(status_code=200)

    if result is None:
        return Response(status_code=200)

    chat_id, reply = result
    client = TelegramClient()
    try:
        if "callback_query" in update:
            client.answer_callback(update["callback_query"]["id"], reply.toast)
        client.send_message(chat_id, reply.text, reply_markup=reply.reply_markup)
    except ProviderError as exc:
        log.warning("telegram.send_failed", chat_id=chat_id, error=str(exc)[:200])
    return Response(status_code=200)
