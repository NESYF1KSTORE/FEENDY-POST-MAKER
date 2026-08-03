"""Long-polling mode for the bot.

A webhook needs a public HTTPS endpoint with a valid certificate. Until the
server has a domain — which is exactly the state of a fresh install — polling is
the only way the bot can work. Both modes share the same handlers, so behaviour
does not change with the transport.

The update offset lives in the database, so a restart neither replays old
updates nor loses new ones.
"""

from __future__ import annotations

import signal
import time

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import ProviderError
from app.core.logging import clear_context, configure, get_logger, set_correlation_id
from app.db import session_scope
from app.models.telegram import TelegramUpdateCursor
from app.telegram import handlers
from app.telegram.client import TelegramClient

log = get_logger("fynix.telegram")

CURSOR_ID = "singleton"


def _cursor(session: Session) -> TelegramUpdateCursor:
    cursor = session.get(TelegramUpdateCursor, CURSOR_ID)
    if cursor is None:
        cursor = TelegramUpdateCursor(id=CURSOR_ID, last_update_id=0)
        session.add(cursor)
        session.flush()
    return cursor


class Poller:
    def __init__(self, client: TelegramClient | None = None, poll_timeout: int = 25) -> None:
        self._client = client or TelegramClient()
        self._poll_timeout = poll_timeout
        self._running = False

    def stop(self, *_args) -> None:
        log.info("telegram.poller_stop_requested")
        self._running = False

    def run_forever(self) -> None:  # pragma: no cover - process entry point
        settings = get_settings()
        configure(settings.log_level, json_output=settings.is_production)
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        if not self._client.configured:
            log.error("telegram.poller_not_configured", hint="set TELEGRAM_BOT_TOKEN")
            return

        me = self._client.get_me()
        log.info("telegram.poller_started", bot=me.get("username"))
        self._running = True

        backoff = 1
        while self._running:
            try:
                self.tick()
                backoff = 1
            except ProviderError as exc:
                # Network blips and 429s must not kill the process.
                log.warning("telegram.poll_failed", error=str(exc)[:200], retry_in=backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
        log.info("telegram.poller_stopped")

    def tick(self) -> int:
        """Fetch and process one batch of updates. Returns how many were handled."""
        with session_scope() as session:
            offset = _cursor(session).last_update_id + 1

        updates = self._client.get_updates(offset, timeout=self._poll_timeout)
        if not updates:
            return 0

        for update in updates:
            update_id = int(update.get("update_id", 0))
            set_correlation_id(f"tg-{update_id}")
            try:
                self.process(update)
            except Exception:
                log.exception("telegram.update_failed", update_id=update_id)
            finally:
                # Advance past the update regardless: a poisonous update that
                # always throws would otherwise block every later message.
                with session_scope() as session:
                    cursor = _cursor(session)
                    cursor.last_update_id = max(cursor.last_update_id, update_id)
                clear_context()
        return len(updates)

    def process(self, update: dict) -> None:
        with session_scope() as session:
            result = handlers.handle_update(session, update)
        if result is None:
            return
        chat_id, reply = result
        if "callback_query" in update:
            self._client.answer_callback(update["callback_query"]["id"], reply.toast)
        self._client.send_message(chat_id, reply.text, reply_markup=reply.reply_markup)


def main() -> None:  # pragma: no cover - process entry point
    Poller().run_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
