"""Telegram bot module — posts content to a Telegram channel."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import httpx

from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"


class TelegramPoster:
    """Posts messages and files to a Telegram channel via Bot API."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        channel_id: Optional[str] = None,
    ) -> None:
        self.bot_token = bot_token or TELEGRAM_BOT_TOKEN
        self.channel_id = channel_id or TELEGRAM_CHANNEL_ID

        if not self.bot_token:
            raise ValueError(
                "Telegram bot token is required. "
                "Set TELEGRAM_BOT_TOKEN environment variable."
            )
        if not self.channel_id:
            raise ValueError(
                "Telegram channel ID is required. "
                "Set TELEGRAM_CHANNEL_ID environment variable."
            )

        self.api_base = TELEGRAM_API_BASE.format(token=self.bot_token)
        self.client = httpx.Client(timeout=30.0)

    def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = False,
    ) -> dict:
        """Send a text message to the channel."""
        url = f"{self.api_base}/sendMessage"
        payload = {
            "chat_id": self.channel_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }

        response = self.client.post(url, json=payload)
        result = response.json()

        if not result.get("ok"):
            logger.error("Failed to send message: %s", result)
            raise RuntimeError(f"Telegram API error: {result.get('description', 'Unknown error')}")

        message_id = result["result"]["message_id"]
        logger.info("Message sent successfully, message_id=%d", message_id)
        return result["result"]

    def send_document(
        self,
        file_path: str,
        caption: Optional[str] = None,
    ) -> dict:
        """Send a document/file to the channel."""
        url = f"{self.api_base}/sendDocument"

        path = Path(file_path)
        with path.open("rb") as f:
            files = {"document": (path.name, f, "application/octet-stream")}
            data = {"chat_id": self.channel_id}
            if caption:
                data["caption"] = caption

            response = self.client.post(url, data=data, files=files)

        result = response.json()

        if not result.get("ok"):
            logger.error("Failed to send document: %s", result)
            raise RuntimeError(f"Telegram API error: {result.get('description', 'Unknown error')}")

        logger.info("Document sent successfully: %s", path.name)
        return result["result"]

    def get_chat_info(self) -> dict:
        """Get information about the channel."""
        url = f"{self.api_base}/getChat"
        payload = {"chat_id": self.channel_id}

        response = self.client.post(url, json=payload)
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result.get('description', 'Unknown error')}")

        return result["result"]

    def get_chat_member_count(self) -> int:
        """Get the number of members in the channel."""
        url = f"{self.api_base}/getChatMemberCount"
        payload = {"chat_id": self.channel_id}

        response = self.client.post(url, json=payload)
        result = response.json()

        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result.get('description', 'Unknown error')}")

        return result["result"]

    def close(self) -> None:
        self.client.close()
