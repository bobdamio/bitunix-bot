"""
GoldasT Bot v2 - Bitunix WebSocket Client
Clean, self-contained async WebSocket for market data and private channels.

Protocol:
    Public:  wss://fapi.bitunix.com/public/   (kline, ticker, depth, trade)
    Private: wss://fapi.bitunix.com/private/   (position, order, balance, tpsl)
    Auth:    {"op": "login", "args": [{apiKey, nonce, timestamp, sign}]}
    Sub:     {"op": "subscribe", "args": [{ch, symbol?}]}
    Ping:    {"op": "ping", "ping": <ts>}
"""

import asyncio
import json
import logging
import ssl
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

import websockets
from websockets.protocol import State as WsState

from .bitunix_client import _ws_sign


logger = logging.getLogger(__name__)


def _ws_is_open(ws) -> bool:
    """Check if websocket connection is open (compatible with websockets >=16)."""
    if ws is None:
        return False
    try:
        return ws.state == WsState.OPEN
    except Exception:
        return False


class BitunixWebSocket:
    """
    Async WebSocket client for Bitunix Futures.
    
    Manages public and private connections with:
    - Auto-reconnect with exponential backoff
    - JSON-level ping/pong heartbeat
    - Channel subscription management
    - Message routing via asyncio.Queue
    """

    PUBLIC_URL = "wss://fapi.bitunix.com/public/"
    PRIVATE_URL = "wss://fapi.bitunix.com/private/"
    PING_INTERVAL = 10  # seconds
    RECONNECT_DELAY = 2  # initial delay (seconds)
    MAX_RECONNECT_DELAY = 60

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret

        # Connections
        self._public_ws = None
        self._private_ws = None

        # Tasks
        self._public_recv_task: Optional[asyncio.Task] = None
        self._private_recv_task: Optional[asyncio.Task] = None
        self._public_ping_task: Optional[asyncio.Task] = None
        self._private_ping_task: Optional[asyncio.Task] = None

        # Subscriptions (for re-subscribe on reconnect)
        self._public_channels: List[Dict[str, str]] = []
        self._private_channels: List[Dict[str, str]] = []

        # Message queue for external consumers
        self.message_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

        # State
        self._running = False
        self._ssl_ctx = self._create_ssl_context()

    @staticmethod
    def _create_ssl_context() -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    # ==================== Connect ====================

    async def connect_public(self) -> bool:
        """Connect to public WebSocket and start recv/ping loops."""
        try:
            self._public_ws = await websockets.connect(
                self.PUBLIC_URL,
                ssl=self._ssl_ctx,
                ping_interval=None,  # We handle pings ourselves
                close_timeout=5,
            )
            self._running = True
            self._public_recv_task = asyncio.create_task(
                self._recv_loop("public")
            )
            self._public_ping_task = asyncio.create_task(
                self._ping_loop("public")
            )
            logger.info("✅ Public WebSocket connected")
            return True
        except Exception as e:
            logger.error(f"Public WS connect failed: {e}")
            return False

    async def connect_private(self) -> bool:
        """Connect and authenticate private WebSocket, start recv/ping loops."""
        try:
            ws = await websockets.connect(
                self.PRIVATE_URL,
                ssl=self._ssl_ctx,
                ping_interval=None,
                close_timeout=5,
            )

            # Read the initial connect confirmation
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            resp = json.loads(raw)
            logger.debug(f"Private WS connect msg: {resp}")

            # Authenticate
            auth = _ws_sign(self.api_key, self.api_secret)
            login_msg = json.dumps({"op": "login", "args": [auth]})
            await ws.send(login_msg)

            # Wait for auth response
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            resp = json.loads(raw)
            if resp.get("op") == "login" and resp.get("data", {}).get("result"):
                logger.info("✅ Private WebSocket authenticated")
            else:
                logger.error(f"Private WS auth failed: {resp}")
                await ws.close()
                return False

            self._private_ws = ws
            self._running = True
            self._private_recv_task = asyncio.create_task(
                self._recv_loop("private")
            )
            self._private_ping_task = asyncio.create_task(
                self._ping_loop("private")
            )
            return True
        except Exception as e:
            logger.error(f"Private WS connect failed: {e}")
            return False

    # ==================== Raw connect (no task spawning) ====================

    async def _raw_connect_public(self) -> bool:
        """Establish public WS connection without spawning recv/ping tasks."""
        try:
            self._public_ws = await websockets.connect(
                self.PUBLIC_URL,
                ssl=self._ssl_ctx,
                ping_interval=None,
                close_timeout=5,
            )
            logger.info("✅ Public WebSocket connected")
            return True
        except Exception as e:
            logger.error(f"Public WS connect failed: {e}")
            return False

    async def _raw_connect_private(self) -> bool:
        """Establish private WS connection + auth without spawning tasks."""
        try:
            ws = await websockets.connect(
                self.PRIVATE_URL,
                ssl=self._ssl_ctx,
                ping_interval=None,
                close_timeout=5,
            )

            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            resp = json.loads(raw)
            logger.debug(f"Private WS connect msg: {resp}")

            auth = _ws_sign(self.api_key, self.api_secret)
            login_msg = json.dumps({"op": "login", "args": [auth]})
            await ws.send(login_msg)

            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            resp = json.loads(raw)
            if resp.get("op") == "login" and resp.get("data", {}).get("result"):
                logger.info("✅ Private WebSocket authenticated")
            else:
                logger.error(f"Private WS auth failed: {resp}")
                await ws.close()
                return False

            self._private_ws = ws
            return True
        except Exception as e:
            logger.error(f"Private WS connect failed: {e}")
            return False

    # ==================== Subscribe ====================

    async def subscribe_public(self, channels: List[Dict[str, str]]) -> None:
        """Subscribe to public channels (accumulates, not replaces)."""
        # Accumulate channels for reconnect (dedupe by symbol+ch)
        existing = {(c.get('symbol',''),c.get('ch','')) for c in self._public_channels}
        for ch in channels:
            key = (ch.get('symbol',''), ch.get('ch',''))
            if key not in existing:
                self._public_channels.append(ch)
                existing.add(key)
        if self._public_ws:
            msg = json.dumps({"op": "subscribe", "args": channels})
            await self._public_ws.send(msg)
            logger.info(f"Subscribed to {len(channels)} public channels (total: {len(self._public_channels)})")

    async def subscribe_private(self, channels: List[Dict[str, str]]) -> None:
        """Subscribe to private channels (accumulates, not replaces)."""
        existing = {c.get('ch','') for c in self._private_channels}
        for ch in channels:
            if ch.get('ch','') not in existing:
                self._private_channels.append(ch)
                existing.add(ch.get('ch',''))
        if self._private_ws:
            msg = json.dumps({"op": "subscribe", "args": channels})
            await self._private_ws.send(msg)
            logger.info(f"Subscribed to {len(channels)} private channels")

    # ==================== Receive Loop ====================

    def _get_ws(self, source: str):
        """Get the current websocket connection for a source."""
        return self._public_ws if source == "public" else self._private_ws

    async def _recv_loop(self, source: str) -> None:
        """
        Receive and route messages.
        Handles reconnection inline — no new tasks spawned.
        The same recv_loop instance persists across reconnects.
        """
        delay = self.RECONNECT_DELAY

        while self._running:
            ws = self._get_ws(source)
            if not _ws_is_open(ws):
                # Need to reconnect
                ok = await self._do_reconnect(source, delay)
                if ok:
                    delay = self.RECONNECT_DELAY  # reset backoff
                else:
                    delay = min(delay * 2, self.MAX_RECONNECT_DELAY)
                continue

            try:
                # Use explicit recv() instead of `async for raw in ws:`
                # websockets 16 iterator can silently stall with concurrent sends
                while _ws_is_open(ws):
                    try:
                        raw = await ws.recv()
                    except websockets.ConnectionClosed:
                        break

                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    op = data.get("op", "")

                    # Skip pong responses and subscribe confirmations
                    if op in ("pong", "ping"):
                        continue
                    if op == "subscribe":
                        logger.debug(f"[{source}] Subscription confirmed: {data}")
                        continue
                    if op == "login":
                        continue
                    if op == "error":
                        logger.warning(f"[{source}] WS error: {data}")
                        continue

                    # Queue the message
                    try:
                        self.message_queue.put_nowait((source, data))
                        self._msg_count = getattr(self, '_msg_count', 0) + 1
                        if self._msg_count <= 5 or self._msg_count % 100 == 0:
                            ch = data.get('ch', '?')
                            sym = data.get('symbol', '?')
                            logger.info(f"[{source}] msg #{self._msg_count}: ch={ch} sym={sym}")
                    except asyncio.QueueFull:
                        # Drop oldest message and log it
                        try:
                            dropped = self.message_queue.get_nowait()
                            dropped_ch = dropped[1].get('ch', '?') if isinstance(dropped, tuple) else '?'
                            logger.warning(f"[{source}] Queue full — dropped oldest msg (ch={dropped_ch})")
                        except asyncio.QueueEmpty:
                            pass
                        self.message_queue.put_nowait((source, data))

                    # Reset backoff on successful message
                    delay = self.RECONNECT_DELAY

                # Inner while exited — connection lost
                logger.warning(f"[{source}] Connection lost, will reconnect...")

            except websockets.ConnectionClosed:
                logger.warning(f"[{source}] Connection closed, will reconnect...")
            except Exception as e:
                logger.error(f"[{source}] Recv error: {e}")
                await asyncio.sleep(1)
            # Loop back — will detect ws is closed and reconnect

    async def _ping_loop(self, source: str) -> None:
        """Send periodic ping to keep connection alive."""
        while self._running:
            try:
                await asyncio.sleep(self.PING_INTERVAL)
                ws = self._get_ws(source)
                if _ws_is_open(ws):
                    ping_msg = json.dumps({"op": "ping", "ping": int(time.time())})
                    await ws.send(ping_msg)
            except Exception:
                break  # Connection lost, recv_loop will handle reconnect

    # ==================== Reconnect ====================

    async def _do_reconnect(self, source: str, delay: float) -> bool:
        """
        Attempt a single reconnection. Does NOT spawn new recv tasks.
        Only re-creates the ping task and re-subscribes channels.
        """
        # Cancel old ping task
        if source == "public":
            ping_task = self._public_ping_task
        else:
            ping_task = self._private_ping_task

        if ping_task and not ping_task.done():
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

        # Close old connection
        old_ws = self._get_ws(source)
        if old_ws:
            try:
                await old_ws.close()
            except Exception:
                pass
            if source == "public":
                self._public_ws = None
            else:
                self._private_ws = None

        logger.info(f"[{source}] Reconnecting in {delay:.0f}s...")
        await asyncio.sleep(delay)

        try:
            if source == "public":
                ok = await self._raw_connect_public()
                if ok and self._public_channels:
                    await self.subscribe_public(self._public_channels)
            else:
                ok = await self._raw_connect_private()
                if ok and self._private_channels:
                    await self.subscribe_private(self._private_channels)

            if ok:
                # Start new ping task
                ws = self._get_ws(source)
                new_ping = asyncio.create_task(self._ping_loop(source))
                if source == "public":
                    self._public_ping_task = new_ping
                else:
                    self._private_ping_task = new_ping
                logger.info(f"[{source}] Reconnected successfully")
                return True
        except Exception as e:
            logger.error(f"[{source}] Reconnect failed: {e}")

        return False

    # ==================== State ====================

    @property
    def is_public_connected(self) -> bool:
        return _ws_is_open(self._public_ws)

    @property
    def is_private_connected(self) -> bool:
        return _ws_is_open(self._private_ws)

    # ==================== Disconnect ====================

    async def disconnect(self) -> None:
        """Close all connections and cancel tasks"""
        self._running = False

        for task in [
            self._public_recv_task, self._private_recv_task,
            self._public_ping_task, self._private_ping_task,
        ]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        for ws in [self._public_ws, self._private_ws]:
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass

        self._public_ws = None
        self._private_ws = None
        logger.info("WebSocket connections closed")
