"""AI content generator — creates Telegram posts about VPN/security topics using DeepSeek."""

from __future__ import annotations

import json
import logging
import random
from typing import List, Optional

from openai import OpenAI

from src.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    MAX_POST_LENGTH,
    POST_TYPES,
    VPN_TOPICS,
)
from src.scraper import NewsArticle

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — профессиональный SMM-менеджер и копирайтер для Telegram-канала о VPN-сервисе.
Твоя задача — создавать интересные, информативные и вовлекающие посты на русском языке.

Правила:
1. Пиши на русском языке
2. Используй эмодзи уместно (не перебарщивай)
3. Пост должен быть структурирован и легко читаем
4. Добавляй призыв к действию (CTA) в конце
5. Используй абзацы и переносы строк для удобства чтения
6. Не используй хештеги больше 3-5 штук
7. Длина поста — до 3500 символов
8. Делай посты разнообразными: новости, советы, образовательные, факты
9. Избегай кликбейта, но делай заголовки цепляющими
10. Указывай источник, если пост основан на новости"""

NEWS_POST_PROMPT = """Создай пост для Telegram-канала о VPN на основе этой новости:

Заголовок: {title}
Источник: {source}
Краткое содержание: {summary}
URL: {url}

Перепиши новость в формате Telegram-поста на русском языке.
Добавь свой экспертный комментарий о том, как это связано с VPN и безопасностью.
В конце добавь призыв к действию."""

TOPIC_POST_PROMPT = """Создай оригинальный пост для Telegram-канала о VPN-сервисе.

Тема: {topic}
Тип поста: {post_type}

Напиши интересный и полезный пост на эту тему.
Пост должен быть информативным и побуждать подписчиков к обсуждению.

Типы постов:
- news: новостной пост
- tip: практический совет по безопасности
- educational: образовательный пост (объяснение технологии/концепции)
- comparison: сравнение технологий/подходов
- fact: интересный факт о кибербезопасности"""


class ContentGenerator:
    """Generates Telegram posts using DeepSeek API."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or DEEPSEEK_API_KEY
        if not self.api_key:
            raise ValueError(
                "DeepSeek API key is required. "
                "Set DEEPSEEK_API_KEY environment variable."
            )
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=DEEPSEEK_BASE_URL,
        )

    def generate_from_news(self, article: NewsArticle) -> str:
        """Generate a Telegram post based on a news article."""
        user_prompt = NEWS_POST_PROMPT.format(
            title=article.title,
            source=article.source,
            summary=article.summary,
            url=article.url,
        )
        return self._generate(user_prompt)

    def generate_from_topic(
        self,
        topic: Optional[str] = None,
        post_type: Optional[str] = None,
    ) -> str:
        """Generate an original post on a VPN-related topic."""
        topic = topic or random.choice(VPN_TOPICS)
        post_type = post_type or random.choice(POST_TYPES)

        user_prompt = TOPIC_POST_PROMPT.format(
            topic=topic,
            post_type=post_type,
        )
        return self._generate(user_prompt)

    def generate_post(self, articles: Optional[List[NewsArticle]] = None) -> str:
        """Generate a post — either from news or an original topic."""
        use_news = articles and random.random() < 0.6

        if use_news and articles:
            article = random.choice(articles)
            logger.info("Generating post from news: %s", article.title)
            return self.generate_from_news(article)

        logger.info("Generating original topic post")
        return self.generate_from_topic()

    def _generate(self, user_prompt: str) -> str:
        """Call OpenAI API to generate content."""
        try:
            response = self.client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=2000,
                temperature=0.8,
            )
            content = response.choices[0].message.content or ""
            if len(content) > MAX_POST_LENGTH:
                content = content[:MAX_POST_LENGTH]
            return content.strip()
        except Exception:
            logger.error("Failed to generate content", exc_info=True)
            raise
