"""Binance historical kline fetcher with on-disk cache.

REST endpoint:
  GET https://api.binance.com/api/v3/klines
    ?symbol=BTCUSDT&interval=1s&startTime=<ms>&endTime=<ms>&limit=1000

Each kline row:
  [open_time, open, high, low, close, volume, close_time, qav, n_trades,
   taker_base_vol, taker_quote_vol, ignore]

We cache by UTC date (one JSON file per day) so repeat backtests are free.
The 1s interval is required for sub-minute window analysis. Binance enforces
a 1000-row limit per request → ~16.7 minutes per call → ~87 calls per day.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import aiohttp

import config

log = logging.getLogger("backtest.binance_history")

KLINES_URL = "https://api.binance.com/api/v3/klines"
SYMBOL = "BTCUSDT"
DEFAULT_INTERVAL = "1s"
LIMIT = 1000  # max rows per request

CACHE_DIR = config.DATA_DIR / "cache" / "binance_klines"


@dataclass
class Kline:
    open_time: int    # epoch seconds
    close_time: int   # epoch seconds (= open_time + interval - 1ms in spec, we trunc)
    open: float
    high: float
    low: float
    close: float
    volume: float     # base asset volume (BTC)

    def to_list(self) -> List:
        return [
            self.open_time, self.open, self.high, self.low, self.close,
            self.volume, self.close_time,
        ]

    @classmethod
    def from_list(cls, row: List) -> "Kline":
        return cls(
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=int(row[6]),
        )


def _cache_path(date_utc: datetime, interval: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = date_utc.strftime("%Y%m%d")
    return CACHE_DIR / f"{SYMBOL.lower()}_{interval}_{key}.json"


def _utc_day_bounds(date_utc: datetime) -> Tuple[int, int]:
    """Return (start_ms, end_ms) for a UTC calendar day."""
    start = datetime(date_utc.year, date_utc.month, date_utc.day,
                     tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


async def _fetch_chunk(session: aiohttp.ClientSession, start_ms: int,
                       end_ms: int, interval: str) -> List[List]:
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": LIMIT,
    }
    for attempt in range(4):
        try:
            async with session.get(KLINES_URL, params=params) as resp:
                if resp.status in (403, 451):
                    # Geo-blocked — retrying won't help. Surface a clear hint.
                    raise RuntimeError(
                        f"binance returned {resp.status} (geo-blocked). "
                        "Run from a region where api.binance.com is reachable, "
                        "or use a Binance.US / proxy alternative."
                    )
                if resp.status == 429:
                    backoff = 2 ** attempt
                    log.warning("binance 429 — sleeping %ds", backoff)
                    await asyncio.sleep(backoff)
                    continue
                resp.raise_for_status()
                data = await resp.json()
                if not isinstance(data, list):
                    raise ValueError(f"unexpected payload: {data!r:.120}")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            backoff = 2 ** attempt
            log.warning("binance fetch failed (%s) — retry in %ds", exc, backoff)
            await asyncio.sleep(backoff)
    raise RuntimeError("binance fetch exhausted retries")


async def fetch_day(date_utc: datetime, interval: str = DEFAULT_INTERVAL,
                    use_cache: bool = True) -> List[Kline]:
    """Fetch all klines for one UTC calendar day. Cached on disk."""
    cache = _cache_path(date_utc, interval)
    if use_cache and cache.exists():
        try:
            rows = json.loads(cache.read_text())
            return [Kline.from_list(r) for r in rows]
        except (json.JSONDecodeError, OSError):
            log.warning("cache %s corrupt — refetching", cache)

    start_ms, end_ms = _utc_day_bounds(date_utc)
    rows: List[List] = []
    cursor = start_ms
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while cursor < end_ms:
            chunk = await _fetch_chunk(session, cursor, end_ms, interval)
            if not chunk:
                break
            rows.extend([
                [int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                 float(r[4]), float(r[5]), int(r[6])]
                for r in chunk
            ])
            last_open_ms = chunk[-1][0]
            # Advance past the last kline received. For 1s intervals each row
            # covers 1000ms so add 1 to avoid re-fetching the same kline.
            cursor = last_open_ms + 1
            if len(chunk) < LIMIT:
                break
            await asyncio.sleep(0.05)  # politeness, well under Binance limits

    cache.write_text(json.dumps(rows))
    log.info("cached %d klines for %s (%s)", len(rows),
             date_utc.strftime("%Y-%m-%d"), interval)
    return [Kline.from_list(r) for r in rows]


async def fetch_range(start_utc: datetime, end_utc: datetime,
                      interval: str = DEFAULT_INTERVAL,
                      use_cache: bool = True) -> List[Kline]:
    """Fetch every kline between two UTC datetimes (inclusive of start day,
    exclusive of end day's slice past `end_utc`)."""
    if start_utc >= end_utc:
        return []
    out: List[Kline] = []
    day = datetime(start_utc.year, start_utc.month, start_utc.day,
                   tzinfo=timezone.utc)
    end_ms = int(end_utc.timestamp() * 1000)
    start_ms = int(start_utc.timestamp() * 1000)
    while day < end_utc:
        klines = await fetch_day(day, interval=interval, use_cache=use_cache)
        out.extend(k for k in klines
                   if start_ms <= k.open_time < end_ms)
        day += timedelta(days=1)
    out.sort(key=lambda k: k.open_time)
    return out


def windows_from_klines(klines: List[Kline]) -> List[Tuple[int, int, List[Kline]]]:
    """Group klines into 5-minute windows aligned on UTC boundaries.

    Returns a list of (window_start_s, window_end_s, klines_in_window).
    """
    win_len_ms = config.WINDOW_LENGTH_SECONDS * 1000
    grouped: dict = {}
    for k in klines:
        anchor_ms = k.open_time - (k.open_time % win_len_ms)
        grouped.setdefault(anchor_ms, []).append(k)
    out = []
    for anchor_ms in sorted(grouped):
        bucket = sorted(grouped[anchor_ms], key=lambda x: x.open_time)
        out.append((anchor_ms // 1000,
                    (anchor_ms + win_len_ms) // 1000,
                    bucket))
    return out
