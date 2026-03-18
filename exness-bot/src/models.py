"""
Exness Bot - Data Models and Enums
Trading data structures for MT5 forex/commodities.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Optional, List, Dict, Any


class TradeDirection(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderState(Enum):
    IDLE = auto()
    PENDING_ENTRY = auto()
    AWAITING_FILL = auto()
    TRACKING = auto()


class FVGType(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    INVERSE = "inverse"


class ZoneType(Enum):
    SUPPLY = "supply"
    DEMAND = "demand"


class Timeframe(Enum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"


@dataclass
class Candle:
    """OHLCV candle data"""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    tick_volume: int = 0

    @classmethod
    def from_mt5_rate(cls, rate) -> "Candle":
        """Create candle from MT5 rate tuple (time, open, high, low, close, tick_volume, spread, real_volume)"""
        return cls(
            timestamp=int(rate[0]),
            open=float(rate[1]),
            high=float(rate[2]),
            low=float(rate[3]),
            close=float(rate[4]),
            tick_volume=int(rate[5]),
            volume=float(rate[7]) if len(rate) > 7 else float(rate[5]),
        )


@dataclass
class FVG:
    """Fair Value Gap data structure"""
    symbol: str
    direction: TradeDirection
    top: float
    bottom: float
    created_at: datetime
    candle_index: int
    fvg_type: FVGType = FVGType.BULLISH
    timeframe: str = "M1"

    # State tracking
    is_filled: bool = False
    is_violated: bool = False
    fill_percent: float = 0.0
    entry_triggered: bool = False
    is_fresh: bool = False

    # Strength scoring
    strength: float = 0.0
    gap_percent: float = 0.0
    volume_ratio: float = 1.0

    @property
    def range(self) -> float:
        return abs(self.top - self.bottom)

    @property
    def mid_price(self) -> float:
        return (self.top + self.bottom) / 2

    def update_fill_status(self, current_price: float) -> None:
        """Update fill percentage based on current price"""
        if self.range == 0:
            return
        if self.direction == TradeDirection.LONG:
            if current_price >= self.top:
                self.fill_percent = 0.0
            elif current_price <= self.bottom:
                self.fill_percent = 1.0
                self.is_violated = True
            else:
                self.fill_percent = (self.top - current_price) / self.range
                self.is_filled = True
        else:
            if current_price <= self.bottom:
                self.fill_percent = 0.0
            elif current_price >= self.top:
                self.fill_percent = 1.0
                self.is_violated = True
            else:
                self.fill_percent = (current_price - self.bottom) / self.range
                self.is_filled = True


@dataclass
class SupplyDemandZone:
    """Supply or Demand zone"""
    symbol: str
    zone_type: ZoneType
    top: float
    bottom: float
    created_at: datetime
    timeframe: str = "M15"

    # State
    strength: float = 0.0
    touch_count: int = 0
    is_fresh: bool = True
    is_broken: bool = False

    # Impulse data
    impulse_size: float = 0.0
    base_candle_count: int = 0

    @property
    def range(self) -> float:
        return abs(self.top - self.bottom)

    @property
    def mid_price(self) -> float:
        return (self.top + self.bottom) / 2

    def contains_price(self, price: float) -> bool:
        return self.bottom <= price <= self.top

    def update_touch(self, price: float) -> None:
        """Record a price touch of the zone"""
        if self.contains_price(price):
            self.touch_count += 1
            self.is_fresh = False


@dataclass
class TradeSignal:
    """Trade signal generated from analysis"""
    symbol: str
    direction: TradeDirection
    entry_price: float
    fvg: Optional[FVG] = None
    zone: Optional[SupplyDemandZone] = None
    lot_size: float = 0.01
    tp_price: float = 0.0
    sl_price: float = 0.0
    order_type: str = "MARKET"  # MARKET / BUY_STOP / SELL_STOP
    timestamp: datetime = field(default_factory=datetime.now)
    signal_id: str = ""
    confluence_score: float = 0.0

    @property
    def risk_reward_ratio(self) -> float:
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
    ticket: int
    symbol: str
    direction: TradeDirection
    entry_price: float
    lot_size: float
    magic: int = 0

    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    tpsl_placed: bool = False

    state: OrderState = OrderState.TRACKING
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    swap: float = 0.0
    commission: float = 0.0

    # Related signal data
    fvg_bottom: float = 0.0
    fvg_top: float = 0.0
    zone_bottom: float = 0.0
    zone_top: float = 0.0

    # Trailing state
    trailing_state: str = "initial"
    trailing_sl_price: float = 0.0
    best_price: float = 0.0


@dataclass
class SymbolState:
    """Per-symbol state tracking"""
    symbol: str
    candles_m1: List[Candle] = field(default_factory=list)
    candles_m5: List[Candle] = field(default_factory=list)
    candles_m15: List[Candle] = field(default_factory=list)

    active_fvgs: List[FVG] = field(default_factory=list)
    supply_zones: List[SupplyDemandZone] = field(default_factory=list)
    demand_zones: List[SupplyDemandZone] = field(default_factory=list)

    positions: List[Position] = field(default_factory=list)
    pending_orders: List[Position] = field(default_factory=list)

    last_price: float = 0.0
    last_update: Optional[datetime] = None
    last_entry_time: Optional[datetime] = None

    # Stats
    signals_generated: int = 0
    trades_executed: int = 0
    fvgs_detected: int = 0


@dataclass
class BotState:
    """Global bot state"""
    symbols: Dict[str, SymbolState] = field(default_factory=dict)

    is_running: bool = False
    start_time: Optional[datetime] = None
    balance: float = 0.0
    equity: float = 0.0
    margin_free: float = 0.0

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0

    in_session: bool = True
    bot_started: datetime = field(default_factory=datetime.now)

    def get_open_positions_count(self) -> int:
        return sum(len(s.positions) for s in self.symbols.values())
