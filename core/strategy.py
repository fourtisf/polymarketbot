"""
Strategy engine v2 — confidence scoring and trade decision.

Key changes from v1:
- Max entry price 55c (was 95c) for better risk/reward
- Entry window T-30 to T-8 (was T-60 to T-5) — enter LATE for certainty
- Higher min delta 0.08% (was 0.05%) — need CLEAR direction
- Require non-reversing trend
- EV-aware scoring: penalizes expensive tokens heavily
- Stricter scoring weights favoring late + strong signals
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


MAX_ENTRY_PRICE = 0.55
MIN_DELTA_HARD = 0.08
ENTRY_WINDOW_START = 30
ENTRY_WINDOW_END = 8


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

    # Delta scoring — stronger move = higher certainty
    if abs_delta >= 0.20:
        score += 40
        reasons.append(f"Delta {abs_delta:.3f}% = VERY STRONG (+40)")
    elif abs_delta >= 0.15:
        score += 35
        reasons.append(f"Delta {abs_delta:.3f}% = STRONG (+35)")
    elif abs_delta >= 0.10:
        score += 25
        reasons.append(f"Delta {abs_delta:.3f}% = SOLID (+25)")
    elif abs_delta >= 0.08:
        score += 15
        reasons.append(f"Delta {abs_delta:.3f}% = MODERATE (+15)")
    else:
        reasons.append(f"Delta {abs_delta:.3f}% = TOO WEAK (+0)")

    # Time scoring — LATER is BETTER (less time for reversal)
    if seconds_remaining <= 12:
        score += 30
        reasons.append(f"{seconds_remaining}s left = near-certain (+30)")
    elif seconds_remaining <= 18:
        score += 25
        reasons.append(f"{seconds_remaining}s left = very safe (+25)")
    elif seconds_remaining <= 25:
        score += 18
        reasons.append(f"{seconds_remaining}s left = safe (+18)")
    elif seconds_remaining <= 30:
        score += 10
        reasons.append(f"{seconds_remaining}s left = risky (+10)")
    else:
        reasons.append(f"{seconds_remaining}s left = TOO EARLY (+0)")

    # Trend — MUST be consistent for high score
    if delta_trend == "consistent":
        score += 20
        reasons.append("Trend CONSISTENT (+20)")
    elif delta_trend == "choppy":
        score += 5
        reasons.append("Trend CHOPPY (+5)")
    else:
        score -= 10
        reasons.append("Trend REVERSING (-10)")

    # Volume — high volume = momentum more likely to hold
    if binance_volume == "high":
        score += 10
        reasons.append("Volume HIGH (+10)")
    elif binance_volume == "normal":
        score += 5
        reasons.append("Volume NORMAL (+5)")
    else:
        reasons.append("Volume LOW (+0)")

    # Price scoring — CHEAPER is MUCH better
    if token_price <= 0.40:
        score += 15
        reasons.append(f"Price ${token_price:.2f} = excellent edge (+15)")
    elif token_price <= 0.47:
        score += 10
        reasons.append(f"Price ${token_price:.2f} = good edge (+10)")
    elif token_price <= 0.52:
        score += 5
        reasons.append(f"Price ${token_price:.2f} = okay edge (+5)")
    elif token_price <= MAX_ENTRY_PRICE:
        score += 0
        reasons.append(f"Price ${token_price:.2f} = thin edge (+0)")
    else:
        score -= 30
        reasons.append(f"Price ${token_price:.2f} = NO edge (-30)")

    return score, reasons


def _size_multiplier_for_score(score: int) -> float:
    if score >= 90:
        return 2.0
    if score >= 75:
        return 1.5
    if score >= 65:
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

    if abs(ctx.delta_pct) < MIN_DELTA_HARD:
        return TradeDecision(
            action="SKIP",
            reasons=[f"Delta {abs(ctx.delta_pct):.3f}% below floor {MIN_DELTA_HARD:.3f}%"],
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

    if token_price >= MAX_ENTRY_PRICE:
        return TradeDecision(
            action="SKIP",
            reasons=[f"${token_price:.2f} >= ${MAX_ENTRY_PRICE} — edge too thin"],
            reason_log={"skip_reason": "price_too_high", **_ctx_dict(ctx)},
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
                    "target_side": target_side, **_ctx_dict(ctx)},
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
