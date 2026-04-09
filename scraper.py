"""
Nintendo MyNintendo rewards scraper.

Fetches the rewards page and extracts platinum-point items from the
embedded Next.js __NEXT_DATA__ JSON blob — no headless browser needed.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

REWARDS_URL = "https://www.nintendo.com/us/store/exclusives/rewards/"
PRODUCT_BASE_URL = "https://www.nintendo.com/us/store/products/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class RewardItem:
    sku: str
    name: str
    platinum_points: int
    in_stock: bool
    url: str
    image_url: Optional[str] = None

    def stock_emoji(self) -> str:
        return "✅" if self.in_stock else "❌"

    def to_dict(self) -> dict:
        return {
            "sku": self.sku,
            "name": self.name,
            "platinum_points": self.platinum_points,
            "in_stock": self.in_stock,
            "url": self.url,
            "image_url": self.image_url,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RewardItem":
        return cls(**d)


def _extract_next_data(html: str) -> dict:
    """Pull the __NEXT_DATA__ JSON blob out of the page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        return json.loads(tag.string)

    # Fallback: regex search
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', html, re.DOTALL)
    if match:
        return json.loads(match.group(1))

    raise ValueError("Could not locate __NEXT_DATA__ in page HTML")


def _find_rewards(data: dict) -> list[dict]:
    """
    Walk the Next.js page props tree looking for the rewards product list.
    Nintendo embeds it somewhere under pageProps; we search recursively.
    """
    results: list[dict] = []

    def walk(node):
        if isinstance(node, list):
            # Heuristic: a list of dicts that all have 'platinumPoints' is our list
            if node and isinstance(node[0], dict) and "platinumPoints" in node[0]:
                results.extend(node)
                return
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)

    try:
        page_props = data["props"]["pageProps"]
    except KeyError:
        page_props = data

    walk(page_props)
    return results


def _parse_image_url(product: dict) -> Optional[str]:
    """Build a usable image URL from the Cloudinary product image data."""
    img = product.get("productImage") or product.get("image") or {}
    if isinstance(img, str):
        return img

    # Cloudinary path pattern
    public_id = img.get("publicId") or img.get("public_id", "")
    if public_id:
        return f"https://assets.nintendo.com/image/upload/ar_1:1,b_auto,c_lpad,q_auto:best/f_auto/{public_id}"

    url = img.get("url") or img.get("src", "")
    return url or None


def fetch_rewards(timeout: int = 30) -> list[RewardItem]:
    """
    Fetch and parse all platinum-point reward items from Nintendo's rewards page.

    Returns a list of RewardItem objects sorted by platinum point cost.
    Raises requests.HTTPError or ValueError on failure.
    """
    logger.info("Fetching Nintendo rewards page…")
    resp = requests.get(REWARDS_URL, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()

    next_data = _extract_next_data(resp.text)
    raw_products = _find_rewards(next_data)

    if not raw_products:
        logger.warning("No reward items found in page data — page structure may have changed")
        return []

    items: list[RewardItem] = []
    for p in raw_products:
        try:
            sku = str(p.get("sku") or p.get("id") or "")
            name = p.get("name") or p.get("title") or "Unknown"
            points_raw = p.get("platinumPoints") or p.get("platinum_points") or 0
            platinum_points = int(str(points_raw).replace(",", ""))

            # Stock: isSalableQty is boolean or a truthy int
            in_stock_raw = p.get("isSalableQty") if "isSalableQty" in p else p.get("in_stock", True)
            in_stock = bool(in_stock_raw)

            url_key = p.get("urlKey") or p.get("url_key") or sku
            url = f"{PRODUCT_BASE_URL}{url_key}/" if url_key else REWARDS_URL

            image_url = _parse_image_url(p)

            items.append(RewardItem(
                sku=sku,
                name=name,
                platinum_points=platinum_points,
                in_stock=in_stock,
                url=url,
                image_url=image_url,
            ))
        except Exception as exc:
            logger.warning("Skipping malformed product entry: %s — %r", exc, p)

    items.sort(key=lambda x: x.platinum_points)
    logger.info("Fetched %d reward item(s) (%d in stock)", len(items), sum(1 for i in items if i.in_stock))
    return items


def diff_items(
    old: list[RewardItem], new: list[RewardItem]
) -> tuple[list[RewardItem], list[RewardItem], list[RewardItem]]:
    """
    Compare two item lists and return (back_in_stock, went_out_of_stock, new_items).
    Matching is done by SKU.
    """
    old_map = {i.sku: i for i in old}
    new_map = {i.sku: i for i in new}

    back_in_stock: list[RewardItem] = []
    went_out: list[RewardItem] = []
    new_items: list[RewardItem] = []

    for sku, item in new_map.items():
        if sku not in old_map:
            new_items.append(item)
        elif item.in_stock and not old_map[sku].in_stock:
            back_in_stock.append(item)
        elif not item.in_stock and old_map[sku].in_stock:
            went_out.append(item)

    return back_in_stock, went_out, new_items
