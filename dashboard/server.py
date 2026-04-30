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

All endpoints require ?token=<DASHBOARD_TOKEN> — without a valid token
the request is rejected with 401.
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
    def __init__(self, pnl_tracker, risk_manager, bot_state, executor=None):
        self.pnl = pnl_tracker
        self.risk = risk_manager
        self.state = bot_state  # BotState — provides current window snapshot
        self.executor = executor  # for /api/execution fill-rate metrics
        self._sse_clients: Set[asyncio.Queue] = set()
        self.app = web.Application()
        self._setup_routes()
        self._runner: web.AppRunner = None

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/api/stats", self.auth(self.handle_stats))
        self.app.router.add_get("/api/trades", self.auth(self.handle_trades))
        self.app.router.add_get("/api/equity", self.auth(self.handle_equity))
        self.app.router.add_get("/api/config", self.auth(self.handle_config))
        self.app.router.add_get("/api/window", self.auth(self.handle_window))
        self.app.router.add_get("/api/execution", self.auth(self.handle_execution))
        self.app.router.add_get("/api/position", self.auth(self.handle_position))
        self.app.router.add_get("/api/live", self.auth(self.handle_sse))

    def auth(self, handler):
        async def wrapper(request: web.Request) -> web.Response:
            token = request.query.get("token", "")
            if token != config.DASHBOARD_TOKEN:
                return web.json_response({"error": "unauthorized"}, status=401)
            return await handler(request)
        return wrapper

    # ── Routes ──────────────────────────────────────────
    async def handle_index(self, request: web.Request) -> web.Response:
        # Dashboard HTML does the token check client-side; the APIs still gate.
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

    async def handle_execution(self, request: web.Request) -> web.Response:
        """Live fill-rate metrics from the executor — see if orders are
        actually getting filled or stuck on the book."""
        if self.executor is None:
            return web.json_response({"placed": 0, "filled": 0,
                                      "fill_rate": 0.0, "avg_attempts": 0.0})
        return web.json_response(self.executor.execution_snapshot())

    async def handle_position(self, request: web.Request) -> web.Response:
        """Current active position with entry / max profit / max loss
        broken out — the dashboard's 'Active Position' card consumes this.

        For binary markets:
          max_profit = (1 - entry_price) * shares     (the win payout)
          max_loss   = -entry_price * shares          (lose entire stake)
        These are the binary-market equivalents of TP and SL.
        """
        rec = self.state.entry_record if self.state else None
        if not rec or not self.state.entered_this_window:
            return web.json_response({"active": False})
        entry = float(rec.get("entry_price", 0) or 0)
        shares = float(rec.get("shares", 0) or 0)
        cost = float(rec.get("cost", entry * shares) or 0)
        max_profit = round((1.0 - entry) * shares, 2)
        max_loss = round(-entry * shares, 2)
        w = self.state.window
        return web.json_response({
            "active": True,
            "side": rec.get("side"),
            "entry_price": entry,
            "shares": shares,
            "stake": cost,
            "max_profit": max_profit,
            "max_loss": max_loss,
            "confidence": rec.get("confidence"),
            "window_slug": rec.get("window_slug"),
            "seconds_remaining": w.seconds_remaining if w else None,
            "price_to_beat": w.price_to_beat if w else None,
        })

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
