"""
Nintendo MyNintendo Rewards Monitor
====================================
Continuously scrapes Nintendo's platinum-point rewards page and keeps a
Discord channel up-to-date via a webhook.

Usage
-----
1. Copy config.json.example → config.json and fill in your webhook URL.
2. pip install -r requirements.txt
3. python monitor.py

Optional env-var overrides
--------------------------
NINTENDO_WEBHOOK_URL   — Discord webhook URL (overrides config.json)
NINTENDO_CHECK_INTERVAL — seconds between checks (overrides config.json)
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from scraper import RewardItem, fetch_rewards, diff_items
from webhook import DiscordWebhook

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_FILE = Path("config.json")
ITEMS_CACHE = Path("items_cache.json")

DEFAULT_CHECK_INTERVAL = 300  # 5 minutes


def load_config() -> dict:
    config: dict = {}

    if CONFIG_FILE.exists():
        try:
            config = json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError as exc:
            logger.error("Invalid config.json: %s", exc)
            sys.exit(1)

    # Environment variables take precedence
    env_url = os.environ.get("NINTENDO_WEBHOOK_URL")
    if env_url:
        config["webhook_url"] = env_url

    env_interval = os.environ.get("NINTENDO_CHECK_INTERVAL")
    if env_interval:
        try:
            config["check_interval"] = int(env_interval)
        except ValueError:
            logger.warning("Invalid NINTENDO_CHECK_INTERVAL value; using default")

    if not config.get("webhook_url"):
        logger.error(
            "No webhook URL configured. Set 'webhook_url' in config.json "
            "or the NINTENDO_WEBHOOK_URL environment variable."
        )
        sys.exit(1)

    config.setdefault("check_interval", DEFAULT_CHECK_INTERVAL)
    return config


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def save_items(items: list[RewardItem]) -> None:
    ITEMS_CACHE.write_text(json.dumps([i.to_dict() for i in items], indent=2))


def load_items() -> list[RewardItem]:
    if not ITEMS_CACHE.exists():
        return []
    try:
        raw = json.loads(ITEMS_CACHE.read_text())
        return [RewardItem.from_dict(d) for d in raw]
    except Exception as exc:
        logger.warning("Could not load item cache: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Monitor loop
# ---------------------------------------------------------------------------

def run_check(webhook: DiscordWebhook, previous: list[RewardItem]) -> list[RewardItem]:
    """
    Fetch current items, diff against previous state, push Discord updates.
    Returns the new item list (or the previous list on fetch failure).
    """
    try:
        current = fetch_rewards()
    except Exception as exc:
        logger.error("Failed to fetch rewards: %s", exc)
        webhook.send_error(f"Failed to fetch rewards page:\n```\n{exc}\n```")
        return previous

    if not current:
        logger.warning("Empty item list returned — skipping update to avoid false clears")
        return previous

    # Diff against previous state
    back_in_stock, went_out, new_items = diff_items(previous, current)

    any_change = back_in_stock or went_out or new_items
    if any_change:
        logger.info(
            "Changes detected — back_in_stock=%d, went_out=%d, new_items=%d",
            len(back_in_stock), len(went_out), len(new_items),
        )
        webhook.send_alerts(back_in_stock, went_out, new_items)
    else:
        logger.info("No stock changes detected")

    # Always refresh the inventory embed (timestamp in footer keeps it "live")
    webhook.update_inventory(current)
    save_items(current)

    return current


def main() -> None:
    config = load_config()
    webhook_url: str = config["webhook_url"]
    interval: int = int(config["check_interval"])

    logger.info("=" * 60)
    logger.info("Nintendo Rewards Monitor starting")
    logger.info("Check interval: %ds", interval)
    logger.info("=" * 60)

    webhook = DiscordWebhook(webhook_url)
    previous = load_items()

    consecutive_failures = 0
    max_failures = 5

    while True:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        logger.info("--- Check @ %s ---", now)

        try:
            previous = run_check(webhook, previous)
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            logger.exception("Unexpected error in check loop: %s", exc)

            if consecutive_failures >= max_failures:
                msg = (
                    f"Monitor has failed {consecutive_failures} times in a row. "
                    f"Last error: {exc}"
                )
                logger.critical(msg)
                try:
                    webhook.send_error(msg)
                except Exception:
                    pass

        logger.info("Sleeping %ds until next check…", interval)
        time.sleep(interval)


if __name__ == "__main__":
    main()
