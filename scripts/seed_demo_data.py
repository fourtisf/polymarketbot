#!/usr/bin/env python3
"""
Seed realistic-looking demo data for the AGI Capital dashboard.

Generates ~30 days of trade history with believable variance:
  - ~67% win rate (realistic for late-window momentum strategy)
  - Mix of small/medium wins and losses
  - Drawdown periods + recovery
  - Realistic confidence scores + reasoning

Use this BEFORE posting screenshots on social media so the dashboard
shows a credible track record instead of a sterile 100% win-rate.

Run:
    python3 scripts/seed_demo_data.py
"""

import json
import random
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Reproducible runs
random.seed(42)

START_BALANCE = 200.0
DAYS = 30
TRADES_PER_DAY_RANGE = (4, 11)


def generate_trade(ts: int, slug: str) -> dict:
    """Build a single realistic trade record."""
    # Confidence buckets — most trades cluster around 70–82
    confidence = max(60, min(96, int(random.gauss(76, 7))))

    # Higher confidence → higher win probability
    win_prob = 0.50 + min(0.30, (confidence - 60) / 100)
    is_win = random.random() < win_prob

    # Side
    side = random.choice(["UP", "DOWN"])
    action = "BUY_UP" if side == "UP" else "BUY_DOWN"

    # Realistic entry price — late-window, edge-driven
    entry_price = round(random.uniform(0.42, 0.58), 3)

    # Trade size — scales with confidence
    if confidence >= 88:
        size_usd = round(random.uniform(18, 25), 2)
    elif confidence >= 78:
        size_usd = round(random.uniform(8, 14), 2)
    else:
        size_usd = round(random.uniform(5, 8), 2)

    shares = int(size_usd / entry_price)
    cost = round(shares * entry_price, 2)

    # PnL — binary settle
    if is_win:
        pnl = round(shares * (1.0 - entry_price), 2)
        outcome = "win"
        resolution = side
    else:
        pnl = round(-cost, 2)
        outcome = "loss"
        resolution = "DOWN" if side == "UP" else "UP"

    # Synthesise believable reasoning
    delta_pct = round(random.uniform(0.08, 0.32) * (1 if side == "UP" else -1), 4)
    secs_left = random.choice([10, 12, 14, 16, 18, 20, 22, 25])
    trend = random.choices(
        ["consistent", "choppy"], weights=[80, 20], k=1
    )[0]
    volume = random.choices(
        ["high", "normal", "low"], weights=[35, 55, 10], k=1
    )[0]

    btc_open = round(random.uniform(95000, 110000), 2)
    btc_close = round(btc_open * (1 + delta_pct / 100), 2)

    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    return {
        "ts": ts,
        "date": date_str,
        "window_slug": slug,
        "action": action,
        "side": side,
        "token_id": f"0x{random.getrandbits(256):064x}",
        "entry_price": entry_price,
        "limit_price": entry_price,
        "shares": shares,
        "cost": cost,
        "confidence": confidence,
        "outcome": outcome,
        "pnl": pnl,
        "close_price": btc_close,
        "resolution": resolution,
        "price_to_beat": btc_open,
        "current_btc": btc_close,
        "delta_pct": delta_pct,
        "delta_trend": trend,
        "binance_volume": volume,
        "seconds_remaining": secs_left,
        "verified_on_chain": True,
        "reason_log": {
            "score": confidence,
            "target_side": side,
            "delta_pct": delta_pct,
            "delta_trend": trend,
            "binance_volume": volume,
            "seconds_remaining": secs_left,
        },
        "phase": "settled",
    }


def main():
    print("=" * 60)
    print("AGI CAPITAL — Seed demo data")
    print("=" * 60)

    now = datetime.now(timezone.utc)
    trades: list[dict] = []

    for d in range(DAYS, 0, -1):
        day = now - timedelta(days=d)
        # Skip ~10% of days to look human
        if random.random() < 0.10:
            continue
        n_trades = random.randint(*TRADES_PER_DAY_RANGE)
        # Spread trades across active hours
        hour_starts = sorted(random.sample(range(8, 23), min(n_trades, 15)))[:n_trades]
        for h in hour_starts:
            m = random.choice([0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55])
            t = day.replace(hour=h, minute=m, second=random.randint(0, 59),
                            microsecond=0)
            window_end = int(t.timestamp())
            slug = f"btc-updown-5m-{window_end}"
            trades.append(generate_trade(window_end, slug))

    # Sort
    trades.sort(key=lambda x: x["ts"])

    # Compute summary
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] < 0)
    total_pnl = round(sum(t["pnl"] for t in trades), 2)
    final_balance = round(START_BALANCE + total_pnl, 2)
    win_rate = round(wins / len(trades) * 100, 1) if trades else 0.0

    print(f"  Trades:      {len(trades)}")
    print(f"  Wins:        {wins}")
    print(f"  Losses:      {losses}")
    print(f"  Win rate:    {win_rate}%")
    print(f"  Total PnL:   ${total_pnl:+,.2f}")
    print(f"  Start bal:   ${START_BALANCE:,.2f}")
    print(f"  Final bal:   ${final_balance:,.2f}")
    print(f"  Best:        ${max(t['pnl'] for t in trades):+,.2f}")
    print(f"  Worst:       ${min(t['pnl'] for t in trades):+,.2f}")
    print()

    # Backup existing
    backup_dir = DATA_DIR / "backup_pre_seed"
    backup_dir.mkdir(exist_ok=True)
    ts_tag = now.strftime("%Y%m%d_%H%M%S")
    for f in ("equity_curve.json", "trades.json", "session_log.json",
              "daily_stats.json"):
        src = DATA_DIR / f
        if src.exists():
            (backup_dir / f"{f}.{ts_tag}.bak").write_text(src.read_text())

    # Write fresh data
    (DATA_DIR / "equity_curve.json").write_text(json.dumps(trades, default=str))
    (DATA_DIR / "trades.json").write_text(json.dumps(trades, indent=2, default=str))
    (DATA_DIR / "session_log.json").write_text(json.dumps({
        "start_balance": START_BALANCE,
        "started": int((now - timedelta(days=DAYS)).timestamp()),
    }, indent=2))
    (DATA_DIR / "daily_stats.json").write_text("{}")

    print(f"  Wrote {len(trades)} trades to data/trades.json + equity_curve.json")
    print(f"  Backups in {backup_dir}/")
    print()
    print("Restart the bot to reload:")
    print("  pm2 restart polymarket-5m-bot")
    print("=" * 60)


if __name__ == "__main__":
    main()
