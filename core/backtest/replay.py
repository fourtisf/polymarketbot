"""Replay validator for data/trades.json.

The live bot logs every entry and every settled outcome. We pair them by
window_slug, bucket by the dimensions the strategy uses for scoring, then
report win-rate + realized EV per bucket so you can see which tiers are
actually profitable and which are leaking money.

No external network — pure post-hoc analysis.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List, Optional, Tuple

import config


# ─────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────

@dataclass
class PairedTrade:
    """One entry record paired with its settled outcome."""
    window_slug: str
    ts: int
    side: str                  # "UP" | "DOWN"
    entry_price: float
    shares: float
    cost: float
    confidence: int
    delta_pct: float
    delta_trend: str
    binance_volume: str
    seconds_remaining: int
    token_price: float         # entry-side ask at decision time
    outcome: str               # "win" | "loss" | "phantom"
    pnl: float
    close_price: Optional[float] = None
    resolution: Optional[str] = None  # "UP" | "DOWN"

    @property
    def is_settled(self) -> bool:
        return self.outcome in ("win", "loss")

    @property
    def is_win(self) -> bool:
        return self.outcome == "win"


@dataclass
class BucketStats:
    """Aggregated stats for one slice of paired trades."""
    label: str
    n: int = 0
    wins: int = 0
    losses: int = 0
    sum_pnl: float = 0.0
    sum_cost: float = 0.0
    avg_entry_price: float = 0.0
    avg_confidence: float = 0.0
    pnls: List[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        s = self.wins + self.losses
        return self.wins / s if s else 0.0

    @property
    def mean_pnl(self) -> float:
        return mean(self.pnls) if self.pnls else 0.0

    @property
    def std_pnl(self) -> float:
        return pstdev(self.pnls) if len(self.pnls) > 1 else 0.0

    @property
    def roi(self) -> float:
        return self.sum_pnl / self.sum_cost if self.sum_cost > 0 else 0.0

    @property
    def breakeven_win_rate(self) -> float:
        """Win rate needed at the average entry price to break even."""
        # Payoff per $1 staked = (1 - entry) on win, -entry on loss
        # 0 = wr * (1-p) - (1-wr) * p  =>  wr = p
        return self.avg_entry_price

    @property
    def edge(self) -> float:
        """Win rate - breakeven. Positive = EV+ in this bucket."""
        return self.win_rate - self.breakeven_win_rate

    def add(self, t: PairedTrade) -> None:
        self.n += 1
        self.sum_cost += t.cost
        self.pnls.append(t.pnl)
        self.sum_pnl += t.pnl
        if t.is_win:
            self.wins += 1
        elif t.outcome == "loss":
            self.losses += 1
        # Running averages
        prev_n = self.n - 1
        self.avg_entry_price = (self.avg_entry_price * prev_n + t.entry_price) / self.n
        self.avg_confidence = (self.avg_confidence * prev_n + t.confidence) / self.n


# ─────────────────────────────────────────────────────────────
# Loader
# ─────────────────────────────────────────────────────────────

def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _extract_ctx(record: Dict[str, Any]) -> Dict[str, Any]:
    """Pull context fields from either reason_log or top-level."""
    rl = record.get("reason_log") or {}
    return {
        "delta_pct": _safe_float(rl.get("delta_pct", record.get("delta_pct", 0.0))),
        "delta_trend": rl.get("delta_trend") or record.get("delta_trend") or "unknown",
        "binance_volume": rl.get("binance_volume") or record.get("binance_volume") or "unknown",
        "seconds_remaining": _safe_int(rl.get("seconds_remaining", record.get("seconds_remaining", 0))),
        "token_price": _safe_float(
            rl.get("token_price")
            or rl.get("token_up_price" if record.get("side") == "UP" else "token_down_price")
            or record.get("entry_price", 0.0)
        ),
    }


def load_paired_trades(path: Optional[Path] = None) -> List[PairedTrade]:
    """Load trades.json and pair entry+settled records by window_slug.

    Records that don't match an entry (orphan settled) or never settled
    (entry without outcome) are silently dropped — they can't be evaluated.
    Phantom trades are dropped as well; they did not actually fill.
    """
    p = Path(path or config.TRADES_FILE)
    if not p.exists():
        return []
    try:
        records = json.loads(p.read_text() or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(records, list):
        return []

    entries: Dict[str, Dict[str, Any]] = {}
    settled: Dict[str, Dict[str, Any]] = {}

    for r in records:
        if not isinstance(r, dict):
            continue
        slug = r.get("window_slug")
        if not slug:
            continue
        phase = r.get("phase")
        action = r.get("action")
        # Skip records (no phase=entry/settled, or action=SKIP) are not paired.
        if action == "SKIP":
            continue
        if phase == "entry":
            entries[slug] = r
        elif phase == "settled":
            settled[slug] = r

    paired: List[PairedTrade] = []
    for slug, e in entries.items():
        s = settled.get(slug)
        if s is None:
            continue
        if s.get("phantom") or s.get("outcome") == "phantom":
            continue
        ctx = _extract_ctx(e)
        paired.append(PairedTrade(
            window_slug=slug,
            ts=_safe_int(e.get("ts")),
            side=e.get("side") or ("UP" if e.get("action") == "BUY_UP" else "DOWN"),
            entry_price=_safe_float(e.get("entry_price")),
            shares=_safe_float(e.get("shares")),
            cost=_safe_float(e.get("cost")),
            confidence=_safe_int(e.get("confidence")),
            delta_pct=ctx["delta_pct"],
            delta_trend=ctx["delta_trend"],
            binance_volume=ctx["binance_volume"],
            seconds_remaining=ctx["seconds_remaining"],
            token_price=ctx["token_price"] or _safe_float(e.get("entry_price")),
            outcome=s.get("outcome") or "unknown",
            pnl=_safe_float(s.get("pnl")),
            close_price=_safe_float(s.get("close_price")) or None,
            resolution=s.get("resolution"),
        ))
    paired.sort(key=lambda t: t.ts)
    return paired


# ─────────────────────────────────────────────────────────────
# Bucketing
# ─────────────────────────────────────────────────────────────

# Edges chosen to mirror the tiers in core/strategy.py so that the report
# directly tells you whether each tier's assumed win-rate holds in reality.

DELTA_EDGES = [0.0, 0.08, 0.12, 0.18, 0.25, math.inf]
DELTA_LABELS = ["<0.08", "0.08-0.12", "0.12-0.18", "0.18-0.25", ">=0.25"]

SECONDS_EDGES = [0, 11, 16, 23, 31, math.inf]
SECONDS_LABELS = ["<=10", "11-15", "16-22", "23-30", ">30"]

PRICE_EDGES = [0.0, 0.42, 0.50, 0.55, 0.62, 1.01]
PRICE_LABELS = ["<=0.42", "0.43-0.50", "0.51-0.55", "0.56-0.62", ">0.62"]

SCORE_EDGES = [0, 65, 78, 90, 1000]
SCORE_LABELS = ["<65", "65-77", "78-89", ">=90"]


def _bucket(value: float, edges: List[float], labels: List[str]) -> str:
    for i in range(len(labels)):
        if value < edges[i + 1]:
            return labels[i]
    return labels[-1]


def _trades_by(key_fn, trades: Iterable[PairedTrade]) -> Dict[str, BucketStats]:
    out: Dict[str, BucketStats] = {}
    for t in trades:
        key = key_fn(t)
        bs = out.get(key)
        if bs is None:
            bs = BucketStats(label=key)
            out[key] = bs
        bs.add(t)
    return out


def bucket_metrics(trades: List[PairedTrade]) -> Dict[str, Dict[str, BucketStats]]:
    """Return a dict of {dimension_name: {bucket_label: BucketStats}}."""
    return {
        "delta_pct": _trades_by(
            lambda t: _bucket(abs(t.delta_pct), DELTA_EDGES, DELTA_LABELS),
            trades,
        ),
        "seconds_remaining": _trades_by(
            lambda t: _bucket(t.seconds_remaining, SECONDS_EDGES, SECONDS_LABELS),
            trades,
        ),
        "entry_price": _trades_by(
            lambda t: _bucket(t.entry_price, PRICE_EDGES, PRICE_LABELS),
            trades,
        ),
        "confidence": _trades_by(
            lambda t: _bucket(t.confidence, SCORE_EDGES, SCORE_LABELS),
            trades,
        ),
        "trend": _trades_by(lambda t: t.delta_trend, trades),
        "volume": _trades_by(lambda t: t.binance_volume, trades),
        "side": _trades_by(lambda t: t.side, trades),
    }


# ─────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────

def _row(stats: BucketStats, label_width: int) -> str:
    edge_pct = stats.edge * 100
    edge_marker = "+" if edge_pct >= 0 else ""
    return (
        f"{stats.label:<{label_width}}  "
        f"n={stats.n:>4}  "
        f"W/L={stats.wins:>3}/{stats.losses:>3}  "
        f"wr={stats.win_rate*100:>5.1f}%  "
        f"avg_px={stats.avg_entry_price:>5.3f}  "
        f"BE={stats.breakeven_win_rate*100:>5.1f}%  "
        f"edge={edge_marker}{edge_pct:>5.1f}pp  "
        f"PnL=${stats.sum_pnl:>+8.2f}  "
        f"ROI={stats.roi*100:>+6.2f}%"
    )


def _section(name: str, stats_dict: Dict[str, BucketStats],
             ordered_labels: Optional[List[str]] = None) -> List[str]:
    if not stats_dict:
        return [f"## {name}", "  (no data)", ""]
    keys = ordered_labels if ordered_labels else sorted(stats_dict.keys())
    width = max((len(k) for k in keys if k in stats_dict), default=10)
    out = [f"## {name}"]
    for k in keys:
        if k not in stats_dict:
            continue
        out.append("  " + _row(stats_dict[k], width))
    out.append("")
    return out


def format_report(trades: List[PairedTrade]) -> str:
    if not trades:
        return "No paired (entry + settled) trades found in trades.json."

    overall = BucketStats(label="ALL")
    for t in trades:
        overall.add(t)

    metrics = bucket_metrics(trades)
    lines: List[str] = []
    lines.append("=" * 86)
    lines.append("BACKTEST REPLAY — strategy validation against live trades.json")
    lines.append("=" * 86)
    lines.append("")
    lines.append("## Overall")
    lines.append("  " + _row(overall, label_width=12))
    lines.append("")
    lines.append("Edge column: realized win rate minus break-even (= avg entry price).")
    lines.append("Positive edge = bucket is EV+, negative = bucket leaks money.")
    lines.append("")

    lines += _section("By |delta_pct|", metrics["delta_pct"], DELTA_LABELS)
    lines += _section("By seconds_remaining", metrics["seconds_remaining"], SECONDS_LABELS)
    lines += _section("By entry_price", metrics["entry_price"], PRICE_LABELS)
    lines += _section("By confidence", metrics["confidence"], SCORE_LABELS)
    lines += _section("By trend", metrics["trend"], ["consistent", "choppy", "reversing"])
    lines += _section("By volume", metrics["volume"], ["high", "normal", "low"])
    lines += _section("By side", metrics["side"], ["UP", "DOWN"])

    # Recommendations: any bucket with n>=10 and edge < -2pp is bleeding.
    bleeding: List[Tuple[str, str, BucketStats]] = []
    glory: List[Tuple[str, str, BucketStats]] = []
    for dim, buckets in metrics.items():
        for label, bs in buckets.items():
            if bs.n < 10:
                continue
            if bs.edge * 100 <= -2.0:
                bleeding.append((dim, label, bs))
            elif bs.edge * 100 >= 4.0:
                glory.append((dim, label, bs))

    if bleeding or glory:
        lines.append("## Tuning suggestions  (n>=10 buckets)")
        if bleeding:
            lines.append("  Bleeding (edge <= -2pp) — tighten or exclude:")
            for dim, label, bs in sorted(bleeding, key=lambda x: x[2].edge):
                lines.append(f"    - {dim}={label}: edge {bs.edge*100:+.1f}pp "
                             f"({bs.wins}W/{bs.losses}L, ${bs.sum_pnl:+.2f})")
        if glory:
            lines.append("  Strong (edge >= +4pp) — consider upsizing:")
            for dim, label, bs in sorted(glory, key=lambda x: -x[2].edge):
                lines.append(f"    - {dim}={label}: edge {bs.edge*100:+.1f}pp "
                             f"({bs.wins}W/{bs.losses}L, ${bs.sum_pnl:+.2f})")
        lines.append("")

    lines.append("Done. Trades analyzed: %d" % len(trades))
    return "\n".join(lines)
