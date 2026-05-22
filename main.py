"""Main entry point — scrape news, generate content, post to Telegram."""

from __future__ import annotations

import logging
import random
import sys

from src.analytics import ChannelAnalytics
from src.config import POST_TYPES
from src.generator import ContentGenerator
from src.scraper import NewsScraper
from src.telegram_bot import TelegramPoster

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("=== VPN Marketing Machine — Daily Post ===")

    # 1. Scrape news
    logger.info("Step 1: Scraping news...")
    scraper = NewsScraper()
    try:
        articles = scraper.get_random_articles(count=5)
        logger.info("Found %d relevant articles", len(articles))
    except Exception:
        logger.warning("News scraping failed, will generate topic-based post", exc_info=True)
        articles = []
    finally:
        scraper.close()

    # 2. Generate content
    logger.info("Step 2: Generating content...")
    generator = ContentGenerator()
    post_text = generator.generate_post(articles if articles else None)
    logger.info("Generated post (%d chars)", len(post_text))

    # 3. Post to Telegram
    logger.info("Step 3: Posting to Telegram...")
    poster = TelegramPoster()
    try:
        result = poster.send_message(post_text, parse_mode="Markdown")
        message_id = result["message_id"]
        logger.info("Posted successfully! message_id=%d", message_id)
    except Exception:
        logger.error("Failed to post with Markdown, trying plain text", exc_info=True)
        result = poster.send_message(post_text, parse_mode="")
        message_id = result["message_id"]
        logger.info("Posted as plain text, message_id=%d", message_id)
    finally:
        poster.close()

    # 4. Log the post for analytics
    logger.info("Step 4: Logging post for analytics...")
    analytics = ChannelAnalytics()
    try:
        post_type = random.choice(POST_TYPES)
        analytics.log_post(message_id, post_text[:200], post_type)
        analytics.collect_snapshot()
        logger.info("Post logged and snapshot collected")
    except Exception:
        logger.warning("Failed to log analytics", exc_info=True)
    finally:
        analytics.close()

    logger.info("=== Done! ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.critical("Fatal error", exc_info=True)
        sys.exit(1)
