"""
Exness Bot - Supply/Demand Zone Detector
Detects supply and demand zones from price action.

Supply Zone (sell before pump): Area where strong selling occurred
  - Price consolidated (base), then dropped sharply (impulse down)
  - Zone = the consolidation area before the drop
  - When price returns to this zone → expect selling reaction

Demand Zone (buy before dump): Area where strong buying occurred
  - Price consolidated (base), then rallied sharply (impulse up)
  - Zone = the consolidation area before the rally
  - When price returns to this zone → expect buying reaction

Multi-timeframe: Zones found on 15m carry more weight, refined on 5m and 1m.
"""

import logging
from datetime import datetime
from typing import List, Optional, Tuple

from .models import Candle, SupplyDemandZone, ZoneType
from .config import SupplyDemandConfig
from . import fmt_price

logger = logging.getLogger(__name__)


class SupplyDemandDetector:
    """
    Detects Supply and Demand zones from candlestick data.

    Detection algorithm:
    1. Find impulse moves (candle body > ATR * min_impulse_atr_mult)
    2. Look backwards for consolidation base (2-5 small-body candles)
    3. The base area forms the zone (zone top/bottom from base high/low)
    4. Score zone strength by impulse size, base quality, and freshness

    Supply zone: Base followed by impulse DOWN
    Demand zone: Base followed by impulse UP
    """

    def __init__(self, config: SupplyDemandConfig):
        self.config = config

    def detect_zones(
        self,
        candles: List[Candle],
        symbol: str,
        timeframe: str = "M15",
    ) -> Tuple[List[SupplyDemandZone], List[SupplyDemandZone]]:
        """
        Detect supply and demand zones from candle data.

        Returns:
            (supply_zones, demand_zones) — sorted by strength descending.
        """
        if len(candles) < 10:
            return [], []

        atr = self._calculate_atr(candles)
        if atr <= 0:
            return [], []

        supply_zones: List[SupplyDemandZone] = []
        demand_zones: List[SupplyDemandZone] = []

        min_impulse = atr * self.config.min_impulse_atr_mult

        # Scan for impulse candles
        for i in range(self.config.max_base_candles + 1, len(candles)):
            candle = candles[i]
            body = abs(candle.close - candle.open)

            if body < min_impulse:
                continue

            # Determine impulse direction
            is_bullish_impulse = candle.close > candle.open
            is_bearish_impulse = candle.close < candle.open

            if not is_bullish_impulse and not is_bearish_impulse:
                continue

            # Look backwards for consolidation base
            base = self._find_base(candles, i, atr)
            if base is None:
                continue

            base_start, base_end, base_high, base_low = base

            if is_bearish_impulse:
                # Bearish impulse after base → SUPPLY zone
                # Price consolidated then dropped → sellers are here
                zone = SupplyDemandZone(
                    symbol=symbol,
                    zone_type=ZoneType.SUPPLY,
                    top=base_high,
                    bottom=base_low,
                    created_at=datetime.now(),
                    timeframe=timeframe,
                    impulse_size=body,
                    base_candle_count=base_end - base_start + 1,
                )
                zone.strength = self._calculate_zone_strength(zone, candle, atr, candles, i)
                # Check if zone is still valid (not broken by subsequent price action)
                if not self._is_zone_broken(zone, candles, i + 1):
                    # Update touch count
                    self._count_touches(zone, candles, i + 1)
                    if zone.strength >= self.config.min_zone_strength:
                        supply_zones.append(zone)

            elif is_bullish_impulse:
                # Bullish impulse after base → DEMAND zone
                # Price consolidated then rallied → buyers are here
                zone = SupplyDemandZone(
                    symbol=symbol,
                    zone_type=ZoneType.DEMAND,
                    top=base_high,
                    bottom=base_low,
                    created_at=datetime.now(),
                    timeframe=timeframe,
                    impulse_size=body,
                    base_candle_count=base_end - base_start + 1,
                )
                zone.strength = self._calculate_zone_strength(zone, candle, atr, candles, i)
                if not self._is_zone_broken(zone, candles, i + 1):
                    self._count_touches(zone, candles, i + 1)
                    if zone.strength >= self.config.min_zone_strength:
                        demand_zones.append(zone)

        # Sort by strength, limit count
        supply_zones.sort(key=lambda z: z.strength, reverse=True)
        demand_zones.sort(key=lambda z: z.strength, reverse=True)

        supply_zones = supply_zones[:self.config.max_zones_per_side]
        demand_zones = demand_zones[:self.config.max_zones_per_side]

        if supply_zones:
            logger.debug(
                f"Supply zones [{timeframe}] {symbol}: {len(supply_zones)} found | "
                f"Best: {fmt_price(supply_zones[0].bottom)}-{fmt_price(supply_zones[0].top)} "
                f"strength={supply_zones[0].strength:.2f}"
            )
        if demand_zones:
            logger.debug(
                f"Demand zones [{timeframe}] {symbol}: {len(demand_zones)} found | "
                f"Best: {fmt_price(demand_zones[0].bottom)}-{fmt_price(demand_zones[0].top)} "
                f"strength={demand_zones[0].strength:.2f}"
            )

        return supply_zones, demand_zones

    def is_price_in_zone(self, price: float, zone: SupplyDemandZone) -> bool:
        """Check if price is within a supply/demand zone."""
        return zone.bottom <= price <= zone.top

    def is_price_near_zone(self, price: float, zone: SupplyDemandZone, tolerance_pct: float = 0.001) -> bool:
        """Check if price is near (approaching) a zone."""
        buffer = zone.range * tolerance_pct * 10  # tolerance based on zone range
        return (zone.bottom - buffer) <= price <= (zone.top + buffer)

    def find_fvg_in_zone(self, fvg, zones: List[SupplyDemandZone]) -> Optional[SupplyDemandZone]:
        """
        Check if an FVG overlaps with or is adjacent to any supply/demand zone.
        Uses a proximity buffer so near-touches count as overlap.

        Returns the overlapping zone or None.
        """
        for zone in zones:
            if zone.is_broken:
                continue
            if zone.touch_count >= self.config.zone_touch_invalidation:
                continue

            # Proximity buffer: 20% of zone range (allows near-touching FVGs)
            proximity = zone.range * 0.20

            # Expand zone slightly for overlap check
            expanded_bottom = zone.bottom - proximity
            expanded_top = zone.top + proximity

            overlap_top = min(fvg.top, expanded_top)
            overlap_bottom = max(fvg.bottom, expanded_bottom)

            if overlap_top >= overlap_bottom:
                # There is overlap (>= to catch exact touching)
                overlap_size = overlap_top - overlap_bottom
                fvg_size = fvg.range
                overlap_pct = overlap_size / fvg_size if fvg_size > 0 else 0

                # Require at least 20% overlap (lowered to accommodate proximity)
                if overlap_pct >= 0.20:
                    return zone

        return None

    # ==================== Internal Methods ====================

    def _find_base(
        self,
        candles: List[Candle],
        impulse_idx: int,
        atr: float,
    ) -> Optional[Tuple[int, int, float, float]]:
        """
        Find consolidation base before an impulse candle.

        Base = 2-5 candles with small bodies (< 0.5 * ATR) before the impulse.
        Returns (start_idx, end_idx, base_high, base_low) or None.
        """
        max_base = self.config.max_base_candles
        min_base = self.config.min_base_candles
        small_body_threshold = atr * 0.5

        base_end = impulse_idx - 1
        base_start = base_end

        if base_end < 0:
            return None

        base_high = candles[base_end].high
        base_low = candles[base_end].low
        base_count = 0

        for j in range(base_end, max(base_end - max_base, -1), -1):
            c = candles[j]
            body = abs(c.close - c.open)

            if body > small_body_threshold:
                break

            base_start = j
            base_high = max(base_high, c.high)
            base_low = min(base_low, c.low)
            base_count += 1

        if base_count < min_base:
            return None

        return base_start, base_end, base_high, base_low

    def _is_zone_broken(self, zone: SupplyDemandZone, candles: List[Candle], start_idx: int) -> bool:
        """Check if price has broken through the zone (invalidating it)."""
        for i in range(start_idx, len(candles)):
            c = candles[i]
            if zone.zone_type == ZoneType.SUPPLY:
                # Supply broken if price closes above zone top
                if c.close > zone.top:
                    zone.is_broken = True
                    return True
            else:
                # Demand broken if price closes below zone bottom
                if c.close < zone.bottom:
                    zone.is_broken = True
                    return True
        return False

    def _count_touches(self, zone: SupplyDemandZone, candles: List[Candle], start_idx: int) -> None:
        """Count how many times price has tested the zone."""
        was_outside = True
        for i in range(start_idx, len(candles)):
            c = candles[i]
            in_zone = zone.bottom <= c.close <= zone.top
            if in_zone and was_outside:
                zone.touch_count += 1
                was_outside = False
            elif not in_zone:
                was_outside = True

        if zone.touch_count > 0:
            zone.is_fresh = False

    def _calculate_zone_strength(
        self,
        zone: SupplyDemandZone,
        impulse_candle: Candle,
        atr: float,
        candles: List[Candle],
        impulse_idx: int,
    ) -> float:
        """
        Calculate zone strength (0.0 - 1.0).

        Factors:
        - Impulse size relative to ATR (larger = stronger)
        - Base quality (tight consolidation = stronger)
        - Volume on impulse candle
        - Freshness (untouched zones are stronger)
        """
        score = 0.0

        # Factor 1: Impulse size (weight: 0.35)
        impulse_ratio = zone.impulse_size / atr if atr > 0 else 1.0
        impulse_score = min(impulse_ratio / 3.0, 1.0)  # 3x ATR = max score
        score += impulse_score * 0.35

        # Factor 2: Base tightness (weight: 0.25)
        # Tighter base (smaller range vs ATR) = stronger zone
        zone_range_ratio = zone.range / atr if atr > 0 else 1.0
        # Ideal base is tight (< 1 ATR). Wider bases are weaker.
        base_score = max(0, 1.0 - zone_range_ratio / 2.0)
        score += base_score * 0.25

        # Factor 3: Volume on impulse (weight: 0.20)
        # Compare to nearby candles
        start = max(0, impulse_idx - 5)
        end = min(len(candles), impulse_idx + 1)
        nearby_volumes = [candles[j].volume for j in range(start, end) if j != impulse_idx and candles[j].volume > 0]
        if nearby_volumes:
            avg_vol = sum(nearby_volumes) / len(nearby_volumes)
            vol_ratio = impulse_candle.volume / avg_vol if avg_vol > 0 else 1.0
            vol_score = min(vol_ratio / 2.0, 1.0)
            score += vol_score * 0.20
        else:
            score += 0.5 * 0.20  # Neutral when no volume data

        # Factor 4: Freshness bonus (weight: 0.20)
        fresh_score = 1.0 if zone.touch_count == 0 else max(0, 1.0 - zone.touch_count * 0.3)
        score += fresh_score * 0.20

        return round(min(max(score, 0.0), 1.0), 4)

    @staticmethod
    def _calculate_atr(candles: List[Candle], period: int = 14) -> float:
        """Calculate ATR."""
        if len(candles) < 2:
            return 0.0
        true_ranges = []
        for i in range(1, len(candles)):
            tr = max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
            true_ranges.append(tr)
        recent = true_ranges[-period:]
        return sum(recent) / len(recent) if recent else 0.0
