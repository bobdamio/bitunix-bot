"""
GoldasT Bot v2 - WebSocket Handler
Manages market data and private channel subscriptions.
Uses clean BitunixWebSocket.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any, List, Callable, Optional, Set
from enum import Enum

from .bitunix_ws import BitunixWebSocket
from .models import Candle, TradeDirection
from .config import APIConfig


logger = logging.getLogger(__name__)


class ChannelType(Enum):
    """WebSocket channel types"""
    KLINE = "kline"
    POSITION = "position"
    TPSL = "tpsl"
    ORDER = "order"
    BALANCE = "balance"
    TICKER = "ticker"
    TRADE = "trade"


@dataclass
class KlineMessage:
    """Parsed kline/candlestick message"""
    symbol: str
    interval: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool

    def to_candle(self) -> Candle:
        """Convert to Candle model"""
        return Candle(
            timestamp=int(self.timestamp.timestamp() * 1000) if isinstance(self.timestamp, datetime) else int(self.timestamp),
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


@dataclass
class PositionUpdate:
    """Parsed position update message"""
    symbol: str
    position_id: str
    side: str  # LONG or SHORT
    quantity: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    realized_pnl: float
    leverage: int
    margin: float
    liquidation_price: Optional[float] = None


@dataclass
class TPSLUpdate:
    """Parsed TP/SL update message"""
    symbol: str
    position_id: str
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    tp_triggered: bool = False
    sl_triggered: bool = False
    close_type: Optional[str] = None  # "tp", "sl", "liquidation"
    pnl: float = 0.0


@dataclass
class OrderUpdate:
    """Parsed order update message"""
    symbol: str
    order_id: str
    side: str
    order_type: str
    status: str  # PENDING, FILLED, CANCELLED, etc.
    quantity: float
    filled_quantity: float
    client_order_id: Optional[str] = None
    price: Optional[float] = None
    avg_fill_price: Optional[float] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


# Interval string → milliseconds
INTERVAL_MS = {
    "1min": 60_000,
    "3min": 180_000,
    "5min": 300_000,
    "15min": 900_000,
    "30min": 1_800_000,
    "60min": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
}

# Callback type aliases
KlineCallback = Callable[[str, KlineMessage], None]
PositionCallback = Callable[[PositionUpdate], None]
TPSLCallback = Callable[[TPSLUpdate], None]
OrderCallback = Callable[[OrderUpdate], None]


class WebSocketHandler:
    """
    Manages WebSocket connections and message routing.

    Subscriptions:
        - Kline channels for each symbol
        - Position channel for fill notifications
        - TPSL channel for TP/SL triggers
        - Order channel for order status updates
    """

    def __init__(
        self,
        config: APIConfig,
        symbols: List[str],
        kline_interval: str = "5min",
    ):
        self.config = config
        self.symbols = symbols
        self.kline_interval = kline_interval

        # Clean WebSocket client
        self._ws = BitunixWebSocket(
            api_key=config.key,
            api_secret=config.secret,
        )

        # Callbacks
        self._kline_callbacks: List[KlineCallback] = []
        self._position_callbacks: List[PositionCallback] = []
        self._tpsl_callbacks: List[TPSLCallback] = []
        self._order_callbacks: List[OrderCallback] = []

        # State
        self._running = False
        self._message_task: Optional[asyncio.Task] = None
        self._seen_positions: Set[str] = set()

        # Candle buffers
        self._candle_buffers: Dict[str, List[Candle]] = {}

        # WS kline tracking — detect candle close by period change
        self._interval_ms = INTERVAL_MS.get(kline_interval, 300_000)
        self._kline_period: Dict[str, int] = {}   # symbol → current period start (ms)
        self._kline_data: Dict[str, Dict] = {}     # symbol → latest OHLCV for current period
        self._kline_last_ts: Dict[str, float] = {} # symbol → last received time (time.time())

        logger.info(
            f"WebSocket handler initialized for {len(symbols)} symbols "
            f"(interval={kline_interval}, ws_channel=market_kline_{kline_interval})"
        )

    # ==================== Callback Registration ====================

    def on_kline(self, callback: KlineCallback) -> None:
        self._kline_callbacks.append(callback)

    def on_position(self, callback: PositionCallback) -> None:
        self._position_callbacks.append(callback)

    def on_tpsl(self, callback: TPSLCallback) -> None:
        self._tpsl_callbacks.append(callback)

    def on_order(self, callback: OrderCallback) -> None:
        self._order_callbacks.append(callback)

    # ==================== Connection Management ====================

    async def connect(self) -> bool:
        """Connect to WebSocket and subscribe to channels"""
        try:
            public_ok = await self._ws.connect_public()
            if not public_ok:
                logger.error("Failed to connect to public WebSocket")
                return False

            private_ok = await self._ws.connect_private()
            if not private_ok:
                logger.error("Failed to connect to private WebSocket")
                return False

            await self._subscribe_channels()

            self._running = True
            self._message_task = asyncio.create_task(self._process_messages())

            logger.info("WebSocket connected and subscribed")
            return True

        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            return False

    async def _subscribe_channels(self) -> None:
        """Subscribe to all required channels.
        
        Sends kline subscriptions in batches of 5 to avoid partial failures
        on the Bitunix WS server. Adds a small delay between batches.
        """
        # Public: kline channel for each symbol
        # Channel name format: market_kline_{interval} (e.g. market_kline_5min)
        # Pushes OHLCV snapshots every 500ms for the current forming candle
        kline_ch = f"market_kline_{self.kline_interval}"
        public_channels = []
        for symbol in self.symbols:
            clean_symbol = symbol.replace("_PERP", "")
            public_channels.append({
                "symbol": clean_symbol,
                "ch": kline_ch,
            })

        # Subscribe in batches of 5 to prevent partial subscribe failures
        BATCH_SIZE = 5
        for i in range(0, len(public_channels), BATCH_SIZE):
            batch = public_channels[i:i + BATCH_SIZE]
            await self._ws.subscribe_public(batch)
            batch_symbols = [ch["symbol"] for ch in batch]
            logger.info(f"Subscribed kline batch {i // BATCH_SIZE + 1}: {batch_symbols}")
            if i + BATCH_SIZE < len(public_channels):
                await asyncio.sleep(0.5)  # Brief pause between batches

        logger.info(f"Subscribed to kline channel: {kline_ch} for {len(public_channels)} symbols")

        # Private: position, tpsl, order
        await self._ws.subscribe_private([
            {"ch": "position"},
            {"ch": "tpsl"},
            {"ch": "order"},
        ])

    async def subscribe_new_symbols(self, symbols: List[str]) -> None:
        """Subscribe new symbols to kline WS channel and add to tracking.

        Called after symbol rotation adds new symbols.
        """
        kline_ch = f"market_kline_{self.kline_interval}"
        added = [s for s in symbols if s not in self.symbols]
        if not added:
            return

        for symbol in added:
            self.symbols.append(symbol)
            clean_symbol = symbol.replace("_PERP", "")
            await self._ws.subscribe_public([{
                "symbol": clean_symbol,
                "ch": kline_ch,
            }])
            await asyncio.sleep(0.3)

        logger.info(f"📡 Subscribed {len(added)} new rotation symbols: {added}")

    def unsubscribe_symbols(self, symbols: List[str]) -> None:
        """Remove symbols from tracking (WS will stop routing them)."""
        for sym in symbols:
            if sym in self.symbols:
                self.symbols.remove(sym)
                self._kline_period.pop(sym, None)
                self._kline_data.pop(sym, None)
                self._kline_last_ts.pop(sym, None)
        if symbols:
            logger.info(f"📡 Removed {len(symbols)} symbols from WS tracking")

    async def check_ws_health(self) -> None:
        """Detect symbols not receiving WS data and re-subscribe them.
        
        Called periodically from the main loop. If a symbol hasn't received
        any kline data in 60+ seconds, it's considered dead and gets
        re-subscribed.
        """
        dead_symbols = []
        kline_ch = f"market_kline_{self.kline_interval}"
        now = __import__('time').time()
        STALE_THRESHOLD = 120  # seconds
        
        for symbol in self.symbols:
            if symbol not in self._kline_period:
                # Never received any data — dead since startup
                dead_symbols.append(symbol)
            elif now - self._kline_last_ts.get(symbol, 0) > STALE_THRESHOLD:
                # Received data before but stale now
                dead_symbols.append(symbol)

        if not dead_symbols:
            return

        logger.warning(
            f"⚠️ WS health: {len(dead_symbols)} symbols with no data: {dead_symbols} — re-subscribing"
        )

        # Re-subscribe dead symbols one by one
        for symbol in dead_symbols:
            clean_symbol = symbol.replace("_PERP", "")
            await self._ws.subscribe_public([{
                "symbol": clean_symbol,
                "ch": kline_ch,
            }])
            await asyncio.sleep(0.3)

        logger.info(f"✅ Re-subscribed {len(dead_symbols)} dead symbols")

    async def disconnect(self) -> None:
        """Disconnect WebSocket"""
        self._running = False
        if self._message_task:
            self._message_task.cancel()
            try:
                await self._message_task
            except asyncio.CancelledError:
                pass
        await self._ws.disconnect()
        logger.info("WebSocket disconnected")

    # ==================== Message Processing ====================

    async def _process_messages(self) -> None:
        """Process messages from the queue"""
        _count = 0
        while self._running:
            try:
                source, data = await asyncio.wait_for(
                    self._ws.message_queue.get(), timeout=30.0
                )
                _count += 1
                if _count <= 5 or _count % 200 == 0:
                    logger.info(f"📨 Processing msg #{_count}: ch={data.get('ch','')} sym={data.get('symbol','')}")

                channel = data.get("ch", "")

                if "kline" in channel:
                    await self._handle_kline(data)
                elif channel == "position":
                    await self._handle_position(data)
                elif channel == "tpsl":
                    await self._handle_tpsl(data)
                elif channel == "order":
                    await self._handle_order(data)
                else:
                    logger.debug(f"Unhandled channel: {channel}")

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Message processing error: {e}")

    async def _handle_kline(self, data: Dict[str, Any]) -> None:
        """
        Parse WS kline push and detect candle close.

        Bitunix WS pushes the current forming candle every 500ms:
            {ch: "market_kline_5min", symbol: "BTCUSDT", ts: <epoch_ms>,
             data: {o, c, h, l, b (base vol), q (quote vol)}}

        There is no "closed" flag — we detect close when the 5-min period
        boundary rolls over (ts moves into a new period).
        """
        try:
            symbol = data.get("symbol", "")
            ts = int(data.get("ts", 0))
            kline_raw = data.get("data", {})
            if not symbol or not ts or not kline_raw:
                return

            # Track last received time for stale detection
            self._kline_last_ts[symbol] = __import__('time').time()

            current_period = (ts // self._interval_ms) * self._interval_ms
            prev_period = self._kline_period.get(symbol)

            # --- Candle close detection ---
            if prev_period is not None and current_period > prev_period:
                # Period rolled over → previous candle is closed
                closed = self._kline_data.get(symbol, {})
                if closed:
                    closed_msg = KlineMessage(
                        symbol=symbol,
                        interval=self.kline_interval,
                        timestamp=datetime.fromtimestamp(prev_period / 1000),
                        open=float(closed.get("o", 0)),
                        high=float(closed.get("h", 0)),
                        low=float(closed.get("l", 0)),
                        close=float(closed.get("c", 0)),
                        volume=float(closed.get("q", 0)),  # quote volume
                        is_closed=True,
                    )
                    for callback in self._kline_callbacks:
                        try:
                            callback(symbol, closed_msg)
                        except Exception as e:
                            logger.error(f"Kline close callback error: {e}")

            # --- Update tracking ---
            self._kline_period[symbol] = current_period
            self._kline_data[symbol] = kline_raw

            # --- Live tick (not closed) for real-time price updates ---
            live_msg = KlineMessage(
                symbol=symbol,
                interval=self.kline_interval,
                timestamp=datetime.fromtimestamp(current_period / 1000),
                open=float(kline_raw.get("o", 0)),
                high=float(kline_raw.get("h", 0)),
                low=float(kline_raw.get("l", 0)),
                close=float(kline_raw.get("c", 0)),
                volume=float(kline_raw.get("q", 0)),
                is_closed=False,
            )
            for callback in self._kline_callbacks:
                try:
                    callback(symbol, live_msg)
                except Exception as e:
                    logger.error(f"Kline live callback error: {e}")

        except Exception as e:
            logger.error(f"Failed to parse kline: {e}")

    async def _handle_position(self, data: Dict[str, Any]) -> None:
        """Parse and route position update"""
        try:
            pos_data = data.get("data", {})

            update = PositionUpdate(
                symbol=pos_data.get("symbol", ""),
                position_id=str(pos_data.get("positionId", "")),
                side=pos_data.get("side", ""),
                quantity=float(pos_data.get("positionAmt", 0)),
                entry_price=float(pos_data.get("entryPrice", 0)),
                mark_price=float(pos_data.get("markPrice", 0)),
                unrealized_pnl=float(pos_data.get("unrealizedPnl", 0)),
                realized_pnl=float(pos_data.get("realizedPnl", 0)),
                leverage=int(pos_data.get("leverage", 1)),
                margin=float(pos_data.get("margin", 0)),
                liquidation_price=(
                    float(pos_data.get("liquidationPrice", 0))
                    if pos_data.get("liquidationPrice") else None
                ),
            )

            # De-duplicate
            key = f"{update.symbol}_{update.position_id}_{update.quantity}"
            if key in self._seen_positions:
                return
            self._seen_positions.add(key)
            if len(self._seen_positions) > 1000:
                self._seen_positions.clear()

            for callback in self._position_callbacks:
                try:
                    callback(update)
                except Exception as e:
                    logger.error(f"Position callback error: {e}")
        except Exception as e:
            logger.error(f"Failed to parse position: {e}")

    async def _handle_tpsl(self, data: Dict[str, Any]) -> None:
        """Parse and route TP/SL update"""
        try:
            tpsl_data = data.get("data", {})

            close_type = None
            if tpsl_data.get("tpTriggered"):
                close_type = "tp"
            elif tpsl_data.get("slTriggered"):
                close_type = "sl"

            update = TPSLUpdate(
                symbol=tpsl_data.get("symbol", ""),
                position_id=str(tpsl_data.get("positionId", "")),
                tp_order_id=tpsl_data.get("tpOrderId"),
                sl_order_id=tpsl_data.get("slOrderId"),
                tp_price=(
                    float(tpsl_data.get("tpPrice", 0))
                    if tpsl_data.get("tpPrice") else None
                ),
                sl_price=(
                    float(tpsl_data.get("slPrice", 0))
                    if tpsl_data.get("slPrice") else None
                ),
                tp_triggered=tpsl_data.get("tpTriggered", False),
                sl_triggered=tpsl_data.get("slTriggered", False),
                close_type=close_type,
                pnl=float(tpsl_data.get("realizedPnl", 0)),
            )

            for callback in self._tpsl_callbacks:
                try:
                    callback(update)
                except Exception as e:
                    logger.error(f"TP/SL callback error: {e}")
        except Exception as e:
            logger.error(f"Failed to parse TP/SL: {e}")

    async def _handle_order(self, data: Dict[str, Any]) -> None:
        """Parse and route order update"""
        try:
            order_data = data.get("data", {})

            update = OrderUpdate(
                symbol=order_data.get("symbol", ""),
                order_id=str(order_data.get("orderId", "")),
                client_order_id=order_data.get("clientOrderId"),
                side=order_data.get("side", ""),
                order_type=order_data.get("orderType", ""),
                status=order_data.get("status", ""),
                quantity=float(order_data.get("qty", 0)),
                filled_quantity=float(order_data.get("filledQty", 0)),
                price=(
                    float(order_data.get("price", 0))
                    if order_data.get("price") else None
                ),
                avg_fill_price=(
                    float(order_data.get("avgPrice", 0))
                    if order_data.get("avgPrice") else None
                ),
            )

            for callback in self._order_callbacks:
                try:
                    callback(update)
                except Exception as e:
                    logger.error(f"Order callback error: {e}")
        except Exception as e:
            logger.error(f"Failed to parse order: {e}")

    # ==================== Candle Buffer ====================

    def get_candle_buffer(self, symbol: str) -> List[Candle]:
        """Get accumulated candles for a symbol"""
        return self._candle_buffers.get(symbol, [])

    def clear_candle_buffer(self, symbol: str) -> None:
        """Clear candle buffer for a symbol"""
        if symbol in self._candle_buffers:
            self._candle_buffers[symbol].clear()

    def add_candle(self, symbol: str, candle: Candle) -> None:
        """Add a closed candle to buffer"""
        if symbol not in self._candle_buffers:
            self._candle_buffers[symbol] = []
        self._candle_buffers[symbol].append(candle)
        # Keep only recent candles
        if len(self._candle_buffers[symbol]) > 100:
            self._candle_buffers[symbol] = self._candle_buffers[symbol][-100:]

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected"""
        return (
            self._ws.is_public_connected
            and self._ws.is_private_connected
            and self._running
        )
