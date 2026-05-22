"""Weekly report entry point — generates and sends analytics report to Telegram."""

from __future__ import annotations

import logging
import sys

from src.analytics import ChannelAnalytics
from src.telegram_bot import TelegramPoster

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("=== VPN Marketing Machine — Weekly Report ===")

    analytics = ChannelAnalytics()
    poster = TelegramPoster()

    try:
        # 1. Generate report
        logger.info("Generating weekly report...")
        report_text = analytics.generate_weekly_report()
        logger.info("Report generated (%d chars)", len(report_text))

        # 2. Save report file
        report_path = analytics.save_report_file()
        logger.info("Report saved to: %s", report_path)

        # 3. Send report text to channel
        logger.info("Sending report to Telegram...")
        poster.send_message(report_text, parse_mode="")
        logger.info("Report message sent")

        # 4. Send CSV file
        csv_path = report_path.replace("weekly_report_", "weekly_data_").replace(".txt", ".csv")
        try:
            poster.send_document(csv_path, caption="📊 Данные за неделю (CSV)")
            logger.info("CSV report sent")
        except Exception:
            logger.warning("Failed to send CSV file", exc_info=True)

    except Exception:
        logger.critical("Failed to generate/send report", exc_info=True)
        raise
    finally:
        analytics.close()
        poster.close()

    logger.info("=== Weekly report done! ===")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.critical("Fatal error", exc_info=True)
        sys.exit(1)
