"""Telegram Bot API client.

Deliberately small: the bot only needs to send text, answer callbacks, manage
its webhook and poll for updates. Everything is synchronous because the worker
and the request handler that use it are synchronous too.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

import httpx

from app.config import get_settings
from app.core.errors import ProviderError
from app.core.logging import get_logger
from app.core.redaction import redact_text

log = get_logger("fynix.telegram")

API_ROOT = "https://api.telegram.org"
#: Telegram rejects messages longer than this.
MAX_MESSAGE_LEN = 4096
#: Cap on brief attachments. Telegram allows 20 MB; half of that is plenty for
#: a requirements document and bounds what one chat can push into storage.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class Button:
    text: str
    callback_data: str


def keyboard(rows: list[list[Button]]) -> dict:
    return {
        "inline_keyboard": [
            [{"text": b.text, "callback_data": b.callback_data} for b in row] for row in rows
        ]
    }


def escape(text: str) -> str:
    """Escape for Telegram's HTML parse mode."""
    return html.escape(text or "", quote=False)


def chunk(text: str, limit: int = MAX_MESSAGE_LEN) -> list[str]:
    """Split on line boundaries so a long report stays readable."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            if current:
                parts.append(current)
            # A single line longer than the limit is cut hard; nothing else to do.
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return parts


class TelegramClient:
    def __init__(self, token: str | None = None, client: httpx.Client | None = None) -> None:
        settings = get_settings()
        self._token = token if token is not None else settings.telegram_bot_token
        self._client = client
        self._timeout = 30

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _call(self, method: str, payload: dict, *, timeout: int | None = None) -> dict:
        if not self._token:
            raise ProviderError("TELEGRAM_BOT_TOKEN is not configured", permanent=True)
        url = f"{API_ROOT}/bot{self._token}/{method}"
        http = self._client or httpx.Client(timeout=timeout or self._timeout)
        try:
            response = http.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"telegram timeout on {method}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"telegram transport error on {method}: {exc}") from exc
        finally:
            if self._client is None:
                http.close()

        if response.status_code >= 400:
            # 429 and 5xx are worth retrying; the rest will fail again.
            permanent = 400 <= response.status_code < 500 and response.status_code != 429
            raise ProviderError(
                f"telegram {method} returned {response.status_code}: "
                f"{redact_text(response.text)[:300]}",
                permanent=permanent,
                details={"status": response.status_code, "method": method},
            )
        body = response.json()
        if not body.get("ok"):
            raise ProviderError(
                f"telegram {method} failed: {redact_text(str(body.get('description')))[:300]}",
                permanent=True,
            )
        return body.get("result", {})

    # -- messaging ---------------------------------------------------------

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict | None = None,
        parse_mode: str = "HTML",
    ) -> list[dict]:
        """Send a message, splitting it if it exceeds Telegram's limit."""
        sent = []
        parts = chunk(text)
        for index, part in enumerate(parts):
            payload: dict = {
                "chat_id": chat_id,
                "text": part,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            # The keyboard belongs on the last chunk only.
            if reply_markup and index == len(parts) - 1:
                payload["reply_markup"] = reply_markup
            sent.append(self._call("sendMessage", payload))
        return sent

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> dict:
        return self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:200], "show_alert": alert},
        )

    def edit_message_text(
        self, chat_id: str, message_id: int, text: str, *, parse_mode: str = "HTML"
    ) -> dict:
        return self._call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text[:MAX_MESSAGE_LEN],
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
        )

    # -- lifecycle ---------------------------------------------------------

    def get_me(self) -> dict:
        return self._call("getMe", {})

    def set_webhook(self, url: str, secret_token: str, drop_pending: bool = True) -> dict:
        return self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "drop_pending_updates": drop_pending,
                "allowed_updates": ["message", "callback_query"],
            },
        )

    def delete_webhook(self, drop_pending: bool = False) -> dict:
        return self._call("deleteWebhook", {"drop_pending_updates": drop_pending})

    def get_webhook_info(self) -> dict:
        return self._call("getWebhookInfo", {})

    # -- files -------------------------------------------------------------

    def get_file(self, file_id: str) -> dict:
        return self._call("getFile", {"file_id": file_id})

    def download_file(self, file_path: str, max_bytes: int = MAX_ATTACHMENT_BYTES) -> bytes:
        """Fetch an uploaded file, refusing anything over `max_bytes`.

        The size is checked while streaming rather than trusting the header, so
        a lying Content-Length cannot make the worker buffer an arbitrary file.
        """
        if not self._token:
            raise ProviderError("TELEGRAM_BOT_TOKEN is not configured", permanent=True)
        url = f"{API_ROOT}/file/bot{self._token}/{file_path}"
        http = self._client or httpx.Client(timeout=60)
        try:
            with http.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise ProviderError(
                        f"telegram file download returned {response.status_code}",
                        permanent=response.status_code < 500,
                    )
                buffer = bytearray()
                for piece in response.iter_bytes():
                    buffer.extend(piece)
                    if len(buffer) > max_bytes:
                        raise ProviderError(
                            f"attachment exceeds {max_bytes // (1024 * 1024)} MB",
                            permanent=True,
                            details={"reason": "attachment_too_large"},
                        )
                return bytes(buffer)
        except httpx.HTTPError as exc:
            raise ProviderError(f"telegram file transport error: {exc}") from exc
        finally:
            if self._client is None:
                http.close()

    def get_updates(self, offset: int, timeout: int = 25, limit: int = 50) -> list[dict]:
        """Long polling. Used when the server has no public HTTPS endpoint."""
        return self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "limit": limit,
                "allowed_updates": ["message", "callback_query"],
            },
            # Wait a little longer than Telegram will hold the request open.
            timeout=timeout + 15,
        )
