"""
Structured trade logging with full reasoning.

Every trade (filled OR skipped) is persisted as a JSON line in
data/trades.json. Each record contains the full TradeContext + score
breakdown + outcome so we can audit the bot's decisions after the fact.
"""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger for both console and file output."""
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-12s %(message)s")
    # Console
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    # File
    fh = logging.FileHandler(config.LOG_DIR / "bot.log")
    fh.setFormatter(fmt)
    root.addHandler(fh)


class TradeLogger:
    """Append-only JSON log of trade decisions + outcomes."""

    def __init__(self, path: Path = None):
        self.path = path or config.TRADES_FILE
        if not self.path.exists():
            self.path.write_text("[]")

    def _load(self) -> List[Dict[str, Any]]:
        try:
            return json.loads(self.path.read_text() or "[]")
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def _save(self, records: List[Dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(records, indent=2, default=str))

    def log_trade(self, record: Dict[str, Any]) -> None:
        record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        record.setdefault("ts", int(time.time()))
        records = self._load()
        records.append(record)
        # Cap storage
        if len(records) > 5000:
            records = records[-5000:]
        self._save(records)

    def recent(self, n: int = 20) -> List[Dict[str, Any]]:
        return self._load()[-n:][::-1]

    def all(self) -> List[Dict[str, Any]]:
        return self._load()


def format_trade_reason(record: Dict[str, Any]) -> str:
    """Render a trade record as a human-readable block for Telegram/logs."""
    lines = ["TRADE REASON LOG:"]
    lines.append(f"├── window: {record.get('window_slug', '?')}")
    lines.append(f"├── price_to_beat: ${record.get('price_to_beat', 0):,.2f}")
    lines.append(f"├── current_btc:   ${record.get('current_btc', 0):,.2f}")
    lines.append(f"├── delta:         {record.get('delta_pct', 0):+.4f}%")
    lines.append(f"├── trend:         {record.get('delta_trend', '?')}")
    lines.append(f"├── volume:        {record.get('binance_volume', '?')}")
    lines.append(f"├── time_left:     {record.get('seconds_remaining', '?')}s")
    lines.append(f"├── target:        {record.get('target_side', '?')}")
    lines.append(f"├── token_price:   ${record.get('token_price', 0):.3f}")
    lines.append(f"├── score:         {record.get('score', 0)}/100")
    action = record.get("action", "SKIP")
    lines.append(f"└── DECISION:     {action}")
    return "\n".join(lines)
