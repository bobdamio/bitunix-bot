"""
Exness Bot - Market Structure Analysis
Tracks Swing Highs/Lows, Break of Structure (BOS), and trend direction.
Adapted from GoldasT Bot for forex/commodities.
"""

import logging
from dataclasses import dataclass
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
    is_high: bool
    is_broken: bool = False


class MarketStructure:
    """
    Tracks market structure (swings, trend) for a single symbol/timeframe.
    Uses 5-candle fractal pattern for swing detection.
    """

    def __init__(self, symbol: str, timeframe: str = "M15", lookback: int = 50):
        self.symbol = symbol
        self.timeframe = timeframe
        self.lookback = lookback

        self.swing_highs: List[SwingPoint] = []
        self.swing_lows: List[SwingPoint] = []
        self.trend: TrendDirection = TrendDirection.NEUTRAL

        self.last_bos_price: float = 0.0
        self.last_bos_time: int = 0
        self.last_bos_direction: TrendDirection = TrendDirection.NEUTRAL
        self._candles_since_bos: int = 9999
        self._candles_since_bos_change: int = 9999
        self._prev_bos_direction: TrendDirection = TrendDirection.NEUTRAL
        self._total_candles_seen: int = 0

        self.range_high: float = 0.0
        self.range_low: float = 0.0

    def warmup(self, candles: List[Candle]) -> None:
        """Warm up BOS state from historical candles."""
        if len(candles) < 5:
            return
        for end in range(5, len(candles) + 1):
            self.update(candles[:end])
        logger.info(
            f"{self.symbol} [{self.timeframe}] BOS warmup: {len(candles)} candles -> "
            f"highs={len(self.swing_highs)} lows={len(self.swing_lows)} "
            f"{self.get_bos_info()}"
        )

    def update(self, candles: List[Candle]) -> None:
        """Update structure with latest candles."""
        if len(candles) < 5:
            return

        self._total_candles_seen += 1
        self._candles_since_bos += 1
        self._candles_since_bos_change += 1

        c = candles
        idx = len(c) - 3
        mid = c[idx]

        # Swing High: middle candle high > surrounding 2 on each side
        if (c[idx-2].high < mid.high and c[idx-1].high < mid.high and
            c[idx+1].high < mid.high and c[idx+2].high < mid.high):
            self._add_swing_point(mid.high, mid.timestamp, is_high=True)

        # Swing Low: middle candle low < surrounding 2 on each side
        if (c[idx-2].low > mid.low and c[idx-1].low > mid.low and
            c[idx+1].low > mid.low and c[idx+2].low > mid.low):
            self._add_swing_point(mid.low, mid.timestamp, is_high=False)

        # Check BOS
        current_price = c[-1].close
        self._check_bos(current_price, c[-1].timestamp)

        # Update range
        recent = c[-self.lookback:]
        self.range_high = max(x.high for x in recent)
        self.range_low = min(x.low for x in recent)

    def _add_swing_point(self, price: float, timestamp: int, is_high: bool) -> None:
        points = self.swing_highs if is_high else self.swing_lows
        if points and points[-1].timestamp == timestamp:
            return
        points.append(SwingPoint(price, timestamp, is_high))
        if len(points) > 20:
            points.pop(0)

    def _check_bos(self, current_price: float, timestamp: int) -> None:
        """Check if current price broke recent structure."""
        # Bullish BOS
        valid_highs = [p for p in self.swing_highs if not p.is_broken]
        if valid_highs:
            last_high = valid_highs[-1]
            if current_price > last_high.price:
                last_high.is_broken = True
                self.trend = TrendDirection.BULLISH
                self.last_bos_price = last_high.price
                self.last_bos_time = timestamp
                self.last_bos_direction = TrendDirection.BULLISH
                if self._prev_bos_direction != TrendDirection.BULLISH:
                    self._candles_since_bos_change = 0
                    self._prev_bos_direction = TrendDirection.BULLISH
                self._candles_since_bos = 0

        # Bearish BOS
        valid_lows = [p for p in self.swing_lows if not p.is_broken]
        if valid_lows:
            last_low = valid_lows[-1]
            if current_price < last_low.price:
                last_low.is_broken = True
                self.trend = TrendDirection.BEARISH
                self.last_bos_price = last_low.price
                self.last_bos_time = timestamp
                self.last_bos_direction = TrendDirection.BEARISH
                if self._prev_bos_direction != TrendDirection.BEARISH:
                    self._candles_since_bos_change = 0
                    self._prev_bos_direction = TrendDirection.BEARISH
                self._candles_since_bos = 0

    def get_premium_discount(self, current_price: float) -> Tuple[str, float]:
        """Return zone status and percentile (0.0-1.0)."""
        if self.range_high == self.range_low:
            return "NEUTRAL", 0.5
        range_size = self.range_high - self.range_low
        position = (current_price - self.range_low) / range_size
        position = max(0.0, min(1.0, position))
        zone = "PREMIUM" if position > 0.5 else "DISCOUNT"
        return zone, position

    def is_bos_recent(self, max_age_candles: int = 20) -> bool:
        return self._candles_since_bos <= max_age_candles

    def is_bos_stable(self, min_hold_candles: int = 3) -> bool:
        return self._candles_since_bos_change >= min_hold_candles

    def is_bos_aligned(self, direction: str, max_age_candles: int = 20) -> bool:
        """Check if BOS aligns with trade direction."""
        if not self.is_bos_recent(max_age_candles):
            return False
        if direction == "LONG":
            return self.last_bos_direction == TrendDirection.BULLISH
        elif direction == "SHORT":
            return self.last_bos_direction == TrendDirection.BEARISH
        return False

    def get_support_resistance(self) -> Tuple[Optional[float], Optional[float]]:
        """Get nearest unbroken support and resistance levels."""
        support = None
        resistance = None

        valid_lows = [p for p in self.swing_lows if not p.is_broken]
        if valid_lows:
            support = valid_lows[-1].price

        valid_highs = [p for p in self.swing_highs if not p.is_broken]
        if valid_highs:
            resistance = valid_highs[-1].price

        return support, resistance

    def get_bos_info(self) -> str:
        return (
            f"BOS={self.last_bos_direction.value} "
            f"age={self._candles_since_bos} trend={self.trend.value}"
        )
