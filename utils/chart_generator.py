"""
Matplotlib chart generator for the Telegram /chart command.

Produces a single PNG with three stacked panels:
  1. Equity curve (line)
  2. Daily PnL (bar)
  3. Rolling 20-trade win rate (line)

Returns raw PNG bytes which the Telegram helper uploads as a photo.
"""

import io
import logging
from typing import List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime

log = logging.getLogger("charts")


def generate_pnl_chart(
    equity_points: List[dict],
    daily_series: List[dict],
    win_rate_series: List[dict],
) -> Optional[bytes]:
    if not equity_points:
        return None

    plt.style.use("dark_background")
    fig, axes = plt.subplots(3, 1, figsize=(10, 11))
    fig.suptitle("Polymarket 5M Bot — Performance", fontsize=15, color="white")

    # ── Equity curve ────────────────────────────────────
    ax0 = axes[0]
    xs = list(range(len(equity_points)))
    ys = [p["balance"] for p in equity_points]
    ax0.plot(xs, ys, color="#22c55e", linewidth=2)
    ax0.fill_between(xs, ys, min(ys), color="#22c55e", alpha=0.15)
    ax0.set_title("Equity Curve", color="white")
    ax0.set_ylabel("Balance ($)")
    ax0.grid(True, alpha=0.2)

    # ── Daily PnL bars ──────────────────────────────────
    ax1 = axes[1]
    if daily_series:
        dates = [d["date"] for d in daily_series]
        pnls = [d["pnl"] for d in daily_series]
        colors = ["#22c55e" if p >= 0 else "#ef4444" for p in pnls]
        ax1.bar(range(len(dates)), pnls, color=colors)
        ax1.axhline(0, color="white", linewidth=0.5)
        step = max(1, len(dates) // 10)
        ax1.set_xticks(range(0, len(dates), step))
        ax1.set_xticklabels([dates[i] for i in range(0, len(dates), step)], rotation=30, fontsize=8)
    ax1.set_title("Daily PnL", color="white")
    ax1.set_ylabel("PnL ($)")
    ax1.grid(True, alpha=0.2)

    # ── Rolling win rate ────────────────────────────────
    ax2 = axes[2]
    if win_rate_series:
        wxs = list(range(len(win_rate_series)))
        wys = [w["win_rate"] for w in win_rate_series]
        ax2.plot(wxs, wys, color="#3b82f6", linewidth=2)
        ax2.axhline(50, color="white", linestyle="--", alpha=0.4)
    ax2.set_title("Rolling 20-Trade Win Rate (%)", color="white")
    ax2.set_ylabel("Win Rate %")
    ax2.set_ylim(0, 100)
    ax2.grid(True, alpha=0.2)

    plt.tight_layout(rect=[0, 0, 1, 0.97])

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
