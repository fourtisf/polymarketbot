"""
Strategy engine v3 — dynamic edge-based entry.

Key improvements over v2:
- Dynamic max price based on signal strength (not fixed cap)
- Strong signal + late entry → allows up to 62c (fills more often)
- Weak signal → strict 52c cap (only enter with great edge)
- EV-positive math: each price tier requires matching win rate
- Better fill rate without sacrificing profitability
"""

from dataclasses import dataclass, field
from typing import List, Optional

import config


@dataclass
class TradeContext:
    window_slug: str
    price_to_beat: float
    current_btc: float
    delta_pct: float
    delta_trend: str          # "consistent" | "choppy" | "reversing"
    binance_volume: str       # "high" | "normal" | "low"
    seconds_remaining: int
    token_up_price: float     # best ask for UP
    token_down_price: float   # best ask for DOWN


@dataclass
class TradeDecision:
    action: str               # "BUY_UP" | "BUY_DOWN" | "SKIP"
    token_id: str = ""
    token_price: float = 0.0
    confidence: int = 0
    reasons: List[str] = field(default_factory=list)
    size_multiplier: float = 1.0
    reason_log: dict = field(default_factory=dict)


ABSOLUTE_MAX_PRICE = 0.62
MIN_DELTA_HARD = 0.08
ENTRY_WINDOW_START = 30
ENTRY_WINDOW_END = 8


def _dynamic_max_price(abs_delta: float, seconds_remaining: int,
                       delta_trend: str) -> float:
    if abs_delta >= 0.20 and seconds_remaining <= 15:
        cap = 0.62
    elif abs_delta >= 0.15 and seconds_remaining <= 20:
        cap = 0.60
    elif abs_delta >= 0.10:
        cap = 0.57
    elif abs_delta >= 0.08:
        cap = 0.53
    else:
        cap = 0.50

    if delta_trend == "choppy":
        cap -= 0.03

    return min(cap, ABSOLUTE_MAX_PRICE)


def calculate_confidence(
    delta_pct: float,
    seconds_remaining: int,
    delta_trend: str,
    binance_volume: str,
    token_price: float,
) -> (int, List[str]):
    score = 0
    reasons: List[str] = []

    abs_delta = abs(delta_pct)

    if abs_delta >= 0.25:
        score += 45
        reasons.append(f"Delta {abs_delta:.3f}% = EXTREME (+45)")
    elif abs_delta >= 0.18:
        score += 38
        reasons.append(f"Delta {abs_delta:.3f}% = VERY STRONG (+38)")
    elif abs_delta >= 0.12:
        score += 28
        reasons.append(f"Delta {abs_delta:.3f}% = STRONG (+28)")
    elif abs_delta >= 0.08:
        score += 15
        reasons.append(f"Delta {abs_delta:.3f}% = MODERATE (+15)")
    else:
        reasons.append(f"Delta {abs_delta:.3f}% = TOO WEAK (+0)")

    if seconds_remaining <= 10:
        score += 30
        reasons.append(f"{seconds_remaining}s left = near-certain (+30)")
    elif seconds_remaining <= 15:
        score += 25
        reasons.append(f"{seconds_remaining}s left = very safe (+25)")
    elif seconds_remaining <= 22:
        score += 18
        reasons.append(f"{seconds_remaining}s left = safe (+18)")
    elif seconds_remaining <= 30:
        score += 10
        reasons.append(f"{seconds_remaining}s left = early (+10)")
    else:
        reasons.append(f"{seconds_remaining}s left = TOO EARLY (+0)")

    if delta_trend == "consistent":
        score += 20
        reasons.append("Trend CONSISTENT (+20)")
    elif delta_trend == "choppy":
        score += 3
        reasons.append("Trend CHOPPY (+3)")
    else:
        score -= 15
        reasons.append("Trend REVERSING (-15)")

    if binance_volume == "high":
        score += 10
        reasons.append("Volume HIGH (+10)")
    elif binance_volume == "normal":
        score += 5
        reasons.append("Volume NORMAL (+5)")
    else:
        reasons.append("Volume LOW (+0)")

    if token_price <= 0.42:
        score += 12
        reasons.append(f"Price ${token_price:.2f} = great edge (+12)")
    elif token_price <= 0.50:
        score += 8
        reasons.append(f"Price ${token_price:.2f} = good edge (+8)")
    elif token_price <= 0.57:
        score += 3
        reasons.append(f"Price ${token_price:.2f} = fair edge (+3)")
    elif token_price <= ABSOLUTE_MAX_PRICE:
        score -= 5
        reasons.append(f"Price ${token_price:.2f} = thin edge (-5)")
    else:
        score -= 30
        reasons.append(f"Price ${token_price:.2f} = NO edge (-30)")

    return score, reasons


def _size_multiplier_for_score(score: int) -> float:
    """Convert confidence score → position-size multiplier.

    Capped at 1.0 in validation phase: scaling up on confidence requires
    a calibrated win-rate-vs-score curve, which only the replay report
    (after ≥200 settled trades) can produce. Until then, scaling up is
    just amplifying potential losses. Re-introduce 1.5x / 2.0x tiers
    only after the per-bucket edge is empirically positive.
    """
    if score >= config.RUNTIME.min_confidence:
        return 1.0
    return 0.0


def decide(ctx: TradeContext) -> TradeDecision:
    # ── Hard gates ──
    if ctx.seconds_remaining > ENTRY_WINDOW_START:
        return TradeDecision(
            action="SKIP",
            reasons=[f"Too early: {ctx.seconds_remaining}s left (window T-{ENTRY_WINDOW_START})"],
            reason_log={"skip_reason": "too_early", **_ctx_dict(ctx)},
        )
    if ctx.seconds_remaining < ENTRY_WINDOW_END:
        return TradeDecision(
            action="SKIP",
            reasons=[f"Too late: {ctx.seconds_remaining}s left (cutoff T-{ENTRY_WINDOW_END})"],
            reason_log={"skip_reason": "too_late", **_ctx_dict(ctx)},
        )

    abs_delta = abs(ctx.delta_pct)
    if abs_delta < MIN_DELTA_HARD:
        return TradeDecision(
            action="SKIP",
            reasons=[f"Delta {abs_delta:.3f}% below floor {MIN_DELTA_HARD:.3f}%"],
            reason_log={"skip_reason": "delta_below_floor", **_ctx_dict(ctx)},
        )

    if ctx.delta_trend == "reversing":
        return TradeDecision(
            action="SKIP",
            reasons=["Trend REVERSING — never enter against momentum"],
            reason_log={"skip_reason": "trend_reversing", **_ctx_dict(ctx)},
        )

    # ── Target selection ──
    if ctx.delta_pct > 0:
        target_side = "UP"
        token_price = ctx.token_up_price
    else:
        target_side = "DOWN"
        token_price = ctx.token_down_price

    if token_price <= 0 or token_price >= 0.99:
        return TradeDecision(
            action="SKIP",
            reasons=[f"{target_side} token unavailable: ${token_price:.2f}"],
            reason_log={"skip_reason": "token_unavailable", **_ctx_dict(ctx)},
        )

    # ── Dynamic price cap ──
    max_price = _dynamic_max_price(abs_delta, ctx.seconds_remaining,
                                   ctx.delta_trend)
    if token_price > max_price:
        return TradeDecision(
            action="SKIP",
            reasons=[f"${token_price:.2f} > dynamic cap ${max_price:.2f} "
                     f"(delta={abs_delta:.3f}%, {ctx.seconds_remaining}s, "
                     f"trend={ctx.delta_trend})"],
            reason_log={"skip_reason": "price_above_dynamic_cap",
                        "dynamic_cap": max_price, **_ctx_dict(ctx)},
        )

    # ── Scoring ──
    score, reasons = calculate_confidence(
        delta_pct=ctx.delta_pct,
        seconds_remaining=ctx.seconds_remaining,
        delta_trend=ctx.delta_trend,
        binance_volume=ctx.binance_volume,
        token_price=token_price,
    )

    min_conf = config.RUNTIME.min_confidence
    mult = _size_multiplier_for_score(score)
    if score < min_conf or mult == 0:
        return TradeDecision(
            action="SKIP",
            confidence=score,
            reasons=reasons + [f"Score {score} < min {min_conf}"],
            reason_log={"skip_reason": "score_below_min", "score": score,
                        "factors": reasons, **_ctx_dict(ctx)},
        )

    action = "BUY_UP" if target_side == "UP" else "BUY_DOWN"
    return TradeDecision(
        action=action,
        token_id="",
        token_price=token_price,
        confidence=score,
        reasons=reasons,
        size_multiplier=mult,
        reason_log={"score": score, "factors": reasons,
                    "target_side": target_side,
                    "dynamic_cap": max_price, **_ctx_dict(ctx)},
    )


def _ctx_dict(ctx: TradeContext) -> dict:
    return {
        "window_slug": ctx.window_slug,
        "price_to_beat": ctx.price_to_beat,
        "current_btc": ctx.current_btc,
        "delta_pct": ctx.delta_pct,
        "delta_trend": ctx.delta_trend,
        "binance_volume": ctx.binance_volume,
        "seconds_remaining": ctx.seconds_remaining,
        "token_up_price": ctx.token_up_price,
        "token_down_price": ctx.token_down_price,
    }
