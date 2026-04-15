"""
Central configuration for the Polymarket 5-Minute BTC Up/Down trading bot.

All secrets are loaded from a .env file (see .env.example).
All tunable trading parameters live here so the operator never has
to dig through code to change them.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Load .env from project root
PROJECT_ROOT = Path(__file__).parent.resolve()
load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


# ─────────────────────────────────────────────────────────────
# Polymarket credentials
# ─────────────────────────────────────────────────────────────
POLYGON_PRIVATE_KEY = _env("POLYGON_PRIVATE_KEY")
POLYGON_PUBLIC_KEY = _env("POLYGON_PUBLIC_KEY")
POLYMARKET_API_KEY = _env("POLYMARKET_API_KEY")
POLYMARKET_API_SECRET = _env("POLYMARKET_API_SECRET")
POLYMARKET_PASSPHRASE = _env("POLYMARKET_PASSPHRASE")

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
POLYGON_CHAIN_ID = 137
POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
POLYMARKET_RTDS_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/rtds"

# ─────────────────────────────────────────────────────────────
# Binance feed
# ─────────────────────────────────────────────────────────────
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_VOLUME_WINDOW_SECONDS = 60  # rolling window for volume classification

# ─────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")

# ─────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────
DASHBOARD_PORT = _env_int("DASHBOARD_PORT", 8081)
DASHBOARD_TOKEN = _env("DASHBOARD_TOKEN", "changeme")
STARTING_BALANCE = _env_float("STARTING_BALANCE", 200.0)

# ─────────────────────────────────────────────────────────────
# Trading parameters
# ─────────────────────────────────────────────────────────────
BASE_TRADE_SIZE_USD = _env_float("BASE_TRADE_SIZE", 5.0)
MAX_TRADE_SIZE_USD = _env_float("MAX_TRADE_SIZE", 25.0)
MIN_TRADE_SIZE_USD = 5.0

MAX_SESSION_LOSS_USD = _env_float("MAX_SESSION_LOSS", 20.0)
MAX_DAILY_LOSS_USD = _env_float("MAX_DAILY_LOSS", 30.0)
MAX_DAILY_TRADES = _env_int("MAX_DAILY_TRADES", 50)
MAX_CONSECUTIVE_LOSSES = _env_int("MAX_CONSECUTIVE_LOSSES", 4)
COOLDOWN_AFTER_LOSS_STREAK_SEC = 600
COOLDOWN_AFTER_BIG_LOSS_SEC = 900

MIN_CONFIDENCE = _env_int("MIN_CONFIDENCE", 60)
MIN_DELTA_PCT = _env_float("MIN_DELTA_PCT", 0.05)

ENTRY_WINDOW_START_SEC = _env_int("ENTRY_WINDOW_START", 60)  # T-60s begin
ENTRY_WINDOW_END_SEC = _env_int("ENTRY_WINDOW_END", 5)       # T-5s last chance

WINDOW_LENGTH_SECONDS = 300

# ─────────────────────────────────────────────────────────────
# Data files
# ─────────────────────────────────────────────────────────────
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

TRADES_FILE = DATA_DIR / "trades.json"
SESSION_FILE = DATA_DIR / "session_log.json"
DAILY_STATS_FILE = DATA_DIR / "daily_stats.json"
EQUITY_CURVE_FILE = DATA_DIR / "equity_curve.json"


@dataclass
class RuntimeFlags:
    """Mutable at runtime — updated by Telegram commands."""
    dry_run: bool = False
    paused: bool = False
    base_size_usd: float = BASE_TRADE_SIZE_USD
    max_session_loss: float = MAX_SESSION_LOSS_USD
    min_confidence: int = MIN_CONFIDENCE


RUNTIME = RuntimeFlags()


def summary() -> dict:
    """Return a sanitized settings dict safe to display/send."""
    return {
        "base_size_usd": RUNTIME.base_size_usd,
        "max_trade_size_usd": MAX_TRADE_SIZE_USD,
        "max_session_loss": RUNTIME.max_session_loss,
        "max_daily_loss": MAX_DAILY_LOSS_USD,
        "max_daily_trades": MAX_DAILY_TRADES,
        "min_confidence": RUNTIME.min_confidence,
        "min_delta_pct": MIN_DELTA_PCT,
        "entry_window": f"T-{ENTRY_WINDOW_START_SEC}s → T-{ENTRY_WINDOW_END_SEC}s",
        "starting_balance": STARTING_BALANCE,
        "dry_run": RUNTIME.dry_run,
        "paused": RUNTIME.paused,
    }
