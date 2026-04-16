#!/usr/bin/env python3
"""
Find ALL conditional tokens using multiple sources, then redeem.

Strategy:
  1. Read token IDs from local trade logs (trades.json + pnl.json)
  2. Query Polymarket CLOB API for trade history
  3. Scan recent blockchain events (last ~6 hours, within RPC limits)
  4. Check balance for every found token ID
  5. Look up conditionId and redeem tokens with balance > 0

Usage:
  cd /root/polymarket-5m-bot
  venv/bin/python3 scripts/redeem_all_onchain.py
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import aiohttp
import eth_abi
from eth_account import Account

PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"


async def rpc(session, method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(POLYGON_RPC, json=payload) as resp:
        data = await resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC {method}: {data['error']}")
    return data.get("result")


async def get_usdc_balance(session, address):
    ap = address.lower().replace("0x", "").rjust(64, "0")
    data = "0x70a08231" + "0" * 24 + ap[-40:]
    result = await rpc(session, "eth_call", [{"to": USDC_E, "data": data}, "latest"])
    return int(result, 16) / 1e6 if result and result != "0x" else 0.0


async def get_ctf_balance(session, address, token_id_int):
    ap = address.lower().replace("0x", "").rjust(64, "0")
    token_hex = hex(token_id_int)[2:].rjust(64, "0")
    data = "0x00fdd58e" + ap + token_hex
    result = await rpc(session, "eth_call", [{"to": CTF_CONTRACT, "data": data}, "latest"])
    return int(result, 16) if result and result != "0x" else 0


# ── Token ID collection from all sources ──────────────────

def collect_from_trade_logs():
    """Read token IDs from trades.json and pnl.json."""
    token_ids = set()
    data_dir = Path(__file__).parent.parent / "data"

    # trades.json
    trades_file = data_dir / "trades.json"
    if trades_file.exists():
        try:
            trades = json.loads(trades_file.read_text() or "[]")
            for t in trades:
                for key in ("token_id", "token_up_id", "token_down_id", "asset_id"):
                    v = t.get(key)
                    if v:
                        token_ids.add(str(v))
            print(f"  trades.json: {len(token_ids)} token IDs")
        except Exception as e:
            print(f"  trades.json error: {e}")

    # pnl.json
    pnl_file = data_dir / "pnl.json"
    if pnl_file.exists():
        try:
            pnl = json.loads(pnl_file.read_text() or "{}")
            for t in pnl.get("all_trades", []):
                for key in ("token_id", "asset_id"):
                    v = t.get(key)
                    if v:
                        token_ids.add(str(v))
            print(f"  pnl.json: {len(token_ids)} total token IDs")
        except Exception as e:
            print(f"  pnl.json error: {e}")

    return token_ids


async def collect_from_clob_api(session, address):
    """Query CLOB API for trade history to get token IDs."""
    token_ids = set()
    endpoints = [
        f"{CLOB_HOST}/data/trades",
        f"{CLOB_HOST}/trades",
    ]
    for url in endpoints:
        for param_name in ["maker_address", "maker", "address"]:
            try:
                async with session.get(url, params={param_name: address}) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        trades = data if isinstance(data, list) else data.get("data", [])
                        for t in trades:
                            aid = t.get("asset_id") or t.get("token_id") or ""
                            if aid:
                                token_ids.add(str(aid))
                        if trades:
                            print(f"  CLOB API ({url.split('/')[-1]}): {len(trades)} trades, {len(token_ids)} tokens")
                            return token_ids
            except Exception:
                pass
    print(f"  CLOB API: no results")
    return token_ids


async def collect_from_gamma_api(session):
    """Query Gamma API for recent BTC 5-min markets."""
    token_ids = set()
    try:
        # Get recent markets - try multiple approaches
        for params in [
            {"tag": "btc-5m", "limit": "100", "closed": "true"},
            {"tag": "btc-5m", "limit": "100"},
            {"slug_contains": "btc-updown-5m", "limit": "100"},
        ]:
            async with session.get(f"{GAMMA_HOST}/markets", params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    markets = data if isinstance(data, list) else data.get("data", [])
                    for m in markets:
                        clob_ids = m.get("clobTokenIds") or m.get("clob_token_ids")
                        if isinstance(clob_ids, str):
                            try:
                                clob_ids = json.loads(clob_ids)
                            except Exception:
                                continue
                        if isinstance(clob_ids, list):
                            for tid in clob_ids:
                                token_ids.add(str(tid))
                    if markets:
                        print(f"  Gamma API: {len(markets)} markets, {len(token_ids)} tokens")
                        return token_ids
    except Exception as e:
        print(f"  Gamma API error: {e}")
    return token_ids


async def collect_from_recent_events(session, address):
    """Scan last ~6 hours of blockchain events (within RPC limits)."""
    token_ids = set()
    addr_topic = "0x" + address.lower().replace("0x", "").rjust(64, "0")

    try:
        latest_hex = await rpc(session, "eth_blockNumber", [])
        latest_block = int(latest_hex, 16)
        # ~6 hours = ~10800 blocks at 2s/block
        from_block = latest_block - 12_000

        print(f"  Events: scanning blocks {from_block}..{latest_block}")

        chunk_size = 5_000
        block = from_block
        while block < latest_block:
            to_block = min(block + chunk_size - 1, latest_block)
            try:
                logs = await rpc(session, "eth_getLogs", [{
                    "address": CTF_CONTRACT,
                    "topics": [TRANSFER_SINGLE_TOPIC, None, None, addr_topic],
                    "fromBlock": hex(block),
                    "toBlock": hex(to_block),
                }])
                if logs:
                    for entry in logs:
                        log_data = entry.get("data", "0x")
                        if len(log_data) >= 130:
                            tid = int(log_data[2:66], 16)
                            token_ids.add(str(tid))
            except Exception:
                pass  # Pruned blocks, skip
            block = to_block + 1

        print(f"  Events: {len(token_ids)} tokens from recent blocks")
    except Exception as e:
        print(f"  Events error: {e}")

    return token_ids


# ── Condition lookup and redemption ──────────────────────

async def lookup_condition_id(session, token_id_str):
    """Look up conditionId from Gamma API by token ID."""
    url = f"{GAMMA_HOST}/markets"
    for param in ["clob_token_ids", "token_id"]:
        try:
            async with session.get(url, params={param: token_id_str}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    markets = data if isinstance(data, list) else data.get("data", [])
                    if markets:
                        m = markets[0]
                        cond = m.get("conditionId") or m.get("condition_id") or ""
                        if cond:
                            return {
                                "condition_id": cond,
                                "neg_risk": bool(m.get("negRisk") or m.get("neg_risk")),
                                "resolved": m.get("resolved") or m.get("is_resolved"),
                                "question": m.get("question", ""),
                            }
        except Exception:
            pass
    return None


def get_condition_from_logs(token_id_str):
    """Try to find conditionId in local trade logs."""
    data_dir = Path(__file__).parent.parent / "data"
    for fname in ["trades.json", "pnl.json"]:
        fpath = data_dir / fname
        if not fpath.exists():
            continue
        try:
            raw = json.loads(fpath.read_text() or "[]" if fname == "trades.json" else "{}")
            trades = raw if isinstance(raw, list) else raw.get("all_trades", [])
            for t in trades:
                tid = str(t.get("token_id", ""))
                if tid == token_id_str:
                    cond = t.get("condition_id", "")
                    if cond:
                        return {
                            "condition_id": cond,
                            "neg_risk": t.get("neg_risk", False),
                        }
        except Exception:
            pass
    return None


async def try_redeem(session, acct, condition_id, neg_risk=True):
    """Redeem from EOA."""
    cond_bytes = bytes.fromhex(condition_id.lower().replace("0x", "").rjust(64, "0"))

    attempts = []
    # CTF: redeemPositions(address, bytes32, bytes32, uint256[])
    ctf_sel = bytes.fromhex("01b7037c")
    ctf_params = eth_abi.encode(
        ["address", "bytes32", "bytes32", "uint256[]"],
        [USDC_E, b'\x00' * 32, cond_bytes, [1, 2]]
    )
    attempts.append((CTF_CONTRACT, "0x" + (ctf_sel + ctf_params).hex(), "CTF"))

    # NegRisk: redeemPositions(bytes32, uint256[])
    nr_sel = bytes.fromhex("dbeccb23")
    nr_params = eth_abi.encode(["bytes32", "uint256[]"], [cond_bytes, [1, 2]])
    attempts.append((NEG_RISK_ADAPTER, "0x" + (nr_sel + nr_params).hex(), "NegRisk"))

    if neg_risk:
        attempts.reverse()

    for target, tx_data, label in attempts:
        try:
            est_hex = await rpc(session, "eth_estimateGas", [{
                "from": acct.address, "to": target, "data": tx_data,
            }])
            estimated = int(est_hex, 16)
            if estimated < 30_000:
                continue

            gas_limit = int(estimated * 1.5)
            print(f"    {label} gas={estimated}, sending TX...")

            nonce = int(await rpc(session, "eth_getTransactionCount",
                                   [acct.address, "latest"]), 16)
            gas_price = int(await rpc(session, "eth_gasPrice", []), 16)

            tx = {
                "to": bytes.fromhex(target.replace("0x", "")),
                "value": 0, "gas": gas_limit, "gasPrice": gas_price,
                "nonce": nonce, "chainId": 137,
                "data": bytes.fromhex(tx_data.replace("0x", "")),
            }
            signed = acct.sign_transaction(tx)
            raw = "0x" + signed.raw_transaction.hex()
            tx_hash = await rpc(session, "eth_sendRawTransaction", [raw])
            print(f"    TX: {tx_hash}")

            deadline = time.time() + 60
            while time.time() < deadline:
                try:
                    receipt = await rpc(session, "eth_getTransactionReceipt", [tx_hash])
                    if receipt is not None:
                        status = int(receipt.get("status", "0x0"), 16)
                        print(f"    {'CONFIRMED' if status == 1 else 'REVERTED'}!")
                        return tx_hash if status == 1 else None
                except Exception:
                    pass
                await asyncio.sleep(2)
            print(f"    TIMEOUT")
            return None
        except Exception:
            pass
    return None


async def main():
    if not PRIVATE_KEY:
        print("ERROR: POLYGON_PRIVATE_KEY not set")
        sys.exit(1)

    acct = Account.from_key(PRIVATE_KEY)
    eoa = acct.address
    print(f"{'='*60}")
    print(f"FIND & REDEEM ALL TOKENS")
    print(f"{'='*60}")
    print(f"EOA: {eoa}\n")

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        bal_before = await get_usdc_balance(session, eoa)
        print(f"USDC.e before: ${bal_before:.2f}\n")

        # ── Collect token IDs from ALL sources ──
        print("COLLECTING TOKEN IDs...")
        all_tokens = set()

        local = collect_from_trade_logs()
        all_tokens.update(local)

        clob = await collect_from_clob_api(session, eoa)
        all_tokens.update(clob)

        gamma = await collect_from_gamma_api(session)
        all_tokens.update(gamma)

        events = await collect_from_recent_events(session, eoa)
        all_tokens.update(events)

        print(f"\n  TOTAL unique token IDs: {len(all_tokens)}\n")

        if not all_tokens:
            print("No token IDs found!")
            return

        # ── Check balances ──
        print("CHECKING BALANCES...")
        tokens_with_balance = []
        checked = 0
        for tid_str in sorted(all_tokens):
            try:
                tid_int = int(tid_str)
            except ValueError:
                continue
            checked += 1
            bal = await get_ctf_balance(session, eoa, tid_int)
            if bal > 0:
                tokens_with_balance.append((tid_str, bal))
                print(f"  HAS BALANCE: {tid_str[:20]}... = {bal} raw (${bal/1e6:.4f})")
            if checked % 20 == 0:
                print(f"  ... checked {checked}/{len(all_tokens)}")

        total_value = sum(b / 1e6 for _, b in tokens_with_balance)
        print(f"\n  Tokens with balance: {len(tokens_with_balance)}")
        print(f"  Total value: ${total_value:.2f}\n")

        if not tokens_with_balance:
            print("No tokens with balance at EOA.")
            print("All winning positions may have already been redeemed or lost.")
            return

        # ── Look up conditions and redeem ──
        print("REDEEMING...")
        redeemed = 0
        failed = 0
        no_cond = 0
        seen_conds = set()

        for tid_str, bal in tokens_with_balance:
            print(f"\n  Token {tid_str[:20]}... (${bal/1e6:.4f}):")

            # Try Gamma API
            info = await lookup_condition_id(session, tid_str)
            if not info:
                # Try local logs
                local_info = get_condition_from_logs(tid_str)
                if local_info:
                    info = local_info

            if info and info.get("condition_id"):
                cond_id = info["condition_id"]
                neg_risk = info.get("neg_risk", False)
                print(f"    Condition: {cond_id[:20]}...")
                if "question" in info:
                    print(f"    Market: {info['question'][:50]}")

                if cond_id in seen_conds:
                    print(f"    Already tried")
                    continue
                seen_conds.add(cond_id)

                result = await try_redeem(session, acct, cond_id, neg_risk)
                if result:
                    redeemed += 1
                else:
                    failed += 1
            else:
                no_cond += 1
                print(f"    No conditionId found (Gamma + local)")

            await asyncio.sleep(0.5)

        # ── Final report ──
        print()
        bal_after = await get_usdc_balance(session, eoa)
        gained = bal_after - bal_before
        print(f"{'='*60}")
        print(f"RESULTS")
        print(f"{'='*60}")
        print(f"Tokens checked: {checked}")
        print(f"Tokens with balance: {len(tokens_with_balance)} (${total_value:.2f})")
        print(f"Redeemed: {redeemed}")
        print(f"Failed: {failed}")
        print(f"No conditionId: {no_cond}")
        print(f"USDC.e before: ${bal_before:.2f}")
        print(f"USDC.e after:  ${bal_after:.2f}")
        if gained > 0:
            print(f"GAINED:        +${gained:.2f}")
        print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
