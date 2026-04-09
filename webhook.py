"""
Discord webhook manager for the Nintendo rewards monitor.

Maintains a persistent "inventory" embed message that is edited in-place
each check cycle, and fires separate alert messages when stock changes.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Discord API base — we only need the webhook endpoints
DISCORD_API = "https://discord.com/api/v10"

# Where we persist the inventory message ID between runs
STATE_FILE = Path("state.json")

# Colour constants (Discord uses decimal integers)
COLOUR_IN_STOCK = 0x57F287   # green
COLOUR_OUT_OF_STOCK = 0xED4245  # red
COLOUR_NEUTRAL = 0x5865F2    # blurple
COLOUR_ALERT_BACK = 0x57F287
COLOUR_ALERT_OUT = 0xED4245
COLOUR_ALERT_NEW = 0xFEE75C   # yellow

# Discord embed field / description hard limits
EMBED_DESC_LIMIT = 4096
FIELD_VALUE_LIMIT = 1024
MAX_EMBEDS_PER_MESSAGE = 10


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _webhook_url(webhook_url: str) -> str:
    """Return the base webhook URL (strip trailing slash)."""
    return webhook_url.rstrip("/")


def _request(
    method: str,
    url: str,
    payload: dict,
    retries: int = 3,
    wait: float = 1.0,
) -> Optional[dict]:
    """Send a JSON request to Discord, honouring rate-limit headers."""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(method, url, json=payload, timeout=15)

            # Handle rate limiting
            if resp.status_code == 429:
                retry_after = float(resp.json().get("retry_after", 1))
                logger.warning("Discord rate limited — sleeping %.1fs", retry_after)
                time.sleep(retry_after + 0.1)
                continue

            resp.raise_for_status()

            if resp.content:
                return resp.json()
            return {}

        except requests.RequestException as exc:
            logger.warning("Discord request failed (attempt %d/%d): %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(wait * attempt)

    logger.error("All %d Discord request attempts failed for %s %s", retries, method, url)
    return None


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "…"


def _build_inventory_embeds(items: list) -> list[dict]:
    """
    Build one or more Discord embeds listing all reward items.
    Items are split across multiple embeds if the description would exceed Discord's limit.
    """
    from scraper import RewardItem  # local import to avoid circular deps

    in_stock = [i for i in items if i.in_stock]
    out_of_stock = [i for i in items if not i.in_stock]

    def format_item(item: RewardItem) -> str:
        return f"{item.stock_emoji()} **[{item.name}]({item.url})** — {item.platinum_points:,} pts\n"

    def chunk_items(item_list: list[RewardItem], header: str) -> list[dict]:
        """Return embed field dicts, splitting if needed."""
        if not item_list:
            return [{"name": header, "value": "_None_", "inline": False}]

        fields = []
        current_lines: list[str] = []
        current_len = 0
        part = 1

        for item in item_list:
            line = format_item(item)
            if current_len + len(line) > FIELD_VALUE_LIMIT and current_lines:
                name = header if part == 1 else f"{header} (cont.)"
                fields.append({"name": name, "value": "".join(current_lines), "inline": False})
                current_lines = []
                current_len = 0
                part += 1
            current_lines.append(line)
            current_len += len(line)

        if current_lines:
            name = header if part == 1 else f"{header} (cont.)"
            fields.append({"name": name, "value": "".join(current_lines), "inline": False})

        return fields

    in_stock_fields = chunk_items(in_stock, f"In Stock ({len(in_stock)})")
    out_fields = chunk_items(out_of_stock, f"Out of Stock ({len(out_of_stock)})")
    all_fields = in_stock_fields + out_fields

    # Split fields across embeds (max 25 fields each)
    embed_chunks: list[list[dict]] = []
    chunk: list[dict] = []
    for field in all_fields:
        if len(chunk) == 25:
            embed_chunks.append(chunk)
            chunk = []
        chunk.append(field)
    if chunk:
        embed_chunks.append(chunk)

    embeds = []
    for idx, fields in enumerate(embed_chunks):
        embed: dict = {
            "color": COLOUR_NEUTRAL,
            "fields": fields,
        }
        if idx == 0:
            embed["title"] = "MyNintendo Platinum Point Rewards"
            embed["url"] = "https://www.nintendo.com/us/store/exclusives/rewards/"
            embed["description"] = (
                f"**{len(in_stock)}** item(s) in stock out of **{len(items)}** total."
            )
            embed["footer"] = {"text": "Updates automatically • nintendo.com/us/store/exclusives/rewards/"}
        embeds.append(embed)

    return embeds[:MAX_EMBEDS_PER_MESSAGE]


def _build_alert_embed(item, event: str) -> dict:
    """Build a small alert embed for a single stock-change event."""
    from scraper import RewardItem  # local import

    if event == "back_in_stock":
        colour = COLOUR_ALERT_BACK
        title = "Back In Stock!"
        desc = f"**{item.name}** is now available for **{item.platinum_points:,} platinum points**."
    elif event == "went_out":
        colour = COLOUR_ALERT_OUT
        title = "Out of Stock"
        desc = f"**{item.name}** ({item.platinum_points:,} pts) is no longer available."
    else:  # new_item
        colour = COLOUR_ALERT_NEW
        title = "New Reward Item!"
        desc = f"**{item.name}** has been added for **{item.platinum_points:,} platinum points**."

    embed: dict = {
        "title": title,
        "description": desc,
        "color": colour,
        "url": item.url,
    }
    if item.image_url:
        embed["thumbnail"] = {"url": item.image_url}

    return embed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class DiscordWebhook:
    """Thin wrapper around a single Discord webhook."""

    def __init__(self, url: str):
        self._url = _webhook_url(url)
        self._state = _load_state()

    # ------------------------------------------------------------------
    # Inventory message (persistent, edited in-place)
    # ------------------------------------------------------------------

    def update_inventory(self, items: list) -> None:
        """
        Post or edit the pinned inventory message with the current item list.
        The message ID is persisted in state.json so it survives restarts.
        """
        embeds = _build_inventory_embeds(items)
        payload = {"embeds": embeds}

        msg_id = self._state.get("inventory_message_id")

        if msg_id:
            # Edit existing message
            edit_url = f"{self._url}/messages/{msg_id}"
            result = _request("PATCH", edit_url, payload)
            if result is not None:
                logger.info("Updated inventory message %s", msg_id)
                return
            # If edit failed (e.g. message was deleted), fall through to re-post
            logger.warning("Failed to edit inventory message — re-posting")
            self._state.pop("inventory_message_id", None)

        # Post new message (append ?wait=true so Discord returns the message object)
        post_url = f"{self._url}?wait=true"
        result = _request("POST", post_url, payload)
        if result and "id" in result:
            self._state["inventory_message_id"] = result["id"]
            _save_state(self._state)
            logger.info("Posted new inventory message %s", result["id"])

    # ------------------------------------------------------------------
    # Alert messages (ephemeral — one per stock-change event)
    # ------------------------------------------------------------------

    def send_alert(self, item, event: str) -> None:
        """Send a one-off alert for a stock change."""
        embed = _build_alert_embed(item, event)
        payload = {"embeds": [embed]}
        result = _request("POST", f"{self._url}?wait=true", payload)
        if result:
            logger.info("Sent %s alert for '%s'", event, item.name)

    def send_alerts(
        self,
        back_in_stock: list,
        went_out: list,
        new_items: list,
    ) -> None:
        """Fire alert messages for all stock changes."""
        for item in back_in_stock:
            self.send_alert(item, "back_in_stock")
            time.sleep(0.5)  # be polite to the API
        for item in went_out:
            self.send_alert(item, "went_out")
            time.sleep(0.5)
        for item in new_items:
            self.send_alert(item, "new_item")
            time.sleep(0.5)

    def send_error(self, message: str) -> None:
        """Post a plain error notification."""
        payload = {
            "embeds": [{
                "title": "Monitor Error",
                "description": _truncate(message, EMBED_DESC_LIMIT),
                "color": 0xED4245,
            }]
        }
        _request("POST", f"{self._url}?wait=true", payload)
