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
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

from aiohttp import web

import config

log = logging.getLogger("dashboard")

HTML_PATH = Path(__file__).parent / "index.html"

ONCHAIN_BALANCE_TTL = 20  # seconds — cap RPC calls from public stats endpoint


class DashboardServer:
    def __init__(self, pnl_tracker, risk_manager, bot_state, trade_log=None):
        self.pnl = pnl_tracker
        self.risk = risk_manager
        self.state = bot_state  # BotState — provides current window snapshot
        self.trade_log = trade_log
        self._sse_clients: Set[asyncio.Queue] = set()
        self._onchain_cache: Dict[str, Any] = {"ts": 0.0, "usdc": None, "pol": None}
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
        week_cutoff = time.time() - 7 * 86400

        def avg_conf(subset):
            vals = [t.get("confidence", 0) for t in subset if t.get("confidence")]
            return round(sum(vals) / len(vals), 1) if vals else 0.0

        today = self.pnl.today_stats()
        today["avg_confidence"] = avg_conf([t for t in trades if t.get("date") == today_key])
        week = self.pnl.week_stats()
        week["avg_confidence"] = avg_conf([t for t in trades if t.get("ts", 0) >= week_cutoff])
        alltime = self.pnl.alltime_stats()
        alltime["avg_confidence"] = avg_conf(trades)

        # On-chain balance — source of truth for the displayed balance.
        # Falls back to STARTING_BALANCE+pnl if the RPC fetch fails.
        onchain = await self._get_onchain_balance()
        onchain_usdc = onchain.get("usdc")
        if onchain_usdc is not None:
            alltime["onchain_balance"] = round(onchain_usdc, 2)
            alltime["onchain_balance_ts"] = onchain.get("ts")

        # Transparency counts: skipped windows + phantom (unfilled CLOB) trades
        # are NOT included in win rate, so surface them so visitors can judge.
        counts = self._trade_log_counts()
        alltime["skipped_count"] = counts["skipped"]
        alltime["phantom_count"] = counts["phantom"]

        # Mode: "live" only when bot is not in dry-run AND wallet has real
        # collateral. Otherwise "paper" so the UI can disclose honestly.
        dry = bool(config.RUNTIME.dry_run)
        has_real_money = onchain_usdc is not None and onchain_usdc >= 1.0
        mode = "live" if (not dry and has_real_money) else "paper"

        return web.json_response({
            "today": today,
            "week": week,
            "alltime": alltime,
            "streak": self.pnl.current_streak(),
            "risk": self.risk.snapshot(),
            "paused": config.RUNTIME.paused,
            "dry_run": dry,
            "mode": mode,
        })

    async def _get_onchain_balance(self) -> Dict[str, Any]:
        addr = getattr(config, "POLYGON_PUBLIC_KEY", "") or ""
        if not addr:
            return {}
        now = time.time()
        if now - self._onchain_cache["ts"] < ONCHAIN_BALANCE_TTL and self._onchain_cache["usdc"] is not None:
            return self._onchain_cache
        try:
            from utils.telegram import fetch_all_usdc
            usdc_e, _usdc_nat, pol = await fetch_all_usdc(addr)
            self._onchain_cache = {"ts": now, "usdc": usdc_e, "pol": pol}
        except Exception as exc:
            log.warning("on-chain balance fetch failed: %s", exc)
            # Keep stale cache if we have one; otherwise leave None.
            self._onchain_cache["ts"] = now
        return self._onchain_cache

    def _trade_log_counts(self) -> Dict[str, int]:
        out = {"skipped": 0, "phantom": 0}
        if not self.trade_log:
            return out
        try:
            for rec in self.trade_log.all():
                if rec.get("phantom") or rec.get("phase") == "phantom_detected":
                    out["phantom"] += 1
                elif rec.get("action") == "SKIP" or rec.get("side") == "SKIP":
                    out["skipped"] += 1
        except Exception as exc:
            log.debug("trade_log count failed: %s", exc)
        return out

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
