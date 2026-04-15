"""
Chainlink BTC/USD oracle price feed — via Polymarket's RTDS WebSocket.

Polymarket broadcasts Chainlink price ticks over a dedicated channel.
We subscribe, filter for btc/usd, and capture the first tick at/after
each window boundary as the authoritative "price to beat".

If the RTDS channel is unreachable, we fall back to Binance price at
window open (captured by bot.py) — the strategy tolerates either source
but logs which one was used.
"""

import asyncio
import logging
import time
from typing import Dict, Optional

from core.websockets import AsyncReconnectingWS
import config

log = logging.getLogger("chainlink")


class ChainlinkFeed(AsyncReconnectingWS):
    def __init__(self):
        super().__init__(config.POLYMARKET_RTDS_WS, name="chainlink")
        self.latest_price: Optional[float] = None
        self.latest_ts: float = 0.0
        # Map window_start (int) -> captured oracle price at/after that ts
        self._window_prices: Dict[int, float] = {}

    async def subscribe_payload(self):
        return {
            "type": "crypto_prices_chainlink",
            "filters": {"symbol": "btc/usd"},
        }

    async def on_message(self, msg) -> None:
        items = msg if isinstance(msg, list) else [msg]
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = (item.get("symbol") or item.get("pair") or "").lower()
            if symbol and "btc" not in symbol:
                continue
            price = item.get("price") or item.get("value")
            if price is None:
                continue
            try:
                price = float(price)
            except (ValueError, TypeError):
                continue
            ts = float(item.get("timestamp") or time.time())
            if ts > 1e12:  # ms → s
                ts /= 1000.0
            self.latest_price = price
            self.latest_ts = ts
            window_start = int(ts) - (int(ts) % config.WINDOW_LENGTH_SECONDS)
            # Only capture the FIRST oracle tick of each window
            self._window_prices.setdefault(window_start, price)
            # Trim old
            if len(self._window_prices) > 200:
                oldest = sorted(self._window_prices.keys())[:100]
                for k in oldest:
                    self._window_prices.pop(k, None)

    def get_price_to_beat(self, window_start: int) -> Optional[float]:
        """Return the Chainlink price captured at the start of `window_start`."""
        return self._window_prices.get(window_start)
