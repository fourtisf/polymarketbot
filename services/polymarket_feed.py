"""
Polymarket CLOB WebSocket feed for token prices + orderbook.

Subscribes to the 'market' channel for the current window's token IDs
and keeps a live snapshot of best bid / best ask / last trade for each
token. Consumers call get_best_bid/ask() for order pricing.

Falls back to REST API polling when WebSocket is unavailable (proxy/geo-block).
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional

import aiohttp

from core.websockets import AsyncReconnectingWS
import config

log = logging.getLogger("polymarket")

MAX_STALENESS_SEC = 30  # prices older than this are considered stale
REST_POLL_INTERVAL = 3  # seconds
CLOB_BOOK_URL = "https://clob.polymarket.com/book"


class TokenBook:
    __slots__ = ("best_bid", "best_ask", "last_trade", "ts")

    def __init__(self):
        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None
        self.last_trade: Optional[float] = None
        self.ts: float = 0.0

    @property
    def is_stale(self) -> bool:
        return self.ts > 0 and (time.time() - self.ts) > MAX_STALENESS_SEC


class PolymarketFeed(AsyncReconnectingWS):
    def __init__(self):
        super().__init__(config.POLYMARKET_WS, name="polymarket")
        self.tokens: Dict[str, TokenBook] = {}
        self._subscribed_ids: List[str] = []
        self._rest_task: Optional[asyncio.Task] = None

    async def run(self) -> None:
        self._rest_task = asyncio.create_task(self._rest_poll_loop())
        await super().run()

    async def stop(self) -> None:
        if self._rest_task:
            self._rest_task.cancel()
        await super().stop()

    async def set_tokens(self, token_ids: List[str]) -> None:
        """Swap tracked token IDs. Triggers a re-subscribe."""
        self._subscribed_ids = list(token_ids)
        for tid in token_ids:
            self.tokens.setdefault(tid, TokenBook())
        # Force a resubscribe by closing the current socket.
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def subscribe_payload(self):
        if not self._subscribed_ids:
            return None
        return {
            "type": "market",
            "assets_ids": self._subscribed_ids,
        }

    async def on_message(self, msg) -> None:
        # Polymarket pushes either a single dict or a list of dicts.
        items = msg if isinstance(msg, list) else [msg]
        for item in items:
            if not isinstance(item, dict):
                continue
            event = item.get("event_type") or item.get("type")
            token_id = item.get("asset_id") or item.get("token_id")
            if token_id is None:
                continue
            book = self.tokens.setdefault(token_id, TokenBook())
            book.ts = time.time()
            if event in ("book", "price_change"):
                bids = item.get("bids") or []
                asks = item.get("asks") or []
                if bids:
                    try:
                        book.best_bid = max(float(b["price"]) for b in bids)
                    except (KeyError, ValueError, TypeError):
                        pass
                if asks:
                    try:
                        book.best_ask = min(float(a["price"]) for a in asks)
                    except (KeyError, ValueError, TypeError):
                        pass
            elif event in ("last_trade_price", "trade"):
                try:
                    book.last_trade = float(item.get("price"))
                except (KeyError, ValueError, TypeError):
                    pass

    async def _rest_poll_loop(self) -> None:
        """Fallback: poll CLOB REST API for order book when WS is unavailable."""
        logged_fallback = False
        while True:
            try:
                if self._ws is not None:
                    logged_fallback = False
                    await asyncio.sleep(5)
                    continue

                if not self._subscribed_ids:
                    await asyncio.sleep(2)
                    continue

                if not logged_fallback:
                    log.info("polymarket WS unavailable — using REST API fallback")
                    logged_fallback = True

                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as session:
                    for token_id in self._subscribed_ids:
                        try:
                            url = f"{CLOB_BOOK_URL}?token_id={token_id}"
                            async with session.get(url) as resp:
                                data = await resp.json()
                                book = self.tokens.setdefault(token_id, TokenBook())
                                book.ts = time.time()
                                bids = data.get("bids") or []
                                asks = data.get("asks") or []
                                if bids:
                                    book.best_bid = max(
                                        float(b["price"]) for b in bids
                                    )
                                if asks:
                                    book.best_ask = min(
                                        float(a["price"]) for a in asks
                                    )
                        except Exception as exc:
                            log.debug("REST book poll %s: %s", token_id[:10], exc)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.debug("polymarket REST poll failed: %s", exc)
            await asyncio.sleep(REST_POLL_INTERVAL)

    # ── Public API ───────────────────────────────────────────
    def get_best_bid(self, token_id: str) -> Optional[float]:
        b = self.tokens.get(token_id)
        if not b or b.is_stale:
            return None
        return b.best_bid

    def get_best_ask(self, token_id: str) -> Optional[float]:
        b = self.tokens.get(token_id)
        if not b or b.is_stale:
            return None
        return b.best_ask

    def get_mid(self, token_id: str) -> Optional[float]:
        b = self.tokens.get(token_id)
        if not b or b.is_stale or b.best_bid is None or b.best_ask is None:
            return None
        return (b.best_bid + b.best_ask) / 2

    def get_last(self, token_id: str) -> Optional[float]:
        b = self.tokens.get(token_id)
        if not b or b.is_stale:
            return None
        return b.last_trade
