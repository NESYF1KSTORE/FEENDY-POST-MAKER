"""News scraper module — fetches VPN/cybersecurity news from RSS feeds and web."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import feedparser
import httpx
from bs4 import BeautifulSoup

from src.config import NEWS_FEEDS

logger = logging.getLogger(__name__)


@dataclass
class NewsArticle:
    title: str
    summary: str
    url: str
    source: str
    published: Optional[datetime] = None
    tags: List[str] = field(default_factory=list)


class NewsScraper:
    """Scrapes VPN and cybersecurity news from RSS feeds."""

    def __init__(self, feeds: Optional[List[str]] = None) -> None:
        self.feeds = feeds or NEWS_FEEDS
        self.client = httpx.Client(
            timeout=15.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; VPNMarketingBot/1.0; "
                    "+https://github.com)"
                )
            },
        )

    def fetch_feed(self, feed_url: str) -> List[NewsArticle]:
        """Fetch and parse a single RSS feed."""
        articles: List[NewsArticle] = []
        try:
            response = self.client.get(feed_url)
            response.raise_for_status()
            feed = feedparser.parse(response.text)

            for entry in feed.entries[:10]:
                summary = ""
                if hasattr(entry, "summary"):
                    soup = BeautifulSoup(entry.summary, "html.parser")
                    summary = soup.get_text(strip=True)[:500]

                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        pass

                tags: List[str] = []
                if hasattr(entry, "tags"):
                    tags = [t.get("term", "") for t in entry.tags if t.get("term")]

                articles.append(
                    NewsArticle(
                        title=entry.get("title", ""),
                        summary=summary,
                        url=entry.get("link", ""),
                        source=feed.feed.get("title", feed_url),
                        published=published,
                        tags=tags,
                    )
                )
        except Exception:
            logger.warning("Failed to fetch feed: %s", feed_url, exc_info=True)

        return articles

    def fetch_all(self) -> List[NewsArticle]:
        """Fetch articles from all configured feeds."""
        all_articles: List[NewsArticle] = []
        for feed_url in self.feeds:
            articles = self.fetch_feed(feed_url)
            all_articles.extend(articles)
            logger.info("Fetched %d articles from %s", len(articles), feed_url)

        all_articles.sort(
            key=lambda a: a.published or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return all_articles

    def filter_vpn_relevant(self, articles: List[NewsArticle]) -> List[NewsArticle]:
        """Filter articles that are relevant to VPN/privacy/security topics."""
        keywords = [
            "vpn", "privacy", "security", "encryption", "data breach",
            "surveillance", "anonymity", "firewall", "proxy",
            "censorship", "cyber", "hack", "malware", "phishing",
            "ransomware", "password", "authentication", "tor",
            "wireguard", "openvpn", "ipsec", "ssl", "tls",
            "data protection", "gdpr", "tracking", "cookie",
            "безопасность", "приватность", "шифрование", "утечка",
            "блокировка", "анонимность", "кибер", "взлом",
        ]
        relevant: List[NewsArticle] = []
        for article in articles:
            text = f"{article.title} {article.summary} {' '.join(article.tags)}".lower()
            if any(kw in text for kw in keywords):
                relevant.append(article)
        return relevant

    def get_random_articles(self, count: int = 3) -> List[NewsArticle]:
        """Get random relevant articles for content generation."""
        all_articles = self.fetch_all()
        relevant = self.filter_vpn_relevant(all_articles)
        if not relevant:
            relevant = all_articles
        return random.sample(relevant, min(count, len(relevant)))

    def close(self) -> None:
        self.client.close()
