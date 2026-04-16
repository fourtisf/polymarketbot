#!/usr/bin/env python3
"""
Verify bot trades: compare CLOB API order history with on-chain token balances.
Identifies phantom trades (CLOB says filled, but no tokens on-chain).

Run on VPS: venv/bin/python3 scripts/verify_trades.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import aiohttp

POLYGON_PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CLOB_HOST = "https://clob.polymarket.com"


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


async def get_ctf_balance(session, address, token_id_str):
    """ERC-1155 balanceOf(address, tokenId) on CTF contract."""
    ap = address.lower().replace("0x", "").rjust(64, "0")
    token_int = int(token_id_str)
    token_hex = hex(token_int)[2:].rjust(64, "0")
    data = "0x00fdd58e" + ap + token_hex
    result = await rpc(session, "eth_call", [{"to": CTF_CONTRACT, "data": data}, "latest"])
    return int(result, 16) if result and result != "0x" else 0


async def main():
    if not POLYGON_PRIVATE_KEY:
        print("ERROR: POLYGON_PRIVATE_KEY not set")
        sys.exit(1)

    from eth_account import Account
    acct = Account.from_key(POLYGON_PRIVATE_KEY)
    eoa = acct.address
    print(f"{'='*70}")
    print(f"TRADE VERIFICATION REPORT")
    print(f"{'='*70}")
    print(f"EOA: {eoa}")

    # ── 1. Check CLOB API for actual trade history ──
    print(f"\n--- CLOB API TRADE HISTORY ---")
    clob_trades = []
    try:
        # Use py_clob_client if available
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        api_key = os.getenv("POLYMARKET_API_KEY", "").strip()
        api_secret = os.getenv("POLYMARKET_API_SECRET", "").strip()
        api_pass = os.getenv("POLYMARKET_PASSPHRASE", "").strip()

        if api_key and api_secret and api_pass:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_pass,
            )
            client = ClobClient(
                host=CLOB_HOST,
                key=POLYGON_PRIVATE_KEY,
                chain_id=137,
                creds=creds,
            )
            print("Using py_clob_client with API credentials")

            # Get trade history
            try:
                clob_trades = client.get_trades()
                if isinstance(clob_trades, dict):
                    clob_trades = clob_trades.get("data", [])
                print(f"CLOB API returned {len(clob_trades)} trades")
            except Exception as e:
                print(f"get_trades() failed: {e}")

            # Get order history
            try:
                orders = client.get_orders()
                if isinstance(orders, dict):
                    orders = orders.get("data", [])
                print(f"CLOB API returned {len(orders)} orders")
                # Show recent orders
                for o in (orders or [])[:10]:
                    status = o.get("status", "?")
                    side = o.get("side", "?")
                    size = o.get("original_size") or o.get("size", "?")
                    matched = o.get("size_matched", "0")
                    price = o.get("price", "?")
                    oid = o.get("id", o.get("order_id", "?"))
                    asset = str(o.get("asset_id", ""))[:15]
                    print(f"  ORDER {oid[:12]}... status={status} side={side} "
                          f"size={size} matched={matched} price={price} "
                          f"asset={asset}...")
            except Exception as e:
                print(f"get_orders() failed: {e}")
        else:
            # Try deriving creds
            print("No API credentials — trying to derive...")
            try:
                tmp = ClobClient(
                    host=CLOB_HOST,
                    key=POLYGON_PRIVATE_KEY,
                    chain_id=137,
                )
                derived = tmp.create_or_derive_api_creds()
                tmp.set_api_creds(derived)
                clob_trades = tmp.get_trades()
                if isinstance(clob_trades, dict):
                    clob_trades = clob_trades.get("data", [])
                print(f"CLOB API returned {len(clob_trades)} trades (derived creds)")
            except Exception as e:
                print(f"Derived auth failed: {e}")

    except ImportError:
        print("py_clob_client not installed — cannot query CLOB API")

    # Show CLOB trades
    if clob_trades:
        print(f"\nRecent CLOB trades:")
        for t in clob_trades[:20]:
            print(f"  {t.get('id', '?')[:12]}... | "
                  f"side={t.get('side', '?')} | "
                  f"size={t.get('size', '?')} | "
                  f"price={t.get('price', '?')} | "
                  f"status={t.get('status', '?')} | "
                  f"asset={str(t.get('asset_id', ''))[:15]}...")
    else:
        print("\nNO CLOB trades found!")
        print("This could mean:")
        print("  1. API credentials are wrong or expired")
        print("  2. Orders were posted but never matched")
        print("  3. The account has no trade history on Polymarket")

    # ── 2. Check bot's PnL tracker data ──
    print(f"\n--- BOT PNL TRACKER DATA ---")
    data_dir = Path(__file__).parent.parent / "data"
    equity_file = data_dir / "equity_curve.json"
    trades_file = data_dir / "trades.json"

    bot_trades = []
    if equity_file.exists():
        try:
            bot_trades = json.loads(equity_file.read_text() or "[]")
            print(f"equity_curve.json: {len(bot_trades)} recorded trades")
        except Exception as e:
            print(f"Error reading equity_curve.json: {e}")
    else:
        print(f"No equity_curve.json at {equity_file}")

    logged_trades = []
    if trades_file.exists():
        try:
            logged_trades = json.loads(trades_file.read_text() or "[]")
            print(f"trades.json: {len(logged_trades)} logged decisions")
        except Exception as e:
            print(f"Error reading trades.json: {e}")
    else:
        print(f"No trades.json at {trades_file}")

    # ── 3. Collect all token IDs from bot records ──
    print(f"\n--- ON-CHAIN TOKEN VERIFICATION ---")
    token_ids = set()
    for t in bot_trades + logged_trades:
        for key in ("token_id", "token_up_id", "token_down_id"):
            v = t.get(key)
            if v:
                token_ids.add(v)

    if not token_ids:
        print("No token IDs in bot records to verify")
    else:
        print(f"Checking {len(token_ids)} unique token IDs on-chain...")

    timeout = aiohttp.ClientTimeout(total=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Check USDC.e balance
        usdc = await get_usdc_balance(session, eoa)
        print(f"\nUSDC.e balance: ${usdc:.2f}")

        # Check each token ID
        tokens_with_balance = []
        for i, tid in enumerate(sorted(token_ids)):
            try:
                bal = await get_ctf_balance(session, eoa, tid)
                if bal > 0:
                    bal_usd = bal / 1e6
                    tokens_with_balance.append((tid, bal, bal_usd))
                    print(f"  [{i+1}/{len(token_ids)}] TOKEN {tid[:20]}... "
                          f"BALANCE: {bal} raw = ${bal_usd:.4f}")
                if (i + 1) % 20 == 0:
                    await asyncio.sleep(0.5)  # rate limit
            except Exception as e:
                print(f"  [{i+1}/{len(token_ids)}] TOKEN {tid[:20]}... ERROR: {e}")

        print(f"\nTokens with balance: {len(tokens_with_balance)}")
        total_token_value = sum(v for _, _, v in tokens_with_balance)
        print(f"Total token value: ${total_token_value:.2f}")

    # ── 4. Cross-reference: Bot PnL vs reality ──
    print(f"\n--- CROSS-REFERENCE ---")
    if bot_trades:
        total_pnl = sum(t.get("pnl", 0) for t in bot_trades)
        wins = [t for t in bot_trades if t.get("pnl", 0) > 0]
        losses = [t for t in bot_trades if t.get("pnl", 0) < 0]
        print(f"Bot PnL tracker claims:")
        print(f"  Total trades: {len(bot_trades)}")
        print(f"  Wins: {len(wins)}, Losses: {len(losses)}")
        print(f"  Total PnL: ${total_pnl:.2f}")
        print(f"  Starting balance: $200.00 (assumed)")
        print(f"  Claimed balance: ${200 + total_pnl:.2f}")
        print()
        print(f"On-chain reality:")
        print(f"  USDC.e: ${usdc:.2f}")
        print(f"  Tokens: ${total_token_value:.2f}")
        print(f"  Total on-chain: ${usdc + total_token_value:.2f}")
        print()
        discrepancy = (200 + total_pnl) - (usdc + total_token_value)
        print(f"  DISCREPANCY: ${discrepancy:.2f}")
        if abs(discrepancy) > 5:
            print(f"  *** SIGNIFICANT DISCREPANCY — bot may be recording phantom trades ***")
        else:
            print(f"  Discrepancy within normal range (gas costs etc.)")

        # Show trades without tx_hash (suspicious)
        no_tx = [t for t in bot_trades if not t.get("tx_hash")]
        if no_tx:
            print(f"\n  Trades WITHOUT tx_hash: {len(no_tx)}/{len(bot_trades)}")
            print(f"  *** These trades may not have been executed on-chain ***")
            for t in no_tx[:5]:
                print(f"    {t.get('window_slug', '?')[:30]} | "
                      f"{t.get('outcome', '?')} | "
                      f"pnl=${t.get('pnl', 0):.2f} | "
                      f"shares={t.get('shares', '?')}")

        # Show trades with phantom marker
        phantoms = [t for t in bot_trades if t.get("phantom")]
        if phantoms:
            print(f"\n  Phantom trades (detected): {len(phantoms)}")
    else:
        print("No bot PnL data to cross-reference")

    print(f"\n{'='*70}")
    print("DONE")
    print()
    print("NEXT STEPS:")
    print("  1. If CLOB shows 0 trades: Check API credentials in .env")
    print("  2. If trades exist but tokens are 0: Orders filled but tokens redeemed/expired")
    print("  3. If DISCREPANCY is large: Bot was recording phantom PnL")
    print("  4. Run: venv/bin/python3 scripts/redeem_all_onchain.py  (to redeem any remaining tokens)")


if __name__ == "__main__":
    asyncio.run(main())
