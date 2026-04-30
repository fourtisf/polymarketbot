"""Historical strategy simulator.

Replays core.strategy.decide() over Binance 1s klines. We cannot model
Polymarket's order book history (the venue does not expose it), so the
simulator focuses on signal edge: given the same (delta, seconds_left,
trend, volume) inputs the bot uses live, what fraction of windows resolve
in the predicted direction? That is the upper bound of profitability —
real fills will always be lower because of slippage and the price gate.

To bypass the price gate in raw-signal mode we inject a token price of
$0.50 (always within the dynamic cap). To stress-test more conservative
caps, pass --token-price 0.55 / 0.60.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import median
from typing import List, Optional, Tuple

import config
from core import strategy
from core.backtest.binance_history import Kline, windows_from_klines

log = logging.getLogger("backtest.simulate")


@dataclass
class SimResult:
    window_start: int
    window_end: int
    price_to_beat: float
    close_price: float
    resolution: str               # "UP" | "DOWN"
    decision: str                 # action from strategy.decide()
    side: Optional[str] = None    # "UP" | "DOWN" if entered
    confidence: int = 0
    delta_at_entry: float = 0.0
    seconds_at_entry: int = 0
    trend_at_entry: str = ""
    volume_at_entry: str = ""
    token_price: float = 0.0
    win: Optional[bool] = None    # None = no entry
    pnl_per_dollar: float = 0.0   # (1-p) on win, -p on loss


@dataclass
class SimSummary:
    n_windows: int = 0
    n_entries: int = 0
    wins: int = 0
    losses: int = 0
    sum_pnl_per_dollar: float = 0.0
    by_score: dict = field(default_factory=dict)
    by_delta: dict = field(default_factory=dict)

    @property
    def entry_rate(self) -> float:
        return self.n_entries / self.n_windows if self.n_windows else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n_entries if self.n_entries else 0.0

    @property
    def avg_pnl_per_dollar(self) -> float:
        return self.sum_pnl_per_dollar / self.n_entries if self.n_entries else 0.0


def _classify_volume(window_qtys: List[float],
                     baseline: List[float]) -> str:
    total = sum(window_qtys)
    if not baseline:
        return "normal"
    med = median(baseline)
    if med <= 0:
        return "normal"
    ratio = total / med
    if ratio >= 1.4:
        return "high"
    if ratio <= 0.6:
        return "low"
    return "normal"


def _classify_trend(deltas: List[float]) -> str:
    if len(deltas) < 6:
        return "choppy"
    sample = deltas[-30:]
    pos = sum(1 for d in sample if d > 0)
    neg = sum(1 for d in sample if d < 0)
    n = len(sample)
    last, first = sample[-1], sample[0]
    if last * first < 0:
        return "reversing"
    if last >= 0 and pos / n >= 0.75:
        return "consistent"
    if last < 0 and neg / n >= 0.75:
        return "consistent"
    return "choppy"


def simulate_window(klines: List[Kline], token_price: float,
                    historical_volumes: List[float]) -> Optional[SimResult]:
    """Simulate one 5-minute window. Returns a SimResult or None if window
    has too few klines to be useful."""
    if len(klines) < 60:  # need at least a minute of data to be meaningful
        return None

    klines = sorted(klines, key=lambda k: k.open_time)
    window_start_s = klines[0].open_time // 1000
    window_start_s -= window_start_s % config.WINDOW_LENGTH_SECONDS
    window_end_s = window_start_s + config.WINDOW_LENGTH_SECONDS

    price_to_beat = klines[0].open
    close_price = klines[-1].close
    resolution = "UP" if close_price >= price_to_beat else "DOWN"

    result = SimResult(
        window_start=window_start_s,
        window_end=window_end_s,
        price_to_beat=price_to_beat,
        close_price=close_price,
        resolution=resolution,
        decision="SKIP",
    )

    # Walk the window second-by-second, computing deltas.
    # We use kline.close as the "current price" at kline.open_time + 1s.
    deltas_history: List[float] = []
    for k in klines:
        delta_pct = (k.close - price_to_beat) / price_to_beat * 100.0
        deltas_history.append(delta_pct)

        seconds_remaining = window_end_s - (k.open_time // 1000) - 1
        if seconds_remaining > config.ENTRY_WINDOW_START_SEC:
            continue
        if seconds_remaining < config.ENTRY_WINDOW_END_SEC:
            break

        # Volume in the trailing 60s (rolling)
        trailing_qtys = [kk.volume for kk in klines
                         if k.open_time - 60_000 <= kk.open_time <= k.open_time]
        vol_label = _classify_volume(trailing_qtys, historical_volumes)
        trend_label = _classify_trend(deltas_history)

        ctx = strategy.TradeContext(
            window_slug=f"sim-{window_end_s}",
            price_to_beat=price_to_beat,
            current_btc=k.close,
            delta_pct=delta_pct,
            delta_trend=trend_label,
            binance_volume=vol_label,
            seconds_remaining=seconds_remaining,
            token_up_price=token_price,
            token_down_price=token_price,
        )
        decision = strategy.decide(ctx)
        if decision.action in ("BUY_UP", "BUY_DOWN"):
            side = "UP" if decision.action == "BUY_UP" else "DOWN"
            win = (side == resolution)
            result.decision = decision.action
            result.side = side
            result.confidence = decision.confidence
            result.delta_at_entry = delta_pct
            result.seconds_at_entry = seconds_remaining
            result.trend_at_entry = trend_label
            result.volume_at_entry = vol_label
            result.token_price = token_price
            result.win = win
            result.pnl_per_dollar = (1.0 - token_price) if win else -token_price
            return result

    return result


def summarize(results: List[SimResult]) -> SimSummary:
    s = SimSummary()
    for r in results:
        s.n_windows += 1
        if r.side is None:
            continue
        s.n_entries += 1
        if r.win:
            s.wins += 1
        else:
            s.losses += 1
        s.sum_pnl_per_dollar += r.pnl_per_dollar

        score_bucket = (r.confidence // 10) * 10
        b = s.by_score.setdefault(score_bucket,
                                  {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        b["wins"] += int(bool(r.win))
        b["pnl"] += r.pnl_per_dollar

        delta_key = "{:.2f}".format(round(abs(r.delta_at_entry), 2))
        d = s.by_delta.setdefault(delta_key,
                                  {"n": 0, "wins": 0, "pnl": 0.0})
        d["n"] += 1
        d["wins"] += int(bool(r.win))
        d["pnl"] += r.pnl_per_dollar
    return s


def run_simulation(klines: List[Kline], token_price: float = 0.50
                   ) -> Tuple[List[SimResult], SimSummary]:
    """Top-level: bucket klines into windows, simulate each, summarize."""
    # Pre-compute baseline (per-minute) volumes for volume classification.
    # We use 60s rolling sums sampled every 60s across the whole history.
    baseline: List[float] = []
    if klines:
        klines_sorted = sorted(klines, key=lambda k: k.open_time)
        cur_minute_start = klines_sorted[0].open_time // 60_000 * 60_000
        cur_total = 0.0
        for k in klines_sorted:
            mstart = k.open_time // 60_000 * 60_000
            if mstart != cur_minute_start:
                if cur_total > 0:
                    baseline.append(cur_total)
                cur_minute_start = mstart
                cur_total = 0.0
            cur_total += k.volume
        if cur_total > 0:
            baseline.append(cur_total)

    windows = windows_from_klines(klines)
    results: List[SimResult] = []
    for ws, we, kls in windows:
        r = simulate_window(kls, token_price=token_price,
                            historical_volumes=baseline)
        if r is not None:
            results.append(r)
    return results, summarize(results)


def format_simulation_report(results: List[SimResult],
                             summary: SimSummary,
                             token_price: float) -> str:
    lines: List[str] = []
    lines.append("=" * 86)
    lines.append("BACKTEST SIMULATION — strategy replay over Binance 1s klines")
    lines.append("=" * 86)
    lines.append(f"Token price (synthetic): ${token_price:.2f}")
    lines.append(f"Windows simulated:       {summary.n_windows}")
    lines.append(f"Windows entered:         {summary.n_entries} "
                 f"({summary.entry_rate*100:.1f}%)")
    if summary.n_entries:
        lines.append(f"Wins / Losses:           {summary.wins} / {summary.losses}")
        lines.append(f"Win rate:                {summary.win_rate*100:.1f}%")
        lines.append(f"Break-even win rate:     {token_price*100:.1f}% "
                     "(at this token price)")
        lines.append(f"Edge:                    "
                     f"{(summary.win_rate - token_price)*100:+.1f}pp")
        lines.append(f"Sum PnL / $ staked:      ${summary.sum_pnl_per_dollar:+.3f}")
        lines.append(f"Avg PnL / $ staked:      ${summary.avg_pnl_per_dollar:+.4f}")
    lines.append("")

    if summary.by_score:
        lines.append("By confidence bucket (10-pt bins):")
        for k in sorted(summary.by_score):
            v = summary.by_score[k]
            wr = v["wins"] / v["n"] if v["n"] else 0.0
            lines.append(f"  score>={k:>3}  n={v['n']:>4}  "
                         f"wr={wr*100:>5.1f}%  "
                         f"sum_pnl_per_$={v['pnl']:+.3f}")
        lines.append("")

    if summary.by_delta:
        lines.append("By |delta_pct| at entry (rounded to 0.01):")
        for k in sorted(summary.by_delta, key=float):
            v = summary.by_delta[k]
            wr = v["wins"] / v["n"] if v["n"] else 0.0
            lines.append(f"  |d|={k:>5}  n={v['n']:>4}  "
                         f"wr={wr*100:>5.1f}%  "
                         f"sum_pnl_per_$={v['pnl']:+.3f}")
        lines.append("")

    return "\n".join(lines)
