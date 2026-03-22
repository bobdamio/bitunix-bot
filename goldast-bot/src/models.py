"""
GoldasT Bot v2 - Data Models and Enums
Clean implementation of trading data structures
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Optional, List, Dict, Any


class TradeDirection(Enum):
    """Trade direction enum"""
    LONG = "LONG"
    SHORT = "SHORT"


class OrderState(Enum):
    """Order state machine states"""
    IDLE = auto()
    PENDING_ENTRY = auto()
    AWAITING_FILL = auto()
    PLACING_TPSL = auto()
    TRACKING = auto()


class FVGType(Enum):
    """FVG type classification"""
    BULLISH = "bullish"
    BEARISH = "bearish"
    INVERSE = "inverse"  # IFVG - when zone is violated
    ORDER_BLOCK = "order_block"  # Pivot-based Order Block


@dataclass
class Candle:
    """OHLCV candle data"""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    
    @classmethod
    def from_ws_data(cls, data: Dict[str, Any]) -> "Candle":
        """Create candle from WebSocket data"""
        return cls(
            timestamp=data.get("ts", 0),
            open=float(data.get("o", 0)),
            high=float(data.get("h", 0)),
            low=float(data.get("l", 0)),
            close=float(data.get("c", 0)),
            volume=float(data.get("b", 0)),  # base volume
        )
    
    @classmethod
    def from_api_data(cls, data: List) -> "Candle":
        """Create candle from REST API kline data [ts, o, h, l, c, vol]"""
        return cls(
            timestamp=int(data[0]),
            open=float(data[1]),
            high=float(data[2]),
            low=float(data[3]),
            close=float(data[4]),
            volume=float(data[5]) if len(data) > 5 else 0,
        )


@dataclass
class FVG:
    """Fair Value Gap data structure"""
    symbol: str
    direction: TradeDirection
    top: float              # Zone top price
    bottom: float           # Zone bottom price
    created_at: datetime
    candle_index: int       # Index of middle candle (c2)
    fvg_type: FVGType = FVGType.BULLISH
    
    # State tracking
    is_filled: bool = False
    is_violated: bool = False
    fill_percent: float = 0.0
    entry_triggered: bool = False
    is_fresh: bool = False  # True = just formed on last 3 candles (aggressive entry)
    
    # Strength scoring
    strength: float = 0.0
    gap_percent: float = 0.0
    volume_ratio: float = 1.0
    
    @property
    def range(self) -> float:
        """Gap size in price units"""
        return abs(self.top - self.bottom)
    
    @property
    def mid_price(self) -> float:
        """Middle of the gap zone"""
        return (self.top + self.bottom) / 2
    
    def update_fill_status(self, current_price: float) -> None:
        """Update fill percentage based on current price"""
        if self.direction == TradeDirection.LONG:
            # Bullish FVG: price drops into zone
            if current_price >= self.top:
                self.fill_percent = 0.0
            elif current_price <= self.bottom:
                self.fill_percent = 1.0
                self.is_violated = True
            else:
                self.fill_percent = (self.top - current_price) / self.range
                self.is_filled = True
        else:
            # Bearish FVG: price rises into zone
            if current_price <= self.bottom:
                self.fill_percent = 0.0
            elif current_price >= self.top:
                self.fill_percent = 1.0
                self.is_violated = True
            else:
                self.fill_percent = (current_price - self.bottom) / self.range
                self.is_filled = True


@dataclass
class TradeSignal:
    """Trade signal generated from FVG analysis"""
    symbol: str
    direction: TradeDirection
    entry_price: float
    fvg: FVG
    leverage: int
    position_size: float    # In base currency
    position_usd: float     # In USD
    tp_price: float
    sl_price: float
    timestamp: datetime = field(default_factory=datetime.now)
    signal_id: str = ""
    
    @property
    def risk_reward_ratio(self) -> float:
        """Calculate R:R ratio"""
        if self.direction == TradeDirection.LONG:
            risk = self.entry_price - self.sl_price
            reward = self.tp_price - self.entry_price
        else:
            risk = self.sl_price - self.entry_price
            reward = self.entry_price - self.tp_price
        return reward / risk if risk > 0 else 0


@dataclass
class Position:
    """Active position tracking"""
    position_id: str
    symbol: str
    direction: TradeDirection
    entry_price: float
    quantity: float
    leverage: int
    margin: float
    
    # TP/SL tracking
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    tpsl_placed: bool = False
    
    # State
    state: OrderState = OrderState.TRACKING
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    # PnL tracking
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    
    # Related FVG
    fvg_id: Optional[str] = None
    signal_id: Optional[str] = None


@dataclass
class SymbolState:
    """Per-symbol state tracking"""
    symbol: str
    candles: List[Candle] = field(default_factory=list)
    active_fvgs: List[FVG] = field(default_factory=list)
    active_fvg: Optional[FVG] = None          # Current FVG being traded
    current_position: Optional[Position] = None
    current_order: Optional[Any] = None        # OrderContext or dict for synced positions
    
    # Convenience flags
    has_position: bool = False
    _order_pending: bool = False  # True while REST fill confirmation is in-flight
    
    # Trailing SL state
    trailing_state: str = "initial"  # initial / breakeven / trailing
    trailing_sl_price: float = 0.0
    partial_tp_done: bool = False
    original_qty: float = 0.0
    
    # Price tracking
    last_price: float = 0.0
    last_update: Optional[datetime] = None
    
    # Cooldowns
    last_signal_time: Optional[datetime] = None
    last_entry_time: Optional[datetime] = None
    
    # Stats
    signals_generated: int = 0
    trades_executed: int = 0
    fvgs_detected: int = 0
    fvgs_filled: int = 0
    
    # State machine
    order_state: OrderState = OrderState.IDLE
    pending_signal: Optional[TradeSignal] = None


@dataclass
class BotState:
    """Global bot state"""
    symbols: Dict[str, SymbolState] = field(default_factory=dict)
    
    # Runtime state
    is_running: bool = False
    start_time: Optional[datetime] = None
    balance: float = 0.0      # Equity (wallet + unrealized PnL)
    available: float = 0.0    # Available for new trades
    
    # Global counters
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    
    # Circuit breaker
    consecutive_failures: int = 0
    circuit_open: bool = False
    circuit_open_until: Optional[datetime] = None
    
    # Session
    in_session: bool = True
    bot_started: datetime = field(default_factory=datetime.now)
    
    def get_open_positions_count(self) -> int:
        """Count total open positions across all symbols"""
        return sum(
            1 for s in self.symbols.values()
            if s.has_position
        )
