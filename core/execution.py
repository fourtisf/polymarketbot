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
    tx_hash: str = ""


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

        if not config.POLYGON_PRIVATE_KEY:
            log.error("POLYGON_PRIVATE_KEY not set — cannot init CLOB client")
            return None

        has_creds = all([
            config.POLYMARKET_API_KEY,
            config.POLYMARKET_API_SECRET,
            config.POLYMARKET_PASSPHRASE,
        ])

        try:
            if has_creds:
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
                log.info("CLOB client initialized with provided API creds")
            else:
                # Derive API creds from the private key (Level-1 auth)
                log.warning(
                    "POLYMARKET_API_* not set — deriving API creds from private key"
                )
                tmp = ClobClient(
                    host=config.CLOB_HOST,
                    key=config.POLYGON_PRIVATE_KEY,
                    chain_id=config.POLYGON_CHAIN_ID,
                )
                derived = tmp.create_or_derive_api_creds()
                tmp.set_api_creds(derived)
                self._client = tmp
                log.info("CLOB client initialized with derived API creds")
        except Exception as exc:
            log.exception("failed to init CLOB client: %s", exc)
            self._client = None
            return None
        return self._client

    # ── Approvals ────────────────────────────────────────────
    async def ensure_approvals(self) -> bool:
        """
        On LIVE startup, ensure USDC (COLLATERAL) and CTF (CONDITIONAL)
        allowances are set for the Polymarket Exchange. Uses
        py-clob-client's update_balance_allowance which triggers on-chain
        approvals for EOA wallets.
        """
        if self.dry_run:
            log.info("dry-run: skipping Polymarket approvals")
            return True
        client = self._init_client()
        if client is None:
            log.error("approvals: CLOB client unavailable")
            return False

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        except ImportError:
            log.error("approvals: BalanceAllowanceParams import failed")
            return False

        def _check_and_set():
            results = {}
            # 1. USDC collateral allowance
            col_params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = client.get_balance_allowance(col_params)
            log.info("approvals: USDC balance/allowance = %s", bal)
            allowance = 0.0
            if isinstance(bal, dict):
                try:
                    allowance = float(bal.get("allowance", 0) or 0)
                except (TypeError, ValueError):
                    allowance = 0.0
            if allowance <= 0:
                log.info("approvals: updating USDC allowance...")
                client.update_balance_allowance(col_params)
                results["usdc"] = "updated"
            else:
                results["usdc"] = f"ok ({allowance})"

            # 2. CTF conditional allowance — approve once for proxy,
            # for EOA we need to pass a token_id but the on-chain approval
            # is per-operator (CTF.setApprovalForAll), so any token works.
            cond_params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id="0",
            )
            try:
                client.update_balance_allowance(cond_params)
                results["ctf"] = "updated"
            except Exception as exc:
                results["ctf"] = f"err:{exc}"
            return results

        try:
            results = await asyncio.to_thread(_check_and_set)
            log.info("approvals complete: %s", results)
            return True
        except Exception as exc:
            log.exception("approvals failed: %s", exc)
            return False

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
        if self._is_balance_error(fill.error):
            return fill  # no point retrying with zero balance

        # Attempt 2: best ask
        await asyncio.sleep(10)
        if fill.order_id:
            await self._cancel(fill.order_id)
        fill2 = await self._try_post(token_id, price + 0.01, shares)
        if fill2.success and fill2.filled_shares > 0:
            return fill2
        if self._is_balance_error(fill2.error):
            return fill2

        # Attempt 3: go taker IF high confidence
        if confidence > 85:
            await asyncio.sleep(5)
            if fill2.order_id:
                await self._cancel(fill2.order_id)
            return await self._try_post(token_id, price + 0.02, shares)

        return FillResult(success=False, error="not filled after retries")

    @staticmethod
    def _is_balance_error(error: str) -> bool:
        """Detect balance/allowance errors that won't resolve with retries."""
        if not error:
            return False
        lower = error.lower()
        return "not enough balance" in lower or "allowance" in lower

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
            err_detail = str(exc)
            resp_attr = getattr(exc, "response", None)
            if resp_attr is not None:
                try:
                    err_detail = f"{exc} | body={resp_attr.text[:300]}"
                except Exception:
                    pass
            log.error("post_order failed (token=%s price=%.3f sh=%d): %s",
                      token_id[:10], price, shares, err_detail)
            return FillResult(success=False, error=err_detail)

        log.info("post_order resp: %s", resp)
        if not isinstance(resp, dict):
            return FillResult(success=False, error=f"unexpected resp: {resp}")
        if resp.get("errorMsg") or resp.get("error"):
            err = resp.get("errorMsg") or resp.get("error")
            log.error("CLOB rejected order: %s", err)
            return FillResult(success=False, error=str(err))
        order_id = resp.get("orderID") or resp.get("order_id") or ""
        status = resp.get("status", "")
        filled = float(resp.get("filled", 0) or 0)
        tx_hash = ""
        hashes = resp.get("transactionsHashes") or resp.get("transaction_hashes")
        if isinstance(hashes, list) and hashes:
            tx_hash = str(hashes[0])
        elif isinstance(hashes, str):
            tx_hash = hashes
        else:
            tx_hash = resp.get("transactHash") or resp.get("txHash") or ""
        return FillResult(
            success=status in ("matched", "live", "filled"),
            order_id=order_id,
            filled_shares=filled,
            avg_price=price,
            tx_hash=tx_hash,
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
        """Query on-chain USDC balance via CLOB client."""
        client = self._init_client()
        if client is None:
            return None
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            def _query():
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                return client.get_balance_allowance(params)

            bal = await asyncio.to_thread(_query)
            if isinstance(bal, dict):
                for key in ("balance", "usdc", "USDC"):
                    if key in bal:
                        return float(bal[key]) / 1e6  # USDC has 6 decimals
        except Exception as exc:
            log.warning("balance query failed: %s", exc)
            return None
        return None
