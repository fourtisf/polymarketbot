"""Backtest harness for the BTC 5-minute strategy.

Two modes:
  - replay:    load data/trades.json, pair entry + settled records,
               compute bucketed win-rate and EV. No external network.
  - simulate:  fetch Binance 1s historical klines, regenerate windows,
               replay strategy.decide() over each window, score the
               raw signal edge (no Polymarket book model).

Use scripts/backtest.py for the CLI.
"""

from core.backtest.replay import (
    PairedTrade,
    BucketStats,
    load_paired_trades,
    bucket_metrics,
    format_report,
)

__all__ = [
    "PairedTrade",
    "BucketStats",
    "load_paired_trades",
    "bucket_metrics",
    "format_report",
]
