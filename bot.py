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
from utils.telegram import (
    CommandBot,
    Notifier,
    fetch_all_usdc,
    market_link_html,
    tx_link_html,
    wallet_link_html,
    window_label_from_slug,
)
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

        # Polymarket approvals (USDC + CTF) — only in LIVE mode
        if not self.dry_run:
            log.info("checking Polymarket approvals...")
            ok = await self.executor.ensure_approvals()
            if not ok:
                await self.notifier.send_text(
                    "⚠️ Polymarket approvals failed — orders may be rejected. Check logs."
                )
            else:
                log.info("Polymarket approvals OK")

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
        """Try Chainlink first, fallback to Binance price at exact window open."""
        if not self.chainlink.disabled:
            ptb = self.chainlink.get_price_to_beat(w.window_start)
            if ptb is not None:
                w.price_source = "chainlink"
                log.info("price_to_beat=%.2f (chainlink)", ptb)
                return ptb
        # Fallback: Binance trade captured at/after the 300s-aligned boundary
        ptb = self.binance.get_window_open_price(w.window_start)
        if ptb is not None:
            w.price_source = "binance-open"
            log.info("price_to_beat=%.2f (binance @ window open)", ptb)
            return ptb
        # Last resort: current Binance price (stale window / just started)
        ptb = self.binance.get_price()
        if ptb is not None:
            w.price_source = "binance-current"
            log.info("price_to_beat=%.2f (binance current)", ptb)
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

        # Check on-chain USDC.e balance (Polymarket collateral) before placing order
        if not self.dry_run and config.POLYGON_PUBLIC_KEY:
            try:
                usdc_e, usdc_nat, _ = await fetch_all_usdc(config.POLYGON_PUBLIC_KEY)
            except Exception as exc:
                log.warning("balance check failed: %s — proceeding anyway", exc)
                usdc_e = None
                usdc_nat = 0.0
            if usdc_e is not None and usdc_e < size_usd:
                log.warning("insufficient USDC.e: $%.2f < order $%.2f (native USDC: $%.2f)",
                            usdc_e, size_usd, usdc_nat)
                self.state.entered_this_window = True
                if usdc_nat > 1.0:
                    reason = (
                        f"⚠️ Order skipped on {w.slug}\n"
                        f"Reason: $0 USDC.e (Polymarket needs USDC.e, not native USDC)\n"
                        f"You have ${usdc_nat:.2f} native USDC — swap to USDC.e on QuickSwap/Uniswap (Polygon)\n"
                        f"⛔ <b>BOT AUTO-PAUSED</b> — swap and /resume"
                    )
                else:
                    reason = (
                        f"⚠️ Order skipped on {w.slug}\n"
                        f"Reason: insufficient USDC.e (${usdc_e:.2f} < ${size_usd:.2f})\n"
                        f"⛔ <b>BOT AUTO-PAUSED</b> — deposit USDC.e and /resume"
                    )
                await self.notifier.send_text(reason)
                config.RUNTIME.paused = True
                return

        # Refresh best_ask from the live feed to avoid stale prices
        fresh_ask = self.polymarket.get_best_ask(decision.token_id)
        best_ask = fresh_ask if fresh_ask and fresh_ask > 0 else decision.token_price
        best_ask = round(max(0.01, best_ask), 2)

        fill = await self.executor.place_limit_buy(
            token_id=decision.token_id,
            price=best_ask,
            size_usd=size_usd,
            confidence=decision.confidence,
        )

        if not fill.success or fill.filled_shares <= 0:
            log.warning("entry not filled: %s", fill.error)
            self.state.entered_this_window = True  # prevent retry spam
            await self.notifier.send_text(
                f"⚠️ <b>ENTRY FAILED</b> — {window_label_from_slug(w.slug)}\n"
                f"Side: {decision.action} | Price: ${best_ask:.3f}\n"
                f"Reason: {fill.error or 'no fill'}\n"
                f"Order ID: <code>{fill.order_id[:20] if fill.order_id else 'none'}</code>"
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
            "limit_price": best_ask,
            "shares": fill.filled_shares,
            "cost": round(fill.avg_price * fill.filled_shares, 2),
            "confidence": decision.confidence,
            "reason_log": decision.reason_log,
            "order_id": fill.order_id,
            "tx_hash": fill.tx_hash,
        }
        self.state.entry_record = record
        self.trade_log.log_trade({**record, "phase": "entry"})

        # Fetch live on-chain balance for transparency
        bal_str = ""
        try:
            usdc_bal, _, _ = await fetch_all_usdc(config.POLYGON_PUBLIC_KEY)
            bal_str = f"\n💵 USDC.e balance: <b>${usdc_bal:,.2f}</b>"
        except Exception:
            pass

        rl = decision.reason_log or {}
        today = self.pnl.today_stats()
        sess = self.risk.state.session_pnl
        market_lnk = market_link_html(w.slug)
        tx_lnk = tx_link_html(fill.tx_hash) if fill.tx_hash else ""
        order_id_str = f"\nOrder: <code>{fill.order_id[:20]}</code>" if fill.order_id else ""
        tx_str = f"\nTX: {tx_lnk}" if tx_lnk else ""
        await self.notifier.send_text(
            f"🟢 <b>ENTRY</b> — {window_label_from_slug(w.slug)}\n"
            f"BUY {record['side']} × {fill.filled_shares:.0f} @ ${fill.avg_price:.3f}\n"
            f"Cost: <b>${record['cost']:.2f}</b> | Score: {decision.confidence}/100\n"
            f"Δ {rl.get('delta_pct', 0):+.3f}% | ⏱ {rl.get('seconds_remaining', '?')}s | "
            f"Trend: {rl.get('delta_trend', '?')}"
            f"{order_id_str}{tx_str}{bal_str}\n"
            f"🔗 {market_lnk}\n"
            f"Session: {sess:+.2f} ({today.get('wins', 0)}W/{today.get('losses', 0)}L)"
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
        close_price = (
            (self.chainlink.latest_price if not self.chainlink.disabled else None)
            or self.binance.get_price()
        )
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

        # Fetch live on-chain balance for transparency
        onchain_bal = ""
        try:
            usdc_bal, _, _ = await fetch_all_usdc(config.POLYGON_PUBLIC_KEY)
            onchain_bal = f"\n💵 USDC.e on-chain: <b>${usdc_bal:,.2f}</b>"
        except Exception:
            pass

        # Notification
        today = self.pnl.today_stats()
        alltime = self.pnl.alltime_stats()
        delta_close = (close_price - w.price_to_beat) / w.price_to_beat * 100
        market_lnk = market_link_html(w.slug)
        tx_lnk = tx_link_html(rec.get("tx_hash", "")) if rec.get("tx_hash") else ""
        wins_today = today.get("wins", 0)
        losses_today = today.get("losses", 0)
        wr = today.get("win_rate", 0)
        bal = alltime["current_balance"]
        tx_line = f"\nTX: {tx_lnk}" if tx_lnk else ""
        if win:
            text = (
                f"🏆 <b>WIN</b> +${pnl:.2f}\n"
                f"BTC: ${w.price_to_beat:,.2f} → ${close_price:,.2f} "
                f"({delta_close:+.3f}%) = {w.resolution} ✅\n"
                f"Entry: {side} × {shares:.0f} @ ${entry:.3f} = ${rec['cost']:.2f}"
                f"{tx_line}\n"
                f"🔗 {market_lnk}\n"
                f"Today: {today['pnl']:+.2f} "
                f"({wins_today}W/{losses_today}L) {wr}% | Bot balance: ${bal:.2f}"
                f"{onchain_bal}"
            )
        else:
            text = (
                f"❌ <b>LOSS</b> -${abs(pnl):.2f}\n"
                f"BTC: ${w.price_to_beat:,.2f} → ${close_price:,.2f} "
                f"({delta_close:+.3f}%) = {w.resolution}\n"
                f"Entry: {side} × {shares:.0f} @ ${entry:.3f} = ${rec['cost']:.2f}"
                f"{tx_line}\n"
                f"🔗 {market_lnk}\n"
                f"Today: {today['pnl']:+.2f} "
                f"({wins_today}W/{losses_today}L) {wr}% | Bot balance: ${bal:.2f}"
                f"{onchain_bal}"
            )
        await self.notifier.send_text(text)
        self.dashboard.broadcast("trade", rec_final)
        self.dashboard.broadcast("stats", {"pnl": pnl})

        # Auto-redeem winning conditional tokens back to USDC.e
        log.info("redeem check: win=%s condition_id=%s dry_run=%s",
                 win, w.condition_id[:16] if w.condition_id else "EMPTY", self.dry_run)
        if win and w.condition_id and not self.dry_run:
            asyncio.create_task(self._auto_redeem(w.condition_id))
        elif win and not w.condition_id:
            log.error("WIN but no condition_id — cannot auto-redeem! slug=%s", w.slug)
            await self.notifier.send_text(
                "⚠️ Won but cannot auto-redeem: no conditionId from Gamma API.\n"
                "Manual redeem may be needed."
            )

        # Check auto-pause conditions
        allowed, why = self.risk.can_trade()
        if not allowed:
            await self.notifier.send_text(f"⛔ <b>BOT AUTO-PAUSED</b>\n{why}\nUse /resume to continue.")
            config.RUNTIME.paused = True

        self.state.window = None
        self.state.entry_record = None
        self.state.entered_this_window = False

    async def _auto_redeem(self, condition_id: str) -> None:
        """Redeem winning conditional tokens → USDC.e with retries.

        The on-chain oracle may take a few seconds to report the resolution,
        so we retry up to 3 times with increasing delays.
        """
        for attempt in range(3):
            # Wait for oracle to report on-chain resolution
            await asyncio.sleep(10 + attempt * 15)
            try:
                tx_hash = await self.executor.redeem_positions(condition_id)
                if tx_hash:
                    # Fetch new balance after redeem
                    bal_str = ""
                    try:
                        usdc_bal, _, _ = await fetch_all_usdc(config.POLYGON_PUBLIC_KEY)
                        bal_str = f"\n💵 USDC.e: <b>${usdc_bal:,.2f}</b>"
                    except Exception:
                        pass
                    await self.notifier.send_text(
                        f"💰 <b>Tokens redeemed</b>\n"
                        f"TX: {tx_link_html(tx_hash)}"
                        f"{bal_str}"
                    )
                    return
                log.info("redeem attempt %d: no tx (oracle may not have reported yet)", attempt + 1)
            except Exception as exc:
                log.warning("redeem attempt %d failed: %s", attempt + 1, exc)
        log.warning("auto-redeem failed after 3 attempts for condition %s", condition_id[:16])
        await self.notifier.send_text(
            f"⚠️ Auto-redeem failed after 3 attempts.\n"
            f"Condition: <code>{condition_id[:20]}</code>\n"
            f"You may need to redeem manually on polymarket.com"
        )


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
