"""
Telegram bot interface — notifications + interactive commands.

Provides:
  - Notifier: one-way messaging (send_text / send_photo / low-level helpers).
  - CommandBot: two-way long-polling bot with a full setup wizard,
    wallet onboarding (/setwallet), inline-keyboard settings editor,
    and live trading controls (/go /stop /mode /pause ...).

Dependencies are kept intentionally minimal: raw HTTP via aiohttp,
eth_account for key derivation, py_clob_client for API credential
derivation. web3 is listed in requirements for convenience but we
speak JSON-RPC directly so it is not imported here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

import config

log = logging.getLogger("telegram")


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

POLYGON_RPC = "https://polygon-rpc.com"
USDC_CONTRACT = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
ENV_PATH = Path(config.PROJECT_ROOT) / ".env"

# Settings exposed through the inline-keyboard editor.
# Each entry maps a short key → (label, env_key, type, presets, apply_fn).
#
# `apply_fn(value)` is a callable that writes the parsed value to the
# appropriate config attribute in-memory. env is persisted separately.
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


def _set_module_attr(name: str, value: Any) -> None:
    setattr(config, name, value)


# ─────────────────────────────────────────────────────────────
# .env file writer (atomic upsert)
# ─────────────────────────────────────────────────────────────

def update_env_file(updates: Dict[str, str], path: Path = ENV_PATH) -> None:
    """Upsert ``updates`` into the .env file, preserving comments/order."""
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
# Polygon RPC helpers (no web3 dependency)
# ─────────────────────────────────────────────────────────────

async def _rpc_call(session: aiohttp.ClientSession, method: str, params: list) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(POLYGON_RPC, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
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
# Notifier — thin HTTP wrapper
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
        await self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

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
        self.trading_bot = trading_bot  # set later by bot.py if needed
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
        params = {"timeout": 25, "offset": self._offset, "allowed_updates": json.dumps(["message", "callback_query"])}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as s:
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

    # ── Dispatchers ────────────────────────────────────────
    async def _handle_message(self, msg: dict) -> None:
        chat_id = str(msg["chat"]["id"])
        msg_id = msg.get("message_id")
        text = (msg.get("text") or "").strip()

        # /setwallet: delete message IMMEDIATELY for security,
        # before any parsing/logging.
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

        # If user owes a typed value for a pending setting edit, consume it.
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
            "/stop": self._cmd_stop,
            "/resume": self._cmd_go,
            "/mode": self._cmd_mode,
            "/status": self._cmd_status,
            "/pnl": self._cmd_pnl,
            "/dashboard": self._cmd_dashboard,
            "/chart": self._cmd_chart,
            "/trades": self._cmd_trades,
            "/history": self._cmd_trades,
            "/risk": self._cmd_risk,
            "/pause": self._cmd_pause,
            "/set": self._cmd_set,
            "/config": self._cmd_dashboard,
            "/stats": self._cmd_pnl,
        }
        handler = handlers.get(cmd)
        if handler is None:
            await self.notifier.send_text(
                "Unknown command.\n\n"
                "Core: /start /setwallet /wallet /go /stop /mode\n"
                "Info: /status /pnl /dashboard /chart /trades /risk\n"
                "Control: /pause /set KEY VALUE",
                chat_id=chat_id,
            )
            return
        await handler(args, chat_id)

    async def _handle_callback(self, cq: dict) -> None:
        await self.notifier.answer_callback(cq["id"])
        data = cq.get("data", "")
        msg = cq.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        msg_id = msg.get("message_id")

        if data.startswith("edit:"):
            key = data.split(":", 1)[1]
            await self._prompt_setting_edit(key, chat_id, msg_id)
        elif data.startswith("val:"):
            _, key, val = data.split(":", 2)
            await self._apply_setting(key, val, chat_id)
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
            await self._send_dashboard(chat_id, edit_msg_id=msg_id)
        elif data == "toggle_mode":
            await self._toggle_mode(chat_id)
        elif data == "toggle_run":
            await self._toggle_run(chat_id)

    # ── /setwallet flow ────────────────────────────────────
    async def _do_setwallet(self, private_key: str, chat_id: str) -> None:
        try:
            from eth_account import Account
        except ImportError:
            await self.notifier.send_text("eth_account not installed.", chat_id=chat_id)
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

        # Balances (never block on RPC failure — just report 0)
        try:
            usdc, pol = await fetch_balances(address)
        except Exception as exc:
            log.warning("balance fetch failed: %s", exc)
            usdc, pol = 0.0, 0.0

        # Derive Polymarket API credentials
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

        # Persist to .env (NEVER log the private key)
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

        # Update in-memory config
        config.POLYGON_PRIVATE_KEY = pk
        config.POLYGON_PUBLIC_KEY = address
        if api_key:
            config.POLYMARKET_API_KEY = api_key
            config.POLYMARKET_API_SECRET = api_secret
            config.POLYMARKET_PASSPHRASE = api_pass

        # Force executor re-init on next trade
        try:
            self.executor._client = None  # type: ignore[attr-defined]
        except Exception:
            pass
        if self.trading_bot is not None and hasattr(self.trading_bot, "reload_wallet"):
            try:
                self.trading_bot.reload_wallet()
            except Exception as exc:
                log.warning("reload_wallet failed: %s", exc)

        short = f"{address[:6]}...{address[-4:]}"
        text = (
            f"✅ <b>Wallet connected</b>: <code>{short}</code>\n"
            f"USDC: <b>${usdc:,.2f}</b>\n"
            f"POL:  <b>{pol:,.4f}</b>\n"
            + ("🔑 Polymarket API credentials derived.\n" if api_key else "")
            + "\nUse /start to open the dashboard."
        )
        if status_msg_id:
            await self.notifier.edit_text(chat_id, status_msg_id, text)
        else:
            await self.notifier.send_text(text, chat_id=chat_id)

    # ── /start dashboard ───────────────────────────────────
    async def _cmd_start(self, args, chat_id):
        if not config.POLYGON_PRIVATE_KEY or config.POLYGON_PRIVATE_KEY.startswith("0x_your"):
            await self._send_wizard(chat_id)
            return
        await self._send_dashboard(chat_id)

    async def _send_wizard(self, chat_id: str) -> None:
        text = (
            "👋 <b>Welcome to Polymarket 5m BTC Bot</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Setup (1 step):</b>\n"
            "Send your Polygon private key with:\n"
            "<code>/setwallet 0xYOUR_PRIVATE_KEY</code>\n\n"
            "🔒 Your message is <b>deleted immediately</b> after receipt.\n"
            "• Public address is derived on-device\n"
            "• USDC + POL balances are fetched from Polygon\n"
            "• Polymarket API credentials are auto-generated\n"
            "• Everything is saved to .env — no manual editing\n\n"
            "Your private key is <b>never</b> logged or shown in any message."
        )
        await self.notifier.send_text(text, chat_id=chat_id)

    async def _send_dashboard(self, chat_id: str, edit_msg_id: Optional[int] = None) -> None:
        status = "⏸ PAUSED" if config.RUNTIME.paused else "🟢 RUNNING"
        mode = "🧪 DRY-RUN" if config.RUNTIME.dry_run else "💸 LIVE"
        addr = config.POLYGON_PUBLIC_KEY or "(not set)"
        short = f"{addr[:6]}...{addr[-4:]}" if addr.startswith("0x") and len(addr) >= 10 else addr

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
            f"━━━━━━━━━━━━━━\n"
            f"Wallet: <code>{short}</code>\n"
            f"USDC: ${usdc:,.2f} · POL: {pol:,.4f}\n"
            f"━━━━━━━━━━━━━━\n"
            f"Session PnL: <b>{session_pnl:+.2f}</b>\n"
            f"Today: {t['pnl']:+.2f} · {t['trades']}t · WR {t['win_rate']}%\n"
            f"All-time: {a['pnl']:+.2f} · {a['trades']}t · WR {a['win_rate']}%\n"
            f"━━━━━━━━━━━━━━\n"
            f"<b>Settings</b> (tap to change):"
        )

        kb_rows = []
        for key, spec in SETTINGS_SPEC.items():
            cur = spec["current"]()
            kb_rows.append([{
                "text": f"{spec['label']}: {cur}",
                "callback_data": f"edit:{key}",
            }])
        kb_rows.append([
            {"text": "🧪 Toggle mode", "callback_data": "toggle_mode"},
            {"text": ("▶️ Start" if config.RUNTIME.paused else "⏸ Stop"),
             "callback_data": "toggle_run"},
        ])
        markup = {"inline_keyboard": kb_rows}

        if edit_msg_id is not None:
            await self.notifier.edit_text(chat_id, edit_msg_id, text, reply_markup=markup)
        else:
            await self.notifier.send_text(text, chat_id=chat_id, reply_markup=markup)

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
            {"text": "⬅️ Back", "callback_data": "back"},
        ])
        await self.notifier.edit_text(
            chat_id, msg_id, text, reply_markup={"inline_keyboard": rows}
        )

    async def _apply_setting(self, key: str, raw_value: str, chat_id: str) -> None:
        spec = SETTINGS_SPEC.get(key)
        if not spec:
            await self.notifier.send_text(f"Unknown setting: {key}", chat_id=chat_id)
            return
        try:
            parsed = spec["parser"](raw_value)
        except ValueError:
            await self.notifier.send_text(
                f"❌ '{raw_value}' is not a valid {spec['parser'].__name__}.",
                chat_id=chat_id,
            )
            return
        try:
            spec["apply"](parsed)
            update_env_file({spec["env"]: str(parsed)})
        except Exception as exc:
            await self.notifier.send_text(f"❌ Failed to apply: {exc}", chat_id=chat_id)
            return
        await self.notifier.send_text(
            f"✅ <b>{spec['label']}</b> set to <b>{parsed}</b>", chat_id=chat_id
        )
        await self._send_dashboard(chat_id)

    # ── Trading control ────────────────────────────────────
    async def _cmd_go(self, args, chat_id):
        config.RUNTIME.paused = False
        await self.notifier.send_text("▶️ Bot running. Good hunting.", chat_id=chat_id)

    async def _cmd_stop(self, args, chat_id):
        config.RUNTIME.paused = True
        await self.notifier.send_text("⏸ Bot stopped. Use /go to resume.", chat_id=chat_id)

    async def _toggle_run(self, chat_id: str) -> None:
        config.RUNTIME.paused = not config.RUNTIME.paused
        await self._send_dashboard(chat_id)

    async def _cmd_mode(self, args, chat_id):
        await self._toggle_mode(chat_id)

    async def _toggle_mode(self, chat_id: str) -> None:
        config.RUNTIME.dry_run = not config.RUNTIME.dry_run
        if self.trading_bot is not None and hasattr(self.trading_bot, "set_dry_run"):
            try:
                self.trading_bot.set_dry_run(config.RUNTIME.dry_run)
            except Exception as exc:
                log.warning("set_dry_run failed: %s", exc)
        mode = "🧪 DRY-RUN" if config.RUNTIME.dry_run else "💸 LIVE"
        await self.notifier.send_text(f"Mode: <b>{mode}</b>", chat_id=chat_id)

    async def _cmd_pause(self, args, chat_id):
        import time as _t
        self.risk.state.cooldown_until = _t.time() + 30 * 60
        self.risk.state.cooldown_reason = "manual /pause"
        await self.notifier.send_text("⏸ Cooldown 30 min applied.", chat_id=chat_id)

    # ── Info commands ──────────────────────────────────────
    async def _cmd_wallet(self, args, chat_id):
        addr = config.POLYGON_PUBLIC_KEY
        if not addr or not addr.startswith("0x"):
            await self.notifier.send_text(
                "No wallet set. Use <code>/setwallet 0x...</code>",
                chat_id=chat_id,
            )
            return
        try:
            usdc, pol = await fetch_balances(addr)
        except Exception as exc:
            await self.notifier.send_text(f"Balance fetch failed: {exc}", chat_id=chat_id)
            return
        short = f"{addr[:6]}...{addr[-4:]}"
        await self.notifier.send_text(
            f"💼 <b>Wallet</b>: <code>{short}</code>\n"
            f"USDC: <b>${usdc:,.2f}</b>\n"
            f"POL:  <b>{pol:,.4f}</b>",
            chat_id=chat_id,
        )

    async def _cmd_status(self, args, chat_id):
        tb = self.trading_bot
        state = getattr(tb, "state", None) if tb else None
        if state is None or state.window is None:
            await self.notifier.send_text(
                "No live window. Bot idle or between windows.", chat_id=chat_id
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
            f"T-{w.seconds_remaining}s remaining\n"
            f"Price-to-beat: ${ptb:,.2f} ({w.price_source or 'n/a'})\n"
            f"BTC now: ${btc:,.2f} (Δ{delta:+.3f}%)\n"
            f"UP ask: ${up:.3f} · DOWN ask: ${dn:.3f}\n"
            f"Signal: <b>{state.signal}</b>",
            chat_id=chat_id,
        )

    async def _cmd_pnl(self, args, chat_id):
        t = self.pnl.today_stats()
        w = self.pnl.week_stats()
        a = self.pnl.alltime_stats()
        sess = self.risk.state.session_pnl
        def ico(x): return "📈" if x >= 0 else "📉"
        await self.notifier.send_text(
            f"💰 <b>PNL</b>\n"
            f"Session:   {sess:+.2f} {ico(sess)}\n"
            f"Today:     {t['pnl']:+.2f} {ico(t['pnl'])} ({t['trades']}t)\n"
            f"This Week: {w['pnl']:+.2f} {ico(w['pnl'])}\n"
            f"All Time:  {a['pnl']:+.2f} {ico(a['pnl'])}",
            chat_id=chat_id,
        )

    async def _cmd_dashboard(self, args, chat_id):
        await self._send_dashboard(chat_id)

    async def _cmd_chart(self, args, chat_id):
        from utils.chart_generator import generate_pnl_chart
        png = generate_pnl_chart(
            self.pnl.equity_curve(),
            self.pnl.daily_pnl_series(),
            self.pnl.rolling_win_rate(),
        )
        if png is None:
            await self.notifier.send_text("No data yet for chart.", chat_id=chat_id)
            return
        await self.notifier.send_photo(png, caption="📈 Performance chart")

    async def _cmd_trades(self, args, chat_id):
        n = 5
        if args:
            try:
                n = max(1, min(20, int(args[0])))
            except ValueError:
                pass
        trades = self.pnl.recent_trades(n)
        if not trades:
            await self.notifier.send_text("No trades yet.", chat_id=chat_id)
            return
        lines = [f"📋 <b>LAST {len(trades)} TRADES</b>"]
        for i, tr in enumerate(trades, 1):
            ts = datetime.utcfromtimestamp(tr.get("ts", 0)).strftime("%H:%M")
            side = tr.get("side", "?")
            price = tr.get("entry_price", 0)
            pnl = tr.get("pnl", 0)
            icon = "🏆" if pnl > 0 else "❌"
            rl = tr.get("reason_log", {}) or {}
            delta = rl.get("delta_pct", 0)
            score = rl.get("score", rl.get("confidence", 0))
            trend = rl.get("delta_trend", "?")
            vol = rl.get("binance_volume", "?")
            lines.append(
                f"{i}. {icon} {ts} — {side} @ ${price:.3f} → {pnl:+.2f}\n"
                f"   Δ{delta:+.3f}% · score {score} · {trend}/{vol}"
            )
        await self.notifier.send_text("\n".join(lines), chat_id=chat_id)

    async def _cmd_risk(self, args, chat_id):
        snap = self.risk.snapshot()
        allowed, why = self.risk.can_trade()
        gate = "✅ open" if allowed else f"⛔ blocked ({why})"
        await self.notifier.send_text(
            f"🛡 <b>RISK</b>\n"
            f"Gate: {gate}\n"
            f"Session PnL: {snap['session_pnl']:+.2f}\n"
            f"Daily PnL:   {snap['daily_pnl']:+.2f}\n"
            f"Trades today: {snap['trades_today']}\n"
            f"Consec losses: {snap['consecutive_losses']}\n"
            f"Cooldown: {snap['cooldown_remaining']}s ({snap['cooldown_reason'] or 'none'})",
            chat_id=chat_id,
        )

    async def _cmd_set(self, args, chat_id):
        if len(args) < 2:
            keys = ", ".join(SETTINGS_SPEC.keys())
            await self.notifier.send_text(
                f"Usage: <code>/set KEY VALUE</code>\nKeys: {keys}",
                chat_id=chat_id,
            )
            return
        await self._apply_setting(args[0], args[1], chat_id)


# ─────────────────────────────────────────────────────────────
# Sync helper — runs in a thread from /setwallet
# ─────────────────────────────────────────────────────────────

def _derive_clob_creds(private_key: str) -> Tuple[str, str, str]:
    from py_clob_client.client import ClobClient
    client = ClobClient(host=config.CLOB_HOST, key=private_key, chain_id=config.POLYGON_CHAIN_ID)
    creds = client.create_or_derive_api_creds()
    return (
        getattr(creds, "api_key", "") or "",
        getattr(creds, "api_secret", "") or "",
        getattr(creds, "api_passphrase", "") or "",
    )
