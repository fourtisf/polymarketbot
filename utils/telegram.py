"""
Telegram bot interface — notifications + interactive commands.

Uses python-telegram-bot v21 (async). Provides:
  - Notifier: one-way messaging (send_text / send_photo)
  - CommandBot: two-way bot that handles /start /stop /stats /chart /trades etc.

The CommandBot is wired to the live PnLTracker and RiskManager so users
can monitor and control the bot from their phone.
"""

import asyncio
import html
import logging
from datetime import datetime
from typing import Optional

import aiohttp

import config

log = logging.getLogger("telegram")


class Notifier:
    """Fire-and-forget one-way messenger (no bot framework needed)."""

    def __init__(self, token: str = "", chat_id: str = ""):
        self.token = token or config.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or config.TELEGRAM_CHAT_ID

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send_text(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(url, json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                })
        except Exception as exc:
            log.warning("telegram send_text failed: %s", exc)

    async def send_photo(self, png_bytes: bytes, caption: str = "") -> None:
        if not self.enabled:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
        try:
            form = aiohttp.FormData()
            form.add_field("chat_id", self.chat_id)
            form.add_field("caption", caption)
            form.add_field("photo", png_bytes, filename="chart.png", content_type="image/png")
            async with aiohttp.ClientSession() as s:
                await s.post(url, data=form)
        except Exception as exc:
            log.warning("telegram send_photo failed: %s", exc)


class CommandBot:
    """
    Interactive Telegram bot. Started in a background task by bot.py.
    Runs a long-poll loop against the Telegram API (no external framework
    required — keeps dependencies minimal).
    """

    def __init__(self, pnl_tracker, risk_manager, executor, notifier: Notifier):
        self.pnl = pnl_tracker
        self.risk = risk_manager
        self.executor = executor
        self.notifier = notifier
        self._offset: int = 0
        self._running = False

    async def run(self) -> None:
        if not self.notifier.enabled:
            log.warning("telegram command bot disabled (no token)")
            return
        self._running = True
        log.info("telegram command bot started")
        while self._running:
            try:
                await self._poll_once()
            except Exception as exc:
                log.warning("telegram poll error: %s", exc)
                await asyncio.sleep(3)

    async def stop(self) -> None:
        self._running = False

    async def _poll_once(self) -> None:
        url = f"https://api.telegram.org/bot{self.notifier.token}/getUpdates"
        params = {"timeout": 25, "offset": self._offset}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as s:
                async with s.get(url, params=params) as resp:
                    data = await resp.json()
        except Exception:
            await asyncio.sleep(2)
            return

        for upd in data.get("result", []):
            self._offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            parts = text.split()
            cmd = parts[0].lower().split("@")[0]
            args = parts[1:]
            try:
                await self._handle(cmd, args)
            except Exception as exc:
                log.exception("command %s failed: %s", cmd, exc)
                await self.notifier.send_text(f"⚠️ Error handling {cmd}: {exc}")

    # ── Command handlers ───────────────────────────────
    async def _handle(self, cmd: str, args: list) -> None:
        handler = {
            "/start": self._cmd_start,
            "/stop": self._cmd_stop,
            "/resume": self._cmd_resume,
            "/stats": self._cmd_stats,
            "/today": self._cmd_today,
            "/history": self._cmd_history,
            "/trades": self._cmd_history,
            "/size": self._cmd_size,
            "/maxloss": self._cmd_maxloss,
            "/config": self._cmd_config,
            "/dashboard": self._cmd_dashboard,
            "/pnl": self._cmd_pnl,
            "/chart": self._cmd_chart,
        }.get(cmd)
        if handler is None:
            await self.notifier.send_text("Unknown command. Try /dashboard /stats /config")
            return
        await handler(args)

    async def _cmd_start(self, args):
        status = "PAUSED" if config.RUNTIME.paused else "RUNNING"
        today = self.pnl.today_stats()
        txt = (
            f"🟢 <b>POLYMARKET 5M BOT</b>\n"
            f"Status: {status}\n"
            f"Today PnL: {today['pnl']:+.2f} ({today['trades']} trades)\n"
            f"Use /dashboard for full view"
        )
        await self.notifier.send_text(txt)

    async def _cmd_stop(self, args):
        config.RUNTIME.paused = True
        await self.notifier.send_text("⏸️ Bot paused. Use /resume to start again.")

    async def _cmd_resume(self, args):
        config.RUNTIME.paused = False
        await self.notifier.send_text("▶️ Bot resumed.")

    async def _cmd_stats(self, args):
        t = self.pnl.today_stats()
        w = self.pnl.week_stats()
        a = self.pnl.alltime_stats()
        txt = (
            f"📊 <b>STATS</b>\n"
            f"<b>Today:</b> {t['pnl']:+.2f} · {t['trades']}t · WR {t['win_rate']}%\n"
            f"<b>Week:</b>  {w['pnl']:+.2f} · {w['trades']}t · WR {w['win_rate']}%\n"
            f"<b>Total:</b> {a['pnl']:+.2f} · {a['trades']}t · WR {a['win_rate']}%\n"
            f"ROI: {a['roi_pct']}% | Max DD: {a['max_drawdown']}"
        )
        await self.notifier.send_text(txt)

    async def _cmd_today(self, args):
        t = self.pnl.today_stats()
        txt = (
            f"📅 <b>TODAY — {datetime.utcnow().strftime('%Y-%m-%d')}</b>\n"
            f"Trades: {t['trades']} ({t['wins']}W/{t['losses']}L)\n"
            f"Win rate: {t['win_rate']}%\n"
            f"PnL: {t['pnl']:+.2f}\n"
            f"Best: {t['best']:+.2f} | Worst: {t['worst']:+.2f}\n"
            f"Profit factor: {t['profit_factor']}"
        )
        await self.notifier.send_text(txt)

    async def _cmd_history(self, args):
        n = 5
        if args:
            try:
                n = max(1, min(20, int(args[0])))
            except ValueError:
                pass
        trades = self.pnl.recent_trades(n)
        if not trades:
            await self.notifier.send_text("No trades yet.")
            return
        lines = [f"📋 <b>LAST {len(trades)} TRADES</b>"]
        for i, t in enumerate(trades, 1):
            ts = datetime.utcfromtimestamp(t.get("ts", 0)).strftime("%H:%M")
            side = t.get("side", "?")
            price = t.get("entry_price", 0)
            pnl = t.get("pnl", 0)
            icon = "🏆" if pnl > 0 else "❌"
            rl = t.get("reason_log", {})
            delta = rl.get("delta_pct", 0)
            score = rl.get("score", 0)
            lines.append(
                f"{i}. {icon} {ts} — {side} @ ${price:.2f} → {pnl:+.2f}\n"
                f"   Δ{delta:+.3f}% · score {score}"
            )
        await self.notifier.send_text("\n".join(lines))

    async def _cmd_size(self, args):
        if not args:
            await self.notifier.send_text(f"Current base size: ${config.RUNTIME.base_size_usd:.2f}\nUsage: /size 5")
            return
        try:
            v = float(args[0])
            if not (1 <= v <= config.MAX_TRADE_SIZE_USD):
                await self.notifier.send_text(f"Size must be between $1 and ${config.MAX_TRADE_SIZE_USD}")
                return
            config.RUNTIME.base_size_usd = v
            await self.notifier.send_text(f"✅ Base size set to ${v:.2f}")
        except ValueError:
            await self.notifier.send_text("Invalid number.")

    async def _cmd_maxloss(self, args):
        if not args:
            await self.notifier.send_text(f"Current session loss limit: ${config.RUNTIME.max_session_loss:.2f}")
            return
        try:
            v = float(args[0])
            if v <= 0:
                await self.notifier.send_text("Must be positive.")
                return
            config.RUNTIME.max_session_loss = v
            await self.notifier.send_text(f"✅ Session loss limit set to ${v:.2f}")
        except ValueError:
            await self.notifier.send_text("Invalid number.")

    async def _cmd_config(self, args):
        s = config.summary()
        lines = ["⚙️ <b>CONFIG</b>"]
        for k, v in s.items():
            lines.append(f"  {k}: {v}")
        await self.notifier.send_text("\n".join(lines))

    async def _cmd_dashboard(self, args):
        t = self.pnl.today_stats()
        w = self.pnl.week_stats()
        a = self.pnl.alltime_stats()
        status = "⏸️ PAUSED" if config.RUNTIME.paused else "🟢 RUNNING"
        streak = self.pnl.current_streak()
        balance = a["current_balance"]
        txt = (
            f"🏠 <b>DASHBOARD</b>\n"
            f"{status} · Balance: ${balance:.2f}\n"
            f"━━━━━━━━━━━━━━\n"
            f"<b>TODAY</b>\n"
            f"Trades: {t['trades']} ({t['wins']}W/{t['losses']}L) WR {t['win_rate']}%\n"
            f"PnL: {t['pnl']:+.2f} · PF {t['profit_factor']}\n"
            f"Best {t['best']:+.2f} · Worst {t['worst']:+.2f}\n"
            f"━━━━━━━━━━━━━━\n"
            f"<b>WEEK</b> {w['pnl']:+.2f} · {w['trades']}t WR {w['win_rate']}%\n"
            f"━━━━━━━━━━━━━━\n"
            f"<b>ALL TIME</b>\n"
            f"Trades: {a['trades']} ({a['wins']}W/{a['losses']}L) WR {a['win_rate']}%\n"
            f"PnL: {a['pnl']:+.2f} · ROI {a['roi_pct']}%\n"
            f"Max DD: {a['max_drawdown']} · Sharpe {a['sharpe_daily']}\n"
            f"Streak: {streak}"
        )
        await self.notifier.send_text(txt)

    async def _cmd_pnl(self, args):
        t = self.pnl.today_stats()
        w = self.pnl.week_stats()
        a = self.pnl.alltime_stats()
        def ico(x): return "📈" if x >= 0 else "📉"
        txt = (
            f"💰 <b>PNL</b>\n"
            f"Today:     {t['pnl']:+.2f} {ico(t['pnl'])}\n"
            f"This Week: {w['pnl']:+.2f} {ico(w['pnl'])}\n"
            f"All Time:  {a['pnl']:+.2f} {ico(a['pnl'])}"
        )
        await self.notifier.send_text(txt)

    async def _cmd_chart(self, args):
        from utils.chart_generator import generate_pnl_chart
        png = generate_pnl_chart(
            self.pnl.equity_curve(),
            self.pnl.daily_pnl_series(),
            self.pnl.rolling_win_rate(),
        )
        if png is None:
            await self.notifier.send_text("No data yet for chart.")
            return
        await self.notifier.send_photo(png, caption="📈 Performance chart")
