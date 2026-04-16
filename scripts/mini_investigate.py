#!/usr/bin/env python3
"""Minimal forensic: scan USDC.e transfers for EOA + proxy."""
import asyncio, json, os, sys, time
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")
import aiohttp
from eth_account import Account

PK = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
RPC = "https://polygon-bor-rpc.publicnode.com"
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXC = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
T = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

async def rpc(s, m, p):
    for _ in range(3):
        try:
            async with s.post(RPC, json={"jsonrpc":"2.0","id":1,"method":m,"params":p}) as r:
                d = await r.json()
            if "error" in d:
                if "range" in str(d["error"]).lower(): raise RuntimeError("RANGE")
                await asyncio.sleep(1); continue
            return d.get("result")
        except RuntimeError: raise
        except Exception: await asyncio.sleep(1)
    return None

async def bal(s, a):
    r = await rpc(s, "eth_call", [{"to":USDC,"data":"0x70a08231"+"0"*24+a.lower().replace("0x","")},"latest"])
    return int(r,16)/1e6 if r and r!="0x" else 0

async def proxy_of(s, a):
    r = await rpc(s, "eth_call", [{"to":EXC,"data":"0xedef7d8e"+"0"*24+a.lower().replace("0x","")},"latest"])
    return "0x"+r[-40:] if r and len(r)>=42 else None

async def scan(s, addr, b0, b1, direction, chunk=9000):
    at = "0x"+addr.lower().replace("0x","").rjust(64,"0")
    topics = [T, at, None] if direction=="OUT" else [T, None, at]
    logs = []
    cur = b0
    while cur <= b1:
        end = min(cur+chunk-1, b1)
        try:
            r = await rpc(s, "eth_getLogs", [{"fromBlock":hex(cur),"toBlock":hex(end),"address":USDC,"topics":topics}])
            for x in (r or []): x["_d"]=direction; logs.append(x)
        except Exception as e:
            if "RANGE" in str(e) and chunk > 500:
                return logs + await scan(s, addr, cur, b1, direction, chunk//2)
            print(f"    err @{cur}: {e}")
        cur = end+1
    return logs

async def main():
    acct = Account.from_key(PK); eoa = acct.address
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        proxy = await proxy_of(s, eoa)
        cur = int(await rpc(s, "eth_blockNumber", []), 16)
        b0 = cur - 600_000  # ~14 days on Polygon
        print(f"EOA: {eoa}\nProxy: {proxy}\nBlocks: {b0} → {cur}")
        print(f"EOA bal: ${await bal(s, eoa):.4f}")
        print(f"Proxy bal: ${await bal(s, proxy):.4f}" if proxy else "no proxy")
        for who, a in [("EOA",eoa)] + ([("PROXY",proxy)] if proxy else []):
            print(f"\n=== {who} {a} ===")
            logs_in = await scan(s, a, b0, cur, "IN")
            logs_out = await scan(s, a, b0, cur, "OUT")
            tot_in = tot_out = 0
            src = defaultdict(float); dst = defaultdict(float)
            for l in logs_in:
                if len(l.get("topics",[]))<3: continue
                amt = int(l.get("data","0x"),16)/1e6
                tot_in += amt
                src["0x"+l["topics"][1][-40:]] += amt
            for l in logs_out:
                if len(l.get("topics",[]))<3: continue
                amt = int(l.get("data","0x"),16)/1e6
                tot_out += amt
                dst["0x"+l["topics"][2][-40:]] += amt
            print(f"IN:  ${tot_in:.2f} ({len(logs_in)} txs)")
            print(f"OUT: ${tot_out:.2f} ({len(logs_out)} txs)")
            print(f"NET: ${tot_in-tot_out:+.2f}")
            print("TOP 10 SOURCES (IN):")
            for k,v in sorted(src.items(), key=lambda x:-x[1])[:10]:
                tag = ""
                if k==CTF.lower(): tag="(CTF redeem)"
                elif k==EXC.lower(): tag="(CTF exch)"
                elif k==NEG.lower(): tag="(NegRisk exch)"
                elif proxy and k==proxy.lower(): tag="(proxy)"
                elif k==eoa.lower(): tag="(EOA)"
                print(f"  ${v:>10.2f}  {k}  {tag}")
            print("TOP 10 DESTS (OUT):")
            for k,v in sorted(dst.items(), key=lambda x:-x[1])[:10]:
                tag = ""
                if k==CTF.lower(): tag="(CTF)"
                elif k==EXC.lower(): tag="(CTF exch - TRADE)"
                elif k==NEG.lower(): tag="(NegRisk exch - TRADE)"
                elif proxy and k==proxy.lower(): tag="(proxy)"
                elif k==eoa.lower(): tag="(EOA)"
                elif k==PROXY_FACTORY.lower(): tag="(factory)"
                else: tag="⚠️ UNKNOWN"
                print(f"  ${v:>10.2f}  {k}  {tag}")

asyncio.run(main())
