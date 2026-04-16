#!/usr/bin/env python3
"""
Redeem ALL resolved winning conditional tokens back to USDC.e.

Workflow:
  1. Query CLOB API for the user's trade history
  2. For each traded market, look up conditionId via Gamma API
  3. Check if user holds any conditional tokens (ERC-1155 balanceOf)
  4. Call redeemPositions on CTF contract for each
  5. Report results

Usage:
  cd /root/polymarket-5m-bot
  python3 scripts/redeem_all.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import aiohttp
from eth_account import Account

# ── Constants ──────────────────────────────────────────────
PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
PROXY_WALLET_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"


async def rpc_call(session, method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(POLYGON_RPC, json=payload) as resp:
        data = await resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC {method}: {data['error']}")
    return data.get("result")


async def get_usdc_balance(session, address):
    addr_padded = address.lower().replace("0x", "").rjust(64, "0")
    data = "0x70a08231" + "0" * 24 + addr_padded[-40:]
    result = await rpc_call(session, "eth_call", [{"to": USDC_E, "data": data}, "latest"])
    if result and result != "0x":
        return int(result, 16) / 1e6
    return 0.0


async def get_ctf_balance(session, address, token_id_int):
    """Check ERC-1155 balanceOf(address, tokenId) on CTF contract."""
    # balanceOf(address,uint256) = 0x00fdd58e
    addr_padded = address.lower().replace("0x", "").rjust(64, "0")
    token_hex = hex(token_id_int)[2:].rjust(64, "0")
    data = "0x00fdd58e" + addr_padded + token_hex
    result = await rpc_call(session, "eth_call", [{"to": CTF_CONTRACT, "data": data}, "latest"])
    if result and result != "0x":
        return int(result, 16)
    return 0


async def fetch_user_trades(session, address):
    """Get recent trades from CLOB API."""
    # Try the trades endpoint
    url = f"{CLOB_HOST}/trades"
    params = {"maker_address": address}
    try:
        async with session.get(url, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        print(f"  trades endpoint failed: {e}")

    # Try alternative: data API
    url2 = f"{CLOB_HOST}/data/trades"
    try:
        async with session.get(url2, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        print(f"  data/trades endpoint failed: {e}")

    return []


async def fetch_condition_id(session, token_id):
    """Look up conditionId from Gamma API using a token ID."""
    url = f"{GAMMA_HOST}/markets"
    params = {"clob_token_ids": token_id}
    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                # Try alternative param name
                params2 = {"token_id": token_id}
                async with session.get(url, params=params2) as resp2:
                    if resp2.status != 200:
                        return None
                    data = await resp2.json()
            else:
                data = await resp.json()
    except Exception:
        return None

    markets = data if isinstance(data, list) else data.get("data", [])
    if not markets:
        return None
    market = markets[0]
    cond_id = market.get("conditionId") or market.get("condition_id") or ""
    resolved = market.get("resolved") or market.get("is_resolved")
    return {
        "condition_id": cond_id,
        "resolved": resolved,
        "question": market.get("question", ""),
        "slug": market.get("slug", ""),
    }


async def get_proxy_wallet(session, address):
    """Look up the proxy wallet address for an EOA."""
    addr_padded = address.lower().replace("0x", "").rjust(64, "0")
    # getPolyProxyWalletAddress(address) selector = 0xedef7d8e
    call_data = "0xedef7d8e" + addr_padded
    result = await rpc_call(session, "eth_call", [
        {"to": CTF_EXCHANGE, "data": call_data}, "latest"
    ])
    if result and result != "0x" and len(result) >= 42:
        return "0x" + result[-40:]
    return None


async def redeem_positions(session, acct, condition_id, neg_risk=True):
    """
    Redeem via ProxyWalletFactory.proxy() — tokens are in the proxy wallet.
    Falls back to direct call from EOA.
    """
    import eth_abi

    cond_bytes = bytes.fromhex(condition_id.lower().replace("0x", "").rjust(64, "0"))

    # Build inner redeemPositions calldata
    if neg_risk:
        # NegRisk: redeemPositions(bytes32, uint256[])
        inner_selector = bytes.fromhex("dbeccb23")
        inner_params = eth_abi.encode(["bytes32", "uint256[]"], [cond_bytes, [1, 2]])
        target = NEG_RISK_ADAPTER
    else:
        # CTF: redeemPositions(address, bytes32, bytes32, uint256[])
        inner_selector = bytes.fromhex("01b7037c")
        inner_params = eth_abi.encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [USDC_E, b'\x00' * 32, cond_bytes, [1, 2]]
        )
        target = CTF_CONTRACT

    inner_calldata = inner_selector + inner_params

    # Wrap in Factory.proxy() call
    factory_selector = bytes.fromhex("34ee9791")
    factory_params = eth_abi.encode(
        ["(uint8,address,uint256,bytes)[]"],
        [[(0, target, 0, inner_calldata)]]
    )
    factory_tx_data = "0x" + (factory_selector + factory_params).hex()

    # Try via proxy factory first
    gas_limit = 300_000
    use_factory = False
    try:
        est_hex = await rpc_call(session, "eth_estimateGas", [{
            "from": acct.address,
            "to": PROXY_WALLET_FACTORY,
            "data": factory_tx_data,
        }])
        estimated = int(est_hex, 16)
        if estimated >= 30_000:
            gas_limit = int(estimated * 1.5)
            use_factory = True
            print(f"  Gas (via proxy factory): {estimated}, using {gas_limit}")
        else:
            print(f"  Proxy factory gas too low ({estimated}), trying direct...")
    except Exception as e:
        print(f"  Proxy factory gas est failed ({e}), trying direct...")

    if not use_factory:
        # Fallback: direct call from EOA
        direct_data = "0x" + inner_calldata.hex()
        try:
            est_hex = await rpc_call(session, "eth_estimateGas", [{
                "from": acct.address,
                "to": target,
                "data": direct_data,
            }])
            estimated = int(est_hex, 16)
            if estimated < 30_000:
                print(f"  Direct gas too low ({estimated}) — nothing to redeem")
                return None
            gas_limit = int(estimated * 1.5)
            print(f"  Gas (direct): {estimated}, using {gas_limit}")
        except Exception as e:
            print(f"  Direct gas est failed ({e}) — skipping")
            return None

    tx_to = PROXY_WALLET_FACTORY if use_factory else target
    tx_data_hex = factory_tx_data if use_factory else "0x" + inner_calldata.hex()

    nonce = int(await rpc_call(session, "eth_getTransactionCount",
                                [acct.address, "latest"]), 16)
    gas_price = int(await rpc_call(session, "eth_gasPrice", []), 16)

    tx = {
        "to": bytes.fromhex(tx_to.replace("0x", "")),
        "value": 0,
        "gas": gas_limit,
        "gasPrice": gas_price,
        "nonce": nonce,
        "chainId": 137,
        "data": bytes.fromhex(tx_data_hex.replace("0x", "")),
    }
    signed = acct.sign_transaction(tx)
    raw = "0x" + signed.raw_transaction.hex()
    tx_hash = await rpc_call(session, "eth_sendRawTransaction", [raw])
    print(f"  TX sent ({'factory' if use_factory else 'direct'}): {tx_hash}")

    # Wait for receipt
    import time
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            receipt = await rpc_call(session, "eth_getTransactionReceipt", [tx_hash])
            if receipt is not None:
                status = int(receipt.get("status", "0x0"), 16)
                gas_used = int(receipt.get("gasUsed", "0x0"), 16)
                if status == 1:
                    print(f"  TX confirmed! Gas used: {gas_used}")
                    return tx_hash
                else:
                    print(f"  TX reverted! Gas used: {gas_used}")
                    return None
        except Exception:
            pass
        await asyncio.sleep(2)
    print("  TX receipt timeout")
    return None


async def main():
    if not PRIVATE_KEY:
        print("ERROR: POLYGON_PRIVATE_KEY not set in .env")
        sys.exit(1)

    acct = Account.from_key(PRIVATE_KEY)
    address = acct.address
    print(f"Wallet (EOA): {address}")
    print()

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Show proxy wallet address
        proxy_addr = await get_proxy_wallet(session, address)
        if proxy_addr:
            print(f"Proxy wallet: {proxy_addr}")
            proxy_bal = await get_usdc_balance(session, proxy_addr)
            print(f"Proxy USDC.e: ${proxy_bal:.2f}")
        else:
            print("Proxy wallet: not found")

        # Show current USDC.e balance
        bal_before = await get_usdc_balance(session, address)
        print(f"EOA USDC.e:   ${bal_before:.2f}")
        print()

        # ── Strategy 1: Check trade log files for token IDs ──
        token_ids = set()

        # Read from bot's trade log
        trades_file = Path(__file__).parent.parent / "data" / "trades.json"
        if trades_file.exists():
            print(f"Reading trade log: {trades_file}")
            try:
                with open(trades_file) as f:
                    trades = json.load(f)
                if isinstance(trades, list):
                    for t in trades:
                        # Look for token IDs in trade records
                        for key in ("token_id", "token_up_id", "token_down_id"):
                            tid = t.get(key)
                            if tid:
                                token_ids.add(tid)
                        # Also check for order responses that contain asset_id
                        aid = t.get("asset_id")
                        if aid:
                            token_ids.add(aid)
                    print(f"  Found {len(token_ids)} unique token IDs in trade log")
            except Exception as e:
                print(f"  Error reading trades: {e}")

        # ── Strategy 2: Query CLOB API for trades ──
        print("Querying CLOB API for trade history...")
        api_trades = await fetch_user_trades(session, address)
        print(f"  Got {len(api_trades)} trades from API")
        for t in api_trades:
            aid = t.get("asset_id") or t.get("token_id") or ""
            if aid:
                token_ids.add(aid)

        # ── Strategy 3: Also try to get from recent Gamma markets ──
        # Query recent BTC 5-min markets
        print("Querying Gamma API for recent BTC 5m markets...")
        try:
            gamma_url = f"{GAMMA_HOST}/markets"
            params = {"tag": "btc-5m", "limit": "20", "closed": "true"}
            async with session.get(gamma_url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    markets = data if isinstance(data, list) else data.get("data", [])
                    print(f"  Found {len(markets)} recent markets")
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
        except Exception as e:
            print(f"  Gamma query failed: {e}")

        if not token_ids:
            print("\nNo token IDs found. Trying slug-based search...")
            # Try to find markets by slug pattern
            import time
            now = int(time.time())
            # Check last 24 hours of windows (288 windows)
            for i in range(288):
                window_end = now - (now % 300) - (i * 300)
                slug = f"btc-updown-5m-{window_end}"
                info = await fetch_condition_id(session, "")
                # This won't work well, let's try direct Gamma slug lookup
                try:
                    async with session.get(f"{GAMMA_HOST}/markets",
                                          params={"slug": slug}) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            markets = data if isinstance(data, list) else data.get("data", [])
                            if markets:
                                m = markets[0]
                                cond = m.get("conditionId") or m.get("condition_id")
                                if cond:
                                    token_ids.add(f"slug:{slug}:{cond}")
                except Exception:
                    pass
                if len(token_ids) >= 10:
                    break

        print(f"\nTotal unique token IDs to check: {len(token_ids)}")
        print()

        # ── Look up condition IDs and redeem ──
        condition_ids_seen = set()
        redeemed = 0
        failed = 0

        for tid in sorted(token_ids):
            # Skip slug-based entries (handled differently)
            if tid.startswith("slug:"):
                parts = tid.split(":")
                cond_id = parts[2] if len(parts) > 2 else ""
                if cond_id and cond_id not in condition_ids_seen:
                    condition_ids_seen.add(cond_id)
                    print(f"Redeeming slug-based: {parts[1]}")
                    print(f"  Condition: {cond_id[:20]}...")
                    result = await redeem_positions(session, acct, cond_id)
                    if result:
                        redeemed += 1
                    else:
                        failed += 1
                continue

            print(f"Looking up token {tid[:20]}...")
            info = await fetch_condition_id(session, tid)
            if not info:
                print(f"  Not found on Gamma")
                continue

            cond_id = info["condition_id"]
            if not cond_id:
                print(f"  No conditionId")
                continue

            if cond_id in condition_ids_seen:
                print(f"  Already processed")
                continue

            condition_ids_seen.add(cond_id)
            print(f"  Market: {info.get('question', info.get('slug', '?'))[:60]}")
            print(f"  Resolved: {info.get('resolved')}")
            print(f"  Condition: {cond_id[:20]}...")

            # Try to redeem regardless of resolved status
            # (redeemPositions will revert if not resolved — that's OK)
            try:
                result = await redeem_positions(session, acct, cond_id)
                if result:
                    redeemed += 1
                else:
                    failed += 1
            except Exception as e:
                print(f"  Error: {e}")
                failed += 1

            # Small delay between redeems
            await asyncio.sleep(1)

        # Final balance
        print()
        bal_after = await get_usdc_balance(session, address)
        gained = bal_after - bal_before
        print(f"{'='*50}")
        print(f"Redeemed: {redeemed} | Failed: {failed}")
        print(f"USDC.e before: ${bal_before:.2f}")
        print(f"USDC.e after:  ${bal_after:.2f}")
        if gained > 0:
            print(f"Gained:        +${gained:.2f} ✅")
        else:
            print(f"Change:        ${gained:.2f}")
        print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())
