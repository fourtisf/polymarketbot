#!/usr/bin/env python3
"""
Comprehensive USDC.e recovery script.
Finds ALL money across EOA, proxy wallet, CLOB exchange balance,
open orders, and unredeemed conditional tokens.

Usage:
  cd /root/polymarket-5m-bot
  venv/bin/python3 scripts/recover_all.py
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
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
PROXY_WALLET_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"

API_KEY = os.getenv("POLYMARKET_API_KEY", "").strip()
API_SECRET = os.getenv("POLYMARKET_API_SECRET", "").strip()
API_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "").strip()

SEP = "=" * 60


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


async def get_token_balance(session, address, token_id):
    """Get ERC-1155 balance for a specific token at an address."""
    try:
        token_int = int(token_id)
        token_hex = hex(token_int)[2:].rjust(64, "0")
    except (ValueError, OverflowError):
        return 0

    addr_padded = address.lower().replace("0x", "").rjust(64, "0")
    call_data = "0x00fdd58e" + addr_padded + token_hex
    result = await rpc(session, "eth_call", [
        {"to": CTF_CONTRACT, "data": call_data}, "latest"
    ])
    return int(result, 16) if result and result != "0x" else 0


async def withdraw_proxy_usdc(session, acct, proxy_addr):
    """Transfer USDC.e from proxy wallet to EOA."""
    proxy_bal_raw = await get_usdc_balance(session, proxy_addr)
    if proxy_bal_raw < 0.01:
        return None, 0

    balance_raw = int(proxy_bal_raw * 1e6)
    print(f"  Proxy has ${proxy_bal_raw:.2f} — withdrawing...")

    transfer_sel = bytes.fromhex("a9059cbb")
    transfer_params = eth_abi.encode(["address", "uint256"], [acct.address, balance_raw])
    transfer_calldata = transfer_sel + transfer_params

    factory_sel = bytes.fromhex("34ee9791")
    factory_params = eth_abi.encode(
        ["(uint8,address,uint256,bytes)[]"],
        [[(0, USDC_E, 0, transfer_calldata)]]
    )
    factory_tx_data = "0x" + (factory_sel + factory_params).hex()

    try:
        est = int(await rpc(session, "eth_estimateGas", [{
            "from": acct.address, "to": PROXY_WALLET_FACTORY,
            "data": factory_tx_data,
        }]), 16)
        gas_limit = int(est * 1.5)
    except Exception as e:
        print(f"  Gas estimate failed: {e}")
        return None, 0

    nonce = int(await rpc(session, "eth_getTransactionCount",
                          [acct.address, "latest"]), 16)
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

    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            receipt = await rpc(session, "eth_getTransactionReceipt", [tx_hash])
            if receipt:
                status = int(receipt.get("status", "0x0"), 16)
                if status == 1:
                    return tx_hash, proxy_bal_raw
                return None, 0
        except Exception:
            pass
        await asyncio.sleep(2)
    return None, 0


def get_clob_client():
    """Initialize CLOB client."""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
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
            return ClobClient(
                host=CLOB_HOST, key=PRIVATE_KEY, chain_id=137, creds=creds)
        else:
            client = ClobClient(host=CLOB_HOST, key=PRIVATE_KEY, chain_id=137)
            derived = client.create_or_derive_api_creds()
            client.set_api_creds(derived)
            return client
    except Exception as e:
        print(f"  CLOB client init failed: {e}")
        return None


def check_clob_balance(client):
    """Check CLOB exchange balance."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal = client.get_balance_allowance(params)
        print(f"  Raw response: {bal}")
        if isinstance(bal, dict):
            balance = bal.get("balance", "0")
            allowance = bal.get("allowance", "0")
            bal_val = float(balance) / 1e6 if balance else 0
            print(f"  Balance: ${bal_val:.2f}")
            print(f"  Allowance: {allowance[:20]}...")
            return bal_val
    except Exception as e:
        print(f"  CLOB balance check failed: {e}")
    return 0


def cancel_all_orders(client):
    """Cancel all open orders."""
    try:
        orders = client.get_orders()
        if orders:
            open_orders = [o for o in orders
                          if isinstance(o, dict)
                          and o.get("status") in ("live", "open")]
            if open_orders:
                print(f"  Found {len(open_orders)} open orders — canceling...")
                for o in open_orders[:10]:
                    oid = o.get("id", "?")
                    print(f"    Order: {oid[:20]} status={o.get('status')} "
                          f"size={o.get('original_size')}")
                result = client.cancel_all()
                print(f"  Cancel result: {result}")
                return len(open_orders)
            else:
                print("  No open orders")
                return 0
        print("  No orders found")
        return 0
    except Exception as e:
        print(f"  Cancel orders failed: {e}")
        return 0


async def check_unredeemed_tokens(session, eoa, proxy_addr):
    """Check for unredeemed conditional tokens from recent trades."""
    trades_file = Path(__file__).parent.parent / "data" / "trades.json"
    if not trades_file.exists():
        print("  No trades.json")
        return 0

    trades = json.loads(trades_file.read_text() or "[]")
    wins = [t for t in trades
            if t.get("outcome") == "win" and t.get("phase") == "settled"]

    total_tokens = 0
    token_value = 0.0

    for t in wins[-20:]:  # Check last 20 winning trades
        token_id = t.get("token_id", "")
        if not token_id:
            continue

        # Check at proxy wallet
        proxy_raw = 0
        if proxy_addr:
            proxy_raw = await get_token_balance(session, proxy_addr, token_id)

        # Check at EOA
        eoa_raw = await get_token_balance(session, eoa, token_id)

        total = proxy_raw + eoa_raw
        if total > 0:
            shares = total / 1e6
            slug = t.get("window_slug", "?")[:30]
            value = shares * 1.0  # Worth $1 each if resolved as win
            total_tokens += total
            token_value += value
            print(f"    FOUND: {slug} | {shares:.0f} tokens "
                  f"(${value:.2f}) | proxy={proxy_raw} eoa={eoa_raw}")

    return token_value


async def redeem_tokens(session, acct, condition_id, neg_risk=True):
    """Try to redeem tokens for a condition."""
    cond_padded = condition_id.lower().replace("0x", "").rjust(64, "0")
    cond_bytes = bytes.fromhex(cond_padded)

    if neg_risk:
        inner_sel = bytes.fromhex("dbeccb23")
        inner_params = eth_abi.encode(["bytes32", "uint256[]"], [cond_bytes, [1, 2]])
        target = NEG_RISK_ADAPTER
    else:
        inner_sel = bytes.fromhex("01b7037c")
        inner_params = eth_abi.encode(
            ["address", "bytes32", "bytes32", "uint256[]"],
            [USDC_E, b'\x00' * 32, cond_bytes, [1, 2]])
        target = CTF_CONTRACT

    inner_calldata = inner_sel + inner_params

    # Via proxy wallet factory
    factory_sel = bytes.fromhex("34ee9791")
    factory_params = eth_abi.encode(
        ["(uint8,address,uint256,bytes)[]"],
        [[(0, target, 0, inner_calldata)]]
    )
    factory_data = "0x" + (factory_sel + factory_params).hex()

    try:
        est = int(await rpc(session, "eth_estimateGas", [{
            "from": acct.address, "to": PROXY_WALLET_FACTORY,
            "data": factory_data,
        }]), 16)
        if est < 30000:
            return None  # No-op
        gas_limit = int(est * 1.5)

        nonce = int(await rpc(session, "eth_getTransactionCount",
                              [acct.address, "latest"]), 16)
        gas_price = int(await rpc(session, "eth_gasPrice", []), 16)

        tx = {
            "to": bytes.fromhex(PROXY_WALLET_FACTORY.replace("0x", "")),
            "value": 0, "gas": gas_limit, "gasPrice": gas_price,
            "nonce": nonce, "chainId": 137,
            "data": bytes.fromhex(factory_data.replace("0x", "")),
        }
        signed = acct.sign_transaction(tx)
        raw = "0x" + signed.raw_transaction.hex()
        tx_hash = await rpc(session, "eth_sendRawTransaction", [raw])

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
    except Exception as e:
        print(f"    Redeem failed: {e}")
    return None


async def main():
    if not PRIVATE_KEY:
        print("ERROR: POLYGON_PRIVATE_KEY not set")
        sys.exit(1)

    acct = Account.from_key(PRIVATE_KEY)
    eoa = acct.address

    print(SEP)
    print("COMPREHENSIVE USDC.e RECOVERY")
    print(SEP)
    print(f"EOA: {eoa}\n")

    total_found = 0.0
    total_recovered = 0.0

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30)
    ) as session:

        # ── 1. ON-CHAIN BALANCES ──
        print("1. ON-CHAIN BALANCES:")
        eoa_bal = await get_usdc_balance(session, eoa)
        print(f"  EOA USDC.e: ${eoa_bal:.2f}")
        total_found += eoa_bal

        proxy_addr = await get_proxy_wallet(session, eoa)
        proxy_bal = 0.0
        if proxy_addr:
            proxy_bal = await get_usdc_balance(session, proxy_addr)
            print(f"  Proxy ({proxy_addr[:10]}...): ${proxy_bal:.2f}")
            total_found += proxy_bal
        else:
            print("  Proxy wallet: not found")

        # ── 2. CLOB EXCHANGE BALANCE ──
        print(f"\n2. CLOB EXCHANGE BALANCE:")
        client = get_clob_client()
        clob_bal = 0
        if client:
            clob_bal = check_clob_balance(client)
            total_found += max(0, clob_bal - eoa_bal)  # Avoid double-counting

        # ── 3. OPEN ORDERS ──
        print(f"\n3. OPEN ORDERS (may lock USDC.e):")
        if client:
            canceled = cancel_all_orders(client)
            if canceled > 0:
                await asyncio.sleep(3)
                # Re-check proxy after cancel
                if proxy_addr:
                    new_proxy_bal = await get_usdc_balance(session, proxy_addr)
                    if new_proxy_bal > proxy_bal:
                        print(f"  Proxy increased: ${proxy_bal:.2f} → ${new_proxy_bal:.2f}")
                        proxy_bal = new_proxy_bal

        # ── 4. UNREDEEMED TOKENS ──
        print(f"\n4. UNREDEEMED CONDITIONAL TOKENS:")
        token_value = await check_unredeemed_tokens(session, eoa, proxy_addr)
        if token_value > 0:
            total_found += token_value
            print(f"  Total unredeemed token value: ${token_value:.2f}")

            # Try to redeem them
            print("\n  Attempting to redeem...")
            trades_file = Path(__file__).parent.parent / "data" / "trades.json"
            trades = json.loads(trades_file.read_text() or "[]")
            wins = [t for t in trades
                    if t.get("outcome") == "win" and t.get("phase") == "settled"
                    and t.get("condition_id")]

            redeemed_conditions = set()
            for t in wins[-20:]:
                cid = t.get("condition_id", "")
                if cid and cid not in redeemed_conditions:
                    token_id = t.get("token_id", "")
                    if not token_id:
                        continue
                    bal = await get_token_balance(
                        session, proxy_addr or eoa, token_id)
                    if bal > 0:
                        print(f"    Redeeming condition {cid[:16]}...")
                        tx = await redeem_tokens(session, acct, cid)
                        if tx:
                            print(f"    SUCCESS: {tx}")
                            redeemed_conditions.add(cid)
                        else:
                            print(f"    No-op or failed")
        else:
            print("  No unredeemed tokens found")

        # ── 5. WITHDRAW PROXY ──
        print(f"\n5. PROXY WALLET WITHDRAWAL:")
        if proxy_addr:
            # Re-check after potential redeems
            proxy_bal = await get_usdc_balance(session, proxy_addr)
            if proxy_bal > 0.01:
                tx, amount = await withdraw_proxy_usdc(session, acct, proxy_addr)
                if tx:
                    print(f"  Withdrawn ${amount:.2f}: {tx}")
                    total_recovered += amount
                else:
                    print(f"  Withdrawal failed")
            else:
                print(f"  Proxy balance: ${proxy_bal:.2f} (nothing to withdraw)")
        else:
            print("  No proxy wallet")

        # ── 6. TRADE ANALYSIS ──
        print(f"\n6. TRADE ANALYSIS:")
        trades_file = Path(__file__).parent.parent / "data" / "trades.json"
        if trades_file.exists():
            trades = json.loads(trades_file.read_text() or "[]")
            settled = [t for t in trades if t.get("phase") == "settled"]
            wins = [t for t in settled if t.get("outcome") == "win"]
            losses = [t for t in settled if t.get("outcome") == "loss"]
            phantoms = [t for t in trades if t.get("outcome") == "phantom"]

            total_pnl = sum(t.get("pnl", 0) for t in settled)
            total_cost = sum(t.get("cost", 0) for t in settled)
            total_win_pnl = sum(t.get("pnl", 0) for t in wins)
            total_win_cost = sum(t.get("cost", 0) for t in wins)

            print(f"  Total trades: {len(settled)} ({len(wins)}W/{len(losses)}L)")
            print(f"  Phantoms: {len(phantoms)}")
            print(f"  Total PnL: ${total_pnl:+.2f}")
            print(f"  Total entry costs: ${total_cost:.2f}")
            print(f"  Winning trade costs: ${total_win_cost:.2f}")
            print(f"  Winning trade PnL: ${total_win_pnl:+.2f}")
            print(f"  Expected returns from wins: ${total_win_cost + total_win_pnl:.2f}")

        # ── 7. FINAL ──
        print(f"\n{SEP}")
        final_bal = await get_usdc_balance(session, eoa)
        print(f"FINAL EOA USDC.e: ${final_bal:.2f}")
        if total_recovered > 0:
            print(f"RECOVERED THIS RUN: +${total_recovered:.2f}")

        # Calculate expected vs actual
        if trades_file.exists():
            trades = json.loads(trades_file.read_text() or "[]")
            settled = [t for t in trades if t.get("phase") == "settled"]
            total_pnl = sum(t.get("pnl", 0) for t in settled)
            first_trade = settled[0] if settled else None
            if first_trade and first_trade.get("balance_before"):
                initial = first_trade["balance_before"]
                expected = initial + total_pnl
                gap = expected - final_bal
                print(f"\nINITIAL BALANCE: ${initial:.2f}")
                print(f"TOTAL PnL: ${total_pnl:+.2f}")
                print(f"EXPECTED BALANCE: ${expected:.2f}")
                print(f"ACTUAL BALANCE: ${final_bal:.2f}")
                print(f"GAP (missing): ${gap:.2f}")

        print(SEP)


if __name__ == "__main__":
    asyncio.run(main())
