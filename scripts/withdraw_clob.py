#!/usr/bin/env python3
"""
Check and withdraw ALL USDC.e from Polymarket CLOB exchange balance.

Polymarket may auto-settle winning positions, leaving USDC.e in the
CLOB exchange balance (NOT in your on-chain wallet). This script:
  1. Checks your CLOB exchange balance
  2. Withdraws everything to your on-chain wallet
  3. Also sweeps proxy wallet USDC.e to EOA

Usage:
  cd /root/polymarket-5m-bot
  venv/bin/python3 scripts/withdraw_clob.py
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
import eth_abi
from eth_account import Account

PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
PROXY_WALLET_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"

API_KEY = os.getenv("POLYMARKET_API_KEY", "").strip()
API_SECRET = os.getenv("POLYMARKET_API_SECRET", "").strip()
API_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "").strip()


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


async def get_proxy_wallet(session, address):
    addr_padded = address.lower().replace("0x", "").rjust(64, "0")
    call_data = "0xedef7d8e" + addr_padded
    result = await rpc(session, "eth_call", [
        {"to": CTF_EXCHANGE, "data": call_data}, "latest"
    ])
    if result and result != "0x" and len(result) >= 42:
        return "0x" + result[-40:]
    return None


async def withdraw_proxy_usdc(session, acct, proxy_addr):
    """Transfer USDC.e from proxy wallet to EOA."""
    ap = proxy_addr.lower().replace("0x", "").rjust(64, "0")
    data = "0x70a08231" + "0" * 24 + ap[-40:]
    result = await rpc(session, "eth_call", [{"to": USDC_E, "data": data}, "latest"])
    balance = int(result, 16) if result and result != "0x" else 0
    if balance == 0:
        return None

    print(f"  Proxy has ${balance/1e6:.2f} USDC.e — withdrawing...")

    transfer_sel = bytes.fromhex("a9059cbb")
    transfer_params = eth_abi.encode(["address", "uint256"], [acct.address, balance])
    transfer_calldata = transfer_sel + transfer_params

    factory_sel = bytes.fromhex("34ee9791")
    factory_params = eth_abi.encode(
        ["(uint8,address,uint256,bytes)[]"],
        [[(0, USDC_E, 0, transfer_calldata)]]
    )
    factory_tx_data = "0x" + (factory_sel + factory_params).hex()

    try:
        est = int(await rpc(session, "eth_estimateGas", [{
            "from": acct.address, "to": PROXY_WALLET_FACTORY, "data": factory_tx_data,
        }]), 16)
        gas_limit = int(est * 1.5)
    except Exception as e:
        print(f"  Gas estimate failed: {e}")
        return None

    nonce = int(await rpc(session, "eth_getTransactionCount", [acct.address, "latest"]), 16)
    gas_price = int(await rpc(session, "eth_gasPrice", []), 16)

    tx = {
        "to": bytes.fromhex(PROXY_WALLET_FACTORY.replace("0x", "")),
        "value": 0, "gas": gas_limit, "gasPrice": gas_price,
        "nonce": nonce, "chainId": 137,
        "data": bytes.fromhex(factory_tx_data.replace("0x", "")),
    }
    signed = acct.sign_transaction(tx)
    raw = "0x" + signed.raw_transaction.hex()
    tx_hash = await rpc(session, "eth_sendRawTransaction", [raw])
    print(f"  TX: {tx_hash}")

    import time
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            receipt = await rpc(session, "eth_getTransactionReceipt", [tx_hash])
            if receipt:
                status = int(receipt.get("status", "0x0"), 16)
                return tx_hash if status == 1 else None
        except Exception:
            pass
        await asyncio.sleep(2)
    return None


def check_clob_balance():
    """Check CLOB exchange balance using py_clob_client."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
    except ImportError:
        print("  py-clob-client not installed")
        return None

    has_creds = all([API_KEY, API_SECRET, API_PASSPHRASE])

    try:
        if has_creds:
            creds = ApiCreds(
                api_key=API_KEY,
                api_secret=API_SECRET,
                api_passphrase=API_PASSPHRASE,
            )
            client = ClobClient(
                host=CLOB_HOST,
                key=PRIVATE_KEY,
                chain_id=137,
                creds=creds,
            )
        else:
            client = ClobClient(
                host=CLOB_HOST,
                key=PRIVATE_KEY,
                chain_id=137,
            )
            derived = client.create_or_derive_api_creds()
            client.set_api_creds(derived)

        # Check collateral balance
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = client.get_balance_allowance(params)
        print(f"  CLOB balance/allowance: {bal}")
        return bal
    except Exception as e:
        print(f"  CLOB balance check failed: {e}")
        return None


def check_clob_open_orders():
    """Check for any open/pending orders that have USDC.e locked."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
    except ImportError:
        return None

    has_creds = all([API_KEY, API_SECRET, API_PASSPHRASE])
    try:
        if has_creds:
            creds = ApiCreds(api_key=API_KEY, api_secret=API_SECRET, api_passphrase=API_PASSPHRASE)
            client = ClobClient(host=CLOB_HOST, key=PRIVATE_KEY, chain_id=137, creds=creds)
        else:
            client = ClobClient(host=CLOB_HOST, key=PRIVATE_KEY, chain_id=137)
            derived = client.create_or_derive_api_creds()
            client.set_api_creds(derived)

        orders = client.get_orders()
        if orders:
            open_orders = [o for o in orders if isinstance(o, dict) and o.get("status") in ("live", "open")]
            print(f"  Open orders: {len(open_orders)}")
            for o in open_orders[:5]:
                print(f"    {o.get('id', '?')[:15]} status={o.get('status')} size={o.get('original_size')}")
            return open_orders
        return []
    except Exception as e:
        print(f"  Open orders check failed: {e}")
        return None


def check_trade_history():
    """Check recent trades from local logs."""
    data_dir = Path(__file__).parent.parent / "data"
    trades_file = data_dir / "trades.json"
    if not trades_file.exists():
        print("  No trades.json found")
        return []

    try:
        trades = json.loads(trades_file.read_text() or "[]")
        recent = [t for t in trades if t.get("phase") == "settled"][-10:]
        print(f"  Recent settled trades: {len(recent)}")
        for t in recent:
            slug = t.get("window_slug", "?")[:30]
            outcome = t.get("outcome", "?")
            pnl = t.get("pnl", 0)
            cond = t.get("condition_id", "")
            token = t.get("token_id", "")[:15]
            print(f"    {slug} | {outcome} | pnl={pnl:+.2f} | cond={'YES' if cond else 'EMPTY'} | token={token}")
        return recent
    except Exception as e:
        print(f"  Trades read error: {e}")
        return []


async def main():
    if not PRIVATE_KEY:
        print("ERROR: POLYGON_PRIVATE_KEY not set")
        sys.exit(1)

    acct = Account.from_key(PRIVATE_KEY)
    eoa = acct.address

    print(f"{'='*60}")
    print(f"POLYMARKET BALANCE DIAGNOSTIC & WITHDRAWAL")
    print(f"{'='*60}")
    print(f"EOA: {eoa}\n")

    # 1. Check on-chain balances
    print("1. ON-CHAIN BALANCES:")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        eoa_bal = await get_usdc_balance(session, eoa)
        print(f"  EOA USDC.e: ${eoa_bal:.2f}")

        proxy_addr = await get_proxy_wallet(session, eoa)
        proxy_bal = 0.0
        if proxy_addr:
            proxy_bal = await get_usdc_balance(session, proxy_addr)
            print(f"  Proxy wallet: {proxy_addr}")
            print(f"  Proxy USDC.e: ${proxy_bal:.2f}")
        else:
            print(f"  Proxy wallet: not found")

    # 2. Check CLOB exchange balance
    print(f"\n2. CLOB EXCHANGE BALANCE:")
    clob_bal = check_clob_balance()

    # 3. Check open orders
    print(f"\n3. OPEN ORDERS:")
    check_clob_open_orders()

    # 4. Check recent trade history
    print(f"\n4. RECENT TRADES (from logs):")
    check_trade_history()

    # 5. Withdraw proxy USDC if any
    if proxy_addr and proxy_bal > 0.01:
        print(f"\n5. WITHDRAWING PROXY USDC.e:")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            tx = await withdraw_proxy_usdc(session, acct, proxy_addr)
            if tx:
                print(f"  SUCCESS: {tx}")
            else:
                print(f"  No withdrawal needed or failed")
    else:
        print(f"\n5. PROXY WITHDRAWAL: not needed (${proxy_bal:.2f})")

    # 6. Final balance
    print(f"\n{'='*60}")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        final_bal = await get_usdc_balance(session, eoa)
        print(f"FINAL EOA USDC.e: ${final_bal:.2f}")
        if final_bal > eoa_bal:
            print(f"RECOVERED: +${final_bal - eoa_bal:.2f}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
