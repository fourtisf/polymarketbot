"""
Binance BTC/USDT live trade feed.

Subscribes to btcusdt@trade and maintains:
  - last_price: most recent trade price (float)
  - last_price_ts: timestamp of last price (epoch seconds)
  - trades_window: rolling list of trades from last 60 seconds
    (used to classify volume: high / normal / low)

Consumers ask binance.get_delta(price_to_beat) to get the current %-delta
vs the window's price-to-beat, and binance.classify_volume() for the
volume tag used in strategy scoring.

Falls back to REST API polling when WebSocket is unavailable (proxy/geo-block).
"""

import asyncio
import logging
import statistics
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

import aiohttp

from core.websockets import AsyncReconnectingWS
import config

log = logging.getLogger("binance")

BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
REST_POLL_INTERVAL = 2  # seconds


class BinanceFeed(AsyncReconnectingWS):
    def __init__(self):
        super().__init__(config.BINANCE_WS, name="binance")
        self.last_price: Optional[float] = None
        self.last_price_ts: float = 0.0
        # Each entry: (ts, price, qty)
        self.trades_window: Deque[Tuple[float, float, float]] = deque()
        # Rolling deltas (ts, delta_pct) used to classify trend
        self._recent_deltas: Deque[Tuple[float, float]] = deque()
        # Baseline volumes per minute (for "high/normal/low")
        self._historical_volumes: Deque[float] = deque(maxlen=30)
        # First price seen at/after each 300s-aligned window boundary.
        self._window_open_prices: Dict[int, float] = {}
        self._rest_task: Optional[asyncio.Task] = None

    async def run(self) -> None:
        self._rest_task = asyncio.create_task(self._rest_poll_loop())
        await super().run()

    async def stop(self) -> None:
        if self._rest_task:
            self._rest_task.cancel()
        await super().stop()

    async def on_message(self, msg) -> None:
        if not isinstance(msg, dict) or msg.get("e") != "trade":
            return
        try:
            price = float(msg["p"])
            qty = float(msg["q"])
            ts = float(msg["T"]) / 1000.0
        except (KeyError, ValueError, TypeError):
            return
        self._update_price(price, ts, qty)

    def _update_price(self, price: float, ts: float, qty: float = 0.01) -> None:
        self.last_price = price
        self.last_price_ts = ts
        self.trades_window.append((ts, price, qty))
        self._trim_window(ts)

        window_start = int(ts) - (int(ts) % config.WINDOW_LENGTH_SECONDS)
        self._window_open_prices.setdefault(window_start, price)
        if len(self._window_open_prices) > 200:
            for k in sorted(self._window_open_prices.keys())[:100]:
                self._window_open_prices.pop(k, None)

    async def _rest_poll_loop(self) -> None:
        """Fallback: poll Binance REST API when WebSocket is unavailable."""
        logged_fallback = False
        while True:
            try:
                # Only poll when WS is disconnected
                if self._ws is not None:
                    logged_fallback = False
                    await asyncio.sleep(5)
                    continue

                if not logged_fallback:
                    log.info("binance WS unavailable — using REST API fallback")
                    logged_fallback = True

                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as session:
                    async with session.get(BINANCE_REST_URL) as resp:
                        data = await resp.json()
                        price = float(data["price"])
                        self._update_price(price, time.time())
            except asyncio.CancelledError:
                return
            except Exception as exc:
                log.debug("binance REST poll failed: %s", exc)
            await asyncio.sleep(REST_POLL_INTERVAL)

    def _trim_window(self, now: float) -> None:
        cutoff = now - config.BINANCE_VOLUME_WINDOW_SECONDS
        while self.trades_window and self.trades_window[0][0] < cutoff:
            self.trades_window.popleft()
        dcutoff = now - 90
        while self._recent_deltas and self._recent_deltas[0][0] < dcutoff:
            self._recent_deltas.popleft()

    # ── Public API ───────────────────────────────────────────
    def get_price(self) -> Optional[float]:
        if self.last_price is None:
            return None
        # Reject stale prices (> 10s old = both ws and rest dead)
        if time.time() - self.last_price_ts > 10:
            return None
        return self.last_price

    def get_window_open_price(self, window_start: int) -> Optional[float]:
        """First Binance trade price at/after the given 300s-aligned window start."""
        return self._window_open_prices.get(window_start)

    def get_delta_pct(self, price_to_beat: float) -> Optional[float]:
        p = self.get_price()
        if p is None or price_to_beat <= 0:
            return None
        delta = (p - price_to_beat) / price_to_beat * 100.0
        self._recent_deltas.append((time.time(), delta))
        return delta

    def classify_volume(self) -> str:
        """Return 'high' | 'normal' | 'low' based on rolling BTC qty."""
        if not self.trades_window:
            return "low"
        total_qty = sum(q for _, _, q in self.trades_window)
        # Keep rolling history of per-minute totals
        if not self._historical_volumes or (
            time.time() - (self.trades_window[-1][0] if self.trades_window else 0) < 1
        ):
            self._historical_volumes.append(total_qty)
        if len(self._historical_volumes) < 5:
            return "normal"
        median = statistics.median(self._historical_volumes)
        if median <= 0:
            return "normal"
        ratio = total_qty / median
        if ratio >= 1.4:
            return "high"
        if ratio <= 0.6:
            return "low"
        return "normal"

    def get_realized_vol_pct(self, lookback_sec: float = 60.0) -> Optional[float]:
        """Return realized volatility (stddev of returns) over the last
        `lookback_sec` seconds, as a percent of mid price.

        Used as a regime filter: when realized vol is below ~0.05% per
        minute the BTC price is essentially flat, the Binance→Chainlink
        latency edge collapses to zero, and any "signal" from delta is
        almost certainly noise. Returns None if not enough samples.
        """
        if not self.trades_window:
            return None
        cutoff = time.time() - lookback_sec
        prices = [p for ts, p, _ in self.trades_window if ts >= cutoff]
        if len(prices) < 5:
            return None
        # log-return-based stdev. For short windows simple % returns work too,
        # but log returns avoid asymmetry on directional moves.
        import math as _m
        rets = []
        for a, b in zip(prices[:-1], prices[1:]):
            if a > 0 and b > 0:
                rets.append(_m.log(b / a))
        if len(rets) < 4:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        # Scale stddev → annualized-ish: just return raw stdev as % to
        # keep thresholds intuitive ("0.05% over 1m = dead market").
        return _m.sqrt(var) * 100.0

    def get_volume_zscore(self) -> float:
        """Return how many stddevs the current 60s volume is above the
        rolling baseline. >+1.5 = high-conviction directional flow, useful
        as an additional confidence bump.
        """
        if not self.trades_window or len(self._historical_volumes) < 5:
            return 0.0
        cur = sum(q for _, _, q in self.trades_window)
        hist = list(self._historical_volumes)
        mean = sum(hist) / len(hist)
        if mean <= 0:
            return 0.0
        var = sum((v - mean) ** 2 for v in hist) / max(len(hist) - 1, 1)
        sd = var ** 0.5
        if sd <= 0:
            return 0.0
        return (cur - mean) / sd

    def classify_trend(self) -> str:
        """'consistent' | 'choppy' | 'reversing' based on last ~60s of deltas."""
        if len(self._recent_deltas) < 6:
            return "choppy"
        deltas = [d for _, d in list(self._recent_deltas)[-30:]]
        positives = sum(1 for d in deltas if d > 0)
        negatives = sum(1 for d in deltas if d < 0)
        total = len(deltas)
        last = deltas[-1]
        first = deltas[0]
        if last * first < 0:
            # Sign flipped over the window → reversing
            return "reversing"
        if last >= 0 and positives / total >= 0.75:
            return "consistent"
        if last < 0 and negatives / total >= 0.75:
            return "consistent"
        return "choppy"
