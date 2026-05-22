"""Configuration for the VPN Marketing Machine."""

import os

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

# OpenAI
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# Content settings
POST_LANGUAGE = os.environ.get("POST_LANGUAGE", "ru")
POSTS_PER_DAY = int(os.environ.get("POSTS_PER_DAY", "2"))
MAX_POST_LENGTH = int(os.environ.get("MAX_POST_LENGTH", "4000"))

# RSS feeds for VPN/cybersecurity news
NEWS_FEEDS = [
    "https://www.bleepingcomputer.com/feed/",
    "https://feeds.feedburner.com/TheHackersNews",
    "https://krebsonsecurity.com/feed/",
    "https://www.darkreading.com/rss.xml",
    "https://threatpost.com/feed/",
    "https://www.schneier.com/feed/atom/",
    "https://nakedsecurity.sophos.com/feed/",
    "https://www.wired.com/feed/category/security/latest/rss",
]

# Topics for content generation
VPN_TOPICS = [
    "VPN и онлайн-безопасность",
    "Защита персональных данных в интернете",
    "Обход блокировок и цензуры",
    "Кибербезопасность для обычных пользователей",
    "Сравнение VPN-протоколов",
    "Приватность в интернете",
    "Безопасность публичных Wi-Fi",
    "Защита от слежки в интернете",
    "Анонимность в сети",
    "Утечки данных и как от них защититься",
    "Шифрование трафика",
    "Безопасность мобильных устройств",
    "Законы о VPN в разных странах",
    "Советы по цифровой гигиене",
    "Новости кибербезопасности",
]

# Post types
POST_TYPES = [
    "news",        # News-based post
    "tip",         # Security tip
    "educational", # Educational content
    "comparison",  # VPN comparison
    "fact",        # Interesting fact
]
