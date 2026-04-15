"""
Polymarket 5-minute window discovery + market metadata.

Windows are deterministic (every 5 minutes UTC), but we still have to
look up the ACTUAL Polymarket market slug + token IDs from the Gamma API,
because token IDs are unique per-market and can't be derived.

Window object exposed to the rest of the bot:
  - window_start: int epoch
  - window_end: int epoch
  - slug: e.g. "btc-updown-5m-1776258600"
  - token_up_id / token_down_id
  - price_to_beat (set later from Chainlink/Binance)
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiohttp

import config

log = logging.getLogger("market")


@dataclass
class Window:
    window_start: int
    window_end: int
    slug: str = ""
    token_up_id: str = ""
    token_down_id: str = ""
    price_to_beat: Optional[float] = None
    price_source: str = ""  # "chainlink" or "binance-fallback"
    resolution: Optional[str] = None  # "UP" | "DOWN" after settle
    close_price: Optional[float] = None

    @property
    def seconds_remaining(self) -> int:
        return max(0, int(self.window_end - time.time()))

    @property
    def is_live(self) -> bool:
        now = time.time()
        return self.window_start <= now < self.window_end

    def to_dict(self) -> dict:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "slug": self.slug,
            "token_up_id": self.token_up_id,
            "token_down_id": self.token_down_id,
            "price_to_beat": self.price_to_beat,
            "price_source": self.price_source,
            "resolution": self.resolution,
            "close_price": self.close_price,
        }


def current_window_bounds() -> Window:
    """Return the Window object for the currently active 5-minute slot."""
    now = int(time.time())
    window_start = now - (now % config.WINDOW_LENGTH_SECONDS)
    window_end = window_start + config.WINDOW_LENGTH_SECONDS
    return Window(
        window_start=window_start,
        window_end=window_end,
        slug=f"btc-updown-5m-{window_end}",
    )


async def fetch_market_tokens(slug: str) -> Optional[Dict[str, str]]:
    """
    Query Gamma API for the market's token IDs.
    Returns {"up": token_id, "down": token_id} or None on failure.
    """
    url = f"{config.GAMMA_HOST}/markets"
    params = {"slug": slug}
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    log.warning("gamma %s returned %s", slug, resp.status)
                    return None
                data = await resp.json()
    except Exception as exc:
        log.warning("gamma fetch failed for %s: %s", slug, exc)
        return None

    markets = data if isinstance(data, list) else data.get("data", [])
    if not markets:
        return None
    market = markets[0]
    # Gamma returns clobTokenIds as either JSON string or list
    clob_ids = market.get("clobTokenIds") or market.get("clob_token_ids")
    if isinstance(clob_ids, str):
        import json
        try:
            clob_ids = json.loads(clob_ids)
        except Exception:
            return None
    if not clob_ids or len(clob_ids) < 2:
        return None
    outcomes = market.get("outcomes")
    if isinstance(outcomes, str):
        import json
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []
    # Map token IDs to UP/DOWN using the outcome labels
    up_id = down_id = ""
    if outcomes and len(outcomes) == len(clob_ids):
        for label, tid in zip(outcomes, clob_ids):
            lab = str(label).lower()
            if "up" in lab or "yes" in lab:
                up_id = tid
            elif "down" in lab or "no" in lab:
                down_id = tid
    if not up_id or not down_id:
        # Fallback: assume first is UP, second is DOWN
        up_id = clob_ids[0]
        down_id = clob_ids[1]
    return {"up": up_id, "down": down_id}


async def resolve_window_metadata(window: Window) -> bool:
    """Populate token IDs on `window` from Gamma. Returns True on success."""
    tokens = await fetch_market_tokens(window.slug)
    if tokens is None:
        return False
    window.token_up_id = tokens["up"]
    window.token_down_id = tokens["down"]
    return True
