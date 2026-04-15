"""
Telegram bot interface — notifications + interactive commands.

Uses raw Telegram Bot API over aiohttp (no python-telegram-bot dep).

Public surface consumed by bot.py:
  - Notifier: send_text / send_photo / edit_text / delete_message / answer_callback
  - CommandBot(pnl_tracker, risk_manager, executor, notifier, trading_bot=None)
      .run()   — long-poll loop
      .stop()  — graceful stop

Wallet onboarding (/setwallet) derives the address, fetches USDC/POL
balances from Polygon RPC, derives Polymarket CLOB API credentials, and
persists everything to .env. The user message containing the private key
is deleted immediately on receipt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

import config

log = logging.getLogger("telegram")


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
USDC_CONTRACT = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
ENV_PATH = Path(config.PROJECT_ROOT) / ".env"

BAR = "━━━━━━━━━━━━━━"


def _set_module_attr(name: str, value: Any) -> None:
    setattr(config, name, value)


# Settings surfaced through /settings — 6 entries, each with presets,
# parser, current-value getter, and an apply function.
SETTINGS_SPEC: Dict[str, Dict[str, Any]] = {
    "base_size": {
        "label": "Trade size (USD)",
        "env": "BASE_TRADE_SIZE",
        "parser": float,
        "presets": [1, 2, 5, 10, 20],
        "current": lambda: config.RUNTIME.base_size_usd,
        "apply": lambda v: setattr(config.RUNTIME, "base_size_usd", float(v)),
    },
    "min_conf": {
        "label": "Min confidence",
        "env": "MIN_CONFIDENCE",
        "parser": int,
        "presets": [50, 60, 70, 80, 90],
        "current": lambda: config.RUNTIME.min_confidence,
        "apply": lambda v: setattr(config.RUNTIME, "min_confidence", int(v)),
    },
    "max_sess": {
        "label": "Max session loss",
        "env": "MAX_SESSION_LOSS",
        "parser": float,
        "presets": [10, 20, 30, 50, 100],
        "current": lambda: config.RUNTIME.max_session_loss,
        "apply": lambda v: setattr(config.RUNTIME, "max_session_loss", float(v)),
    },
    "max_daily": {
        "label": "Max daily loss",
        "env": "MAX_DAILY_LOSS",
        "parser": float,
        "presets": [20, 30, 50, 100, 200],
        "current": lambda: config.MAX_DAILY_LOSS_USD,
        "apply": lambda v: _set_module_attr("MAX_DAILY_LOSS_USD", float(v)),
    },
    "min_delta": {
        "label": "Min delta %",
        "env": "MIN_DELTA_PCT",
        "parser": float,
        "presets": [0.02, 0.05, 0.08, 0.10, 0.15],
        "current": lambda: config.MIN_DELTA_PCT,
        "apply": lambda v: _set_module_attr("MIN_DELTA_PCT", float(v)),
    },
    "entry_win": {
        "label": "Entry window start (s)",
        "env": "ENTRY_WINDOW_START",
        "parser": int,
        "presets": [30, 45, 60, 90, 120],
        "current": lambda: config.ENTRY_WINDOW_START_SEC,
        "apply": lambda v: _set_module_attr("ENTRY_WINDOW_START_SEC", int(v)),
    },
}


# ─────────────────────────────────────────────────────────────
# .env upsert (atomic, preserves comments/order)
# ─────────────────────────────────────────────────────────────

def update_env_file(updates: Dict[str, str], path: Path = ENV_PATH) -> None:
    lines: List[str] = []
    if path.exists():
        lines = path.read_text().splitlines()
    seen: set = set()
    out: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(out) + "\n")
    try:
        tmp.chmod(0o600)
    except Exception:
        pass
    tmp.replace(path)


# ─────────────────────────────────────────────────────────────
# Polygon RPC helpers
# ─────────────────────────────────────────────────────────────

async def _rpc_call(session: aiohttp.ClientSession, method: str, params: list) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(
        POLYGON_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=15)
    ) as r:
        data = await r.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "rpc error"))
    return data.get("result")


async def fetch_balances(address: str) -> Tuple[float, float]:
    """Return (usdc, pol) balances for ``address`` on Polygon mainnet."""
    addr = address.lower().replace("0x", "").rjust(40, "0")
    data = "0x70a08231" + "0" * 24 + addr  # balanceOf(address)
    async with aiohttp.ClientSession() as s:
        pol_hex = await _rpc_call(s, "eth_getBalance", [address, "latest"])
        usdc_hex = await _rpc_call(
            s, "eth_call",
            [{"to": USDC_CONTRACT, "data": data}, "latest"],
        )
    pol = int(pol_hex, 16) / 1e18 if pol_hex else 0.0
    usdc = int(usdc_hex, 16) / 1e6 if usdc_hex and usdc_hex != "0x" else 0.0
    return usdc, pol


# ─────────────────────────────────────────────────────────────
# Notifier — thin HTTP wrapper around Bot API
# ─────────────────────────────────────────────────────────────

class Notifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token = token or config.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or config.TELEGRAM_CHAT_ID

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    async def _post(self, method: str, payload: dict) -> Optional[dict]:
        if not self.token:
            return None
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=payload) as r:
                    return await r.json()
        except Exception as exc:
            log.warning("telegram %s failed: %s", method, exc)
            return None

    async def send_text(
        self,
        text: str,
        chat_id: Optional[str] = None,
        reply_markup: Optional[dict] = None,
    ) -> Optional[dict]:
        if not self.enabled and chat_id is None:
            return None
        payload: Dict[str, Any] = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._post("sendMessage", payload)

    async def edit_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[dict] = None,
    ) -> Optional[dict]:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._post("editMessageText", payload)

    async def delete_message(self, chat_id: str, message_id: int) -> None:
        await self._post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._post(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text},
        )

    async def send_photo(
        self,
        png_bytes: bytes,
        caption: str = "",
        chat_id: Optional[str] = None,
    ) -> None:
        if not self.token:
            return
        target = chat_id or self.chat_id
        if not target:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendPhoto"
        try:
            form = aiohttp.FormData()
            form.add_field("chat_id", str(target))
            form.add_field("caption", caption)
            form.add_field("parse_mode", "HTML")
            form.add_field(
                "photo", png_bytes,
                filename="chart.png", content_type="image/png",
            )
            async with aiohttp.ClientSession() as s:
                await s.post(url, data=form)
        except Exception as exc:
            log.warning("telegram send_photo failed: %s", exc)


# ─────────────────────────────────────────────────────────────
# Small formatting helpers
# ─────────────────────────────────────────────────────────────

def _ico(pnl: float) -> str:
    return "📈" if pnl >= 0 else "📉"


def _short_addr(addr: str) -> str:
    if not addr or not addr.startswith("0x") or len(addr) < 10:
        return addr or "(not set)"
    return f"{addr[:6]}...{addr[-4:]}"


def wallet_link_html(addr: str, label: str = "") -> str:
    """Return an HTML <a> link to Polygonscan for ``addr``."""
    if not addr or not addr.startswith("0x") or len(addr) < 10:
        return label or "(not set)"
    text = label or _short_addr(addr)
    return f'<a href="https://polygonscan.com/address/{addr}">{text}</a>'


def tx_link_html(tx_hash: str, label: str = "View TX") -> str:
    if not tx_hash:
        return ""
    return f'<a href="https://polygonscan.com/tx/{tx_hash}">{label}</a>'


def market_link_html(slug: str, label: str = "Market") -> str:
    if not slug:
        return ""
    return f'<a href="https://polymarket.com/event/{slug}">{label}</a>'


def window_label_from_slug(slug: str) -> str:
    """Convert a window slug like 'btc-updown-5m-1776258600' to 'HH:MM-HH:MM ET'."""
    try:
        end = int(slug.rsplit("-", 1)[-1])
    except Exception:
        return slug
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    except Exception:
        tz = timezone.utc
    start = end - 300
    s = datetime.fromtimestamp(start, tz=tz).strftime("%H:%M")
    e = datetime.fromtimestamp(end, tz=tz).strftime("%H:%M")
    return f"{s}-{e} ET"


def _fmt_stats_block(title: str, s: Dict[str, Any]) -> str:
    return (
        f"<b>{title}</b>\n"
        f"PnL: <b>{s.get('pnl', 0):+.2f}</b> {_ico(s.get('pnl', 0))}\n"
        f"Trades: {s.get('trades', 0)} · WR {s.get('win_rate', 0)}%\n"
        f"W/L: {s.get('wins', 0)}/{s.get('losses', 0)} · "
        f"PF {s.get('profit_factor', 0)}\n"
        f"Avg win: {s.get('avg_win', 0):+.2f} · "
        f"Avg loss: {s.get('avg_loss', 0):+.2f}\n"
        f"Best: {s.get('best', 0):+.2f} · Worst: {s.get('worst', 0):+.2f}"
    )


# ─────────────────────────────────────────────────────────────
# CommandBot — interactive long-polling bot
# ─────────────────────────────────────────────────────────────

class CommandBot:
    def __init__(
        self,
        pnl_tracker,
        risk_manager,
        executor,
        notifier: Notifier,
        trading_bot: Any = None,
    ):
        self.pnl = pnl_tracker
        self.risk = risk_manager
        self.executor = executor
        self.notifier = notifier
        self.trading_bot = trading_bot

        self._offset: int = 0
        self._running = False
        # chat_id → setting key awaiting a typed value
        self._pending_edit: Dict[str, str] = {}

    # ── Lifecycle ──────────────────────────────────────────
    async def run(self) -> None:
        if not self.notifier.token:
            log.warning("telegram command bot disabled (no token)")
            return
        self._running = True
        log.info("telegram command bot started")
        # Install BotFather-style command menu (best-effort).
        asyncio.create_task(self._install_command_menu())
        while self._running:
            try:
                await self._poll_once()
            except Exception as exc:
                log.warning("telegram poll error: %s", exc)
                await asyncio.sleep(3)

    async def stop(self) -> None:
        self._running = False

    async def _install_command_menu(self) -> None:
        commands = [
            {"command": "start", "description": "Main dashboard"},
            {"command": "wallet", "description": "Wallet address + balances"},
            {"command": "setwallet", "description": "Set private key (auto-deleted)"},
            {"command": "go", "description": "Start auto trading"},
            {"command": "stop", "description": "Stop auto trading"},
            {"command": "mode", "description": "Toggle dry-run / live"},
            {"command": "pnl", "description": "Profit / loss summary"},
            {"command": "stats", "description": "Full statistics"},
            {"command": "trades", "description": "Recent trades"},
            {"command": "chart", "description": "PnL chart image"},
            {"command": "risk", "description": "Risk status"},
            {"command": "status", "description": "Current live window"},
            {"command": "settings", "description": "Edit settings"},
            {"command": "pause", "description": "30 min cooldown"},
            {"command": "help", "description": "Show all commands"},
        ]
        await self.notifier._post("setMyCommands", {"commands": commands})

    async def _poll_once(self) -> None:
        url = f"https://api.telegram.org/bot{self.notifier.token}/getUpdates"
        params = {
            "timeout": 25,
            "offset": self._offset,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=35)
            ) as s:
                async with s.get(url, params=params) as resp:
                    data = await resp.json()
        except Exception:
            await asyncio.sleep(2)
            return

        for upd in data.get("result", []):
            self._offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    await self._handle_callback(upd["callback_query"])
                    continue
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                await self._handle_message(msg)
            except Exception as exc:
                log.exception("update handling failed: %s", exc)

    # ── Message dispatch ───────────────────────────────────
    async def _handle_message(self, msg: dict) -> None:
        chat_id = str(msg["chat"]["id"])
        msg_id = msg.get("message_id")
        text = (msg.get("text") or "").strip()

        # /setwallet — delete message IMMEDIATELY before parsing/logging.
        if text.lower().startswith("/setwallet"):
            await self.notifier.delete_message(chat_id, msg_id)
            parts = text.split()
            if len(parts) < 2:
                await self.notifier.send_text(
                    "Usage: <code>/setwallet 0xPRIVATEKEY</code>\n"
                    "(your message is deleted immediately)",
                    chat_id=chat_id,
                )
                return
            await self._do_setwallet(parts[1], chat_id)
            return

        if not text:
            return

        # If awaiting typed value for a pending setting edit, consume it.
        if chat_id in self._pending_edit and not text.startswith("/"):
            key = self._pending_edit.pop(chat_id)
            await self._apply_setting(key, text, chat_id)
            return

        if not text.startswith("/"):
            return

        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]
        await self._handle_command(cmd, args, chat_id)

    async def _handle_command(self, cmd: str, args: list, chat_id: str) -> None:
        handlers = {
            "/start": self._cmd_start,
            "/wallet": self._cmd_wallet,
            "/go": self._cmd_go,
            "/resume": self._cmd_go,
            "/stop": self._cmd_stop,
            "/mode": self._cmd_mode,
            "/pnl": self._cmd_pnl,
            "/stats": self._cmd_stats,
            "/trades": self._cmd_trades,
            "/history": self._cmd_trades,
            "/chart": self._cmd_chart,
            "/status": self._cmd_status,
            "/risk": self._cmd_risk,
            "/settings": self._cmd_settings,
            "/config": self._cmd_settings,
            "/pause": self._cmd_pause,
            "/help": self._cmd_help,
        }
        handler = handlers.get(cmd)
        if handler is None:
            await self._cmd_help(args, chat_id)
            return
        await handler(args, chat_id)

    async def _handle_callback(self, cq: dict) -> None:
        await self.notifier.answer_callback(cq["id"])
        data = cq.get("data", "")
        msg = cq.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        msg_id = msg.get("message_id")

        # Dashboard-level navigation buttons
        if data == "nav:go":
            await self._cmd_go([], chat_id)
            await self._send_dashboard(chat_id)
        elif data == "nav:stop":
            await self._cmd_stop([], chat_id)
            await self._send_dashboard(chat_id)
        elif data == "nav:stats":
            await self._cmd_stats([], chat_id)
        elif data == "nav:pnl":
            await self._cmd_pnl([], chat_id)
        elif data == "nav:trades":
            await self._cmd_trades([], chat_id)
        elif data == "nav:chart":
            await self._cmd_chart([], chat_id)
        elif data == "nav:settings":
            await self._send_settings(chat_id, edit_msg_id=msg_id)
        elif data == "nav:wallet":
            await self._cmd_wallet([], chat_id)
        elif data == "nav:dashboard":
            await self._send_dashboard(chat_id, edit_msg_id=msg_id)

        # Wallet refresh
        elif data == "wallet:refresh":
            await self._cmd_wallet([], chat_id)

        # Mode toggle flow (confirmation required for LIVE)
        elif data == "mode:toggle":
            await self._mode_prompt(chat_id, msg_id)
        elif data == "mode:confirm_live":
            await self._set_mode(False, chat_id, msg_id)
        elif data == "mode:confirm_dry":
            await self._set_mode(True, chat_id, msg_id)
        elif data == "mode:cancel":
            await self._send_dashboard(chat_id, edit_msg_id=msg_id)

        # Settings editor
        elif data == "settings:open":
            await self._send_settings(chat_id, edit_msg_id=msg_id)
        elif data.startswith("edit:"):
            key = data.split(":", 1)[1]
            await self._prompt_setting_edit(key, chat_id, msg_id)
        elif data.startswith("val:"):
            _, key, val = data.split(":", 2)
            await self._apply_setting(key, val, chat_id, edit_msg_id=msg_id)
        elif data.startswith("type:"):
            key = data.split(":", 1)[1]
            self._pending_edit[chat_id] = key
            spec = SETTINGS_SPEC.get(key)
            label = spec["label"] if spec else key
            await self.notifier.send_text(
                f"✏️ Send a new value for <b>{label}</b> as your next message.",
                chat_id=chat_id,
            )
        elif data == "back":
            await self._send_settings(chat_id, edit_msg_id=msg_id)

    # ── /setwallet flow ────────────────────────────────────
    async def _do_setwallet(self, private_key: str, chat_id: str) -> None:
        try:
            from eth_account import Account
        except ImportError:
            await self.notifier.send_text(
                "eth_account not installed.", chat_id=chat_id
            )
            return

        pk = private_key.strip()
        if not pk.startswith("0x"):
            pk = "0x" + pk
        try:
            acct = Account.from_key(pk)
            address = acct.address
        except Exception:
            await self.notifier.send_text(
                "❌ Invalid private key. (message already deleted)",
                chat_id=chat_id,
            )
            return

        status_resp = await self.notifier.send_text(
            "🔐 Wallet received. Deriving credentials and checking balances…",
            chat_id=chat_id,
        )
        status_msg_id = None
        try:
            status_msg_id = status_resp.get("result", {}).get("message_id")
        except Exception:
            pass

        try:
            usdc, pol = await fetch_balances(address)
        except Exception as exc:
            log.warning("balance fetch failed: %s", exc)
            usdc, pol = 0.0, 0.0

        api_key = api_secret = api_pass = ""
        try:
            api_key, api_secret, api_pass = await asyncio.to_thread(
                _derive_clob_creds, pk
            )
        except Exception as exc:
            log.warning("clob creds derivation failed: %s", exc)
            await self.notifier.send_text(
                f"⚠️ Could not derive Polymarket API creds: {exc}\n"
                "Wallet is set but you may need to retry /setwallet.",
                chat_id=chat_id,
            )

        updates = {
            "POLYGON_PRIVATE_KEY": pk,
            "POLYGON_PUBLIC_KEY": address,
        }
        if api_key:
            updates["POLYMARKET_API_KEY"] = api_key
            updates["POLYMARKET_API_SECRET"] = api_secret
            updates["POLYMARKET_PASSPHRASE"] = api_pass
        try:
            update_env_file(updates)
        except Exception as exc:
            log.warning("env write failed: %s", exc)

        config.POLYGON_PRIVATE_KEY = pk
        config.POLYGON_PUBLIC_KEY = address
        if api_key:
            config.POLYMARKET_API_KEY = api_key
            config.POLYMARKET_API_SECRET = api_secret
            config.POLYMARKET_PASSPHRASE = api_pass

        try:
            self.executor._client = None  # type: ignore[attr-defined]
        except Exception:
            pass
        if self.trading_bot is not None and hasattr(self.trading_bot, "reload_wallet"):
            try:
                self.trading_bot.reload_wallet()
            except Exception as exc:
                log.warning("reload_wallet failed: %s", exc)

        text = (
            f"✅ <b>Wallet connected</b>\n"
            f"<code>{_short_addr(address)}</code>\n"
            f"{BAR}\n"
            f"💵 USDC: <b>${usdc:,.2f}</b>\n"
            f"⛽ POL:  <b>{pol:,.4f}</b>\n"
            + ("🔑 Polymarket API credentials derived.\n" if api_key else "")
            + "\nUse /start to open the dashboard."
        )
        if status_msg_id:
            await self.notifier.edit_text(chat_id, status_msg_id, text)
        else:
            await self.notifier.send_text(text, chat_id=chat_id)

    # ── /start dashboard ───────────────────────────────────
    async def _cmd_start(self, args, chat_id):
        pk = config.POLYGON_PRIVATE_KEY or ""
        if not pk or pk.startswith("0x_your"):
            await self._send_wizard(chat_id)
            return
        await self._send_dashboard(chat_id)

    async def _send_wizard(self, chat_id: str) -> None:
        text = (
            "👋 <b>Welcome to Polymarket 5m BTC Bot</b>\n"
            f"{BAR}\n"
            "<b>Setup (1 step):</b>\n"
            "Send your Polygon private key with:\n"
            "<code>/setwallet 0xYOUR_PRIVATE_KEY</code>\n\n"
            "🔒 Your message is <b>deleted immediately</b> after receipt.\n"
            "• Public address derived on-device\n"
            "• USDC + POL balances fetched from Polygon\n"
            "• Polymarket API credentials auto-generated\n"
            "• Everything saved to .env — no manual editing\n\n"
            "Your private key is <b>never</b> logged or shown in any message."
        )
        await self.notifier.send_text(text, chat_id=chat_id)

    async def _send_dashboard(
        self, chat_id: str, edit_msg_id: Optional[int] = None
    ) -> None:
        paused = config.RUNTIME.paused
        status = "⏸ PAUSED" if paused else "🟢 RUNNING"
        mode = "🧪 DRY-RUN" if config.RUNTIME.dry_run else "💸 LIVE"
        addr = config.POLYGON_PUBLIC_KEY or ""

        usdc = pol = 0.0
        if addr.startswith("0x") and len(addr) == 42:
            try:
                usdc, pol = await fetch_balances(addr)
            except Exception:
                pass

        t = self.pnl.today_stats()
        a = self.pnl.alltime_stats()
        session_pnl = self.risk.state.session_pnl

        text = (
            f"🏠 <b>DASHBOARD</b>\n"
            f"{status} · {mode}\n"
            f"{BAR}\n"
            f"👛 {wallet_link_html(addr, addr)}\n"
            f"💵 ${usdc:,.2f} · ⛽ {pol:,.4f}\n"
            f"{BAR}\n"
            f"Session:  <b>{session_pnl:+.2f}</b> {_ico(session_pnl)}\n"
            f"Today:    {t['pnl']:+.2f} · {t['trades']}t · WR {t['win_rate']}%\n"
            f"All-time: {a['pnl']:+.2f} · {a['trades']}t · WR {a['win_rate']}%\n"
            f"Balance:  ${a['current_balance']:.2f} (ROI {a['roi_pct']:+.1f}%)\n"
            f"{BAR}\n"
            f"Pick an action:"
        )

        kb = {"inline_keyboard": [
            [
                {"text": "▶️ Start Trading", "callback_data": "nav:go"},
                {"text": "⏸ Stop Trading",  "callback_data": "nav:stop"},
            ],
            [
                {"text": "📊 Stats",  "callback_data": "nav:stats"},
                {"text": "💰 PnL",    "callback_data": "nav:pnl"},
            ],
            [
                {"text": "📋 Trades", "callback_data": "nav:trades"},
                {"text": "📈 Chart",  "callback_data": "nav:chart"},
            ],
            [
                {"text": "⚙️ Settings", "callback_data": "nav:settings"},
                {"text": "👛 Wallet",   "callback_data": "nav:wallet"},
            ],
        ]}

        if edit_msg_id is not None:
            await self.notifier.edit_text(chat_id, edit_msg_id, text, reply_markup=kb)
        else:
            await self.notifier.send_text(text, chat_id=chat_id, reply_markup=kb)

    # ── /settings ──────────────────────────────────────────
    async def _cmd_settings(self, args, chat_id):
        await self._send_settings(chat_id)

    async def _send_settings(
        self, chat_id: str, edit_msg_id: Optional[int] = None
    ) -> None:
        text = (
            "⚙️ <b>SETTINGS</b>\n"
            f"{BAR}\n"
            "Tap a setting to change its value.\n"
            "Changes persist to <code>.env</code>."
        )
        rows = []
        for key, spec in SETTINGS_SPEC.items():
            cur = spec["current"]()
            rows.append([{
                "text": f"{spec['label']}: {cur}",
                "callback_data": f"edit:{key}",
            }])
        rows.append([{"text": "⬅️ Dashboard", "callback_data": "nav:dashboard"}])
        kb = {"inline_keyboard": rows}
        if edit_msg_id is not None:
            await self.notifier.edit_text(chat_id, edit_msg_id, text, reply_markup=kb)
        else:
            await self.notifier.send_text(text, chat_id=chat_id, reply_markup=kb)

    async def _prompt_setting_edit(self, key: str, chat_id: str, msg_id: int) -> None:
        spec = SETTINGS_SPEC.get(key)
        if not spec:
            return
        cur = spec["current"]()
        text = (
            f"⚙️ <b>{spec['label']}</b>\n"
            f"Current: <b>{cur}</b>\n\n"
            f"Pick a preset or type a custom value:"
        )
        rows = []
        row: List[dict] = []
        for p in spec["presets"]:
            row.append({"text": str(p), "callback_data": f"val:{key}:{p}"})
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([
            {"text": "✏️ Custom…", "callback_data": f"type:{key}"},
            {"text": "⬅️ Back",    "callback_data": "back"},
        ])
        await self.notifier.edit_text(
            chat_id, msg_id, text, reply_markup={"inline_keyboard": rows}
        )

    async def _apply_setting(
        self,
        key: str,
        raw_value: str,
        chat_id: str,
        edit_msg_id: Optional[int] = None,
    ) -> None:
        spec = SETTINGS_SPEC.get(key)
        if not spec:
            await self.notifier.send_text(
                f"Unknown setting: {key}", chat_id=chat_id
            )
            return
        try:
            parsed = spec["parser"](raw_value)
        except ValueError:
            await self.notifier.send_text(
                f"❌ '{raw_value}' is not a valid "
                f"{spec['parser'].__name__}.",
                chat_id=chat_id,
            )
            return
        try:
            spec["apply"](parsed)
            update_env_file({spec["env"]: str(parsed)})
        except Exception as exc:
            await self.notifier.send_text(
                f"❌ Failed to apply: {exc}", chat_id=chat_id
            )
            return
        await self.notifier.send_text(
            f"✅ <b>{spec['label']}</b> set to <b>{parsed}</b>",
            chat_id=chat_id,
        )
        await self._send_settings(chat_id, edit_msg_id=edit_msg_id)

    # ── Trading control ────────────────────────────────────
    async def _cmd_go(self, args, chat_id):
        config.RUNTIME.paused = False
        await self.notifier.send_text(
            "▶️ <b>Bot running.</b> Good hunting.", chat_id=chat_id
        )

    async def _cmd_stop(self, args, chat_id):
        config.RUNTIME.paused = True
        await self.notifier.send_text(
            "⏸ <b>Bot stopped.</b> Use /go to resume.", chat_id=chat_id
        )

    async def _cmd_mode(self, args, chat_id):
        await self._mode_prompt(chat_id, None)

    async def _mode_prompt(self, chat_id: str, msg_id: Optional[int]) -> None:
        currently_dry = config.RUNTIME.dry_run
        if currently_dry:
            text = (
                "💸 <b>Switch to LIVE mode?</b>\n"
                f"{BAR}\n"
                "You are currently in 🧪 DRY-RUN.\n"
                "LIVE mode places <b>real orders</b> with real USDC.\n\n"
                "Confirm to continue."
            )
            kb = {"inline_keyboard": [[
                {"text": "💸 Yes, go LIVE", "callback_data": "mode:confirm_live"},
                {"text": "❌ Cancel",        "callback_data": "mode:cancel"},
            ]]}
        else:
            text = (
                "🧪 <b>Switch to DRY-RUN?</b>\n"
                f"{BAR}\n"
                "You are currently in 💸 LIVE.\n"
                "DRY-RUN simulates trades — no real orders."
            )
            kb = {"inline_keyboard": [[
                {"text": "🧪 Yes, DRY-RUN", "callback_data": "mode:confirm_dry"},
                {"text": "❌ Cancel",         "callback_data": "mode:cancel"},
            ]]}
        if msg_id is not None:
            await self.notifier.edit_text(chat_id, msg_id, text, reply_markup=kb)
        else:
            await self.notifier.send_text(text, chat_id=chat_id, reply_markup=kb)

    async def _set_mode(self, dry_run: bool, chat_id: str, msg_id: int) -> None:
        config.RUNTIME.dry_run = dry_run
        if self.trading_bot is not None and hasattr(self.trading_bot, "set_dry_run"):
            try:
                self.trading_bot.set_dry_run(dry_run)
            except Exception as exc:
                log.warning("set_dry_run failed: %s", exc)
        mode = "🧪 DRY-RUN" if dry_run else "💸 LIVE"
        await self.notifier.edit_text(
            chat_id, msg_id, f"✅ Mode set to <b>{mode}</b>"
        )
        await self._send_dashboard(chat_id)

    async def _cmd_pause(self, args, chat_id):
        self.risk.state.cooldown_until = time.time() + 30 * 60
        self.risk.state.cooldown_reason = "manual /pause"
        await self.notifier.send_text(
            "⏸ <b>30 min cooldown</b> applied.\n"
            "Trading will auto-resume after the cooldown.",
            chat_id=chat_id,
        )

    # ── Info commands ──────────────────────────────────────
    async def _cmd_wallet(self, args, chat_id):
        addr = config.POLYGON_PUBLIC_KEY or ""
        if not addr.startswith("0x"):
            await self.notifier.send_text(
                "No wallet set. Use <code>/setwallet 0x...</code>",
                chat_id=chat_id,
            )
            return
        try:
            usdc, pol = await fetch_balances(addr)
        except Exception as exc:
            await self.notifier.send_text(
                f"Balance fetch failed: {exc}", chat_id=chat_id
            )
            return
        text = (
            f"👛 <b>WALLET</b>\n"
            f"{BAR}\n"
            f"🔑 {wallet_link_html(addr, addr)}\n"
            f"💵 USDC: <b>${usdc:,.2f}</b> · ⛽ POL: <b>{pol:,.4f}</b>"
        )
        kb = {"inline_keyboard": [[
            {"text": "🔄 Refresh",    "callback_data": "wallet:refresh"},
            {"text": "⬅️ Dashboard", "callback_data": "nav:dashboard"},
        ]]}
        await self.notifier.send_text(text, chat_id=chat_id, reply_markup=kb)

    async def _cmd_status(self, args, chat_id):
        tb = self.trading_bot
        state = getattr(tb, "state", None) if tb else None
        if state is None or state.window is None:
            await self.notifier.send_text(
                "📡 No live window. Bot is idle or between windows.",
                chat_id=chat_id,
            )
            return
        w = state.window
        ptb = w.price_to_beat or 0.0
        btc = state.current_btc or 0.0
        delta = state.delta_pct or 0.0
        up = state.token_up_price or 0.0
        dn = state.token_down_price or 0.0
        await self.notifier.send_text(
            f"📡 <b>LIVE WINDOW</b> — {w.slug}\n"
            f"{BAR}\n"
            f"⏱ T-{w.seconds_remaining}s remaining\n"
            f"🎯 Price-to-beat: ${ptb:,.2f} ({w.price_source or 'n/a'})\n"
            f"₿ BTC now: ${btc:,.2f} (Δ{delta:+.3f}%)\n"
            f"🟢 UP ask:   ${up:.3f}\n"
            f"🔴 DOWN ask: ${dn:.3f}\n"
            f"Signal: <b>{state.signal}</b>",
            chat_id=chat_id,
        )

    async def _cmd_pnl(self, args, chat_id):
        t = self.pnl.today_stats()
        w = self.pnl.week_stats()
        m = self._month_stats()
        a = self.pnl.alltime_stats()
        sess = self.risk.state.session_pnl
        await self.notifier.send_text(
            f"💰 <b>PnL SUMMARY</b>\n"
            f"{BAR}\n"
            f"Session:   <b>{sess:+.2f}</b> {_ico(sess)}\n"
            f"Today:     {t['pnl']:+.2f} {_ico(t['pnl'])} "
            f"({t['trades']}t · WR {t['win_rate']}%)\n"
            f"This Week: {w['pnl']:+.2f} {_ico(w['pnl'])} "
            f"({w['trades']}t · WR {w['win_rate']}%)\n"
            f"30 Days:   {m['pnl']:+.2f} {_ico(m['pnl'])} "
            f"({m['trades']}t · WR {m['win_rate']}%)\n"
            f"All Time:  {a['pnl']:+.2f} {_ico(a['pnl'])} "
            f"({a['trades']}t · WR {a['win_rate']}%)\n"
            f"{BAR}\n"
            f"Balance: <b>${a['current_balance']:.2f}</b> "
            f"(ROI {a['roi_pct']:+.1f}%)",
            chat_id=chat_id,
        )

    def _month_stats(self) -> Dict[str, Any]:
        cutoff = time.time() - 30 * 86400
        trades = [
            t for t in getattr(self.pnl, "_resolved", [])
            if t.get("ts", 0) >= cutoff
        ]
        return self.pnl._stats(trades)

    async def _cmd_stats(self, args, chat_id):
        t = self.pnl.today_stats()
        a = self.pnl.alltime_stats()
        streak = self.pnl.current_streak()
        text = (
            f"📊 <b>FULL STATISTICS</b>\n"
            f"{BAR}\n"
            f"{_fmt_stats_block('Today', t)}\n"
            f"{BAR}\n"
            f"{_fmt_stats_block('All-time', a)}\n"
            f"{BAR}\n"
            f"Balance:     <b>${a['current_balance']:.2f}</b>\n"
            f"Starting:    ${a['starting_balance']:.2f}\n"
            f"ROI:         {a['roi_pct']:+.2f}%\n"
            f"Max DD:      {a['max_drawdown']:+.2f}\n"
            f"Sharpe (d):  {a['sharpe_daily']}\n"
            f"Best day:    {a['best_day']:+.2f}\n"
            f"Worst day:   {a['worst_day']:+.2f}\n"
            f"Days active: {a['days_active']}\n"
            f"Streak:      {streak}"
        )
        await self.notifier.send_text(text, chat_id=chat_id)

    async def _cmd_trades(self, args, chat_id):
        n = 10
        if args:
            try:
                n = max(1, min(20, int(args[0])))
            except ValueError:
                pass
        trades = self.pnl.recent_trades(n)
        if not trades:
            await self.notifier.send_text(
                "📋 No trades yet.", chat_id=chat_id
            )
            return
        lines = [f"📋 <b>LAST {len(trades)} TRADES</b>", BAR]
        for i, tr in enumerate(trades, 1):
            ts = datetime.fromtimestamp(
                tr.get("ts", 0), tz=timezone.utc
            ).strftime("%H:%M")
            side = tr.get("side", "?")
            short_side = "UP" if side == "UP" else "DN"
            price = tr.get("entry_price", 0)
            pnl = tr.get("pnl", 0)
            icon = "🏆" if pnl > 0 else "❌"
            rl = tr.get("reason_log", {}) or {}
            score = rl.get("score", rl.get("confidence", tr.get("confidence", 0)))
            mlink = market_link_html(tr.get("window_slug", ""))
            lines.append(
                f"{i}. {icon} {ts} {short_side} @${price:.3f} "
                f"→ <b>{pnl:+.2f}</b> | Score {score}"
                + (f" | {mlink}" if mlink else "")
            )
        await self.notifier.send_text("\n".join(lines), chat_id=chat_id)

    async def _cmd_chart(self, args, chat_id):
        try:
            from utils.chart_generator import generate_pnl_chart
        except Exception as exc:
            await self.notifier.send_text(
                f"Chart module unavailable: {exc}", chat_id=chat_id
            )
            return
        png = generate_pnl_chart(
            self.pnl.equity_curve(),
            self.pnl.daily_pnl_series(),
            self.pnl.rolling_win_rate(),
        )
        if png is None:
            await self.notifier.send_text(
                "📈 No data yet for chart.", chat_id=chat_id
            )
            return
        a = self.pnl.alltime_stats()
        caption = (
            f"📈 <b>Performance</b>\n"
            f"PnL: {a['pnl']:+.2f} · WR {a['win_rate']}% · "
            f"Trades {a['trades']}"
        )
        await self.notifier.send_photo(png, caption=caption, chat_id=chat_id)

    async def _cmd_risk(self, args, chat_id):
        snap = self.risk.snapshot()
        allowed, why = self.risk.can_trade()
        gate = "✅ open" if allowed else f"⛔ blocked ({why})"
        await self.notifier.send_text(
            f"🛡 <b>RISK STATUS</b>\n"
            f"{BAR}\n"
            f"Gate: {gate}\n"
            f"{BAR}\n"
            f"Session PnL:   {snap['session_pnl']:+.2f}\n"
            f"Daily PnL:     {snap['daily_pnl']:+.2f}\n"
            f"Trades today:  {snap['trades_today']}\n"
            f"Consec losses: {snap['consecutive_losses']}\n"
            f"Cooldown:      {snap['cooldown_remaining']}s "
            f"({snap['cooldown_reason'] or 'none'})\n"
            f"{BAR}\n"
            f"<b>Limits</b>\n"
            f"Max session loss: ${config.RUNTIME.max_session_loss:.2f}\n"
            f"Max daily loss:   ${config.MAX_DAILY_LOSS_USD:.2f}\n"
            f"Max daily trades: {config.MAX_DAILY_TRADES}\n"
            f"Max consec loss:  {config.MAX_CONSECUTIVE_LOSSES}\n"
            f"Min confidence:   {config.RUNTIME.min_confidence}",
            chat_id=chat_id,
        )

    async def _cmd_help(self, args, chat_id):
        text = (
            "❓ <b>HELP — Command List</b>\n"
            f"{BAR}\n"
            "<b>Main</b>\n"
            "/start — Dashboard with buttons\n"
            "/help  — This message\n"
            f"{BAR}\n"
            "<b>Wallet</b>\n"
            "/wallet    — Address + balances\n"
            "/setwallet — Set private key (auto-deleted)\n"
            f"{BAR}\n"
            "<b>Trading</b>\n"
            "/go    — Start auto trading\n"
            "/stop  — Stop auto trading\n"
            "/mode  — Toggle DRY-RUN / LIVE\n"
            "/pause — 30 min cooldown\n"
            f"{BAR}\n"
            "<b>Info</b>\n"
            "/pnl    — Profit / loss summary\n"
            "/stats  — Full statistics\n"
            "/trades — Recent trades + reasons\n"
            "/chart  — PnL chart image\n"
            "/risk   — Risk status + limits\n"
            "/status — Current live window\n"
            f"{BAR}\n"
            "<b>Config</b>\n"
            "/settings — Edit settings with buttons"
        )
        await self.notifier.send_text(text, chat_id=chat_id)


# ─────────────────────────────────────────────────────────────
# Sync helper — runs in a thread from /setwallet
# ─────────────────────────────────────────────────────────────

def _derive_clob_creds(private_key: str) -> Tuple[str, str, str]:
    from py_clob_client.client import ClobClient
    client = ClobClient(
        host=config.CLOB_HOST,
        key=private_key,
        chain_id=config.POLYGON_CHAIN_ID,
    )
    creds = client.create_or_derive_api_creds()
    return (
        getattr(creds, "api_key", "") or "",
        getattr(creds, "api_secret", "") or "",
        getattr(creds, "api_passphrase", "") or "",
    )
