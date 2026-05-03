"""
Web dashboard server — aiohttp + Server-Sent Events.

Endpoints:
  GET  /                  → dashboard HTML
  GET  /api/stats         → today/week/alltime JSON
  GET  /api/trades        → recent trades
  GET  /api/equity        → equity curve points
  GET  /api/live          → SSE stream of live events (window + trades)
  GET  /api/config        → current runtime config
  GET  /api/window        → current window snapshot

All endpoints are public — no auth.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, Set

from aiohttp import web

import config

log = logging.getLogger("dashboard")

HTML_PATH = Path(__file__).parent / "index.html"


class DashboardServer:
    def __init__(self, pnl_tracker, risk_manager, bot_state):
        self.pnl = pnl_tracker
        self.risk = risk_manager
        self.state = bot_state  # BotState — provides current window snapshot
        self._sse_clients: Set[asyncio.Queue] = set()
        self.app = web.Application()
        self._setup_routes()
        self._runner: web.AppRunner = None

    def _setup_routes(self) -> None:
        # Dashboard is intentionally public — no token gating.
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/stats", self.handle_stats)
        self.app.router.add_get("/api/trades", self.handle_trades)
        self.app.router.add_get("/api/equity", self.handle_equity)
        self.app.router.add_get("/api/config", self.handle_config)
        self.app.router.add_get("/api/window", self.handle_window)
        self.app.router.add_get("/api/live", self.handle_sse)
        self.app.on_response_prepare.append(self._cors)

    @staticmethod
    async def _cors(request: web.Request, response: web.StreamResponse) -> None:
        # Allow the landing page (and any other origin) to fetch read-only stats.
        response.headers["Access-Control-Allow-Origin"] = "*"

    # ── Routes ──────────────────────────────────────────
    async def handle_index(self, request: web.Request) -> web.Response:
        try:
            html = HTML_PATH.read_text()
        except FileNotFoundError:
            return web.Response(text="dashboard/index.html missing", status=500)
        return web.Response(text=html, content_type="text/html")

    async def handle_stats(self, request: web.Request) -> web.Response:
        trades = self.pnl.all_trades()
        today_key = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d")
        week_cutoff = __import__("time").time() - 7 * 86400

        def avg_conf(subset):
            vals = [t.get("confidence", 0) for t in subset if t.get("confidence")]
            return round(sum(vals) / len(vals), 1) if vals else 0.0

        today = self.pnl.today_stats()
        today["avg_confidence"] = avg_conf([t for t in trades if t.get("date") == today_key])
        week = self.pnl.week_stats()
        week["avg_confidence"] = avg_conf([t for t in trades if t.get("ts", 0) >= week_cutoff])
        alltime = self.pnl.alltime_stats()
        alltime["avg_confidence"] = avg_conf(trades)

        return web.json_response({
            "today": today,
            "week": week,
            "alltime": alltime,
            "streak": self.pnl.current_streak(),
            "risk": self.risk.snapshot(),
            "paused": config.RUNTIME.paused,
            "dry_run": config.RUNTIME.dry_run,
        })

    async def handle_trades(self, request: web.Request) -> web.Response:
        try:
            limit = max(1, min(200, int(request.query.get("limit", "50"))))
        except ValueError:
            limit = 50
        return web.json_response(self.pnl.recent_trades(limit))

    async def handle_equity(self, request: web.Request) -> web.Response:
        return web.json_response({
            "equity": self.pnl.equity_curve(),
            "daily": self.pnl.daily_pnl_series(),
            "win_rate": self.pnl.rolling_win_rate(),
        })

    async def handle_config(self, request: web.Request) -> web.Response:
        return web.json_response(config.summary())

    async def handle_window(self, request: web.Request) -> web.Response:
        return web.json_response(self.state.snapshot())

    async def handle_sse(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        })
        await resp.prepare(request)
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._sse_clients.add(queue)
        try:
            # Initial snapshot
            await resp.write(self._sse_pack("hello", self.state.snapshot()))
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    await resp.write(self._sse_pack(event["type"], event["data"]))
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self._sse_clients.discard(queue)
        return resp

    def _sse_pack(self, event: str, data: Any) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()

    # ── Broadcasting ────────────────────────────────────
    def broadcast(self, event_type: str, data: Any) -> None:
        """Called by bot.py to push events to all connected SSE clients."""
        dead = []
        for q in self._sse_clients:
            try:
                q.put_nowait({"type": event_type, "data": data})
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._sse_clients.discard(q)

    # ── Lifecycle ───────────────────────────────────────
    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        port = config.DASHBOARD_PORT
        site = web.TCPSite(self._runner, "0.0.0.0", port, reuse_address=True)
        try:
            await site.start()
            log.info("dashboard listening on port %d", port)
        except OSError as exc:
            log.error("dashboard failed to bind port %d: %s — bot continues without dashboard", port, exc)
            # Don't crash the entire bot over a dashboard port conflict
            await self._runner.cleanup()
            self._runner = None

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
