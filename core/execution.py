"""
Order execution via py-clob-client.

Wraps CLOB client in an async-friendly interface. The underlying SDK is
sync, so we call it via asyncio.to_thread().

The fill strategy:
  1. Post GTC at best_ask — should cross the spread for immediate fill
  2. Poll order status for up to 6s to catch delayed matches
  3. If still unfilled, cancel + repost at best_ask + 0.01 (taker)
  4. Poll for 4s more, then give up
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

    # Polymarket exchange contracts that need USDC.e spending approval
    USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    EXCHANGE_SPENDERS = [
        "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",  # CTF Exchange
        "0xC5d563A36AE78145C45a50134d48A1215220f80a",  # NegRisk CTF Exchange
        "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",  # NegRisk Adapter
    ]
    MAX_UINT256_HEX = "f" * 64  # 2^256 - 1 as hex
    POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"

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
        `price` should be the current best_ask. The method posts at
        that price to cross the spread, then polls for fills. If not
        filled, escalates to taker pricing.
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

        # Attempt 1: post at best_ask (should cross the spread)
        fill = await self._try_post(token_id, price, shares)
        if self._is_balance_error(fill.error):
            return fill
        if fill.success and fill.filled_shares > 0:
            return fill

        # Order is live but not yet matched — poll for delayed fills
        if fill.order_id:
            polled = await self._poll_order_fills(fill.order_id, price, timeout=6)
            if polled.filled_shares > 0:
                return polled
            await self._cancel(fill.order_id)

        # Attempt 2: taker at best_ask + 0.01
        fill2 = await self._try_post(token_id, price + 0.01, shares)
        if self._is_balance_error(fill2.error):
            return fill2
        if fill2.success and fill2.filled_shares > 0:
            return fill2

        if fill2.order_id:
            polled = await self._poll_order_fills(fill2.order_id, price + 0.01, timeout=4)
            if polled.filled_shares > 0:
                return polled
            await self._cancel(fill2.order_id)

        # Attempt 3: deep taker at best_ask + 0.02
        fill3 = await self._try_post(token_id, price + 0.02, shares)
        if self._is_balance_error(fill3.error):
            return fill3
        if fill3.success and fill3.filled_shares > 0:
            return fill3
        if fill3.order_id:
            await self._cancel(fill3.order_id)

        return FillResult(success=False, error="not filled after retries")

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
            await asyncio.sleep(2)
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
                if size_matched > 0 or status in ("matched", "filled"):
                    tx_hash = self._extract_tx_hash(resp)
                    avg = price
                    if size_matched > 0 and resp.get("makingAmount"):
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
                        filled_shares=size_matched if size_matched > 0 else 1.0,
                        avg_price=avg,
                        tx_hash=tx_hash,
                    )
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

    async def redeem_positions(self, condition_id: str) -> Optional[str]:
        """
        Redeem resolved conditional tokens back to USDC.e.
        Calls redeemPositions on the CTF contract.
        Returns tx hash on success, None on failure.
        """
        if self.dry_run or not condition_id:
            return None

        import aiohttp
        from eth_account import Account

        acct = Account.from_key(config.POLYGON_PRIVATE_KEY)
        # redeemPositions(address collateralToken, bytes32 parentCollectionId,
        #                 bytes32 conditionId, uint256[] indexSets)
        # Function selector: keccak256("redeemPositions(address,bytes32,bytes32,uint256[])")
        # We compute it at call time to avoid hardcoding errors
        try:
            from eth_hash.auto import keccak as _keccak
            selector = _keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])").hex()[:8]
        except ImportError:
            selector = "01a18627"  # known Gnosis CTF selector

        usdc_e_padded = self.USDC_E.lower().replace("0x", "").rjust(64, "0")
        parent_collection = "0" * 64  # bytes32(0)
        cond_padded = condition_id.lower().replace("0x", "").rjust(64, "0")
        # offset to dynamic array (4 params × 32 bytes = 128 = 0x80)
        array_offset = "0" * 62 + "80"  # hex 128 = 64 hex chars
        # array length = 2
        array_len = "0" * 63 + "2"
        # indexSet[0] = 1 (first outcome), indexSet[1] = 2 (second outcome)
        idx_0 = "0" * 63 + "1"
        idx_1 = "0" * 63 + "2"

        tx_data = "0x" + selector + usdc_e_padded + parent_collection + cond_padded + \
                  array_offset + array_len + idx_0 + idx_1

        log.info("redeem: condition_id=%s selector=%s data_len=%d",
                 condition_id[:16], selector, len(tx_data))

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            try:
                # Estimate gas — if this fails, there's nothing to redeem
                # (condition not resolved or no tokens held)
                try:
                    est_hex = await self._rpc(session, "eth_estimateGas", [{
                        "from": acct.address,
                        "to": self.CTF_CONTRACT,
                        "data": tx_data,
                    }])
                    estimated = int(est_hex, 16)
                    gas_limit = int(estimated * 1.5)  # 50% buffer
                    log.info("redeem: estimated gas=%d, using %d", estimated, gas_limit)
                except Exception as est_exc:
                    log.info("redeem: gas estimate failed — nothing to redeem or "
                             "not yet resolved: %s", est_exc)
                    return None

                nonce = int(await self._rpc(session, "eth_getTransactionCount",
                                            [acct.address, "latest"]), 16)
                gas_price_hex = await self._rpc(session, "eth_gasPrice", [])
                gas_price = int(gas_price_hex, 16)

                tx_hash = await self._sign_and_send(
                    session, acct, self.CTF_CONTRACT, tx_data, nonce, gas_price,
                    gas=gas_limit,
                )
                if tx_hash:
                    ok = await self._wait_receipt(session, tx_hash)
                    log.info("redeem tx=%s ok=%s", tx_hash, ok)
                    if not ok:
                        log.error("redeem TX reverted: %s", tx_hash)
                    return tx_hash if ok else None
                else:
                    log.error("redeem: sign_and_send returned None")
            except Exception as exc:
                log.exception("redeem failed: %s", exc)
        return None

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
