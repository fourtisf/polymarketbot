#!/usr/bin/env python3
"""
Deep forensic investigation: find where ALL USDC.e went.

Scans on-chain transfer events for EOA and proxy addresses to find:
- Every incoming USDC.e (from CTF redemptions, swaps, etc.)
- Every outgoing USDC.e (spent on trades, withdrawn elsewhere, etc.)
- All conditional token balances at EOA AND proxy (for EVERY unique token
  ever traded, not just recent 20)
- Transfer history of conditional tokens
- Current vs expected cash position

Usage:
  cd /root/polymarket-5m-bot
  venv/bin/python3 scripts/investigate_missing.py
"""

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import aiohttp
from eth_account import Account

PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
PROXY_WALLET_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TRANSFER_BATCH_TOPIC = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"

SEP = "=" * 70


async def rpc(session, method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(3):
        try:
            async with session.post(POLYGON_RPC, json=payload) as resp:
                data = await resp.json()
            if "error" in data:
                err = data["error"]
                if attempt < 2:
                    await asyncio.sleep(1)
                    continue
                raise RuntimeError(f"RPC {method}: {err}")
            return data.get("result")
        except asyncio.TimeoutError:
            if attempt < 2:
                await asyncio.sleep(1)
                continue
            raise


async def get_block_number(session):
    r = await rpc(session, "eth_blockNumber", [])
    return int(r, 16)


async def get_usdc_balance(session, address):
    ap = address.lower().replace("0x", "").rjust(64, "0")
    data = "0x70a08231" + "0" * 24 + ap[-40:]
    result = await rpc(session, "eth_call",
                       [{"to": USDC_E, "data": data}, "latest"])
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


async def get_ctf_balance(session, address, token_id):
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


async def get_usdc_transfers(session, address, from_block, to_block,
                             chunk_size=9_000):
    """Get all USDC.e transfers to/from address via eth_getLogs.
    Polygon public RPC caps block range at 10,000."""
    addr_topic = "0x" + address.lower().replace("0x", "").rjust(64, "0")
    all_logs = []
    cur = from_block
    total_chunks = (to_block - from_block) // chunk_size + 1
    done = 0
    while cur <= to_block:
        end = min(cur + chunk_size - 1, to_block)
        for direction, topics in [
            ("IN",  [TRANSFER_TOPIC, None, addr_topic]),
            ("OUT", [TRANSFER_TOPIC, addr_topic, None]),
        ]:
            try:
                logs = await rpc(session, "eth_getLogs", [{
                    "fromBlock": hex(cur), "toBlock": hex(end),
                    "address": USDC_E, "topics": topics,
                }])
                for log in logs or []:
                    log["_direction"] = direction
                    all_logs.append(log)
            except Exception as e:
                emsg = str(e).lower()
                if "range" in emsg or "limit" in emsg or "query" in emsg:
                    new_chunk = chunk_size // 2
                    if new_chunk < 500:
                        print(f"    RPC still failing: {e}")
                        break
                    return await get_usdc_transfers(
                        session, address, cur, to_block, new_chunk)
                # rate-limit or transient — wait + retry
                await asyncio.sleep(2)
                try:
                    logs = await rpc(session, "eth_getLogs", [{
                        "fromBlock": hex(cur), "toBlock": hex(end),
                        "address": USDC_E, "topics": topics,
                    }])
                    for log in logs or []:
                        log["_direction"] = direction
                        all_logs.append(log)
                except Exception:
                    pass
        done += 1
        if done % 10 == 0:
            print(f"    Scanned {done}/{total_chunks} chunks, "
                  f"{len(all_logs)} logs found so far...")
        cur = end + 1
    return all_logs


async def check_all_trade_tokens(session, trades, eoa, proxy):
    """Check token balance at BOTH proxy and EOA for every unique token."""
    token_ids = {}
    for t in trades:
        tid = t.get("token_id", "")
        if tid and str(tid).isdigit():
            slug = t.get("window_slug", "?")
            outcome = t.get("outcome", "?")
            phase = t.get("phase", "?")
            ts = t.get("ts", 0)
            token_ids.setdefault(str(tid), {
                "slug": slug, "outcome": outcome,
                "phase": phase, "ts": ts,
            })
    print(f"  Unique token IDs found in trade history: {len(token_ids)}")

    total_value = 0.0
    found_tokens = []
    for i, (tid, meta) in enumerate(token_ids.items(), 1):
        if i % 10 == 0:
            print(f"    Checked {i}/{len(token_ids)}...")
        proxy_bal = await get_ctf_balance(session, proxy, tid) if proxy else 0
        eoa_bal = await get_ctf_balance(session, eoa, tid)
        if proxy_bal > 0 or eoa_bal > 0:
            shares = (proxy_bal + eoa_bal) / 1e6
            value = shares * 1.0
            total_value += value
            found_tokens.append({
                "token_id": tid, "slug": meta["slug"],
                "outcome": meta["outcome"], "phase": meta["phase"],
                "proxy": proxy_bal, "eoa": eoa_bal,
                "shares": shares, "value": value,
            })
    return found_tokens, total_value


def hex_to_int(h):
    return int(h, 16) if h and h != "0x" else 0


def topic_to_addr(t):
    return "0x" + t[-40:]


async def main():
    if not PRIVATE_KEY:
        print("ERROR: POLYGON_PRIVATE_KEY not set")
        sys.exit(1)

    acct = Account.from_key(PRIVATE_KEY)
    eoa = acct.address

    print(SEP)
    print("DEEP FORENSIC INVESTIGATION: WHERE DID THE MONEY GO?")
    print(SEP)
    print(f"EOA: {eoa}")

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60)
    ) as session:

        proxy = await get_proxy_wallet(session, eoa)
        print(f"Proxy: {proxy}")

        current_block = await get_block_number(session)
        print(f"Current block: {current_block}")

        # Load trade history
        trades_file = Path(__file__).parent.parent / "data" / "trades.json"
        trades = []
        if trades_file.exists():
            try:
                trades = json.loads(trades_file.read_text() or "[]")
            except Exception as e:
                print(f"Error reading trades: {e}")
        print(f"Total trade records: {len(trades)}")

        # Find earliest trade timestamp (but minimum 14 days ago)
        earliest_ts = min((t.get("ts", int(time.time()))
                          for t in trades if t.get("ts")),
                         default=int(time.time()) - 86400 * 14)
        # Always scan at least 14 days (trade log may be truncated)
        earliest_ts = min(earliest_ts, int(time.time()) - 86400 * 14)
        print(f"Scan-from timestamp: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(earliest_ts))}")

        # Estimate starting block (~2.3s/block on Polygon)
        seconds_back = int(time.time()) - earliest_ts + 3600
        blocks_back = min(seconds_back // 2, 3_000_000)
        start_block = max(0, current_block - blocks_back)
        print(f"Scanning blocks: {start_block} → {current_block} "
              f"(~{(current_block-start_block)/43200:.1f} days)")
        print()

        # ── 1. CURRENT BALANCES ──
        print("1. CURRENT ON-CHAIN BALANCES")
        print("-" * 70)
        eoa_bal = await get_usdc_balance(session, eoa)
        print(f"  EOA USDC.e:   ${eoa_bal:.6f}")
        proxy_bal = 0
        if proxy:
            proxy_bal = await get_usdc_balance(session, proxy)
            print(f"  Proxy USDC.e: ${proxy_bal:.6f}")
        print()

        # ── 2. USDC.e TRANSFER HISTORY (EOA) ──
        print("2. USDC.e TRANSFER HISTORY — EOA")
        print("-" * 70)
        try:
            eoa_logs = await get_usdc_transfers(
                session, eoa, start_block, current_block)
        except Exception as e:
            print(f"  Error scanning EOA logs: {e}")
            eoa_logs = []

        eoa_in = 0.0
        eoa_out = 0.0
        eoa_destinations = defaultdict(float)
        eoa_sources = defaultdict(float)

        for log in eoa_logs:
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue
            amt = hex_to_int(log.get("data", "0x")) / 1e6
            from_a = topic_to_addr(topics[1])
            to_a = topic_to_addr(topics[2])
            if log["_direction"] == "IN":
                eoa_in += amt
                eoa_sources[from_a.lower()] += amt
            else:
                eoa_out += amt
                eoa_destinations[to_a.lower()] += amt

        print(f"  Total IN:  ${eoa_in:.2f} ({sum(1 for l in eoa_logs if l['_direction']=='IN')} txs)")
        print(f"  Total OUT: ${eoa_out:.2f} ({sum(1 for l in eoa_logs if l['_direction']=='OUT')} txs)")
        print(f"  Net:       ${eoa_in - eoa_out:+.2f}")

        if eoa_sources:
            print("\n  TOP INCOMING SOURCES:")
            for addr, amt in sorted(eoa_sources.items(),
                                    key=lambda x: -x[1])[:10]:
                label = ""
                if addr == USDC_E.lower():
                    label = "(USDC contract)"
                elif addr == CTF_CONTRACT.lower():
                    label = "(CTF redeem)"
                elif addr == CTF_EXCHANGE.lower():
                    label = "(CTF exchange)"
                elif proxy and addr == proxy.lower():
                    label = "(PROXY WALLET)"
                print(f"    {addr}  ${amt:>10.2f}  {label}")

        if eoa_destinations:
            print("\n  TOP OUTGOING DESTINATIONS:")
            for addr, amt in sorted(eoa_destinations.items(),
                                    key=lambda x: -x[1])[:10]:
                label = ""
                if addr == CTF_EXCHANGE.lower():
                    label = "(CTF exchange — trades)"
                elif addr == CTF_CONTRACT.lower():
                    label = "(CTF contract)"
                elif proxy and addr == proxy.lower():
                    label = "(PROXY — deposit)"
                elif addr == PROXY_WALLET_FACTORY.lower():
                    label = "(proxy factory)"
                else:
                    label = "⚠️  UNKNOWN!"
                print(f"    {addr}  ${amt:>10.2f}  {label}")
        print()

        # ── 3. USDC.e TRANSFER HISTORY (PROXY) ──
        if proxy:
            print("3. USDC.e TRANSFER HISTORY — PROXY WALLET")
            print("-" * 70)
            try:
                proxy_logs = await get_usdc_transfers(
                    session, proxy, start_block, current_block)
            except Exception as e:
                print(f"  Error scanning proxy logs: {e}")
                proxy_logs = []

            p_in = 0.0
            p_out = 0.0
            p_dest = defaultdict(float)
            p_src = defaultdict(float)
            for log in proxy_logs:
                topics = log.get("topics", [])
                if len(topics) < 3:
                    continue
                amt = hex_to_int(log.get("data", "0x")) / 1e6
                from_a = topic_to_addr(topics[1])
                to_a = topic_to_addr(topics[2])
                if log["_direction"] == "IN":
                    p_in += amt
                    p_src[from_a.lower()] += amt
                else:
                    p_out += amt
                    p_dest[to_a.lower()] += amt
            print(f"  Total IN:  ${p_in:.2f} ({sum(1 for l in proxy_logs if l['_direction']=='IN')} txs)")
            print(f"  Total OUT: ${p_out:.2f} ({sum(1 for l in proxy_logs if l['_direction']=='OUT')} txs)")
            print(f"  Net:       ${p_in - p_out:+.2f}")

            if p_src:
                print("\n  TOP INCOMING TO PROXY:")
                for addr, amt in sorted(p_src.items(),
                                        key=lambda x: -x[1])[:10]:
                    label = ""
                    if addr == eoa.lower():
                        label = "(from EOA)"
                    elif addr == CTF_CONTRACT.lower():
                        label = "(CTF redeem ✓)"
                    elif addr == CTF_EXCHANGE.lower():
                        label = "(CTF exchange)"
                    print(f"    {addr}  ${amt:>10.2f}  {label}")

            if p_dest:
                print("\n  TOP OUTGOING FROM PROXY:")
                for addr, amt in sorted(p_dest.items(),
                                        key=lambda x: -x[1])[:10]:
                    label = ""
                    if addr == eoa.lower():
                        label = "(withdraw to EOA ✓)"
                    elif addr == CTF_EXCHANGE.lower():
                        label = "(trade collateral)"
                    elif addr == CTF_CONTRACT.lower():
                        label = "(split position)"
                    else:
                        label = "⚠️  UNKNOWN!"
                    print(f"    {addr}  ${amt:>10.2f}  {label}")
            print()

        # ── 4. ALL CONDITIONAL TOKENS (every unique trade, not just 20) ──
        print("4. CONDITIONAL TOKEN BALANCES — ALL UNIQUE TRADES")
        print("-" * 70)
        found_tokens, token_total = await check_all_trade_tokens(
            session, trades, eoa, proxy)
        if found_tokens:
            print(f"\n  Found {len(found_tokens)} tokens with non-zero balance:")
            for ft in found_tokens:
                loc = []
                if ft["proxy"] > 0:
                    loc.append(f"proxy={ft['proxy']}")
                if ft["eoa"] > 0:
                    loc.append(f"eoa={ft['eoa']}")
                print(f"    [{ft['outcome']:<5} {ft['phase']:<8}] "
                      f"{ft['slug'][:30]:<30} "
                      f"${ft['value']:>7.2f}  ({', '.join(loc)})")
            print(f"\n  Total locked in tokens: ${token_total:.2f}")
        else:
            print("  No tokens with non-zero balance.")
        print()

        # ── 5. CASH FLOW RECONCILIATION ──
        print("5. CASH FLOW RECONCILIATION")
        print("-" * 70)
        wins = [t for t in trades if t.get("outcome") == "win"]
        losses = [t for t in trades if t.get("outcome") == "loss"]
        total_entry = sum(float(t.get("entry_cost", 0) or 0) for t in trades)
        total_pnl = sum(float(t.get("pnl", 0) or 0) for t in trades)
        settled_count = sum(1 for t in trades if t.get("phase") == "settled")

        print(f"  Trades: {len(trades)} ({len(wins)}W / {len(losses)}L)")
        print(f"  Settled: {settled_count}")
        print(f"  Sum of entry_cost: ${total_entry:.2f}")
        print(f"  Sum of pnl:        ${total_pnl:+.2f}")
        print()

        starting_balance = 187.0
        expected = starting_balance + total_pnl
        current = eoa_bal + proxy_bal + token_total
        gap = expected - current

        print(f"  Starting balance:         ${starting_balance:.2f}")
        print(f"  Expected (start + PnL):   ${expected:.2f}")
        print(f"  Current (EOA+proxy+tok):  ${current:.2f}")
        print(f"  {'-'*50}")
        print(f"  GAP:                      ${gap:+.2f}")
        print()

        # ── 6. DIAGNOSIS ──
        print("6. DIAGNOSIS")
        print("-" * 70)
        if abs(gap) < 5:
            print("  ✓ Balances reconcile — no missing funds.")
        else:
            print(f"  ⚠️  ${abs(gap):.2f} UNACCOUNTED FOR.")
            print()
            unknown_out = 0.0
            for addr, amt in eoa_destinations.items():
                if addr not in (CTF_EXCHANGE.lower(), CTF_CONTRACT.lower(),
                                PROXY_WALLET_FACTORY.lower(),
                                (proxy or "").lower(), USDC_E.lower()):
                    unknown_out += amt
            if unknown_out > 5:
                print(f"  ⚠️  ${unknown_out:.2f} sent from EOA to UNKNOWN "
                      "addresses (check section 2).")

            if proxy:
                p_unknown = 0.0
                for addr, amt in p_dest.items():
                    if addr not in (eoa.lower(), CTF_EXCHANGE.lower(),
                                    CTF_CONTRACT.lower(), USDC_E.lower()):
                        p_unknown += amt
                if p_unknown > 5:
                    print(f"  ⚠️  ${p_unknown:.2f} sent from PROXY to UNKNOWN "
                          "addresses (check section 3).")

            # Actual net cash flow from chain
            chain_eoa_net = eoa_in - eoa_out
            chain_proxy_net = (p_in - p_out) if proxy else 0
            print()
            print(f"  Chain EOA net flow:   ${chain_eoa_net:+.2f}")
            print(f"  Chain PROXY net flow: ${chain_proxy_net:+.2f}")
            print(f"  Reported PnL:         ${total_pnl:+.2f}")
            chain_total = chain_eoa_net + chain_proxy_net
            if abs(chain_total - total_pnl) > 5:
                print(f"  ⚠️  On-chain flow disagrees with reported PnL "
                      f"by ${chain_total - total_pnl:+.2f}")
                print("     → PnL calculation in bot is likely WRONG.")
                print("     → Actual money change matches chain, not logs.")

        print()
        print(SEP)
        print("INVESTIGATION COMPLETE")
        print(SEP)


if __name__ == "__main__":
    asyncio.run(main())
