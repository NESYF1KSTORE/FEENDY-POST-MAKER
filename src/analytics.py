"""Analytics module — tracks channel performance and generates weekly reports."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from src.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
STATS_FILE = DATA_DIR / "channel_stats.json"
POSTS_LOG_FILE = DATA_DIR / "posts_log.json"


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


class ChannelAnalytics:
    """Collects and analyzes Telegram channel statistics."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        channel_id: Optional[str] = None,
    ) -> None:
        self.bot_token = bot_token or TELEGRAM_BOT_TOKEN
        self.channel_id = channel_id or TELEGRAM_CHANNEL_ID
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self.client = httpx.Client(timeout=30.0)
        _ensure_data_dir()

    def collect_snapshot(self) -> Dict[str, Any]:
        """Collect a snapshot of current channel metrics."""
        snapshot: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "member_count": self._get_member_count(),
        }

        chat_info = self._get_chat_info()
        if chat_info:
            snapshot["title"] = chat_info.get("title", "")
            snapshot["description"] = chat_info.get("description", "")

        self._save_snapshot(snapshot)
        logger.info("Channel snapshot collected: %s", snapshot)
        return snapshot

    def log_post(self, message_id: int, text_preview: str, post_type: str) -> None:
        """Log a published post for tracking."""
        _ensure_data_dir()
        posts = self._load_posts_log()
        posts.append({
            "message_id": message_id,
            "text_preview": text_preview[:200],
            "post_type": post_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        POSTS_LOG_FILE.write_text(json.dumps(posts, ensure_ascii=False, indent=2))

    def generate_weekly_report(self) -> str:
        """Generate a weekly analytics report as formatted text."""
        stats = self._load_stats()
        posts = self._load_posts_log()

        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)

        # Filter data for the last week
        weekly_stats = [
            s for s in stats
            if datetime.fromisoformat(s["timestamp"]) >= week_ago
        ]
        weekly_posts = [
            p for p in posts
            if datetime.fromisoformat(p["timestamp"]) >= week_ago
        ]

        current_members = self._get_member_count()

        # Calculate growth
        members_start = (
            weekly_stats[0].get("member_count", current_members)
            if weekly_stats
            else current_members
        )
        member_growth = current_members - members_start
        growth_pct = (
            (member_growth / members_start * 100) if members_start > 0 else 0
        )

        # Post type breakdown
        type_counts: Dict[str, int] = {}
        for post in weekly_posts:
            pt = post.get("post_type", "unknown")
            type_counts[pt] = type_counts.get(pt, 0) + 1

        report_date = now.strftime("%d.%m.%Y")
        week_start = week_ago.strftime("%d.%m.%Y")

        report = f"""📊 ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ
━━━━━━━━━━━━━━━━━━━━━
📅 Период: {week_start} — {report_date}

👥 ПОДПИСЧИКИ
• Текущее количество: {current_members}
• Рост за неделю: {member_growth:+d} ({growth_pct:+.1f}%)
• На начало периода: {members_start}

📝 ПУБЛИКАЦИИ
• Всего постов за неделю: {len(weekly_posts)}
"""

        if type_counts:
            report += "• По типам:\n"
            type_labels = {
                "news": "📰 Новости",
                "tip": "💡 Советы",
                "educational": "📚 Образовательные",
                "comparison": "⚖️ Сравнения",
                "fact": "🔍 Факты",
                "unknown": "📋 Другое",
            }
            for ptype, count in sorted(type_counts.items()):
                label = type_labels.get(ptype, ptype)
                report += f"  - {label}: {count}\n"

        report += f"""
📈 АКТИВНОСТЬ
• Среднее постов/день: {len(weekly_posts) / 7:.1f}

━━━━━━━━━━━━━━━━━━━━━
🤖 Отчёт сгенерирован автоматически
"""
        return report

    def generate_csv_report(self) -> str:
        """Generate a CSV report of weekly data."""
        posts = self._load_posts_log()
        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)

        weekly_posts = [
            p for p in posts
            if datetime.fromisoformat(p["timestamp"]) >= week_ago
        ]

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Дата", "ID сообщения", "Тип", "Превью текста"])

        for post in weekly_posts:
            ts = datetime.fromisoformat(post["timestamp"]).strftime("%d.%m.%Y %H:%M")
            writer.writerow([
                ts,
                post.get("message_id", ""),
                post.get("post_type", ""),
                post.get("text_preview", "")[:100],
            ])

        return output.getvalue()

    def save_report_file(self) -> str:
        """Save weekly report to a file and return the path."""
        _ensure_data_dir()
        report_text = self.generate_weekly_report()
        csv_data = self.generate_csv_report()

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report_path = DATA_DIR / f"weekly_report_{date_str}.txt"
        csv_path = DATA_DIR / f"weekly_data_{date_str}.csv"

        report_path.write_text(report_text, encoding="utf-8")
        csv_path.write_text(csv_data, encoding="utf-8")

        logger.info("Report saved: %s", report_path)
        return str(report_path)

    # --- Private helpers ---

    def _get_member_count(self) -> int:
        try:
            url = f"{self.api_base}/getChatMemberCount"
            resp = self.client.post(url, json={"chat_id": self.channel_id})
            data = resp.json()
            return data["result"] if data.get("ok") else 0
        except Exception:
            logger.warning("Failed to get member count", exc_info=True)
            return 0

    def _get_chat_info(self) -> Optional[Dict[str, Any]]:
        try:
            url = f"{self.api_base}/getChat"
            resp = self.client.post(url, json={"chat_id": self.channel_id})
            data = resp.json()
            return data.get("result") if data.get("ok") else None
        except Exception:
            logger.warning("Failed to get chat info", exc_info=True)
            return None

    def _save_snapshot(self, snapshot: Dict[str, Any]) -> None:
        stats = self._load_stats()
        stats.append(snapshot)
        STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2))

    def _load_stats(self) -> List[Dict[str, Any]]:
        if STATS_FILE.exists():
            try:
                return json.loads(STATS_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _load_posts_log(self) -> List[Dict[str, Any]]:
        if POSTS_LOG_FILE.exists():
            try:
                return json.loads(POSTS_LOG_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def close(self) -> None:
        self.client.close()
