"""
Resilient async WebSocket connection manager.

Both Binance and Polymarket feeds extend AsyncReconnectingWS. The base class:
  - connects
  - dispatches each incoming message to on_message()
  - on any disconnect / error, waits with exponential backoff and reconnects
  - exposes a .running flag so consumers can gracefully stop it
"""

import asyncio
import json
import logging
from typing import Any, Optional

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

log = logging.getLogger("ws")


class AsyncReconnectingWS:
    """
    Base class for a self-healing WebSocket worker.

    Subclasses override:
      - subscribe_payload() -> dict | list | None (sent after connect)
      - on_message(msg) -> None (called for every decoded message)

    If `optional=True`, the feed will permanently disable itself after
    `max_failures` consecutive failed connects (e.g. persistent HTTP 404)
    instead of retry-spamming forever. `.disabled` is set True so the
    rest of the bot can check and use a fallback path.
    """

    def __init__(
        self,
        url: str,
        name: str = "ws",
        optional: bool = False,
        max_failures: int = 5,
    ):
        self.url = url
        self.name = name
        self.running = False
        self.disabled = False
        self.optional = optional
        self.max_failures = max_failures
        self._fail_count = 0
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._backoff = 1.0
        self._backoff_max = 30.0

    async def subscribe_payload(self) -> Any:
        return None

    async def on_message(self, msg: Any) -> None:
        raise NotImplementedError

    async def on_connect(self) -> None:
        """Hook called right after a successful connection."""
        payload = await self.subscribe_payload()
        if payload is not None and self._ws is not None:
            await self._ws.send(json.dumps(payload))

    async def send(self, data: Any) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps(data))

    async def run(self) -> None:
        self.running = True
        while self.running:
            connected = False
            try:
                log.info("[%s] connecting to %s", self.name, self.url)
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=8 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    self._backoff = 1.0
                    self._fail_count = 0
                    connected = True
                    await self.on_connect()
                    log.info("[%s] connected", self.name)
                    async for raw in ws:
                        try:
                            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                        except json.JSONDecodeError:
                            msg = raw
                        try:
                            await self.on_message(msg)
                        except Exception as exc:
                            log.exception("[%s] on_message error: %s", self.name, exc)
            except (InvalidHandshake, InvalidURI) as exc:
                # HTTP 404, 401, bad URI, etc. — endpoint is likely gone.
                self._fail_count += 1
                log.warning("[%s] handshake failed (%s) [%d/%d]",
                            self.name, exc, self._fail_count, self.max_failures)
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                if not connected:
                    self._fail_count += 1
                log.warning("[%s] disconnected: %s", self.name, exc)
            except Exception as exc:
                if not connected:
                    self._fail_count += 1
                log.exception("[%s] unexpected error: %s", self.name, exc)
            finally:
                self._ws = None
            if not self.running:
                break
            if self.optional and self._fail_count >= self.max_failures:
                self.disabled = True
                self.running = False
                log.warning(
                    "[%s] giving up after %d failed connects — "
                    "feed disabled, bot will use fallback",
                    self.name, self._fail_count,
                )
                break
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, self._backoff_max)

    async def stop(self) -> None:
        self.running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
