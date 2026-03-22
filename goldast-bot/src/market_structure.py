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
        self.last_bos_direction: TrendDirection = TrendDirection.NEUTRAL
        self._candles_since_bos: int = 9999  # Large default = no recent BOS
        self._candles_since_bos_change: int = 9999  # Candles since BOS direction changed
        self._prev_bos_direction: TrendDirection = TrendDirection.NEUTRAL  # Previous BOS direction (for stability)
        self._total_candles_seen: int = 0
        
        # Range tracking
        self.range_high: float = 0.0
        self.range_low: float = 0.0

    def warmup(self, candles: List[Candle]) -> None:
        """Warm up BOS state from historical candles.

        Replays candles progressively so that swing points and BOS events
        are detected exactly as if they had arrived one-by-one via websocket.
        """
        if len(candles) < 5:
            return
        for end in range(5, len(candles) + 1):
            self.update(candles[:end])
        logger.info(
            f"🏗️ {self.symbol} BOS warmup: {len(candles)} candles → "
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
                self.last_bos_direction = TrendDirection.BULLISH
                if self._prev_bos_direction != TrendDirection.BULLISH:
                    self._candles_since_bos_change = 0
                    self._prev_bos_direction = TrendDirection.BULLISH
                self._candles_since_bos = 0
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
                self.last_bos_direction = TrendDirection.BEARISH
                if self._prev_bos_direction != TrendDirection.BEARISH:
                    self._candles_since_bos_change = 0
                    self._prev_bos_direction = TrendDirection.BEARISH
                self._candles_since_bos = 0
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

    def is_bos_recent(self, max_age_candles: int = 20) -> bool:
        """Check if a BOS event happened within the last N candles."""
        return self._candles_since_bos <= max_age_candles

    def is_bos_stable(self, min_hold_candles: int = 3) -> bool:
        """Check if BOS direction has been stable (unchanged) for at least N candles.
        
        Prevents entering on flip-flop BOS signals that reverse within minutes.
        A BOS that just changed direction (< min_hold_candles ago) is considered unstable.
        """
        return self._candles_since_bos_change >= min_hold_candles

    def is_bos_aligned(self, direction: str, max_age_candles: int = 20) -> bool:
        """Check if recent BOS aligns with trade direction.
        
        LONG trade requires BULLISH BOS (price broke above swing high).
        SHORT trade requires BEARISH BOS (price broke below swing low).
        """
        if not self.is_bos_recent(max_age_candles):
            return False
        if direction == "LONG":
            return self.last_bos_direction == TrendDirection.BULLISH
        elif direction == "SHORT":
            return self.last_bos_direction == TrendDirection.BEARISH
        return False

    def get_bos_info(self) -> str:
        """Get formatted BOS status string for logging."""
        if self._candles_since_bos > 999:
            return "BOS:none"
        stable = "stable" if self._candles_since_bos_change >= 3 else f"unstable({self._candles_since_bos_change}c)"
        return (
            f"BOS:{self.last_bos_direction.value}"
            f"({self._candles_since_bos}c ago)"
            f"@{self.last_bos_price:.4f}"
            f"[{stable}]"
        )

    # ==================== Liquidity Sweep Detection ====================

    def check_liquidity_sweep(
        self,
        candles: List[Candle],
        direction: str,
        max_age_candles: int = 10,
    ) -> Tuple[bool, str]:
        """Check if a recent liquidity sweep occurred that supports the trade direction.

        A sweep happens when price pierces a swing point but closes back on the
        original side — indicating stop-hunt / liquidity grab followed by reversal.

        LONG entry: price swept BELOW a swing low (took sell-side liquidity) then
                    closed back above it. Institutions grabbed stops, now likely to push up.

        SHORT entry: price swept ABOVE a swing high (took buy-side liquidity) then
                     closed back below it. Institutions grabbed stops, now likely to push down.

        Args:
            candles: Recent candle data.
            direction: "LONG" or "SHORT".
            max_age_candles: Look back this many candles for sweeps.

        Returns:
            (sweep_detected, info_string)
        """
        if len(candles) < 3:
            return False, "insufficient data"

        lookback = min(max_age_candles, len(candles))

        if direction == "LONG":
            # Look for sell-side sweep: wick below swing low, close above
            for sp in reversed(self.swing_lows):
                if sp.is_broken:
                    continue
                for i in range(len(candles) - lookback, len(candles)):
                    c = candles[i]
                    # Wick pierced below swing low but candle closed above it
                    if c.low < sp.price and c.close > sp.price:
                        return True, f"sweep LOW@{sp.price:.4f} (wick={c.low:.4f} close={c.close:.4f})"
            return False, "no sell-side sweep"

        elif direction == "SHORT":
            # Look for buy-side sweep: wick above swing high, close below
            for sp in reversed(self.swing_highs):
                if sp.is_broken:
                    continue
                for i in range(len(candles) - lookback, len(candles)):
                    c = candles[i]
                    # Wick pierced above swing high but candle closed below it
                    if c.high > sp.price and c.close < sp.price:
                        return True, f"sweep HIGH@{sp.price:.4f} (wick={c.high:.4f} close={c.close:.4f})"
            return False, "no buy-side sweep"

        return False, "unknown direction"
