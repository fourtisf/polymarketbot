"""
Order execution via py-clob-client.

Wraps CLOB client in an async-friendly interface. The underlying SDK is
sync, so we call it via asyncio.to_thread().

The fill strategy (cross-spread ladder, 4 attempts, ~7s budget):
  1. Post GTC at best_ask + 0.01 — cross spread for immediate match
  2. Poll order status (0.5s interval) for up to 2.0s
  3. If still unfilled, cancel + repost at best_ask + 0.03
  4. Repeat at +0.05, then +0.07 — capped at ABSOLUTE_MAX_PRICE (0.62)

Tunable knobs are at module level so the operator can tune via code or
expose them through config later. The ladder must finish well within
the entry window (T-30s → T-8s = 22s of opportunity).
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import List, Optional

import config

log = logging.getLogger("execution")


# ─────────────────────────────────────────────────────────────
# Execution tuning constants
# ─────────────────────────────────────────────────────────────

# Price cap for cross-spread ladder. Matches strategy.ABSOLUTE_MAX_PRICE
# so we never accidentally pay more than the strategy was willing to
# stake on at decision time. Raising this changes break-even win rate.
EXECUTION_MAX_PRICE = 0.62

# Cents to add at each attempt vs the original best_ask. A wider step
# crosses the spread harder and converts more orders to immediate fills,
# at the cost of paying a slightly higher entry price. Empirically the
# 5-min book moves 1-3 cents during a 22s window, so the early steps must
# be aggressive enough to catch up.
LADDER_STEPS = (0.01, 0.03, 0.05, 0.07)

# Per-attempt fill-poll timeout. Each attempt cancels at the end if not
# filled, so total wall-clock budget = sum(POLL_TIMEOUTS) + post latency.
POLL_TIMEOUTS = (2.0, 1.5, 1.5, 1.0)  # total 6.0s polling

# How fast to poll for fills. The previous 2.0s wasted half of every
# attempt's budget. 0.5s is fast enough to catch fills in a 22s window.
POLL_SLEEP_SEC = 0.5

# Probabilistic fill probability per ladder step in dry-run mode.
# Calibrated to roughly match observed live fill rates: a small +0.01
# bump fills sometimes, deeper bumps fill almost always. This stops
# dry-run from being naively optimistic (always 100% fill) and lets
# the simulation actually predict whether the strategy survives the
# real-world unfilled-order tax.
DRY_RUN_LADDER_FILL_PROBS = (0.50, 0.78, 0.92, 0.98)


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
        # Session counters — visible in dashboard / logs to spot
        # execution-layer regressions. Placed counts every entry attempt
        # the strategy approves; filled counts the ones that actually
        # received any shares. fill_rate = filled / placed.
        self.placed_count: int = 0
        self.filled_count: int = 0
        self.cumulative_avg_attempts: float = 0.0  # rolling mean

    def fill_rate(self) -> float:
        if self.placed_count == 0:
            return 0.0
        return self.filled_count / self.placed_count

    def execution_snapshot(self) -> dict:
        return {
            "placed": self.placed_count,
            "filled": self.filled_count,
            "fill_rate": round(self.fill_rate(), 3),
            "avg_attempts": round(self.cumulative_avg_attempts, 2),
        }

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

    # Polymarket exchange contracts that need USDC.e spending approval
    USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
    NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
    CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    PROXY_WALLET_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
    EXCHANGE_SPENDERS = [
        "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",  # CTF Exchange
        "0xC5d563A36AE78145C45a50134d48A1215220f80a",  # NegRisk CTF Exchange
        "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",  # NegRisk Adapter
    ]
    MAX_UINT256_HEX = "f" * 64  # 2^256 - 1 as hex
    POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
    _proxy_wallet_address: Optional[str] = None  # cached

    async def ensure_approvals(self) -> bool:
        """
        Ensure USDC.e and CTF allowances are set for Polymarket exchange
        contracts. Uses raw RPC calls + eth_account (no web3 dependency).
        """
        if self.dry_run:
            log.info("dry-run: skipping Polymarket approvals")
            return True
        client = self._init_client()
        if client is None:
            log.error("approvals: CLOB client unavailable")
            return False

        # Log current CLOB state for diagnostics
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            def _check():
                col_params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                return client.get_balance_allowance(col_params)

            bal = await asyncio.to_thread(_check)
            log.info("approvals: CLOB balance/allowance = %s", bal)
        except Exception as exc:
            log.warning("approvals: CLOB check failed: %s", exc)

        # Direct on-chain approvals via raw RPC + eth_account
        try:
            results = await self._raw_rpc_approvals()
            log.info("approvals complete: %s", results)
            failed = [k for k, v in results.items() if v == "FAILED"]
            if failed:
                log.error("some approvals failed: %s", failed)
                return False
            return True
        except Exception as exc:
            log.exception("approvals failed: %s", exc)
            return False

    async def _raw_rpc_approvals(self) -> dict:
        """Send ERC-20 approve() and CTF setApprovalForAll() via raw RPC."""
        import aiohttp
        from eth_account import Account

        acct = Account.from_key(config.POLYGON_PRIVATE_KEY)
        owner = acct.address.lower()
        owner_padded = owner.replace("0x", "").rjust(64, "0")
        results = {}

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            # Get nonce and gas price
            nonce = int(await self._rpc(session, "eth_getTransactionCount", [acct.address, "latest"]), 16)
            gas_price_hex = await self._rpc(session, "eth_gasPrice", [])
            gas_price = int(gas_price_hex, 16)
            log.info("approvals: nonce=%d gas_price=%d", nonce, gas_price)

            # 1. USDC.e approve for each exchange spender
            for spender in self.EXCHANGE_SPENDERS:
                label = spender[:10]
                spender_padded = spender.lower().replace("0x", "").rjust(64, "0")

                # Check current allowance: allowance(owner, spender) = 0xdd62ed3e
                call_data = "0xdd62ed3e" + owner_padded + spender_padded
                allowance_hex = await self._rpc(session, "eth_call", [
                    {"to": self.USDC_E, "data": call_data}, "latest"
                ])
                allowance = int(allowance_hex, 16) if allowance_hex and allowance_hex != "0x" else 0

                if allowance > 10**12:
                    log.info("USDC.e allowance for %s OK: %d", label, allowance)
                    results[f"usdc_{label}"] = "ok"
                    continue

                log.info("USDC.e allowance for %s is %d — approving...", label, allowance)
                # approve(spender, max_uint256) = 0x095ea7b3
                tx_data = "0x095ea7b3" + spender_padded + self.MAX_UINT256_HEX
                tx_hash = await self._sign_and_send(
                    session, acct, self.USDC_E, tx_data, nonce, gas_price
                )
                if tx_hash:
                    receipt_ok = await self._wait_receipt(session, tx_hash)
                    log.info("USDC.e approved %s: tx=%s ok=%s", label, tx_hash, receipt_ok)
                    results[f"usdc_{label}"] = "approved" if receipt_ok else "FAILED"
                    nonce += 1
                else:
                    results[f"usdc_{label}"] = "FAILED"

            # 2. CTF setApprovalForAll for each exchange operator
            for spender in self.EXCHANGE_SPENDERS:
                label = spender[:10]
                spender_padded = spender.lower().replace("0x", "").rjust(64, "0")

                # Check: isApprovedForAll(owner, operator) = 0xe985e9c5
                call_data = "0xe985e9c5" + owner_padded + spender_padded
                approved_hex = await self._rpc(session, "eth_call", [
                    {"to": self.CTF_CONTRACT, "data": call_data}, "latest"
                ])
                is_approved = int(approved_hex, 16) if approved_hex and approved_hex != "0x" else 0

                if is_approved:
                    log.info("CTF approval for %s OK", label)
                    results[f"ctf_{label}"] = "ok"
                    continue

                log.info("CTF not approved for %s — approving...", label)
                # setApprovalForAll(operator, true) = 0xa22cb465
                true_padded = "0" * 63 + "1"
                tx_data = "0xa22cb465" + spender_padded + true_padded
                tx_hash = await self._sign_and_send(
                    session, acct, self.CTF_CONTRACT, tx_data, nonce, gas_price
                )
                if tx_hash:
                    receipt_ok = await self._wait_receipt(session, tx_hash)
                    log.info("CTF approved %s: tx=%s ok=%s", label, tx_hash, receipt_ok)
                    results[f"ctf_{label}"] = "approved" if receipt_ok else "FAILED"
                    nonce += 1
                else:
                    results[f"ctf_{label}"] = "FAILED"

        return results

    async def _rpc(self, session, method: str, params: list):
        """Make a JSON-RPC call to Polygon."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with session.post(self.POLYGON_RPC, json=payload) as resp:
            data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC {method}: {data['error']}")
        return data.get("result")

    async def _sign_and_send(self, session, acct, to: str, data: str,
                              nonce: int, gas_price: int,
                              gas: int = 100_000) -> Optional[str]:
        """Sign and broadcast a transaction, return tx hash or None."""
        tx = {
            "to": bytes.fromhex(to.replace("0x", "")),
            "value": 0,
            "gas": gas,
            "gasPrice": gas_price,
            "nonce": nonce,
            "chainId": 137,
            "data": bytes.fromhex(data.replace("0x", "")),
        }
        try:
            signed = acct.sign_transaction(tx)
            raw = "0x" + signed.raw_transaction.hex()
            tx_hash = await self._rpc(session, "eth_sendRawTransaction", [raw])
            return tx_hash
        except Exception as exc:
            log.error("sign_and_send failed: %s", exc)
            return None

    async def _wait_receipt(self, session, tx_hash: str, timeout: int = 60) -> bool:
        """Poll for transaction receipt."""
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            try:
                receipt = await self._rpc(session, "eth_getTransactionReceipt", [tx_hash])
                if receipt is not None:
                    return int(receipt.get("status", "0x0"), 16) == 1
            except Exception:
                pass
            await asyncio.sleep(2)
        log.warning("tx %s receipt timeout after %ds", tx_hash, timeout)
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

        `price` should be the freshly-fetched best_ask. We post immediately
        at price+LADDER_STEPS[0] to cross the spread (Polymarket has no
        taker fee, so there is no cost to crossing). If the post or the
        subsequent poll does not fill within POLL_TIMEOUTS[i] seconds, we
        cancel and re-post at the next ladder step. We never exceed
        EXECUTION_MAX_PRICE.
        """
        shares = max(5, int(size_usd / max(price, 0.01)))
        log.info(
            "entry: token=%s base_price=%.3f size=$%.2f (%d sh) conf=%d",
            token_id[:10], price, size_usd, shares, confidence,
        )
        self.placed_count += 1
        if self.dry_run:
            # Probabilistic fill simulation. Walk the same ladder as live
            # would; at each step roll the dice using the calibrated
            # probability. If the bumped price exceeds EXECUTION_MAX_PRICE,
            # ladder is exhausted and the order does not fill — same
            # behaviour as the live ladder hitting the cap.
            for bump, fill_prob in zip(LADDER_STEPS, DRY_RUN_LADDER_FILL_PROBS):
                attempt_price = round(price + bump, 2)
                if attempt_price > EXECUTION_MAX_PRICE:
                    break
                if random.random() < fill_prob:
                    self._record_fill(attempts=1)
                    log.info("dry-run FILL: token=%s price=$%.3f (sim ladder)",
                             token_id[:10], attempt_price)
                    return FillResult(
                        success=True,
                        order_id=f"dry-{int(time.time()*1000)}",
                        filled_shares=shares,
                        avg_price=attempt_price,
                    )
            log.info("dry-run NO FILL: token=%s ladder exhausted (sim)",
                     token_id[:10])
            return FillResult(
                success=False,
                error="dry-run: ladder exhausted (simulated)",
            )

        ladder_start = time.monotonic()
        last_fill: Optional[FillResult] = None
        attempts_used = 0

        for attempt, (bump, timeout) in enumerate(
            zip(LADDER_STEPS, POLL_TIMEOUTS), start=1
        ):
            attempt_price = round(price + bump, 2)
            if attempt_price > EXECUTION_MAX_PRICE:
                log.info("ladder cap hit: $%.3f > $%.3f (skipping attempt %d)",
                         attempt_price, EXECUTION_MAX_PRICE, attempt)
                break
            attempts_used = attempt

            fill = await self._try_post(token_id, attempt_price, shares)
            last_fill = fill
            elapsed = time.monotonic() - ladder_start
            log.info("ladder attempt %d/%d: price=$%.3f elapsed=%.2fs "
                     "ok=%s filled=%.1f order=%s err=%s",
                     attempt, len(LADDER_STEPS), attempt_price, elapsed,
                     fill.success, fill.filled_shares,
                     (fill.order_id or "")[:10], fill.error or "-")

            if self._is_balance_error(fill.error):
                # Balance/allowance won't resolve via retries; fail fast.
                return fill
            if fill.success and fill.filled_shares > 0:
                self._record_fill(attempts_used)
                return fill

            # Order accepted but not yet matched — poll briefly for fills.
            if fill.order_id:
                polled = await self._poll_order_fills(
                    fill.order_id, attempt_price, timeout=timeout
                )
                if polled.filled_shares > 0:
                    self._record_fill(attempts_used)
                    return polled
                await self._cancel(fill.order_id)

        last_err = last_fill.error if last_fill and last_fill.error else "no fill"
        log.warning("ladder exhausted (%d attempts) — fill_rate=%.1f%% (%d/%d)",
                    attempts_used, self.fill_rate() * 100,
                    self.filled_count, self.placed_count)
        return FillResult(success=False, error=f"not filled after retries ({last_err})")

    def _record_fill(self, attempts: int) -> None:
        """Update counters after a successful fill. Tracks rolling avg
        attempts so we can see if executions are getting harder over time."""
        self.filled_count += 1
        prev_n = self.filled_count - 1
        self.cumulative_avg_attempts = (
            (self.cumulative_avg_attempts * prev_n + attempts) / self.filled_count
        )
        log.info("FILLED: %d/%d (%.1f%% fill rate, avg %.2f attempts)",
                 self.filled_count, self.placed_count, self.fill_rate() * 100,
                 self.cumulative_avg_attempts)

    @staticmethod
    def _is_balance_error(error: str) -> bool:
        """Detect balance/allowance errors that won't resolve with retries."""
        if not error:
            return False
        lower = error.lower()
        return "not enough balance" in lower or "allowance" in lower

    async def _poll_order_fills(
        self, order_id: str, price: float, timeout: int = 6
    ) -> FillResult:
        """Poll CLOB for fill status on a live order."""
        client = self._init_client()
        if client is None:
            return FillResult(success=False, error="client unavailable")

        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(POLL_SLEEP_SEC)
            try:
                resp = await asyncio.to_thread(client.get_order, order_id)
                if not isinstance(resp, dict):
                    continue
                size_matched = float(
                    resp.get("size_matched")
                    or resp.get("takingAmount")
                    or resp.get("filled")
                    or 0
                )
                status = resp.get("status", "")
                log.debug("poll %s: status=%s matched=%.1f", order_id[:10], status, size_matched)
                if size_matched > 0:
                    tx_hash = self._extract_tx_hash(resp)
                    avg = price
                    if resp.get("makingAmount"):
                        try:
                            avg = round(float(resp["makingAmount"]) / size_matched, 4)
                        except (ValueError, ZeroDivisionError):
                            pass
                    elif resp.get("associate_trades"):
                        try:
                            avg = float(resp["associate_trades"][0].get("price", price))
                        except (IndexError, KeyError, ValueError):
                            pass
                    return FillResult(
                        success=True,
                        order_id=order_id,
                        filled_shares=size_matched,
                        avg_price=avg,
                        tx_hash=tx_hash,
                    )
                elif status in ("matched", "filled"):
                    # Status says matched but size_matched is 0 — likely
                    # the fill data hasn't propagated yet. DON'T default to
                    # 1.0 shares which creates phantom fills. Keep polling.
                    log.warning("poll %s: status=%s but size_matched=0 — still polling",
                                order_id[:10], status)
            except Exception as exc:
                log.debug("poll order %s error: %s", order_id[:10], exc)

        return FillResult(success=False, order_id=order_id)

    @staticmethod
    def _extract_tx_hash(resp: dict) -> str:
        hashes = resp.get("transactionsHashes") or resp.get("transaction_hashes")
        if isinstance(hashes, list) and hashes:
            return str(hashes[0])
        if isinstance(hashes, str):
            return hashes
        return resp.get("transactHash") or resp.get("txHash") or ""

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
        # Polymarket returns takingAmount (shares received) and makingAmount (USDC paid)
        # for matched orders, NOT a "filled" field
        filled = float(
            resp.get("filled")
            or resp.get("takingAmount")
            or resp.get("size_matched")
            or 0
        )
        tx_hash = self._extract_tx_hash(resp)
        # Calculate actual avg price from makingAmount/takingAmount if available
        avg = price
        if filled > 0 and resp.get("makingAmount"):
            try:
                avg = round(float(resp["makingAmount"]) / filled, 4)
            except (ValueError, ZeroDivisionError):
                pass
        log.info("order %s status=%s filled=%.1f avg=%.4f", order_id[:10], status, filled, avg)
        return FillResult(
            success=status in ("matched", "live", "filled"),
            order_id=order_id,
            filled_shares=filled,
            avg_price=avg,
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

    async def get_proxy_wallet_address(self, session=None) -> Optional[str]:
        """
        Look up the Polymarket proxy wallet address for our EOA.
        Calls getPolyProxyWalletAddress(address) on the Exchange contract.
        """
        if self._proxy_wallet_address:
            return self._proxy_wallet_address

        from eth_account import Account
        acct = Account.from_key(config.POLYGON_PRIVATE_KEY)

        # getPolyProxyWalletAddress(address) selector = 0xedef7d8e
        addr_padded = acct.address.lower().replace("0x", "").rjust(64, "0")
        call_data = "0xedef7d8e" + addr_padded

        close_session = False
        if session is None:
            import aiohttp
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10))
            close_session = True

        try:
            result = await self._rpc(session, "eth_call", [
                {"to": self.CTF_EXCHANGE, "data": call_data}, "latest"
            ])
            if result and result != "0x" and len(result) >= 42:
                proxy_addr = "0x" + result[-40:]
                self._proxy_wallet_address = proxy_addr
                log.info("proxy wallet for %s: %s", acct.address, proxy_addr)
                return proxy_addr
        except Exception as exc:
            log.warning("failed to get proxy wallet address: %s", exc)
        finally:
            if close_session:
                await session.close()
        return None

    async def redeem_positions(self, condition_id: str,
                              neg_risk: bool = True) -> Optional[str]:
        """
        Redeem resolved conditional tokens back to USDC.e.

        Polymarket holds tokens at the PROXY WALLET, not the EOA.
        Strategy:
          1. Try via ProxyWalletFactory.proxy() (tokens at proxy wallet)
          2. Fallback to direct EOA call (in case tokens are at EOA)
        """
        if self.dry_run or not condition_id:
            return None

        import aiohttp
        import eth_abi
        from eth_account import Account

        acct = Account.from_key(config.POLYGON_PRIVATE_KEY)
        cond_padded = condition_id.lower().replace("0x", "").rjust(64, "0")
        cond_bytes = bytes.fromhex(cond_padded)

        # Build redeemPositions calldata for BOTH contract types
        if neg_risk:
            # NegRisk: redeemPositions(bytes32 conditionId, uint256[] indexSets)
            inner_selector = bytes.fromhex("dbeccb23")
            inner_params = eth_abi.encode(
                ["bytes32", "uint256[]"],
                [cond_bytes, [1, 2]]
            )
            target_contract = self.NEG_RISK_ADAPTER
        else:
            # Standard CTF: redeemPositions(address, bytes32, bytes32, uint256[])
            inner_selector = bytes.fromhex("01b7037c")
            inner_params = eth_abi.encode(
                ["address", "bytes32", "bytes32", "uint256[]"],
                [self.USDC_E, b'\x00' * 32, cond_bytes, [1, 2]]
            )
            target_contract = self.CTF_CONTRACT

        inner_calldata = inner_selector + inner_params

        log.info("redeem: condition=%s neg_risk=%s eoa=%s",
                 condition_id[:16], neg_risk, acct.address)

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            # ── Approach 1: Via ProxyWalletFactory.proxy() ──
            # This is the correct approach — tokens live at the proxy wallet
            factory_selector = bytes.fromhex("34ee9791")
            factory_params = eth_abi.encode(
                ["(uint8,address,uint256,bytes)[]"],
                [[(0, target_contract, 0, inner_calldata)]]
            )
            factory_tx_data = "0x" + (factory_selector + factory_params).hex()

            log.info("redeem: trying via ProxyWalletFactory.proxy()")
            try:
                est_hex = await self._rpc(session, "eth_estimateGas", [{
                    "from": acct.address,
                    "to": self.PROXY_WALLET_FACTORY,
                    "data": factory_tx_data,
                }])
                estimated = int(est_hex, 16)
                if estimated >= 30_000:
                    gas_limit = int(estimated * 1.5)
                    log.info("redeem: proxy factory gas=%d, using %d",
                             estimated, gas_limit)
                    nonce = int(await self._rpc(
                        session, "eth_getTransactionCount",
                        [acct.address, "latest"]
                    ), 16)
                    gas_price = int(await self._rpc(
                        session, "eth_gasPrice", []
                    ), 16)
                    tx_hash = await self._sign_and_send(
                        session, acct, self.PROXY_WALLET_FACTORY,
                        factory_tx_data, nonce, gas_price, gas=gas_limit,
                    )
                    if tx_hash:
                        ok = await self._wait_receipt(session, tx_hash)
                        log.info("redeem proxy factory tx=%s ok=%s", tx_hash, ok)
                        if ok:
                            return tx_hash
                        log.error("redeem proxy factory TX reverted: %s", tx_hash)
                else:
                    log.info("redeem: proxy factory gas=%d (too low) — trying direct",
                             estimated)
            except Exception as exc:
                log.info("redeem: proxy factory gas est failed: %s — trying direct", exc)

            # ── Approach 2: Direct EOA call (fallback) ──
            # In case tokens are at EOA (e.g., manual transfers)
            direct_tx_data = "0x" + inner_calldata.hex()
            # Also try both contract types for direct calls
            direct_attempts = [
                (target_contract, direct_tx_data, "primary"),
            ]
            # Add the other contract type as fallback
            if neg_risk:
                ctf_selector = bytes.fromhex("01b7037c")
                ctf_params = eth_abi.encode(
                    ["address", "bytes32", "bytes32", "uint256[]"],
                    [self.USDC_E, b'\x00' * 32, cond_bytes, [1, 2]]
                )
                direct_attempts.append(
                    (self.CTF_CONTRACT, "0x" + (ctf_selector + ctf_params).hex(), "CTF-fallback")
                )
            else:
                nr_selector = bytes.fromhex("dbeccb23")
                nr_params = eth_abi.encode(
                    ["bytes32", "uint256[]"],
                    [cond_bytes, [1, 2]]
                )
                direct_attempts.append(
                    (self.NEG_RISK_ADAPTER, "0x" + (nr_selector + nr_params).hex(), "NegRisk-fallback")
                )

            for contract, tx_data, label in direct_attempts:
                log.info("redeem: trying %s direct (contract=%s)",
                         label, contract[:10])
                try:
                    est_hex = await self._rpc(session, "eth_estimateGas", [{
                        "from": acct.address,
                        "to": contract,
                        "data": tx_data,
                    }])
                    estimated = int(est_hex, 16)
                    if estimated < 30_000:
                        log.info("redeem: %s gas=%d (too low, no-op) — skipping",
                                 label, estimated)
                        continue
                    gas_limit = int(estimated * 1.5)
                    log.info("redeem: %s gas estimate=%d, using %d",
                             label, estimated, gas_limit)
                except Exception as est_exc:
                    log.info("redeem: %s gas est failed: %s — skipping",
                             label, est_exc)
                    continue

                try:
                    nonce = int(await self._rpc(
                        session, "eth_getTransactionCount",
                        [acct.address, "latest"]
                    ), 16)
                    gas_price = int(await self._rpc(
                        session, "eth_gasPrice", []
                    ), 16)
                    tx_hash = await self._sign_and_send(
                        session, acct, contract, tx_data,
                        nonce, gas_price, gas=gas_limit,
                    )
                    if tx_hash:
                        ok = await self._wait_receipt(session, tx_hash)
                        log.info("redeem %s tx=%s ok=%s", label, tx_hash, ok)
                        if ok:
                            return tx_hash
                        log.error("redeem %s TX reverted: %s", label, tx_hash)
                    else:
                        log.error("redeem %s: sign_and_send returned None", label)
                except Exception as exc:
                    log.exception("redeem %s failed: %s", label, exc)

        log.warning("redeem: all approaches failed for condition %s", condition_id[:16])
        return None

    async def withdraw_proxy_usdc(self) -> Optional[str]:
        """
        Transfer USDC.e from proxy wallet back to EOA.
        Calls Factory.proxy() to execute USDC.transfer(eoa, balance) from proxy.
        """
        import aiohttp
        import eth_abi
        from eth_account import Account

        acct = Account.from_key(config.POLYGON_PRIVATE_KEY)

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as session:
            proxy_addr = await self.get_proxy_wallet_address(session)
            if not proxy_addr:
                log.warning("withdraw_proxy: no proxy wallet found")
                return None

            # Check USDC.e balance at proxy wallet
            addr_padded = proxy_addr.lower().replace("0x", "").rjust(64, "0")
            bal_data = "0x70a08231" + "0" * 24 + addr_padded[-40:]
            bal_hex = await self._rpc(session, "eth_call", [
                {"to": self.USDC_E, "data": bal_data}, "latest"
            ])
            balance = int(bal_hex, 16) if bal_hex and bal_hex != "0x" else 0
            if balance == 0:
                log.info("withdraw_proxy: proxy wallet has 0 USDC.e")
                return None

            log.info("withdraw_proxy: proxy has %d USDC.e raw (%.2f)",
                     balance, balance / 1e6)

            # Build transfer(address,uint256) calldata
            # transfer selector = 0xa9059cbb
            transfer_selector = bytes.fromhex("a9059cbb")
            transfer_params = eth_abi.encode(
                ["address", "uint256"],
                [acct.address, balance]
            )
            transfer_calldata = transfer_selector + transfer_params

            # Wrap in Factory.proxy() call
            factory_selector = bytes.fromhex("34ee9791")
            factory_params = eth_abi.encode(
                ["(uint8,address,uint256,bytes)[]"],
                [[(0, self.USDC_E, 0, transfer_calldata)]]
            )
            factory_tx_data = "0x" + (factory_selector + factory_params).hex()

            try:
                est_hex = await self._rpc(session, "eth_estimateGas", [{
                    "from": acct.address,
                    "to": self.PROXY_WALLET_FACTORY,
                    "data": factory_tx_data,
                }])
                estimated = int(est_hex, 16)
                gas_limit = int(estimated * 1.5)
            except Exception as exc:
                log.warning("withdraw_proxy: gas estimate failed: %s", exc)
                return None

            nonce = int(await self._rpc(
                session, "eth_getTransactionCount",
                [acct.address, "latest"]
            ), 16)
            gas_price = int(await self._rpc(session, "eth_gasPrice", []), 16)

            tx_hash = await self._sign_and_send(
                session, acct, self.PROXY_WALLET_FACTORY,
                factory_tx_data, nonce, gas_price, gas=gas_limit,
            )
            if tx_hash:
                ok = await self._wait_receipt(session, tx_hash)
                log.info("withdraw_proxy tx=%s ok=%s amount=%.2f",
                         tx_hash, ok, balance / 1e6)
                if ok:
                    return tx_hash
            return None

    async def verify_token_balance(self, token_id: str) -> float:
        """
        Check on-chain CTF balance for a token at BOTH EOA and proxy wallet.
        Returns the total balance in USDC-equivalent (raw / 1e6).

        Polymarket holds conditional tokens at the proxy wallet (not EOA),
        so we must check both locations.
        """
        import aiohttp
        from eth_account import Account

        acct = Account.from_key(config.POLYGON_PRIVATE_KEY)

        try:
            token_int = int(token_id)
            token_hex = hex(token_int)[2:].rjust(64, "0")
        except (ValueError, OverflowError):
            log.warning("verify_token_balance: invalid token_id %s", token_id[:20])
            return 0.0

        total_raw = 0
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                # Check EOA balance
                eoa_padded = acct.address.lower().replace("0x", "").rjust(64, "0")
                call_data = "0x00fdd58e" + eoa_padded + token_hex
                result = await self._rpc(session, "eth_call", [
                    {"to": self.CTF_CONTRACT, "data": call_data}, "latest"
                ])
                if result and result != "0x":
                    total_raw += int(result, 16)

                # Check proxy wallet balance
                proxy_addr = await self.get_proxy_wallet_address(session)
                if proxy_addr:
                    proxy_padded = proxy_addr.lower().replace("0x", "").rjust(64, "0")
                    call_data_proxy = "0x00fdd58e" + proxy_padded + token_hex
                    result_proxy = await self._rpc(session, "eth_call", [
                        {"to": self.CTF_CONTRACT, "data": call_data_proxy}, "latest"
                    ])
                    if result_proxy and result_proxy != "0x":
                        proxy_raw = int(result_proxy, 16)
                        total_raw += proxy_raw
                        if proxy_raw > 0:
                            log.info("verify_token_balance: found %d raw at PROXY %s",
                                     proxy_raw, proxy_addr[:10])

                if total_raw > 0:
                    log.info("verify_token_balance: total=%d raw (EOA+proxy)", total_raw)
                return total_raw / 1e6  # Polymarket uses 1e6 decimals
        except Exception as exc:
            log.warning("verify_token_balance failed: %s", exc)

        return 0.0

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

    async def get_onchain_usdc_balance(self, address: str) -> float:
        """Get USDC.e balance for any address via raw RPC."""
        import aiohttp
        addr_padded = address.lower().replace("0x", "").rjust(64, "0")
        data = "0x70a08231" + "0" * 24 + addr_padded[-40:]
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                result = await self._rpc(session, "eth_call", [
                    {"to": self.USDC_E, "data": data}, "latest"
                ])
                return int(result, 16) / 1e6 if result and result != "0x" else 0.0
        except Exception as exc:
            log.warning("get_onchain_usdc_balance(%s) failed: %s", address[:10], exc)
            return 0.0

    async def cancel_all_open_orders(self) -> int:
        """Cancel all open CLOB orders to free locked USDC.e."""
        client = self._init_client()
        if client is None:
            return 0
        try:
            def _cancel():
                return client.cancel_all()
            resp = await asyncio.to_thread(_cancel)
            if resp:
                log.info("cancel_all_orders: %s", resp)
                return 1
            return 0
        except Exception as exc:
            log.warning("cancel_all_orders failed: %s", exc)
            return 0

    async def get_open_orders(self) -> list:
        """Get list of open orders from CLOB."""
        client = self._init_client()
        if client is None:
            return []
        try:
            def _get():
                return client.get_orders()
            orders = await asyncio.to_thread(_get)
            if orders:
                return [o for o in orders
                        if isinstance(o, dict)
                        and o.get("status") in ("live", "open")]
            return []
        except Exception as exc:
            log.warning("get_open_orders failed: %s", exc)
            return []

    async def full_balance_recovery(self) -> dict:
        """
        Comprehensive balance recovery: check all locations where USDC.e
        could be stuck and attempt to recover it.

        Returns dict with amounts found at each location.
        """
        import aiohttp
        from eth_account import Account

        acct = Account.from_key(config.POLYGON_PRIVATE_KEY)
        result = {
            "eoa_balance": 0.0,
            "proxy_balance": 0.0,
            "clob_balance": 0.0,
            "open_orders": 0,
            "recovered": 0.0,
            "actions": [],
        }

        try:
            # 1. Check EOA balance
            eoa_bal = await self.get_onchain_usdc_balance(acct.address)
            result["eoa_balance"] = eoa_bal

            # 2. Check proxy wallet balance
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                proxy_addr = await self.get_proxy_wallet_address(session)
            if proxy_addr:
                proxy_bal = await self.get_onchain_usdc_balance(proxy_addr)
                result["proxy_balance"] = proxy_bal
                if proxy_bal > 0.01:
                    log.info("recovery: proxy has $%.2f — withdrawing", proxy_bal)
                    tx = await self.withdraw_proxy_usdc()
                    if tx:
                        result["recovered"] += proxy_bal
                        result["actions"].append(
                            f"Withdrew ${proxy_bal:.2f} from proxy: {tx}")

            # 3. Check CLOB exchange balance
            clob_bal = await self.get_balance_usdc()
            if clob_bal is not None:
                result["clob_balance"] = clob_bal

            # 4. Cancel open orders (free locked USDC)
            open_orders = await self.get_open_orders()
            result["open_orders"] = len(open_orders)
            if open_orders:
                log.info("recovery: %d open orders — canceling", len(open_orders))
                await self.cancel_all_open_orders()
                result["actions"].append(
                    f"Canceled {len(open_orders)} open orders")
                # Wait and check if proxy balance increased
                await asyncio.sleep(3)
                if proxy_addr:
                    proxy_bal2 = await self.get_onchain_usdc_balance(proxy_addr)
                    if proxy_bal2 > 0.01:
                        tx = await self.withdraw_proxy_usdc()
                        if tx:
                            result["recovered"] += proxy_bal2
                            result["actions"].append(
                                f"Post-cancel proxy withdraw: ${proxy_bal2:.2f}")

            # 5. Final EOA balance
            result["eoa_balance"] = await self.get_onchain_usdc_balance(
                acct.address)

        except Exception as exc:
            log.exception("full_balance_recovery failed: %s", exc)
            result["actions"].append(f"Error: {exc}")

        return result
