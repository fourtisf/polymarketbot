"""Smoke tests for dashboard /api/execution and /api/position endpoints.

Verifies they return correctly shaped JSON given known inputs, without
spinning up a real aiohttp server.
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("POLYGON_PRIVATE_KEY", "")
os.environ.setdefault("POLYGON_PUBLIC_KEY", "")
# Dashboard token must match what the auth wrapper checks
os.environ.setdefault("DASHBOARD_TOKEN", "test-token")

import config  # noqa: E402
config.DASHBOARD_TOKEN = "test-token"

from aiohttp.test_utils import make_mocked_request  # noqa: E402

from core.execution import Executor  # noqa: E402
from dashboard.server import DashboardServer  # noqa: E402


def _make_request(query: str = "token=test-token"):
    return make_mocked_request("GET", "/api/x?" + query)


class ExecutionEndpointTests(unittest.TestCase):
    def test_returns_zero_metrics_when_executor_missing(self):
        srv = DashboardServer(MagicMock(), MagicMock(), MagicMock(),
                              executor=None)
        resp = asyncio.run(srv.handle_execution(_make_request()))
        import json as _j
        body = _j.loads(resp.body)
        self.assertEqual(body, {"placed": 0, "filled": 0,
                                 "fill_rate": 0.0, "avg_attempts": 0.0})

    def test_returns_executor_snapshot(self):
        exe = Executor(dry_run=True)
        # Simulate two placed, one filled
        exe.placed_count = 2
        exe.filled_count = 1
        exe.cumulative_avg_attempts = 1.5
        srv = DashboardServer(MagicMock(), MagicMock(), MagicMock(),
                              executor=exe)
        resp = asyncio.run(srv.handle_execution(_make_request()))
        import json as _j
        body = _j.loads(resp.body)
        self.assertEqual(body["placed"], 2)
        self.assertEqual(body["filled"], 1)
        self.assertAlmostEqual(body["fill_rate"], 0.5)


class PositionEndpointTests(unittest.TestCase):
    def test_returns_inactive_when_no_entry(self):
        state = MagicMock()
        state.entry_record = None
        state.entered_this_window = False
        srv = DashboardServer(MagicMock(), MagicMock(), state)
        resp = asyncio.run(srv.handle_position(_make_request()))
        import json as _j
        body = _j.loads(resp.body)
        self.assertEqual(body, {"active": False})

    def test_returns_position_with_tp_sl_breakdown(self):
        state = MagicMock()
        state.entered_this_window = True
        state.entry_record = {
            "side": "UP",
            "entry_price": 0.45,
            "shares": 10.0,
            "cost": 4.50,
            "confidence": 80,
            "window_slug": "btc-updown-5m-1700000300",
        }
        state.window = MagicMock(
            seconds_remaining=15,
            price_to_beat=100000.0,
        )
        srv = DashboardServer(MagicMock(), MagicMock(), state)
        resp = asyncio.run(srv.handle_position(_make_request()))
        import json as _j
        body = _j.loads(resp.body)
        self.assertTrue(body["active"])
        self.assertEqual(body["side"], "UP")
        self.assertAlmostEqual(body["entry_price"], 0.45)
        self.assertAlmostEqual(body["stake"], 4.50)
        # Max profit = (1 - 0.45) * 10 = 5.50
        self.assertAlmostEqual(body["max_profit"], 5.50)
        # Max loss = -0.45 * 10 = -4.50
        self.assertAlmostEqual(body["max_loss"], -4.50)
        self.assertEqual(body["seconds_remaining"], 15)


if __name__ == "__main__":
    unittest.main()
