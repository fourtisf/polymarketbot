"""Backtest CLI for the Polymarket BTC 5-minute strategy.

Two subcommands:

  replay     Load data/trades.json, pair entry+settled records, print a
             bucketed win-rate / EV report. No network access.

  simulate   Fetch Binance 1s klines for the given UTC date range and
             replay strategy.decide() over each 5-minute window. Reports
             the raw signal edge at a synthetic token price.

Examples:
  python3 scripts/backtest.py replay
  python3 scripts/backtest.py replay --trades-file data/trades.json
  python3 scripts/backtest.py simulate --since 2026-04-23 --until 2026-04-30
  python3 scripts/backtest.py simulate --since 2026-04-29 --until 2026-04-30 \\
      --token-price 0.55
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python3 scripts/backtest.py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core.backtest.replay import (  # noqa: E402
    format_report,
    load_paired_trades,
)
from core.backtest.binance_history import fetch_range  # noqa: E402
from core.backtest.simulate import (  # noqa: E402
    format_simulation_report,
    run_simulation,
)


def _parse_date(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid date {s!r} (expected YYYY-MM-DD)"
        ) from e


def cmd_replay(args: argparse.Namespace) -> int:
    path = Path(args.trades_file) if args.trades_file else config.TRADES_FILE
    trades = load_paired_trades(path)
    print(format_report(trades))
    return 0


async def _run_simulate(args: argparse.Namespace) -> int:
    klines = await fetch_range(args.since, args.until,
                               interval=args.interval,
                               use_cache=not args.no_cache)
    if not klines:
        print("No klines returned for the requested range.", file=sys.stderr)
        return 1
    print(f"Fetched {len(klines):,} klines "
          f"({args.since.date()} → {args.until.date()})")
    results, summary = run_simulation(klines, token_price=args.token_price)
    print(format_simulation_report(results, summary,
                                   token_price=args.token_price))
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    return asyncio.run(_run_simulate(args))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="backtest")
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("replay", help="analyze data/trades.json")
    rp.add_argument("--trades-file", default=None,
                    help="path to trades.json (default: %s)" % config.TRADES_FILE)
    rp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("simulate",
                        help="historical Binance kline simulation")
    sp.add_argument("--since", required=True, type=_parse_date,
                    help="UTC start date (YYYY-MM-DD)")
    sp.add_argument("--until", required=True, type=_parse_date,
                    help="UTC end date, exclusive (YYYY-MM-DD)")
    sp.add_argument("--interval", default="1s",
                    choices=["1s", "1m"],
                    help="kline interval (default: 1s)")
    sp.add_argument("--token-price", type=float, default=0.50,
                    help="synthetic token price for the price gate "
                         "(default: 0.50)")
    sp.add_argument("--no-cache", action="store_true",
                    help="bypass on-disk kline cache")
    sp.set_defaults(func=cmd_simulate)
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    rc = args.func(args)
    sys.exit(rc)


if __name__ == "__main__":
    main()
