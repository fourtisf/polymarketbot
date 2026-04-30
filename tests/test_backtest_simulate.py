"""Unit tests for core.backtest.simulate.

The simulator is exercised end-to-end with synthetic 1-second klines so
we don't need a live Binance connection.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("POLYGON_PRIVATE_KEY", "")
os.environ.setdefault("POLYGON_PUBLIC_KEY", "")

import config  # noqa: E402
from core.backtest.binance_history import Kline  # noqa: E402
from core.backtest.simulate import (  # noqa: E402
    run_simulation,
    simulate_window,
    summarize,
)


def _build_klines(window_start_s: int, deltas_per_second: list,
                  base_price: float = 100000.0) -> list:
    """Build N synthetic 1s klines whose `close` follows `base_price + delta`.

    Each kline volume = 1.0 BTC (so trend/volume classifiers stay neutral).
    """
    out = []
    price = base_price
    for i, d in enumerate(deltas_per_second):
        new_price = base_price + d
        open_price = price
        close_price = new_price
        high = max(open_price, close_price)
        low = min(open_price, close_price)
        ts_ms = (window_start_s + i) * 1000
        out.append(Kline(
            open_time=ts_ms,
            close_time=ts_ms + 999,
            open=open_price,
            high=high,
            low=low,
            close=close_price,
            volume=1.0,
        ))
        price = new_price
    return out


class SimulateWindowTests(unittest.TestCase):
    def test_window_with_strong_uptrend_enters_up_and_wins(self) -> None:
        # 5min = 300s. Build a window where BTC drifts steadily up by $200
        # over the window — so resolution is UP.
        ws = 1700000000 - (1700000000 % 300)
        # Linear ramp: 0 .. 200 across 300 seconds
        deltas = [i * (200.0 / 300.0) for i in range(300)]
        klines = _build_klines(ws, deltas)
        result = simulate_window(klines, token_price=0.50,
                                 historical_volumes=[60.0])
        self.assertIsNotNone(result)
        self.assertEqual(result.resolution, "UP")
        # Should have entered UP at some second in the late window.
        self.assertEqual(result.side, "UP")
        self.assertTrue(result.win)
        self.assertGreater(result.confidence, 0)
        self.assertGreater(result.delta_at_entry, 0)
        # Token at 0.50, win → +0.50 per dollar
        self.assertAlmostEqual(result.pnl_per_dollar, 0.50, places=3)

    def test_window_with_strong_downtrend_enters_down_and_wins(self) -> None:
        ws = 1700000000 - (1700000000 % 300)
        deltas = [-i * (200.0 / 300.0) for i in range(300)]
        klines = _build_klines(ws, deltas)
        result = simulate_window(klines, token_price=0.50,
                                 historical_volumes=[60.0])
        self.assertIsNotNone(result)
        self.assertEqual(result.resolution, "DOWN")
        self.assertEqual(result.side, "DOWN")
        self.assertTrue(result.win)

    def test_flat_window_does_not_enter(self) -> None:
        ws = 1700000000 - (1700000000 % 300)
        # Tiny noise (well below 0.08% delta floor at 100k = $80)
        deltas = [0.5 if i % 2 else -0.5 for i in range(300)]
        klines = _build_klines(ws, deltas)
        result = simulate_window(klines, token_price=0.50,
                                 historical_volumes=[60.0])
        self.assertIsNotNone(result)
        self.assertIsNone(result.side)
        self.assertEqual(result.decision, "SKIP")
        self.assertIsNone(result.win)

    def test_too_few_klines_returns_none(self) -> None:
        ws = 1700000000 - (1700000000 % 300)
        klines = _build_klines(ws, [0.0] * 30)  # only 30s of data
        self.assertIsNone(simulate_window(klines, 0.50, [60.0]))


class RunSimulationTests(unittest.TestCase):
    def test_run_simulation_groups_windows_and_summarizes(self) -> None:
        # Build two consecutive 300s windows, both uptrending.
        ws1 = 1700000000 - (1700000000 % 300)
        ws2 = ws1 + 300
        deltas1 = [i * 0.7 for i in range(300)]   # up
        deltas2 = [-i * 0.7 for i in range(300)]  # down
        all_klines = _build_klines(ws1, deltas1) + _build_klines(ws2, deltas2)

        results, summary = run_simulation(all_klines, token_price=0.50)
        self.assertEqual(summary.n_windows, 2)
        # Both windows had clear directional moves → both entered, both won.
        self.assertEqual(summary.n_entries, 2)
        self.assertEqual(summary.wins, 2)
        self.assertAlmostEqual(summary.win_rate, 1.0)
        self.assertAlmostEqual(summary.sum_pnl_per_dollar, 1.0, places=3)


class SummarizeTests(unittest.TestCase):
    def test_summarize_counts_only_entered_results(self) -> None:
        from core.backtest.simulate import SimResult
        results = [
            SimResult(window_start=0, window_end=300, price_to_beat=100,
                      close_price=101, resolution="UP", decision="SKIP"),
            SimResult(window_start=300, window_end=600, price_to_beat=100,
                      close_price=101, resolution="UP", decision="BUY_UP",
                      side="UP", confidence=80, delta_at_entry=0.15,
                      seconds_at_entry=20, trend_at_entry="consistent",
                      volume_at_entry="normal", token_price=0.50,
                      win=True, pnl_per_dollar=0.50),
            SimResult(window_start=600, window_end=900, price_to_beat=100,
                      close_price=99, resolution="DOWN", decision="BUY_UP",
                      side="UP", confidence=75, delta_at_entry=0.10,
                      seconds_at_entry=15, trend_at_entry="consistent",
                      volume_at_entry="normal", token_price=0.50,
                      win=False, pnl_per_dollar=-0.50),
        ]
        s = summarize(results)
        self.assertEqual(s.n_windows, 3)
        self.assertEqual(s.n_entries, 2)
        self.assertEqual(s.wins, 1)
        self.assertEqual(s.losses, 1)
        self.assertAlmostEqual(s.sum_pnl_per_dollar, 0.0)


if __name__ == "__main__":
    unittest.main()
