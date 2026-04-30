"""Unit tests for multi-factor strategy signals.

Covers:
  - Volatility regime gate (skip dead-vol windows)
  - Wide-spread regime gate (skip thin books)
  - Book-imbalance bonus (aligned imbalance lifts score, opposed depresses)
  - Volume z-score bonus
  - Backwards compatibility (calls without new fields still work)
  - Fill-rate counters in Executor
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("POLYGON_PRIVATE_KEY", "")
os.environ.setdefault("POLYGON_PUBLIC_KEY", "")

from core import strategy  # noqa: E402
from core.execution import Executor, FillResult  # noqa: E402


def _ctx(**overrides) -> strategy.TradeContext:
    """Build a TradeContext with sensible defaults; override anything."""
    base = dict(
        window_slug="t-1",
        price_to_beat=100_000.0,
        current_btc=100_180.0,
        delta_pct=0.18,                 # well above MIN_DELTA_HARD
        delta_trend="consistent",
        binance_volume="normal",
        seconds_remaining=18,
        token_up_price=0.45,
        token_down_price=0.55,
        realized_vol_pct=0.10,          # healthy vol
        volume_zscore=0.0,
        book_imbalance_up=None,
        book_imbalance_down=None,
        spread_pct_up=None,
        spread_pct_down=None,
    )
    base.update(overrides)
    return strategy.TradeContext(**base)


class RegimeGateTests(unittest.TestCase):
    def test_dead_vol_regime_skips(self):
        decision = strategy.decide(_ctx(
            realized_vol_pct=0.005  # below MIN_REALIZED_VOL_PCT (0.015)
        ))
        self.assertEqual(decision.action, "SKIP")
        self.assertEqual(decision.reason_log["skip_reason"], "regime_dead_vol")

    def test_healthy_vol_does_not_skip_for_regime(self):
        decision = strategy.decide(_ctx(realized_vol_pct=0.10))
        # May still SKIP for other reasons (eg score) but not for regime
        if decision.action == "SKIP":
            self.assertNotEqual(decision.reason_log.get("skip_reason"),
                                "regime_dead_vol")

    def test_missing_vol_does_not_skip(self):
        # Backwards compat: legacy callers don't supply realized_vol
        decision = strategy.decide(_ctx(realized_vol_pct=None))
        if decision.action == "SKIP":
            self.assertNotEqual(decision.reason_log.get("skip_reason"),
                                "regime_dead_vol")

    def test_wide_spread_skips_target_side(self):
        decision = strategy.decide(_ctx(
            delta_pct=0.18,            # → UP target
            spread_pct_up=0.30,        # 30% spread > 20% cap
        ))
        self.assertEqual(decision.action, "SKIP")
        self.assertEqual(decision.reason_log["skip_reason"], "regime_wide_spread")

    def test_wide_spread_on_other_side_does_not_skip(self):
        # Wide spread on DOWN should not block UP entry
        decision = strategy.decide(_ctx(
            delta_pct=0.18,            # → UP target
            spread_pct_up=0.05,        # narrow ✓
            spread_pct_down=0.40,      # wide on DOWN, irrelevant
        ))
        if decision.action == "SKIP":
            self.assertNotEqual(decision.reason_log.get("skip_reason"),
                                "regime_wide_spread")


class BookImbalanceTests(unittest.TestCase):
    def test_aligned_imbalance_bumps_score(self):
        s_no_imb, _ = strategy.calculate_confidence(
            delta_pct=0.18, seconds_remaining=18,
            delta_trend="consistent", binance_volume="normal",
            token_price=0.45,
        )
        s_aligned, reasons = strategy.calculate_confidence(
            delta_pct=0.18, seconds_remaining=18,
            delta_trend="consistent", binance_volume="normal",
            token_price=0.45,
            book_imbalance=0.30,  # aligned buyers
        )
        self.assertGreater(s_aligned, s_no_imb)
        self.assertTrue(any("aligned" in r for r in reasons))

    def test_opposed_imbalance_drops_score(self):
        s_no_imb, _ = strategy.calculate_confidence(
            delta_pct=0.18, seconds_remaining=18,
            delta_trend="consistent", binance_volume="normal",
            token_price=0.45,
        )
        s_opposed, _ = strategy.calculate_confidence(
            delta_pct=0.18, seconds_remaining=18,
            delta_trend="consistent", binance_volume="normal",
            token_price=0.45,
            book_imbalance=-0.30,  # sellers stronger on the side we're buying
        )
        self.assertLess(s_opposed, s_no_imb)

    def test_neutral_imbalance_does_not_change_score(self):
        s_no_imb, _ = strategy.calculate_confidence(
            delta_pct=0.18, seconds_remaining=18,
            delta_trend="consistent", binance_volume="normal",
            token_price=0.45,
        )
        s_neutral, _ = strategy.calculate_confidence(
            delta_pct=0.18, seconds_remaining=18,
            delta_trend="consistent", binance_volume="normal",
            token_price=0.45,
            book_imbalance=0.05,  # below threshold → neutral
        )
        self.assertEqual(s_no_imb, s_neutral)


class VolumeZScoreTests(unittest.TestCase):
    def test_high_z_bumps_score(self):
        s_neutral, _ = strategy.calculate_confidence(
            delta_pct=0.18, seconds_remaining=18,
            delta_trend="consistent", binance_volume="normal",
            token_price=0.45, volume_zscore=0.0,
        )
        s_spike, _ = strategy.calculate_confidence(
            delta_pct=0.18, seconds_remaining=18,
            delta_trend="consistent", binance_volume="normal",
            token_price=0.45, volume_zscore=2.0,
        )
        self.assertGreater(s_spike, s_neutral)

    def test_negative_z_drops_score(self):
        s_neutral, _ = strategy.calculate_confidence(
            delta_pct=0.18, seconds_remaining=18,
            delta_trend="consistent", binance_volume="normal",
            token_price=0.45, volume_zscore=0.0,
        )
        s_quiet, _ = strategy.calculate_confidence(
            delta_pct=0.18, seconds_remaining=18,
            delta_trend="consistent", binance_volume="normal",
            token_price=0.45, volume_zscore=-1.5,
        )
        self.assertLess(s_quiet, s_neutral)


class FillRateMetricsTests(unittest.TestCase):
    def test_counters_start_at_zero(self):
        exe = Executor(dry_run=True)
        snap = exe.execution_snapshot()
        self.assertEqual(snap["placed"], 0)
        self.assertEqual(snap["filled"], 0)
        self.assertEqual(snap["fill_rate"], 0.0)

    def test_dry_run_increments_both_counters(self):
        # Seed random so the probabilistic dry-run ladder always fills
        # at the first step in this test.
        import random as _r
        _r.seed(0)
        exe = Executor(dry_run=True)
        asyncio.run(exe.place_limit_buy(
            token_id="tok", price=0.45, size_usd=5.0, confidence=80))
        snap = exe.execution_snapshot()
        self.assertEqual(snap["placed"], 1)
        self.assertEqual(snap["filled"], 1)
        self.assertAlmostEqual(snap["fill_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
