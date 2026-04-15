"""
Polymarket BTC 5-Minute Up/Down Trading Bot — main entry point.

Wires together:
  - Binance BTC price feed (WebSocket)
  - Polymarket token feed (WebSocket)
  - Chainlink price-to-beat feed (RTDS WebSocket)
  - Strategy engine (confidence scoring + decision)
  - Risk manager (sizing + limits + cooldowns)
  - Executor (limit orders via CLOB)
  - PnL tracker (session/daily/alltime/equity)
  - Telegram bot (notifications + commands)
  - Web dashboard (aiohttp + SSE)

Main loop behavior:
  - At each 5-minute boundary, resolve new window metadata (token IDs)
  - Capture opening price-to-beat (Chainlink preferred, Binance fallback)
  - Subscribe Polymarket feed to the new token IDs
  - Monitor from T-60s → T-5s, trying to enter once per window
  - On window close, check resolution, record PnL, notify

Run it:
  python3 bot.py --dry-run    # no real orders
  python3 bot.py              # live trading
"""

import argparse
import asyncio
import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import config
from core import market, strategy
from core.execution import Executor
from core.risk import RiskManager
from services.binance_feed import BinanceFeed
from services.chainlink_feed import ChainlinkFeed
from services.polymarket_feed import PolymarketFeed
from utils.logger import TradeLogger, setup_logging
from utils.pnl_tracker import PnLTracker
from utils.telegram import CommandBot, Notifier
from dashboard.server import DashboardServer

log = logging.getLogger("bot")


# ─────────────────────────────────────────────────────────────
# Bot state (shared with the web dashboard)
# ─────────────────────────────────────────────────────────────

@dataclass
class BotState:
    """Live snapshot of the bot — consumed by dashboard/api/window."""
    window: Optional[market.Window] = None
    current_btc: Optional[float] = None
    delta_pct: Optional[float] = None
    token_up_price: Optional[float] = None
    token_down_price: Optional[float] = None
    signal: str = "idle"
    last_decision: Optional[dict] = None
    entered_this_window: bool = False
    entry_record: Optional[dict] = None

    def snapshot(self) -> Dict[str, Any]:
        w = self.window
        return {
            "slug": w.slug if w else None,
            "window_start": w.window_start if w else None,
            "window_end": w.window_end if w else None,
            "seconds_remaining": w.seconds_remaining if w else None,
            "price_to_beat": w.price_to_beat if w else None,
            "price_source": w.price_source if w else None,
            "current_btc": self.current_btc,
            "delta_pct": self.delta_pct,
            "token_up_price": self.token_up_price,
            "token_down_price": self.token_down_price,
            "signal": self.signal,
            "last_decision": self.last_decision,
            "entered": self.entered_this_window,
        }


# ─────────────────────────────────────────────────────────────
# Main bot
# ─────────────────────────────────────────────────────────────

class TradingBot:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        config.RUNTIME.dry_run = dry_run

        self.binance = BinanceFeed()
        self.polymarket = PolymarketFeed()
        self.chainlink = ChainlinkFeed()
        self.executor = Executor(dry_run=dry_run)
        self.risk = RiskManager()
        self.pnl = PnLTracker()
        self.trade_log = TradeLogger()
        self.notifier = Notifier()
        self.state = BotState()

        self.dashboard = DashboardServer(self.pnl, self.risk, self.state)
        self.cmdbot = CommandBot(self.pnl, self.risk, self.executor, self.notifier, trading_bot=self)

        self._tasks: list = []
        self._stopping = False

    # ── Runtime control hooks (used by Telegram CommandBot) ─
    def set_dry_run(self, dry_run: bool) -> None:
        """Flip dry-run mode at runtime and force executor re-init."""
        self.dry_run = dry_run
        config.RUNTIME.dry_run = dry_run
        self.executor.dry_run = dry_run
        self.executor._client = None

    def reload_wallet(self) -> None:
        """Called after /setwallet updates credentials in-memory."""
        self.executor._client = None

    # ── Lifecycle ───────────────────────────────────────
    async def start(self) -> None:
        setup_logging("INFO")
        log.info("starting bot (dry_run=%s)", self.dry_run)

        self._tasks.append(asyncio.create_task(self.binance.run(), name="binance"))
        self._tasks.append(asyncio.create_task(self.polymarket.run(), name="polymarket"))
        self._tasks.append(asyncio.create_task(self.chainlink.run(), name="chainlink"))
        self._tasks.append(asyncio.create_task(self.cmdbot.run(), name="cmdbot"))

        await self.dashboard.start()

        # Main trading loop
        self._tasks.append(asyncio.create_task(self._trading_loop(), name="trading"))
        self._tasks.append(asyncio.create_task(self._live_state_loop(), name="live_state"))

        mode = "DRY-RUN" if self.dry_run else "LIVE"
        await self.notifier.send_text(
            f"🚀 <b>Bot started</b> ({mode})\n"
            f"Dashboard: http://&lt;vps-ip&gt;:{config.DASHBOARD_PORT}?token=..."
        )
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        log.info("shutting down...")
        await self.notifier.send_text("🛑 Bot shutting down — cancelling open orders")
        try:
            await self.executor.cancel_all()
        except Exception:
            pass
        await self.binance.stop()
        await self.polymarket.stop()
        await self.chainlink.stop()
        await self.cmdbot.stop()
        for t in self._tasks:
            t.cancel()
        await self.dashboard.stop()

    # ── Live state publisher ────────────────────────────
    async def _live_state_loop(self) -> None:
        """Update BotState and broadcast to dashboard SSE every second."""
        while not self._stopping:
            try:
                if self.state.window is not None:
                    ptb = self.state.window.price_to_beat
                    if ptb:
                        btc = self.binance.get_price()
                        if btc:
                            self.state.current_btc = btc
                            self.state.delta_pct = self.binance.get_delta_pct(ptb)
                    up = self.polymarket.get_best_ask(self.state.window.token_up_id)
                    dn = self.polymarket.get_best_ask(self.state.window.token_down_id)
                    self.state.token_up_price = up
                    self.state.token_down_price = dn
                self.dashboard.broadcast("window", self.state.snapshot())
            except Exception as exc:
                log.debug("live state loop error: %s", exc)
            await asyncio.sleep(1.0)

    # ── Trading loop ────────────────────────────────────
    async def _trading_loop(self) -> None:
        """One iteration per 5-minute window."""
        # Let WS feeds warm up
        await asyncio.sleep(5)
        last_slug = None
        while not self._stopping:
            try:
                w = market.current_window_bounds()
                if w.slug == last_slug:
                    await asyncio.sleep(1)
                    continue
                last_slug = w.slug

                # Enter new window
                self.state.window = w
                self.state.entered_this_window = False
                self.state.last_decision = None
                self.state.signal = "resolving metadata"
                log.info("═══ new window %s (end in %ds)", w.slug, w.seconds_remaining)

                ok = await market.resolve_window_metadata(w)
                if not ok:
                    log.warning("could not resolve market metadata for %s — skipping window", w.slug)
                    await asyncio.sleep(w.seconds_remaining + 2)
                    continue

                await self.polymarket.set_tokens([w.token_up_id, w.token_down_id])

                # Capture price to beat (prefer Chainlink, fallback to Binance)
                w.price_to_beat = self._capture_price_to_beat(w)

                # Monitor from now → T-5s
                await self._monitor_window(w)

                # After window close, settle
                await self._settle_window(w)

            except Exception as exc:
                log.exception("trading loop error: %s", exc)
                await asyncio.sleep(2)

    def _capture_price_to_beat(self, w: market.Window) -> Optional[float]:
        """Try Chainlink first, fallback to Binance price at window open."""
        ptb = self.chainlink.get_price_to_beat(w.window_start)
        if ptb is not None:
            w.price_source = "chainlink"
            log.info("price_to_beat=%.2f (chainlink)", ptb)
            return ptb
        ptb = self.binance.get_price()
        if ptb is not None:
            w.price_source = "binance-fallback"
            log.info("price_to_beat=%.2f (binance fallback)", ptb)
            return ptb
        log.warning("no price source available for %s", w.slug)
        return None

    async def _monitor_window(self, w: market.Window) -> None:
        """From now until T-5s, look for an entry."""
        while not self._stopping and w.is_live and w.seconds_remaining > config.ENTRY_WINDOW_END_SEC:
            if w.seconds_remaining > config.ENTRY_WINDOW_START_SEC:
                self.state.signal = f"waiting (T-{w.seconds_remaining}s)"
                await asyncio.sleep(1)
                continue

            if self.state.entered_this_window:
                await asyncio.sleep(1)
                continue

            # If price_to_beat was None earlier, try again
            if w.price_to_beat is None:
                w.price_to_beat = self._capture_price_to_beat(w)
                if w.price_to_beat is None:
                    await asyncio.sleep(1)
                    continue

            btc = self.binance.get_price()
            if btc is None:
                await asyncio.sleep(0.5)
                continue
            delta = self.binance.get_delta_pct(w.price_to_beat) or 0.0
            trend = self.binance.classify_trend()
            vol = self.binance.classify_volume()
            up_px = self.polymarket.get_best_ask(w.token_up_id) or 0.0
            dn_px = self.polymarket.get_best_ask(w.token_down_id) or 0.0

            ctx = strategy.TradeContext(
                window_slug=w.slug,
                price_to_beat=w.price_to_beat,
                current_btc=btc,
                delta_pct=delta,
                delta_trend=trend,
                binance_volume=vol,
                seconds_remaining=w.seconds_remaining,
                token_up_price=up_px,
                token_down_price=dn_px,
            )
            decision = strategy.decide(ctx)
            self.state.last_decision = {
                "action": decision.action,
                "confidence": decision.confidence,
                "reasons": decision.reasons[-3:],
            }
            self.state.signal = f"{decision.action} (score {decision.confidence})"

            if decision.action in ("BUY_UP", "BUY_DOWN"):
                # Risk gates
                allowed, why = self.risk.can_trade()
                if not allowed:
                    log.info("risk gate blocks entry: %s", why)
                    self.state.signal = f"blocked: {why}"
                    await self._log_skip(ctx, {"skip_reason": f"risk:{why}"})
                    break

                decision.token_id = w.token_up_id if decision.action == "BUY_UP" else w.token_down_id
                size_usd = self.risk.get_trade_size(decision.confidence) * decision.size_multiplier
                size_usd = min(size_usd, config.MAX_TRADE_SIZE_USD)

                await self._enter_trade(w, decision, size_usd)

            await asyncio.sleep(1)

    async def _enter_trade(self, w: market.Window, decision: strategy.TradeDecision, size_usd: float) -> None:
        log.info("ENTRY %s size=$%.2f price=%.3f conf=%d",
                 decision.action, size_usd, decision.token_price, decision.confidence)

        # Post limit at best_ask - 0.01 (aggressive maker)
        limit_price = round(max(0.01, decision.token_price - 0.01), 2)
        fill = await self.executor.place_limit_buy(
            token_id=decision.token_id,
            price=limit_price,
            size_usd=size_usd,
            confidence=decision.confidence,
        )

        if not fill.success or fill.filled_shares <= 0:
            log.warning("entry not filled: %s", fill.error)
            await self.notifier.send_text(
                f"⚠️ Entry not filled on {w.slug}\nReason: {fill.error or 'no fill'}"
            )
            await self._log_skip(None, {"skip_reason": f"not_filled:{fill.error}", "decision": decision.reason_log})
            return

        self.state.entered_this_window = True
        record = {
            "window_slug": w.slug,
            "ts": int(time.time()),
            "action": decision.action,
            "side": "UP" if decision.action == "BUY_UP" else "DOWN",
            "entry_price": fill.avg_price,
            "limit_price": limit_price,
            "shares": fill.filled_shares,
            "cost": round(fill.avg_price * fill.filled_shares, 2),
            "confidence": decision.confidence,
            "reason_log": decision.reason_log,
            "order_id": fill.order_id,
        }
        self.state.entry_record = record
        self.trade_log.log_trade({**record, "phase": "entry"})

        rl = decision.reason_log or {}
        expected_profit = round((1.0 - fill.avg_price) * fill.filled_shares, 2)
        max_loss = round(fill.avg_price * fill.filled_shares, 2)
        today = self.pnl.today_stats()
        await self.notifier.send_text(
            f"🟢 <b>ENTRY</b> — {record['side']} {w.slug}\n"
            f"Shares: <b>{fill.filled_shares:.0f}</b> @ ${fill.avg_price:.3f}\n"
            f"Cost: <b>${record['cost']:.2f}</b>\n"
            f"Confidence: <b>{decision.confidence}/100</b>\n"
            f"━━━━━━━━━━━━\n"
            f"<b>Reason breakdown</b>\n"
            f"• Δ: {rl.get('delta_pct', 0):+.3f}%\n"
            f"• Time left: {rl.get('seconds_remaining', '?')}s\n"
            f"• Trend: {rl.get('delta_trend', '?')}\n"
            f"• Volume: {rl.get('binance_volume', '?')}\n"
            f"• Token px: UP ${rl.get('token_up_price', 0):.3f} / "
            f"DOWN ${rl.get('token_down_price', 0):.3f}\n"
            f"━━━━━━━━━━━━\n"
            f"Expected: +${expected_profit:.2f} / -${max_loss:.2f}\n"
            f"Session: {self.risk.state.session_pnl:+.2f} · "
            f"Today: {today['pnl']:+.2f} ({today['trades']}t)"
        )
        self.dashboard.broadcast("trade", record)

    async def _log_skip(self, ctx: Optional[strategy.TradeContext], reason_log: dict) -> None:
        rec = {
            "ts": int(time.time()),
            "action": "SKIP",
            "side": "SKIP",
            "reason_log": reason_log,
        }
        if ctx is not None:
            rec.update({
                "window_slug": ctx.window_slug,
                "price_to_beat": ctx.price_to_beat,
                "delta_pct": ctx.delta_pct,
            })
        self.trade_log.log_trade(rec)
        self.risk.record_skip()

    async def _settle_window(self, w: market.Window) -> None:
        """Wait until window close + 2s, resolve outcome, update PnL."""
        wait_for = max(0, (w.window_end + 2) - int(time.time()))
        await asyncio.sleep(wait_for)

        # Determine resolution using Chainlink if present, else Binance close
        close_price = self.chainlink.latest_price or self.binance.get_price()
        if close_price is None or w.price_to_beat is None:
            log.warning("cannot settle %s — missing prices", w.slug)
            return
        w.close_price = close_price
        w.resolution = "UP" if close_price >= w.price_to_beat else "DOWN"
        log.info("resolution: %s (open=%.2f close=%.2f)", w.resolution, w.price_to_beat, close_price)

        if not self.state.entered_this_window or self.state.entry_record is None:
            # No trade this window — nothing to settle
            self.state.window = None
            return

        rec = self.state.entry_record
        side = rec["side"]
        win = (side == w.resolution)
        entry = rec["entry_price"]
        shares = rec["shares"]
        if win:
            pnl = round((1.0 - entry) * shares, 2)
            outcome = "win"
        else:
            pnl = round(-entry * shares, 2)
            outcome = "loss"
        rec_final = {
            **rec,
            "outcome": outcome,
            "pnl": pnl,
            "close_price": close_price,
            "resolution": w.resolution,
        }
        self.pnl.record(rec_final)
        self.risk.record_trade(pnl)
        self.trade_log.log_trade({**rec_final, "phase": "settled"})

        # Notification
        today = self.pnl.today_stats()
        alltime = self.pnl.alltime_stats()
        streak = self.pnl.current_streak()
        icon = "🏆 WIN" if win else "❌ LOSS"
        delta_close = (close_price - w.price_to_beat) / w.price_to_beat * 100
        await self.notifier.send_text(
            f"📊 <b>WINDOW RESULT</b> — {w.slug}\n"
            f"BTC open:  ${w.price_to_beat:,.2f}\n"
            f"BTC close: ${close_price:,.2f} ({delta_close:+.3f}%)\n"
            f"Result: <b>{w.resolution}</b>\n"
            f"Our {side} @ ${entry:.3f} × {shares:.0f}\n"
            f"{icon}: <b>{pnl:+.2f}</b>\n"
            f"━━━━━━━━━━━━\n"
            f"Session: {self.risk.state.session_pnl:+.2f} · "
            f"Today: {today['pnl']:+.2f} ({today['trades']}t · WR {today['win_rate']}%)\n"
            f"Streak: {streak} · Balance: ${alltime['current_balance']:.2f}"
        )
        self.dashboard.broadcast("trade", rec_final)
        self.dashboard.broadcast("stats", {"pnl": pnl})

        # Check auto-pause conditions
        allowed, why = self.risk.can_trade()
        if not allowed:
            await self.notifier.send_text(f"⛔ <b>BOT AUTO-PAUSED</b>\n{why}\nUse /resume to continue.")
            config.RUNTIME.paused = True

        self.state.window = None
        self.state.entry_record = None
        self.state.entered_this_window = False


# ─────────────────────────────────────────────────────────────
# Entry
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="no real orders")
    args = parser.parse_args()

    bot = TradingBot(dry_run=args.dry_run)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _signal_handler():
        log.info("signal received")
        asyncio.ensure_future(bot.shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(bot.shutdown())
        loop.close()


if __name__ == "__main__":
    main()
