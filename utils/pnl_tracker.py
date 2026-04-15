"""
PnL tracking — session, daily, all-time, and equity curve.

Records every resolved trade and exposes aggregated stats for the
Telegram bot and the web dashboard.

All data persists to JSON files under /data so the bot survives restarts.
"""

import json
import logging
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

log = logging.getLogger("pnl")


class PnLTracker:
    def __init__(self):
        self.equity_file: Path = config.EQUITY_CURVE_FILE
        self.daily_file: Path = config.DAILY_STATS_FILE
        self.session_file: Path = config.SESSION_FILE
        self._resolved: List[Dict[str, Any]] = []
        self._session_start_balance: float = config.STARTING_BALANCE
        self._session_started: int = int(time.time())
        self._load()

    def _load(self) -> None:
        if self.equity_file.exists():
            try:
                self._resolved = json.loads(self.equity_file.read_text() or "[]")
            except json.JSONDecodeError:
                self._resolved = []
        if self.session_file.exists():
            try:
                s = json.loads(self.session_file.read_text() or "{}")
                self._session_start_balance = float(s.get("start_balance", config.STARTING_BALANCE))
                self._session_started = int(s.get("started", time.time()))
            except Exception:
                pass

    def _save(self) -> None:
        self.equity_file.write_text(json.dumps(self._resolved, default=str)[-5_000_000:])
        self.session_file.write_text(json.dumps({
            "start_balance": self._session_start_balance,
            "started": self._session_started,
        }))

    # ── Recording ──────────────────────────────────────────
    def record(self, trade: Dict[str, Any]) -> None:
        """
        trade: {
          ts, window_slug, side, entry_price, shares,
          outcome ('win'|'loss'), pnl, confidence, reason_log
        }
        """
        trade.setdefault("ts", int(time.time()))
        trade.setdefault("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        self._resolved.append(trade)
        # Cap
        if len(self._resolved) > 10000:
            self._resolved = self._resolved[-10000:]
        self._save()

    # ── Aggregations ───────────────────────────────────────
    def all_trades(self) -> List[Dict[str, Any]]:
        return list(self._resolved)

    def _stats(self, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not trades:
            return {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "pnl": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "profit_factor": 0.0,
                "best": 0.0,
                "worst": 0.0,
            }
        wins = [t for t in trades if t.get("pnl", 0) > 0]
        losses = [t for t in trades if t.get("pnl", 0) < 0]
        total_wins = sum(t["pnl"] for t in wins)
        total_losses = abs(sum(t["pnl"] for t in losses))
        return {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "pnl": round(sum(t.get("pnl", 0) for t in trades), 2),
            "avg_win": round(total_wins / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-total_losses / len(losses), 2) if losses else 0.0,
            "profit_factor": round(total_wins / total_losses, 2) if total_losses else (9.99 if wins else 0.0),
            "best": round(max((t["pnl"] for t in trades), default=0), 2),
            "worst": round(min((t["pnl"] for t in trades), default=0), 2),
        }

    def today_stats(self) -> Dict[str, Any]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self._stats([t for t in self._resolved if t.get("date") == today])

    def week_stats(self) -> Dict[str, Any]:
        cutoff = time.time() - 7 * 86400
        return self._stats([t for t in self._resolved if t.get("ts", 0) >= cutoff])

    def alltime_stats(self) -> Dict[str, Any]:
        stats = self._stats(self._resolved)
        stats["starting_balance"] = config.STARTING_BALANCE
        stats["current_balance"] = round(config.STARTING_BALANCE + stats["pnl"], 2)
        stats["roi_pct"] = round(stats["pnl"] / config.STARTING_BALANCE * 100, 2) if config.STARTING_BALANCE else 0
        # Max drawdown
        peak = config.STARTING_BALANCE
        max_dd = 0.0
        running = config.STARTING_BALANCE
        for t in self._resolved:
            running += t.get("pnl", 0)
            if running > peak:
                peak = running
            dd = running - peak
            if dd < max_dd:
                max_dd = dd
        stats["max_drawdown"] = round(max_dd, 2)
        # Days active
        days = {t.get("date") for t in self._resolved if t.get("date")}
        stats["days_active"] = len(days)
        # Sharpe (daily returns)
        daily_pnl: Dict[str, float] = defaultdict(float)
        for t in self._resolved:
            if t.get("date"):
                daily_pnl[t["date"]] += t.get("pnl", 0)
        if len(daily_pnl) >= 2:
            vals = list(daily_pnl.values())
            mean = statistics.mean(vals)
            stdev = statistics.stdev(vals) if len(vals) > 1 else 0
            stats["sharpe_daily"] = round(mean / stdev, 2) if stdev else 0.0
        else:
            stats["sharpe_daily"] = 0.0
        # Best / worst day
        if daily_pnl:
            stats["best_day"] = round(max(daily_pnl.values()), 2)
            stats["worst_day"] = round(min(daily_pnl.values()), 2)
        else:
            stats["best_day"] = 0.0
            stats["worst_day"] = 0.0
        return stats

    def equity_curve(self, limit: int = 500) -> List[Dict[str, Any]]:
        """List of {ts, balance} points for charting."""
        pts = []
        running = config.STARTING_BALANCE
        for t in self._resolved[-limit:]:
            running += t.get("pnl", 0)
            pts.append({"ts": t.get("ts"), "balance": round(running, 2)})
        return pts

    def daily_pnl_series(self) -> List[Dict[str, Any]]:
        daily: Dict[str, float] = defaultdict(float)
        for t in self._resolved:
            if t.get("date"):
                daily[t["date"]] += t.get("pnl", 0)
        return [{"date": d, "pnl": round(v, 2)} for d, v in sorted(daily.items())]

    def rolling_win_rate(self, window: int = 20) -> List[Dict[str, Any]]:
        out = []
        for i, t in enumerate(self._resolved):
            start = max(0, i - window + 1)
            slice_ = self._resolved[start:i + 1]
            if not slice_:
                continue
            wins = sum(1 for x in slice_ if x.get("pnl", 0) > 0)
            out.append({"ts": t.get("ts"), "win_rate": round(wins / len(slice_) * 100, 1)})
        return out

    def recent_trades(self, n: int = 10) -> List[Dict[str, Any]]:
        return self._resolved[-n:][::-1]

    def current_streak(self) -> str:
        if not self._resolved:
            return "—"
        sign = 1 if self._resolved[-1].get("pnl", 0) > 0 else -1
        count = 0
        for t in reversed(self._resolved):
            p = t.get("pnl", 0)
            if (p > 0 and sign == 1) or (p < 0 and sign == -1):
                count += 1
            else:
                break
        return f"{count}{'W 🔥' if sign == 1 else 'L ❄️'}"
