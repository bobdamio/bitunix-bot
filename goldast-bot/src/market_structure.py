"""
Market Structure Analysis Module
Tracks Swing Highs/Lows, BOS (Break of Structure), and Premium/Discount zones.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from .models import Candle

logger = logging.getLogger(__name__)


class TrendDirection(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


@dataclass
class SwingPoint:
    price: float
    timestamp: int
    is_high: bool  # True=High, False=Low
    is_broken: bool = False


class MarketStructure:
    """
    Tracks market structure (swings, trend) for a single symbol.
    Uses a standard 5-candle fractal pattern (High surrounded by 2 lower highs).
    """

    def __init__(self, symbol: str, lookback: int = 50):
        self.symbol = symbol
        self.lookback = lookback  # Candles to look back for Range (Prem/Disc)
        
        self.swing_highs: List[SwingPoint] = []
        self.swing_lows: List[SwingPoint] = []
        self.trend: TrendDirection = TrendDirection.NEUTRAL
        
        self.last_bos_price: float = 0.0
        self.last_bos_time: int = 0
        
        # Range tracking
        self.range_high: float = 0.0
        self.range_low: float = 0.0

    def update(self, candles: List[Candle]) -> None:
        """Update structure with latest candles."""
        if len(candles) < 5:
            return

        # 1. Detect new Fractals (Swing Points)
        # We look at the candle at index -3 (middle of 5)
        # Pattern: [0,1] < [2] > [3,4] for High
        c = candles
        idx = len(c) - 3
        mid = c[idx]
        
        # Swing High
        if (c[idx-2].high < mid.high and c[idx-1].high < mid.high and
            c[idx+1].high < mid.high and c[idx+2].high < mid.high):
            self._add_swing_point(mid.high, mid.timestamp, is_high=True)

        # Swing Low
        if (c[idx-2].low > mid.low and c[idx-1].low > mid.low and
            c[idx+1].low > mid.low and c[idx+2].low > mid.low):
            self._add_swing_point(mid.low, mid.timestamp, is_high=False)

        # 2. Check for Break of Structure (BOS)
        current_price = c[-1].close
        self._check_bos(current_price, c[-1].timestamp)
        
        # 3. Update Range (Premium/Discount)
        # Simple approach: High/Low of last N candles
        recent = c[-self.lookback:]
        self.range_high = max(x.high for x in recent)
        self.range_low = min(x.low for x in recent)

    def _add_swing_point(self, price: float, timestamp: int, is_high: bool):
        """Add a new swing point if it's significant (higher/lower than recent)."""
        # Simplify: just append for now, or filter for major pivots
        # For this implementation, we simply track the most recent valid fractals
        
        points = self.swing_highs if is_high else self.swing_lows
        
        # Avoid duplicates (same timestamp)
        if points and points[-1].timestamp == timestamp:
            return

        points.append(SwingPoint(price, timestamp, is_high))
        
        # Keep list manageable
        if len(points) > 20:
            points.pop(0)

    def _check_bos(self, current_price: float, timestamp: int):
        """Check if current price broke recent structure."""
        # Check Bullish BOS (Break of recent Swing High)
        # We look at the most recent UNBROKEN swing high
        valid_highs = [p for p in self.swing_highs if not p.is_broken]
        if valid_highs:
            last_high = valid_highs[-1]
            if current_price > last_high.price:
                # BOS UP
                last_high.is_broken = True
                self.trend = TrendDirection.BULLISH
                self.last_bos_price = last_high.price
                self.last_bos_time = timestamp
                logger.debug(f"🚀 BOS BULLISH on {self.symbol} @ {current_price} (broke {last_high.price})")

        # Check Bearish BOS (Break of recent Swing Low)
        valid_lows = [p for p in self.swing_lows if not p.is_broken]
        if valid_lows:
            last_low = valid_lows[-1]
            if current_price < last_low.price:
                # BOS DOWN
                last_low.is_broken = True
                self.trend = TrendDirection.BEARISH
                self.last_bos_price = last_low.price
                self.last_bos_time = timestamp
                logger.debug(f"🔻 BOS BEARISH on {self.symbol} @ {current_price} (broke {last_low.price})")

    def get_premium_discount(self, current_price: float) -> Tuple[str, float]:
        """
        Return zone status and percentile (0.0 - 1.0).
        > 0.5 = Premium (Sell)
        < 0.5 = Discount (Buy)
        """
        if self.range_high == self.range_low:
            return "NEUTRAL", 0.5
            
        range_size = self.range_high - self.range_low
        position = (current_price - self.range_low) / range_size
        position = max(0.0, min(1.0, position))  # Clamp
        
        zone = "PREMIUM" if position > 0.5 else "DISCOUNT"
        return zone, position
