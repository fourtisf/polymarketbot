"""Unit tests for core.execution.place_limit_buy ladder.

We don't have a real CLOB to talk to, so we patch Executor's internal
methods directly. The goal is to verify the ladder logic — price
progression, fast-fail on balance errors, cap enforcement, fill on
early attempt — without exercising the network code.
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

from core.execution import (  # noqa: E402
    EXECUTION_MAX_PRICE,
    Executor,
    FillResult,
    LADDER_STEPS,
)


class _StubExecutor(Executor):
    """Executor with the network-touching helpers replaced by stubs."""

    def __init__(self, post_results, poll_results=None):
        super().__init__(dry_run=False)
        self.post_results = list(post_results)
        self.poll_results = list(poll_results or [])
        self.posts: list = []
        self.cancels: list = []
        self.polls: list = []

    async def _try_post(self, token_id, price, shares):  # type: ignore[override]
        self.posts.append((token_id, price, shares))
        if not self.post_results:
            return FillResult(success=False, error="no stubbed result")
        return self.post_results.pop(0)

    async def _poll_order_fills(self, order_id, price, timeout):  # type: ignore[override]
        self.polls.append((order_id, price, timeout))
        if self.poll_results:
            return self.poll_results.pop(0)
        return FillResult(success=False, order_id=order_id)

    async def _cancel(self, order_id):  # type: ignore[override]
        self.cancels.append(order_id)


def _filled(price: float, shares: float = 10.0,
            order_id: str = "ord-x") -> FillResult:
    return FillResult(success=True, order_id=order_id,
                      filled_shares=shares, avg_price=price)


def _live(order_id: str = "ord-x") -> FillResult:
    """Order accepted by CLOB but no fill yet."""
    return FillResult(success=True, order_id=order_id,
                      filled_shares=0.0, avg_price=0.0)


def _err(msg: str) -> FillResult:
    return FillResult(success=False, error=msg)


class LadderTests(unittest.TestCase):
    def _run(self, exe, **kwargs):
        return asyncio.run(exe.place_limit_buy(
            token_id=kwargs.get("token_id", "tok-1"),
            price=kwargs.get("price", 0.45),
            size_usd=kwargs.get("size_usd", 5.0),
            confidence=kwargs.get("confidence", 80),
        ))

    # ── Step progression ─────────────────────────────────────

    def test_first_attempt_immediate_fill_returns_first(self):
        exe = _StubExecutor(post_results=[_filled(0.46)])
        out = self._run(exe)
        self.assertTrue(out.success)
        self.assertEqual(len(exe.posts), 1)
        self.assertAlmostEqual(exe.posts[0][1], 0.45 + LADDER_STEPS[0])
        self.assertEqual(len(exe.cancels), 0)

    def test_walks_full_ladder_when_nothing_fills(self):
        # Every post is accepted but never fills, no poll fills either.
        exe = _StubExecutor(
            post_results=[_live(f"o{i}") for i in range(len(LADDER_STEPS))],
        )
        out = self._run(exe)
        self.assertFalse(out.success)
        self.assertIn("not filled after retries", out.error)
        self.assertEqual(len(exe.posts), len(LADDER_STEPS))
        # Ladder prices must monotonically increase
        prices = [p for _, p, _ in exe.posts]
        self.assertEqual(prices, sorted(prices))
        # Each step should match the configured ladder bumps
        for (_, p, _), bump in zip(exe.posts, LADDER_STEPS):
            self.assertAlmostEqual(p, round(0.45 + bump, 2))

    def test_fills_on_third_attempt(self):
        exe = _StubExecutor(
            post_results=[_live("o1"), _live("o2"),
                          _filled(0.50, order_id="o3")],
        )
        out = self._run(exe)
        self.assertTrue(out.success)
        self.assertEqual(out.order_id, "o3")
        self.assertEqual(len(exe.posts), 3)
        # First two orders must be cancelled before next attempt
        self.assertEqual(exe.cancels, ["o1", "o2"])

    def test_poll_fills_short_circuits_ladder(self):
        # Post returns "live" (no immediate fill) but poll catches a fill.
        exe = _StubExecutor(
            post_results=[_live("o1")],
            poll_results=[FillResult(success=True, order_id="o1",
                                     filled_shares=10.0, avg_price=0.46)],
        )
        out = self._run(exe)
        self.assertTrue(out.success)
        self.assertEqual(out.filled_shares, 10.0)
        self.assertEqual(len(exe.posts), 1)
        self.assertEqual(len(exe.polls), 1)
        self.assertEqual(exe.cancels, [])  # not cancelled, fill came through

    # ── Fast-fail paths ─────────────────────────────────────

    def test_balance_error_returns_immediately_no_retry(self):
        exe = _StubExecutor(post_results=[_err("not enough balance")])
        out = self._run(exe)
        self.assertFalse(out.success)
        self.assertIn("balance", out.error.lower())
        # Must not progress through the ladder
        self.assertEqual(len(exe.posts), 1)

    def test_allowance_error_returns_immediately(self):
        exe = _StubExecutor(post_results=[_err("allowance is 0")])
        out = self._run(exe)
        self.assertFalse(out.success)
        self.assertEqual(len(exe.posts), 1)

    # ── Price cap ────────────────────────────────────────────

    def test_skips_attempt_above_execution_max_price(self):
        # Start so close to the cap that the second ladder step exceeds
        # EXECUTION_MAX_PRICE. The ladder must stop early instead of
        # paying above-cap.
        base = EXECUTION_MAX_PRICE - LADDER_STEPS[0]  # so step 0 lands on the cap
        exe = _StubExecutor(
            post_results=[_live("o1")] * len(LADDER_STEPS),
        )
        out = self._run(exe, price=base)
        self.assertFalse(out.success)
        # Only the steps whose price stays at-or-under the cap should be
        # attempted. We expect at least one (the cap itself) and strictly
        # fewer than the full ladder.
        self.assertGreaterEqual(len(exe.posts), 1)
        self.assertLess(len(exe.posts), len(LADDER_STEPS))
        for _, p, _ in exe.posts:
            self.assertLessEqual(p, EXECUTION_MAX_PRICE + 1e-9)

    # ── Dry-run path ────────────────────────────────────────

    def test_dry_run_does_not_post_when_ladder_fills(self):
        # Patch random to always return 0.0 → first ladder step (50%
        # fill prob) always succeeds. Verifies dry-run never touches the
        # network when a simulated fill succeeds.
        from unittest.mock import patch
        exe = _StubExecutor(post_results=[])
        exe.dry_run = True
        with patch("core.execution.random.random", return_value=0.0):
            out = self._run(exe, price=0.45, size_usd=5.0)
        self.assertTrue(out.success)
        self.assertGreater(out.filled_shares, 0)
        self.assertEqual(len(exe.posts), 0)
        # First ladder step = price + LADDER_STEPS[0]
        self.assertAlmostEqual(out.avg_price, round(0.45 + LADDER_STEPS[0], 2),
                               places=2)

    def test_dry_run_can_simulate_no_fill(self):
        # Patch random.random() to always return 0.999 → above all
        # ladder fill probs → should fail to fill, exactly as a tough
        # window in live mode would.
        from unittest.mock import patch
        exe = _StubExecutor(post_results=[])
        exe.dry_run = True
        with patch("core.execution.random.random", return_value=0.999):
            out = self._run(exe, price=0.45, size_usd=5.0)
        self.assertFalse(out.success)
        self.assertIn("ladder exhausted", out.error)
        self.assertEqual(exe.placed_count, 1)
        self.assertEqual(exe.filled_count, 0)

    # ── Sizing ──────────────────────────────────────────────

    def test_minimum_share_count_floor(self):
        """At dust sizes the executor must still post at least 5 shares
        (Polymarket minimum order size)."""
        exe = _StubExecutor(post_results=[_filled(0.46)])
        self._run(exe, price=0.45, size_usd=0.50)  # would be 1 share at 0.45
        _, _, shares = exe.posts[0]
        self.assertGreaterEqual(shares, 5)


if __name__ == "__main__":
    unittest.main()
