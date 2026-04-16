#!/usr/bin/env python3
"""
Find ALL conditional tokens at the EOA by scanning on-chain events,
then redeem every resolved position.

This bypasses the Gamma API (which deletes old 5-min markets) by
scanning TransferSingle events on the CTF contract directly.

Usage:
  cd /root/polymarket-5m-bot
  venv/bin/python3 scripts/redeem_all_onchain.py
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
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
GAMMA_HOST = "https://gamma-api.polymarket.com"

# TransferSingle(address,address,address,uint256,uint256)
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


async def scan_token_transfers(session, address):
    """Scan CTF TransferSingle events where 'to' = our address."""
    addr_topic = "0x" + address.lower().replace("0x", "").rjust(64, "0")

    # Get current block
    latest_hex = await rpc(session, "eth_blockNumber", [])
    latest_block = int(latest_hex, 16)

    # Scan last ~7 days of blocks (~2s per block on Polygon = ~302400 blocks)
    from_block = max(0, latest_block - 350_000)

    print(f"Scanning blocks {from_block} to {latest_block} (~{(latest_block - from_block) * 2 // 3600}h)...")

    token_ids = set()

    # Scan in chunks (max 10000 blocks per query on public RPCs)
    chunk_size = 10_000
    block = from_block

    while block < latest_block:
        to_block = min(block + chunk_size - 1, latest_block)
        try:
            logs = await rpc(session, "eth_getLogs", [{
                "address": CTF_CONTRACT,
                "topics": [
                    TRANSFER_SINGLE_TOPIC,
                    None,  # operator (any)
                    None,  # from (any)
                    addr_topic,  # to = our address
                ],
                "fromBlock": hex(block),
                "toBlock": hex(to_block),
            }])

            if logs:
                for log_entry in logs:
                    # data contains: uint256 id, uint256 value
                    log_data = log_entry.get("data", "0x")
                    if len(log_data) >= 130:  # 0x + 64 + 64
                        token_id = int(log_data[2:66], 16)
                        value = int(log_data[66:130], 16)
                        token_ids.add(token_id)

        except Exception as e:
            # If chunk too big, try smaller
            if "too many" in str(e).lower() or "limit" in str(e).lower():
                chunk_size = chunk_size // 2
                if chunk_size < 500:
                    print(f"  Block {block}: chunk too small, skipping: {e}")
                    block = to_block + 1
                continue
            print(f"  Block {block}: {e}")

        block = to_block + 1

        # Progress
        pct = (block - from_block) * 100 // (latest_block - from_block)
        if pct % 10 == 0:
            sys.stdout.write(f"\r  Progress: {pct}% ({len(token_ids)} tokens found)")
            sys.stdout.flush()

    print(f"\r  Done! Found {len(token_ids)} unique token IDs from events")
    return token_ids


async def lookup_condition_id_by_token(session, token_id_str):
    """Look up conditionId from Gamma API using clob_token_ids."""
    url = f"{GAMMA_HOST}/markets"
    for param_name in ["clob_token_ids", "token_id"]:
        try:
            async with session.get(url, params={param_name: token_id_str}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    markets = data if isinstance(data, list) else data.get("data", [])
                    if markets:
                        m = markets[0]
                        cond = m.get("conditionId") or m.get("condition_id") or ""
                        neg_risk = bool(m.get("negRisk") or m.get("neg_risk"))
                        resolved = m.get("resolved") or m.get("is_resolved")
                        return {
                            "condition_id": cond,
                            "neg_risk": neg_risk,
                            "resolved": resolved,
                            "question": m.get("question", ""),
                        }
        except Exception:
            pass
    return None


async def try_redeem(session, acct, condition_id, neg_risk=True):
    """Try to redeem a condition directly from EOA."""
    cond_bytes = bytes.fromhex(condition_id.lower().replace("0x", "").rjust(64, "0"))

    # Build calldata for both approaches
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
    nr_params = eth_abi.encode(
        ["bytes32", "uint256[]"],
        [cond_bytes, [1, 2]]
    )
    attempts.append((NEG_RISK_ADAPTER, "0x" + (nr_sel + nr_params).hex(), "NegRisk"))

    if neg_risk:
        attempts.reverse()  # Try NegRisk first

    for target, tx_data, label in attempts:
        try:
            est_hex = await rpc(session, "eth_estimateGas", [{
                "from": acct.address,
                "to": target,
                "data": tx_data,
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
                "value": 0,
                "gas": gas_limit,
                "gasPrice": gas_price,
                "nonce": nonce,
                "chainId": 137,
                "data": bytes.fromhex(tx_data.replace("0x", "")),
            }
            signed = acct.sign_transaction(tx)
            raw = "0x" + signed.raw_transaction.hex()
            tx_hash = await rpc(session, "eth_sendRawTransaction", [raw])
            print(f"    TX: {tx_hash}")

            import time
            deadline = time.time() + 60
            while time.time() < deadline:
                try:
                    receipt = await rpc(session, "eth_getTransactionReceipt", [tx_hash])
                    if receipt is not None:
                        status = int(receipt.get("status", "0x0"), 16)
                        if status == 1:
                            print(f"    CONFIRMED!")
                            return tx_hash
                        else:
                            print(f"    REVERTED")
                            return None
                except Exception:
                    pass
                await asyncio.sleep(2)
            print(f"    TIMEOUT")
            return None
        except Exception as e:
            err = str(e)[:80]
            # Don't print boring gas estimation failures
            if "execution reverted" not in err.lower():
                print(f"    {label}: {err}")

    return None


async def main():
    if not PRIVATE_KEY:
        print("ERROR: POLYGON_PRIVATE_KEY not set")
        sys.exit(1)

    acct = Account.from_key(PRIVATE_KEY)
    eoa = acct.address
    print(f"{'='*60}")
    print(f"FULL ON-CHAIN REDEEM")
    print(f"{'='*60}")
    print(f"EOA: {eoa}")
    print()

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        bal_before = await get_usdc_balance(session, eoa)
        print(f"USDC.e before: ${bal_before:.2f}")
        print()

        # Step 1: Scan on-chain events for all token IDs
        print("STEP 1: Scanning blockchain for all token transfers...")
        all_token_ids = await scan_token_transfers(session, eoa)

        if not all_token_ids:
            print("No token transfers found!")
            return

        # Step 2: Check which tokens have balance > 0
        print(f"\nSTEP 2: Checking balances for {len(all_token_ids)} tokens...")
        tokens_with_balance = []
        for i, tid in enumerate(sorted(all_token_ids)):
            bal = await get_ctf_balance(session, eoa, tid)
            if bal > 0:
                tokens_with_balance.append((tid, bal))
                print(f"  TOKEN {tid}: balance={bal} (${bal/1e6:.4f})")
            if (i + 1) % 50 == 0:
                sys.stdout.write(f"\r  Checked {i+1}/{len(all_token_ids)}...")
                sys.stdout.flush()

        print(f"\n  Found {len(tokens_with_balance)} tokens with balance > 0")
        total_value = sum(b/1e6 for _, b in tokens_with_balance)
        print(f"  Total token value: ${total_value:.2f}")

        if not tokens_with_balance:
            print("\nNo tokens with balance! All positions already redeemed or lost.")
            return

        # Step 3: Look up conditionIds and redeem
        print(f"\nSTEP 3: Looking up conditions and redeeming...")
        redeemed = 0
        failed = 0
        no_condition = 0
        condition_ids_seen = set()

        for tid, bal in tokens_with_balance:
            print(f"\n  Token {tid} (${bal/1e6:.4f}):")

            # Look up conditionId from Gamma API
            info = await lookup_condition_id_by_token(session, str(tid))
            if info and info.get("condition_id"):
                cond_id = info["condition_id"]
                neg_risk = info.get("neg_risk", False)
                print(f"    Condition: {cond_id[:20]}...")
                print(f"    Resolved: {info.get('resolved')}")
                print(f"    Market: {info.get('question', '?')[:50]}")

                if cond_id in condition_ids_seen:
                    print(f"    Already tried")
                    continue
                condition_ids_seen.add(cond_id)

                result = await try_redeem(session, acct, cond_id, neg_risk)
                if result:
                    redeemed += 1
                else:
                    failed += 1
            else:
                no_condition += 1
                print(f"    No conditionId from Gamma API")

                # Try to find condition from trade logs
                trades_file = Path(__file__).parent.parent / "data" / "trades.json"
                if trades_file.exists():
                    try:
                        trades = json.loads(trades_file.read_text() or "[]")
                        for t in trades:
                            if str(t.get("token_id")) == str(tid):
                                cond = t.get("condition_id")
                                if cond:
                                    print(f"    Found in trades.json: {cond[:20]}...")
                                    if cond not in condition_ids_seen:
                                        condition_ids_seen.add(cond)
                                        result = await try_redeem(
                                            session, acct, cond,
                                            t.get("neg_risk", False)
                                        )
                                        if result:
                                            redeemed += 1
                                            no_condition -= 1
                                        else:
                                            failed += 1
                                            no_condition -= 1
                                    break
                    except Exception:
                        pass

            await asyncio.sleep(0.5)  # Rate limit

        # Final report
        print()
        bal_after = await get_usdc_balance(session, eoa)
        gained = bal_after - bal_before
        print(f"{'='*60}")
        print(f"RESULTS")
        print(f"{'='*60}")
        print(f"Tokens with balance: {len(tokens_with_balance)}")
        print(f"Redeemed: {redeemed}")
        print(f"Failed: {failed}")
        print(f"No conditionId: {no_condition}")
        print(f"USDC.e before: ${bal_before:.2f}")
        print(f"USDC.e after:  ${bal_after:.2f}")
        if gained > 0:
            print(f"Gained:        +${gained:.2f}")
        else:
            print(f"Change:        ${gained:.2f}")
        print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
