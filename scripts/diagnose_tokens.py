#!/usr/bin/env python3
"""
Diagnose where conditional tokens are held — EOA vs proxy wallet.
Run on VPS: python3 scripts/diagnose_tokens.py
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
from eth_account import Account

PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"


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
    # token_id can be a large decimal number
    token_int = int(token_id_str)
    token_hex = hex(token_int)[2:].rjust(64, "0")
    data = "0x00fdd58e" + ap + token_hex
    result = await rpc(session, "eth_call", [{"to": CTF_CONTRACT, "data": data}, "latest"])
    return int(result, 16) if result and result != "0x" else 0


async def get_proxy_wallet(session, address, exchange):
    """Call getPolyProxyWalletAddress(address) on an exchange."""
    ap = address.lower().replace("0x", "").rjust(64, "0")
    data = "0xedef7d8e" + ap
    result = await rpc(session, "eth_call", [{"to": exchange, "data": data}, "latest"])
    if result and result != "0x" and len(result) >= 42:
        return "0x" + result[-40:]
    return None


async def has_code(session, address):
    code = await rpc(session, "eth_getCode", [address, "latest"])
    return code and code != "0x" and len(code) > 2


async def main():
    if not PRIVATE_KEY:
        print("ERROR: POLYGON_PRIVATE_KEY not set")
        sys.exit(1)

    acct = Account.from_key(PRIVATE_KEY)
    eoa = acct.address
    print(f"{'='*60}")
    print(f"TOKEN DIAGNOSTICS")
    print(f"{'='*60}")
    print(f"EOA: {eoa}")
    print()

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # Get proxy wallet addresses
        proxy_ctf = await get_proxy_wallet(session, eoa, CTF_EXCHANGE)
        proxy_neg = await get_proxy_wallet(session, eoa, NEG_RISK_EXCHANGE)
        print(f"CTF Exchange proxy:     {proxy_ctf or 'NOT FOUND'}")
        print(f"NegRisk Exchange proxy: {proxy_neg or 'NOT FOUND'}")

        # Check if proxies are deployed
        proxies = set()
        for label, addr in [("CTF proxy", proxy_ctf), ("NegRisk proxy", proxy_neg)]:
            if addr:
                deployed = await has_code(session, addr)
                print(f"  {label} deployed: {deployed}")
                proxies.add(addr.lower())

        # USDC.e balances
        print(f"\n--- USDC.e BALANCES ---")
        eoa_usdc = await get_usdc_balance(session, eoa)
        print(f"EOA:            ${eoa_usdc:.2f}")
        for addr in proxies:
            pbal = await get_usdc_balance(session, addr)
            print(f"Proxy {addr[:8]}...: ${pbal:.2f}")

        # Collect token IDs from trade logs
        token_ids = set()
        trades_file = Path(__file__).parent.parent / "data" / "trades.json"
        if trades_file.exists():
            try:
                trades = json.loads(trades_file.read_text() or "[]")
                for t in trades:
                    tid = t.get("token_id")
                    if tid:
                        token_ids.add(tid)
                    for k in ("token_up_id", "token_down_id"):
                        v = t.get(k)
                        if v:
                            token_ids.add(v)
                print(f"\nFound {len(token_ids)} unique token IDs in trades.json")
            except Exception as e:
                print(f"Error reading trades.json: {e}")
        else:
            print(f"\nNo trades.json at {trades_file}")

        # Also read PnL tracker
        pnl_file = Path(__file__).parent.parent / "data" / "pnl.json"
        if pnl_file.exists():
            try:
                pnl_data = json.loads(pnl_file.read_text() or "{}")
                all_trades = pnl_data.get("all_trades", [])
                for t in all_trades:
                    tid = t.get("token_id")
                    if tid:
                        token_ids.add(tid)
                print(f"PnL tracker has {len(all_trades)} trades total")
                wins = [t for t in all_trades if t.get("outcome") == "win"]
                losses = [t for t in all_trades if t.get("outcome") == "loss"]
                print(f"  Wins: {len(wins)}, Losses: {len(losses)}")
                # Show recent wins
                for w in wins[-5:]:
                    print(f"  WIN: {w.get('window_slug', '?')[:30]} "
                          f"pnl=${w.get('pnl', 0):.2f} "
                          f"token={str(w.get('token_id', ''))[:15]}... "
                          f"cond={str(w.get('condition_id', ''))[:15]}...")
            except Exception as e:
                print(f"Error reading pnl.json: {e}")

        if not token_ids:
            print("\nNo token IDs found — cannot check balances")
            return

        # Check conditional token balances at each address
        print(f"\n--- CONDITIONAL TOKEN BALANCES ---")
        addresses_to_check = [("EOA", eoa)]
        for p in proxies:
            addresses_to_check.append(("Proxy", p))

        tokens_found_anywhere = False
        for tid in sorted(token_ids):
            try:
                for label, addr in addresses_to_check:
                    bal = await get_ctf_balance(session, addr, tid)
                    if bal > 0:
                        tokens_found_anywhere = True
                        # Convert to human readable (1e18 for conditional tokens)
                        bal_human = bal / 1e6  # Polymarket uses 1e6
                        print(f"  TOKEN {tid[:15]}... at {label} ({addr[:10]}...): "
                              f"{bal} raw = ${bal_human:.4f}")
            except Exception as e:
                print(f"  TOKEN {tid[:15]}... error: {e}")

        if not tokens_found_anywhere:
            print("  NO conditional tokens found at EOA or proxy wallet!")
            print()
            print("This means either:")
            print("  1. Tokens were already redeemed (but USDC didn't increase)")
            print("  2. Orders were never actually filled on-chain")
            print("  3. Tokens are at a different address entirely")
            print()
            # Check the CLOB API for order history
            print("--- CHECKING CLOB API ---")
            try:
                clob_url = "https://clob.polymarket.com/data/trades"
                params = {"maker_address": eoa}
                async with session.get(clob_url, params=params) as resp:
                    if resp.status == 200:
                        clob_data = await resp.json()
                        trades_list = clob_data if isinstance(clob_data, list) else clob_data.get("data", [])
                        print(f"CLOB API shows {len(trades_list)} trades for this address")
                        for t in trades_list[:5]:
                            print(f"  {t.get('status', '?')} | "
                                  f"side={t.get('side', '?')} | "
                                  f"size={t.get('size', '?')} | "
                                  f"price={t.get('price', '?')} | "
                                  f"asset={str(t.get('asset_id', ''))[:15]}...")
                    else:
                        print(f"CLOB API returned {resp.status}")
            except Exception as e:
                print(f"CLOB API error: {e}")

            # Also check proxy wallet CLOB trades
            if proxies:
                for p in proxies:
                    try:
                        params = {"maker_address": p}
                        async with session.get(clob_url, params=params) as resp:
                            if resp.status == 200:
                                clob_data = await resp.json()
                                trades_list = clob_data if isinstance(clob_data, list) else clob_data.get("data", [])
                                print(f"CLOB API shows {len(trades_list)} trades for PROXY {p[:10]}...")
                    except Exception as e:
                        print(f"CLOB proxy check error: {e}")

        print(f"\n{'='*60}")
        print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
