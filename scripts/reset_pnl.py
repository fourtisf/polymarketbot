#!/usr/bin/env python3
"""
Reset PnL tracker data after phantom trade detection.
Keeps a backup of the old data, then resets to current on-chain balance.

Run on VPS: venv/bin/python3 scripts/reset_pnl.py
"""

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

DATA_DIR = Path(__file__).parent.parent / "data"


def main():
    print("=" * 60)
    print("PnL DATA RESET")
    print("=" * 60)

    # Create backup
    backup_dir = DATA_DIR / "backup_phantom_pnl"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    files_to_reset = [
        "equity_curve.json",
        "trades.json",
        "session_log.json",
        "daily_stats.json",
    ]

    for fname in files_to_reset:
        fpath = DATA_DIR / fname
        if fpath.exists():
            backup_path = backup_dir / f"{fname}.{ts}.bak"
            shutil.copy2(fpath, backup_path)
            print(f"  Backed up: {fname} -> {backup_path.name}")

            # Read and show summary before reset
            try:
                data = json.loads(fpath.read_text() or "[]")
                if isinstance(data, list):
                    print(f"    Had {len(data)} records")
                elif isinstance(data, dict):
                    print(f"    Had {len(data)} keys")
            except Exception:
                pass
        else:
            print(f"  {fname} does not exist (nothing to reset)")

    # Reset files
    print()
    print("Resetting PnL data...")

    # equity_curve.json -> empty list
    (DATA_DIR / "equity_curve.json").write_text("[]")
    print("  equity_curve.json -> []")

    # trades.json -> empty list
    (DATA_DIR / "trades.json").write_text("[]")
    print("  trades.json -> []")

    # session_log.json -> fresh session with current on-chain balance
    # Use 127.58 as starting balance (current on-chain reality)
    session_data = {
        "start_balance": 127.58,
        "started": int(datetime.now(timezone.utc).timestamp()),
        "reset_reason": "phantom_pnl_detected",
        "reset_ts": datetime.now(timezone.utc).isoformat(),
    }
    (DATA_DIR / "session_log.json").write_text(json.dumps(session_data, indent=2))
    print(f"  session_log.json -> start_balance=$127.58")

    # daily_stats.json -> empty
    (DATA_DIR / "daily_stats.json").write_text("{}")
    print("  daily_stats.json -> {}")

    print()
    print("IMPORTANT: Also update STARTING_BALANCE in .env to 127.58")
    print("  echo 'STARTING_BALANCE=127.58' >> .env")
    print()
    print("Then restart the bot:")
    print("  pm2 restart polymarket-bot")
    print()
    print(f"Backups saved to: {backup_dir}/")
    print("=" * 60)
    print("DONE")


if __name__ == "__main__":
    main()
