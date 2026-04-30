"""Unit tests for core.backtest.replay.

These tests use a synthetic trades.json fixture written to a temp path so
they don't touch the live data directory.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# .env loading would normally pollute import; replace POLYGON_PRIVATE_KEY etc.
# with empty strings so config import doesn't try to do anything fancy.
os.environ.setdefault("POLYGON_PRIVATE_KEY", "")
os.environ.setdefault("POLYGON_PUBLIC_KEY", "")

from core.backtest.replay import (  # noqa: E402
    BucketStats,
    DELTA_LABELS,
    PRICE_LABELS,
    SCORE_LABELS,
    SECONDS_LABELS,
    bucket_metrics,
    format_report,
    load_paired_trades,
)


def _entry(slug: str, side: str, entry_price: float, confidence: int,
           delta_pct: float, seconds: int, trend: str = "consistent",
           volume: str = "normal", shares: float = 100.0,
           ts: int = 1700000000) -> dict:
    cost = round(entry_price * shares, 2)
    return {
        "phase": "entry",
        "window_slug": slug,
        "ts": ts,
        "action": f"BUY_{side}",
        "side": side,
        "entry_price": entry_price,
        "shares": shares,
        "cost": cost,
        "confidence": confidence,
        "reason_log": {
            "delta_pct": delta_pct,
            "delta_trend": trend,
            "binance_volume": volume,
            "seconds_remaining": seconds,
            "token_price": entry_price,
            "score": confidence,
            "target_side": side,
        },
    }


def _settled(slug: str, side: str, entry_price: float, shares: float,
             win: bool, ts: int = 1700000300) -> dict:
    pnl = round((1.0 - entry_price) * shares, 2) if win \
        else round(-entry_price * shares, 2)
    return {
        "phase": "settled",
        "window_slug": slug,
        "ts": ts,
        "side": side,
        "entry_price": entry_price,
        "shares": shares,
        "outcome": "win" if win else "loss",
        "pnl": pnl,
        "close_price": 100000.0,
        "resolution": side if win else ("DOWN" if side == "UP" else "UP"),
        "verified_on_chain": True,
    }


class LoadPairedTradesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        )
        self.path = Path(self.tmp.name)
        self.tmp.close()

    def tearDown(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def _write(self, records) -> None:
        self.path.write_text(json.dumps(records))

    def test_pairs_entry_with_settled(self) -> None:
        self._write([
            _entry("w1", "UP", 0.45, 80, 0.15, 18),
            _settled("w1", "UP", 0.45, 100, win=True),
        ])
        trades = load_paired_trades(self.path)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t.side, "UP")
        self.assertEqual(t.outcome, "win")
        self.assertAlmostEqual(t.pnl, 55.0, places=2)

    def test_drops_orphan_entry_without_settled(self) -> None:
        self._write([_entry("w1", "UP", 0.45, 80, 0.15, 18)])
        self.assertEqual(load_paired_trades(self.path), [])

    def test_drops_orphan_settled_without_entry(self) -> None:
        self._write([_settled("w1", "UP", 0.45, 100, win=True)])
        self.assertEqual(load_paired_trades(self.path), [])

    def test_drops_phantom(self) -> None:
        self._write([
            _entry("w1", "UP", 0.45, 80, 0.15, 18),
            {**_settled("w1", "UP", 0.45, 100, win=True),
             "phantom": True, "outcome": "phantom"},
        ])
        self.assertEqual(load_paired_trades(self.path), [])

    def test_skips_skip_records(self) -> None:
        self._write([
            {"phase": "entry", "window_slug": "w0", "action": "SKIP",
             "side": "SKIP"},
            _entry("w1", "UP", 0.45, 80, 0.15, 18),
            _settled("w1", "UP", 0.45, 100, win=True),
        ])
        trades = load_paired_trades(self.path)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].window_slug, "w1")

    def test_handles_missing_file(self) -> None:
        self.assertEqual(load_paired_trades(Path("/no/such/path")), [])

    def test_handles_corrupt_json(self) -> None:
        self.path.write_text("{not json")
        self.assertEqual(load_paired_trades(self.path), [])


class BucketStatsTests(unittest.TestCase):
    def test_win_rate_breakeven_and_edge(self) -> None:
        bs = BucketStats(label="x")
        for w in (True, True, True, False, False):
            t = type("T", (), {})()
            t.cost = 50.0
            t.entry_price = 0.50
            t.confidence = 70
            t.outcome = "win" if w else "loss"
            t.is_win = w
            t.pnl = 50.0 if w else -50.0
            bs.add(t)
        self.assertEqual(bs.n, 5)
        self.assertEqual(bs.wins, 3)
        self.assertEqual(bs.losses, 2)
        self.assertAlmostEqual(bs.win_rate, 0.6)
        self.assertAlmostEqual(bs.breakeven_win_rate, 0.5)
        self.assertAlmostEqual(bs.edge, 0.1, places=6)
        self.assertAlmostEqual(bs.sum_pnl, 50.0)

    def test_roi_zero_when_no_cost(self) -> None:
        bs = BucketStats(label="empty")
        self.assertEqual(bs.roi, 0.0)


class BucketMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        )
        self.path = Path(self.tmp.name)
        self.tmp.close()

    def tearDown(self) -> None:
        if self.path.exists():
            self.path.unlink()

    def test_buckets_deltas_correctly(self) -> None:
        records = []
        # Three trades, each at a distinct delta tier
        cases = [
            ("w1", 0.05, True),   # below floor
            ("w2", 0.10, True),   # 0.08-0.12
            ("w3", 0.20, False),  # 0.18-0.25
        ]
        for slug, d, win in cases:
            records.append(_entry(slug, "UP", 0.45, 80, d, 18))
            records.append(_settled(slug, "UP", 0.45, 100, win=win))
        self.path.write_text(json.dumps(records))
        trades = load_paired_trades(self.path)
        m = bucket_metrics(trades)
        self.assertEqual(m["delta_pct"]["<0.08"].n, 1)
        self.assertEqual(m["delta_pct"]["0.08-0.12"].n, 1)
        self.assertEqual(m["delta_pct"]["0.18-0.25"].n, 1)

    def test_buckets_seconds_remaining(self) -> None:
        records = []
        for i, sec in enumerate([8, 12, 20, 28, 40]):
            slug = f"w{i}"
            records.append(_entry(slug, "UP", 0.45, 80, 0.15, sec))
            records.append(_settled(slug, "UP", 0.45, 100, win=True))
        self.path.write_text(json.dumps(records))
        trades = load_paired_trades(self.path)
        m = bucket_metrics(trades)
        self.assertEqual({k: v.n for k, v in m["seconds_remaining"].items()
                          if v.n > 0},
                         {"<=10": 1, "11-15": 1, "16-22": 1,
                          "23-30": 1, ">30": 1})

    def test_format_report_contains_overall_and_no_data_for_empty(self) -> None:
        # Empty
        self.assertIn("No paired", format_report([]))

        # Non-empty
        self.path.write_text(json.dumps([
            _entry("w1", "UP", 0.40, 70, 0.15, 18),
            _settled("w1", "UP", 0.40, 100, win=True),
            _entry("w2", "DOWN", 0.55, 80, -0.20, 12, trend="consistent"),
            _settled("w2", "DOWN", 0.55, 100, win=False),
        ]))
        trades = load_paired_trades(self.path)
        report = format_report(trades)
        self.assertIn("Overall", report)
        self.assertIn("By |delta_pct|", report)
        self.assertIn("By confidence", report)
        self.assertIn("By side", report)
        # Two trades, one win, one loss → win rate 50%
        self.assertIn("W/L=  1/  1", report)


if __name__ == "__main__":
    unittest.main()
