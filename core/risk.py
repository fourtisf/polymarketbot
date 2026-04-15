"""
Risk manager — hard rules the strategy cannot override.

Tracks session + daily PnL, consecutive losses, cooldowns, and
provides get_trade_size() which the executor calls before every entry.
can_trade() is the single source of truth for "is the bot allowed to
place an order right now".
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Tuple

import config

log = logging.getLogger("risk")


@dataclass
class SessionState:
    session_pnl: float = 0.0
    daily_pnl: float = 0.0
    daily_date: str = ""
    consecutive_losses: int = 0
    trades_today: int = 0
    cooldown_until: float = 0.0
    cooldown_reason: str = ""

    def today_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def roll_day_if_needed(self) -> None:
        key = self.today_key()
        if self.daily_date != key:
            self.daily_date = key
            self.daily_pnl = 0.0
            self.trades_today = 0


class RiskManager:
    def __init__(self):
        self.state = SessionState()
        self.state.roll_day_if_needed()

    # ── Gate: can we place an order right now? ──────────────
    def can_trade(self) -> Tuple[bool, str]:
        self.state.roll_day_if_needed()

        if config.RUNTIME.paused:
            return False, "bot is paused"
        if time.time() < self.state.cooldown_until:
            remaining = int(self.state.cooldown_until - time.time())
            return False, f"cooldown: {self.state.cooldown_reason} ({remaining}s left)"
        if self.state.session_pnl <= -config.RUNTIME.max_session_loss:
            return False, f"session loss limit hit (${self.state.session_pnl:.2f})"
        if self.state.daily_pnl <= -config.MAX_DAILY_LOSS_USD:
            return False, f"daily loss limit hit (${self.state.daily_pnl:.2f})"
        if self.state.trades_today >= config.MAX_DAILY_TRADES:
            return False, f"daily trade cap hit ({self.state.trades_today})"
        if self.state.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            return False, f"{self.state.consecutive_losses} consecutive losses"
        return True, "ok"

    # ── Sizing ──────────────────────────────────────────────
    def get_trade_size(self, confidence_score: int) -> float:
        if confidence_score >= 90:
            base = config.MAX_TRADE_SIZE_USD
        elif confidence_score >= 75:
            base = config.RUNTIME.base_size_usd * 2
        elif confidence_score >= config.RUNTIME.min_confidence:
            base = config.RUNTIME.base_size_usd
        else:
            return 0.0
        if self.state.session_pnl < -10:
            base *= 0.5
        if self.state.session_pnl < -15:
            base *= 0.5
        size = max(config.MIN_TRADE_SIZE_USD, min(base, config.MAX_TRADE_SIZE_USD))
        return round(size, 2)

    # ── Record trade outcome ────────────────────────────────
    def record_trade(self, pnl: float) -> None:
        self.state.roll_day_if_needed()
        self.state.session_pnl += pnl
        self.state.daily_pnl += pnl
        self.state.trades_today += 1
        if pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        # Cooldowns
        if self.state.consecutive_losses >= 3:
            self.state.cooldown_until = time.time() + config.COOLDOWN_AFTER_LOSS_STREAK_SEC
            self.state.cooldown_reason = f"{self.state.consecutive_losses} loss streak"
            log.warning("cooldown: %s", self.state.cooldown_reason)
        elif pnl <= -10:
            self.state.cooldown_until = time.time() + config.COOLDOWN_AFTER_BIG_LOSS_SEC
            self.state.cooldown_reason = f"big loss ${pnl:.2f}"
            log.warning("cooldown: %s", self.state.cooldown_reason)

    def record_skip(self) -> None:
        self.state.roll_day_if_needed()

    def reset_session(self) -> None:
        self.state.session_pnl = 0.0
        self.state.consecutive_losses = 0
        self.state.cooldown_until = 0.0
        self.state.cooldown_reason = ""

    def snapshot(self) -> dict:
        return {
            "session_pnl": round(self.state.session_pnl, 2),
            "daily_pnl": round(self.state.daily_pnl, 2),
            "trades_today": self.state.trades_today,
            "consecutive_losses": self.state.consecutive_losses,
            "cooldown_remaining": max(0, int(self.state.cooldown_until - time.time())),
            "cooldown_reason": self.state.cooldown_reason,
        }
