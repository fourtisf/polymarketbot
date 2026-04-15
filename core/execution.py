"""
Order execution via py-clob-client.

Wraps CLOB client in an async-friendly interface. The underlying SDK is
sync, so we call it via asyncio.to_thread().

All orders are maker (limit) by default to avoid the 1.8% taker fee.
The retry ladder is:
  1. Post at best_ask - 0.01 (aggressive maker)
  2. If not filled in 10s, cancel + repost at best_ask
  3. If not filled and confidence > 85, repost at best_ask + 0.01 (takes)
  4. Otherwise give up on this window
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

import config

log = logging.getLogger("execution")


@dataclass
class FillResult:
    success: bool
    order_id: str = ""
    filled_shares: float = 0.0
    avg_price: float = 0.0
    error: str = ""


class Executor:
    """
    Thin wrapper around py_clob_client. Lazily initialized so that
    the bot can run in --dry-run mode without real credentials.
    """

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._client = None

    def _init_client(self):
        if self._client is not None:
            return self._client
        if self.dry_run:
            return None
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError:
            log.error("py-clob-client not installed — live trading disabled")
            return None

        creds = ApiCreds(
            api_key=config.POLYMARKET_API_KEY,
            api_secret=config.POLYMARKET_API_SECRET,
            api_passphrase=config.POLYMARKET_PASSPHRASE,
        )
        self._client = ClobClient(
            host=config.CLOB_HOST,
            key=config.POLYGON_PRIVATE_KEY,
            chain_id=config.POLYGON_CHAIN_ID,
            creds=creds,
        )
        return self._client

    # ── Public API ───────────────────────────────────────────
    async def place_limit_buy(
        self,
        token_id: str,
        price: float,
        size_usd: float,
        confidence: int,
    ) -> FillResult:
        """
        Place a limit BUY for `size_usd` worth of shares at `price`.
        Retries with the ladder described at module top.
        Returns FillResult.
        """
        shares = max(5, int(size_usd / max(price, 0.01)))
        log.info(
            "entry: token=%s price=%.3f size=$%.2f (%d sh) conf=%d",
            token_id[:10], price, size_usd, shares, confidence,
        )
        if self.dry_run:
            return FillResult(
                success=True,
                order_id=f"dry-{int(time.time()*1000)}",
                filled_shares=shares,
                avg_price=price,
            )

        # Attempt 1: aggressive maker price
        fill = await self._try_post(token_id, price, shares)
        if fill.success and fill.filled_shares > 0:
            return fill

        # Attempt 2: best ask
        await asyncio.sleep(10)
        if fill.order_id:
            await self._cancel(fill.order_id)
        fill2 = await self._try_post(token_id, price + 0.01, shares)
        if fill2.success and fill2.filled_shares > 0:
            return fill2

        # Attempt 3: go taker IF high confidence
        if confidence > 85:
            await asyncio.sleep(5)
            if fill2.order_id:
                await self._cancel(fill2.order_id)
            return await self._try_post(token_id, price + 0.02, shares)

        return FillResult(success=False, error="not filled after retries")

    async def _try_post(self, token_id: str, price: float, shares: int) -> FillResult:
        client = self._init_client()
        if client is None:
            return FillResult(success=False, error="client unavailable")
        price = round(min(0.99, max(0.01, price)), 2)
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
        except ImportError:
            return FillResult(success=False, error="clob types missing")

        def _post():
            order_args = OrderArgs(
                price=price,
                size=shares,
                side="BUY",
                token_id=token_id,
            )
            signed = client.create_order(order_args)
            return client.post_order(signed, OrderType.GTC)

        try:
            resp = await asyncio.to_thread(_post)
        except Exception as exc:
            log.exception("post_order failed: %s", exc)
            return FillResult(success=False, error=str(exc))

        if not isinstance(resp, dict):
            return FillResult(success=False, error=f"unexpected resp: {resp}")
        order_id = resp.get("orderID") or resp.get("order_id") or ""
        status = resp.get("status", "")
        filled = float(resp.get("filled", 0) or 0)
        return FillResult(
            success=status in ("matched", "live", "filled"),
            order_id=order_id,
            filled_shares=filled,
            avg_price=price,
        )

    async def _cancel(self, order_id: str) -> None:
        client = self._init_client()
        if client is None:
            return
        try:
            await asyncio.to_thread(client.cancel, order_id)
        except Exception as exc:
            log.warning("cancel %s failed: %s", order_id, exc)

    async def cancel_all(self) -> None:
        """Cancel every open order — called on shutdown for safety."""
        client = self._init_client()
        if client is None:
            return
        try:
            await asyncio.to_thread(client.cancel_all)
            log.info("all open orders cancelled")
        except Exception as exc:
            log.warning("cancel_all failed: %s", exc)

    async def get_balance_usdc(self) -> Optional[float]:
        """Best-effort USDC balance query for the dashboard."""
        client = self._init_client()
        if client is None:
            return None
        try:
            bal = await asyncio.to_thread(getattr(client, "get_balance_allowance", lambda **_: None))
            if isinstance(bal, dict):
                for key in ("balance", "usdc", "USDC"):
                    if key in bal:
                        return float(bal[key])
        except Exception:
            return None
        return None
