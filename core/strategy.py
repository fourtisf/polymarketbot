"""
Strategy engine — confidence scoring and trade decision.

This module is PURE logic. It does not read feeds directly; it accepts
a TradeContext from the caller and returns a TradeDecision. That makes
it trivially unit-testable and dry-runnable.
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
    size_multiplier: float = 1.0  # caller applies this to base size
    # Full reason blob — logged verbatim
    reason_log: dict = field(default_factory=dict)


def calculate_confidence(
    delta_pct: float,
    seconds_remaining: int,
    delta_trend: str,
    binance_volume: str,
    token_price: float,
) -> (int, List[str]):
    """See README / system prompt for the full scoring rubric."""
    score = 0
    reasons: List[str] = []

    abs_delta = abs(delta_pct)
    if abs_delta >= 0.15:
        score += 35
        reasons.append(f"Delta {abs_delta:.3f}% = DECISIVE move (+35)")
    elif abs_delta >= 0.10:
        score += 28
        reasons.append(f"Delta {abs_delta:.3f}% = STRONG move (+28)")
    elif abs_delta >= 0.07:
        score += 20
        reasons.append(f"Delta {abs_delta:.3f}% = MODERATE move (+20)")
    elif abs_delta >= 0.05:
        score += 12
        reasons.append(f"Delta {abs_delta:.3f}% = WEAK move (+12)")
    else:
        reasons.append(f"Delta {abs_delta:.3f}% = TOO SMALL (+0)")

    if seconds_remaining <= 15:
        score += 25
        reasons.append(f"{seconds_remaining}s left = very unlikely to reverse (+25)")
    elif seconds_remaining <= 30:
        score += 20
        reasons.append(f"{seconds_remaining}s left = unlikely to reverse (+20)")
    elif seconds_remaining <= 45:
        score += 12
        reasons.append(f"{seconds_remaining}s left = could reverse (+12)")
    elif seconds_remaining <= 60:
        score += 5
        reasons.append(f"{seconds_remaining}s left = risky (+5)")
    else:
        reasons.append(f"{seconds_remaining}s left = TOO EARLY (+0)")

    if delta_trend == "consistent":
        score += 20
        reasons.append("Delta trend CONSISTENT (+20)")
    elif delta_trend == "choppy":
        score += 8
        reasons.append("Delta trend CHOPPY (+8)")
    else:
        reasons.append("Delta trend REVERSING (+0)")

    if binance_volume == "high":
        score += 10
        reasons.append("Binance volume HIGH (+10)")
    elif binance_volume == "normal":
        score += 5
        reasons.append("Binance volume NORMAL (+5)")
    else:
        reasons.append("Binance volume LOW (+0)")

    if token_price <= 0.45:
        score += 15
        reasons.append(f"Token @ ${token_price:.2f} = great value (+15)")
    elif token_price <= 0.52:
        score += 10
        reasons.append(f"Token @ ${token_price:.2f} = good value (+10)")
    elif token_price <= 0.58:
        score += 5
        reasons.append(f"Token @ ${token_price:.2f} = okay value (+5)")
    elif token_price <= 0.65:
        score += 0
        reasons.append(f"Token @ ${token_price:.2f} = marginal (+0)")
    else:
        score -= 20
        reasons.append(f"Token @ ${token_price:.2f} = too expensive (-20)")

    return score, reasons


def _size_multiplier_for_score(score: int) -> float:
    if score >= 90:
        return 2.5
    if score >= 75:
        return 2.0
    if score >= 60:
        return 1.0
    return 0.0


def decide(ctx: TradeContext) -> TradeDecision:
    """
    Given a TradeContext, return a TradeDecision.
    Every SKIP path includes an explicit reason in reason_log["skip_reason"].
    """
    # ── Hard gates ────────────────────────────────────────────
    if ctx.seconds_remaining > config.ENTRY_WINDOW_START_SEC:
        return TradeDecision(
            action="SKIP",
            reasons=[f"Too early: {ctx.seconds_remaining}s left, window starts at T-{config.ENTRY_WINDOW_START_SEC}s"],
            reason_log={"skip_reason": "too_early", **_ctx_dict(ctx)},
        )
    if ctx.seconds_remaining < config.ENTRY_WINDOW_END_SEC:
        return TradeDecision(
            action="SKIP",
            reasons=[f"Too late: {ctx.seconds_remaining}s left, window closes at T-{config.ENTRY_WINDOW_END_SEC}s"],
            reason_log={"skip_reason": "too_late", **_ctx_dict(ctx)},
        )
    if abs(ctx.delta_pct) < config.MIN_DELTA_PCT:
        return TradeDecision(
            action="SKIP",
            reasons=[f"Delta {abs(ctx.delta_pct):.3f}% below floor {config.MIN_DELTA_PCT:.3f}%"],
            reason_log={"skip_reason": "delta_below_floor", **_ctx_dict(ctx)},
        )
    if ctx.delta_trend == "reversing":
        return TradeDecision(
            action="SKIP",
            reasons=["Delta trend is REVERSING"],
            reason_log={"skip_reason": "trend_reversing", **_ctx_dict(ctx)},
        )

    # Higher delta threshold late in the window (safer entries)
    if ctx.seconds_remaining <= 10 and abs(ctx.delta_pct) < 0.07:
        return TradeDecision(
            action="SKIP",
            reasons=[f"Late entry but delta only {abs(ctx.delta_pct):.3f}% (<0.07%)"],
            reason_log={"skip_reason": "late_delta_too_small", **_ctx_dict(ctx)},
        )

    # ── Target selection ─────────────────────────────────────
    if ctx.delta_pct > 0:
        target_side = "UP"
        token_price = ctx.token_up_price
    else:
        target_side = "DOWN"
        token_price = ctx.token_down_price

    if token_price <= 0 or token_price >= 0.99:
        return TradeDecision(
            action="SKIP",
            reasons=[f"Target {target_side} token unavailable or fully priced: ${token_price:.2f}"],
            reason_log={"skip_reason": "token_unavailable", **_ctx_dict(ctx)},
        )

    # Profit margin floor — need at least 35c of edge for good risk/reward
    if token_price >= 0.65:
        return TradeDecision(
            action="SKIP",
            reasons=[f"Token ${token_price:.2f} too expensive — need price below 65c"],
            reason_log={"skip_reason": "margin_too_thin", **_ctx_dict(ctx)},
        )

    score, reasons = calculate_confidence(
        delta_pct=ctx.delta_pct,
        seconds_remaining=ctx.seconds_remaining,
        delta_trend=ctx.delta_trend,
        binance_volume=ctx.binance_volume,
        token_price=token_price,
    )

    mult = _size_multiplier_for_score(score)
    if score < config.RUNTIME.min_confidence or mult == 0:
        return TradeDecision(
            action="SKIP",
            confidence=score,
            reasons=reasons + [f"Score {score} below min {config.RUNTIME.min_confidence}"],
            reason_log={
                "skip_reason": "score_below_min",
                "score": score,
                "factors": reasons,
                **_ctx_dict(ctx),
            },
        )

    action = "BUY_UP" if target_side == "UP" else "BUY_DOWN"
    return TradeDecision(
        action=action,
        token_id="",  # filled in by caller
        token_price=token_price,
        confidence=score,
        reasons=reasons,
        size_multiplier=mult,
        reason_log={
            "score": score,
            "factors": reasons,
            "target_side": target_side,
            **_ctx_dict(ctx),
        },
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
