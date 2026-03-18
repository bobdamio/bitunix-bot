"""
Exness Bot - FVG (Fair Value Gap) Detector
Adapted from GoldasT Bot — detects bullish/bearish FVGs with strength scoring.
Supports multi-timeframe detection (M1, M5, M15).
"""

import logging
from datetime import datetime
from typing import List, Optional, Tuple

from .models import Candle, FVG, TradeDirection, FVGType
from .config import FVGConfig
from . import fmt_price

logger = logging.getLogger(__name__)


class FVGDetector:
    """
    Detects Fair Value Gaps (FVGs) and IFVGs in candle data.

    FVG Detection Logic:
    - Bullish FVG (LONG): c1.high < c3.low (gap up between candles 1 and 3)
    - Bearish FVG (SHORT): c1.low > c3.high (gap down between candles 1 and 3)
    - IFVG: Inverse FVG entry when price violates the FVG zone

    Entry occurs when price fills into the gap zone (retest) or anticipates at zone edge.
    """

    def __init__(self, config: FVGConfig):
        self.config = config

    def _compute_min_gap(self, candles: List[Candle], ref_price: float) -> float:
        """Compute minimum gap size using ATR or percentage floor."""
        pct_floor = ref_price * self.config.min_gap_percent
        atr_mult = self.config.min_gap_atr_mult
        if atr_mult > 0 and len(candles) >= 15:
            atr = self.calculate_atr(candles, period=14)
            if atr > 0:
                return max(pct_floor, atr * atr_mult)
        return pct_floor

    @staticmethod
    def calculate_atr(candles: List[Candle], period: int = 14) -> float:
        """Calculate Average True Range."""
        if len(candles) < 2:
            return 0.0
        true_ranges = []
        for i in range(1, len(candles)):
            high = candles[i].high
            low = candles[i].low
            prev_close = candles[i - 1].close
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)
        recent = true_ranges[-period:]
        return sum(recent) / len(recent) if recent else 0.0

    def detect_fvg(self, candles: List[Candle], symbol: str, timeframe: str = "M1") -> Optional[FVG]:
        """Detect FVG from the last 3 candles."""
        if len(candles) < 3:
            return None

        c1, c2, c3 = candles[-3], candles[-2], candles[-1]
        min_gap_abs = self._compute_min_gap(candles, c2.close)

        # Bullish FVG: c1.high < c3.low (gap up)
        bullish_gap = c3.low - c1.high
        if bullish_gap > 0 and bullish_gap >= min_gap_abs:
            gap_percent = bullish_gap / c2.close
            fvg = FVG(
                symbol=symbol,
                direction=TradeDirection.LONG,
                fvg_type=FVGType.BULLISH,
                top=c3.low,
                bottom=c1.high,
                created_at=datetime.now(),
                candle_index=len(candles) - 2,
                gap_percent=gap_percent,
                timeframe=timeframe,
            )
            fvg.strength = self._calculate_strength(fvg, c2, candles)
            logger.info(
                f"Bullish FVG [{timeframe}]: {symbol} "
                f"zone=[{fmt_price(fvg.bottom)} - {fmt_price(fvg.top)}] "
                f"gap={gap_percent*100:.3f}% strength={fvg.strength:.2f}"
            )
            return fvg

        # Bearish FVG: c1.low > c3.high (gap down)
        bearish_gap = c1.low - c3.high
        if bearish_gap > 0 and bearish_gap >= min_gap_abs:
            gap_percent = bearish_gap / c2.close
            fvg = FVG(
                symbol=symbol,
                direction=TradeDirection.SHORT,
                fvg_type=FVGType.BEARISH,
                top=c1.low,
                bottom=c3.high,
                created_at=datetime.now(),
                candle_index=len(candles) - 2,
                gap_percent=gap_percent,
                timeframe=timeframe,
            )
            fvg.strength = self._calculate_strength(fvg, c2, candles)
            logger.info(
                f"Bearish FVG [{timeframe}]: {symbol} "
                f"zone=[{fmt_price(fvg.bottom)} - {fmt_price(fvg.top)}] "
                f"gap={gap_percent*100:.3f}% strength={fvg.strength:.2f}"
            )
            return fvg

        return None

    def detect_fvg_sliding_window(
        self, candles: List[Candle], symbol: str, current_price: float, timeframe: str = "M1"
    ) -> List[FVG]:
        """
        Scan ALL 3-candle windows for non-violated FVGs.
        Returns list of valid FVGs sorted by combined score (proximity + strength).
        """
        if len(candles) < 3:
            return []

        candidates: List[FVG] = []
        min_gap_abs = self._compute_min_gap(candles, candles[-1].close)

        for i in range(len(candles) - 2):
            c1, c2, c3 = candles[i], candles[i + 1], candles[i + 2]

            # Bullish FVG
            bull_gap = c3.low - c1.high
            if bull_gap > 0 and bull_gap >= min_gap_abs:
                gap_pct = bull_gap / c2.close
                fvg = FVG(
                    symbol=symbol,
                    direction=TradeDirection.LONG,
                    fvg_type=FVGType.BULLISH,
                    top=c3.low,
                    bottom=c1.high,
                    created_at=datetime.now(),
                    candle_index=i + 1,
                    gap_percent=gap_pct,
                    timeframe=timeframe,
                )
                fvg.strength = self._calculate_strength(fvg, c2, candles)
                # Verify zone not violated by subsequent candles
                violated = any(candles[j].low <= fvg.bottom for j in range(i + 3, len(candles)))
                if not violated:
                    candidates.append(fvg)

            # Bearish FVG
            bear_gap = c1.low - c3.high
            if bear_gap > 0 and bear_gap >= min_gap_abs:
                gap_pct = bear_gap / c2.close
                fvg = FVG(
                    symbol=symbol,
                    direction=TradeDirection.SHORT,
                    fvg_type=FVGType.BEARISH,
                    top=c1.low,
                    bottom=c3.high,
                    created_at=datetime.now(),
                    candle_index=i + 1,
                    gap_percent=gap_pct,
                    timeframe=timeframe,
                )
                fvg.strength = self._calculate_strength(fvg, c2, candles)
                violated = any(candles[j].high >= fvg.top for j in range(i + 3, len(candles)))
                if not violated:
                    candidates.append(fvg)

        if not candidates:
            return []

        # Filter by proximity
        max_entry_dist = self.config.max_entry_distance
        reachable = [
            f for f in candidates
            if abs(current_price - f.mid_price) / current_price <= max_entry_dist
        ]
        if reachable:
            candidates = reachable

        # Sort by combined score (proximity 60% + strength 40%)
        def combined_score(f: FVG) -> float:
            dist = abs(current_price - f.mid_price) / current_price
            proximity = 1.0 / (1.0 + dist * 100)
            return proximity * 0.6 + f.strength * 0.4

        candidates.sort(key=combined_score, reverse=True)

        # Limit to max_active_fvgs
        return candidates[:self.config.max_active_fvgs]

    def check_entry_conditions(self, fvg: FVG, current_price: float) -> Tuple[bool, str]:
        """
        Check if entry conditions are met for an FVG.
        Returns (should_enter, reason).
        """
        fvg.update_fill_status(current_price)

        if fvg.entry_triggered:
            return False, "Entry already triggered"

        if fvg.is_violated:
            return False, "FVG violated (zone broken)"

        # Zone-edge proximity entry (anticipation)
        # LONG: price just dipped below FVG bottom = anticipating bounce up
        # SHORT: price just rose above FVG top = anticipating drop down
        edge_tolerance = self.config.edge_entry_tolerance
        if fvg.fill_percent == 0.0:
            if fvg.direction == TradeDirection.LONG:
                if current_price < fvg.bottom:
                    dist_to_edge = (fvg.bottom - current_price) / fvg.bottom
                    if 0 < dist_to_edge <= edge_tolerance:
                        return True, f"Zone-edge anticipation LONG ({dist_to_edge*100:.3f}%)"
            else:
                if current_price > fvg.top:
                    dist_to_edge = (current_price - fvg.top) / fvg.top
                    if 0 < dist_to_edge <= edge_tolerance:
                        return True, f"Zone-edge anticipation SHORT ({dist_to_edge*100:.3f}%)"

        # Standard fill zone entry
        if self.config.entry_zone_min <= fvg.fill_percent <= self.config.entry_zone_max:
            return True, f"Zone fill entry ({fvg.fill_percent*100:.1f}%)"

        # IFVG — inverse entry on full zone violation
        if fvg.fill_percent >= (1.0 - self.config.ifvg_threshold_pct / 100):
            if not fvg.is_violated:
                return True, f"IFVG entry ({fvg.fill_percent*100:.1f}% fill)"

        return False, f"Price outside entry zone (fill={fvg.fill_percent*100:.1f}%)"

    # ==================== Strength Calculation ====================

    @staticmethod
    def _impulse_ratio(candle: Candle) -> float:
        """Body/range ratio (0.0-1.0). High = impulse, Low = indecision."""
        candle_range = candle.high - candle.low
        if candle_range <= 0:
            return 0.0
        return abs(candle.close - candle.open) / candle_range

    def _calculate_strength(self, fvg: FVG, gap_candle: Candle, candles: List[Candle]) -> float:
        """
        Calculate FVG strength score (0.0 - 1.0).
        Factors: gap size, volume, trend alignment, impulse quality.
        """
        score = 0.0
        weights_total = 0.0

        # Factor 1: Gap size (weight: 0.30)
        gap_score = min(fvg.gap_percent * 100, 1.0)
        score += gap_score * 0.30
        weights_total += 0.30

        # Factor 2: Volume ratio (weight: 0.25)
        gap_idx = fvg.candle_index
        neighbor_start = max(0, gap_idx - 5)
        neighbor_end = min(len(candles), gap_idx + 6)
        neighbor_volumes = [
            candles[j].volume for j in range(neighbor_start, neighbor_end)
            if j != gap_idx and candles[j].volume > 0
        ]
        if len(neighbor_volumes) >= 3:
            avg_vol = sum(neighbor_volumes) / len(neighbor_volumes)
            vol_ratio = gap_candle.volume / avg_vol if avg_vol > 0 else 1.0
            fvg.volume_ratio = vol_ratio
            volume_score = min(vol_ratio / 2, 1.0)
            score += volume_score * 0.25
            weights_total += 0.25
        else:
            fvg.volume_ratio = 1.0

        # Factor 3: Trend alignment (weight: 0.25)
        if len(candles) >= 20:
            trend_score = self._calculate_trend_alignment(fvg, candles)
            score += trend_score * 0.25
            weights_total += 0.25

        # Factor 4: Impulse candle quality (weight: 0.20)
        imp_ratio = self._impulse_ratio(gap_candle)
        imp_threshold = self.config.impulse_body_ratio
        if imp_threshold > 0:
            impulse_score = min(imp_ratio / imp_threshold, 1.0)
            score += impulse_score * 0.20
            weights_total += 0.20

        final_score = score / weights_total if weights_total > 0 else 0.5
        return round(min(max(final_score, 0.0), 1.0), 4)

    def _calculate_trend_alignment(self, fvg: FVG, candles: List[Candle]) -> float:
        """Trend alignment using SMA around FVG formation."""
        gap_idx = fvg.candle_index
        end_idx = min(gap_idx + 1, len(candles))
        start_idx = max(0, end_idx - 20)
        context = candles[start_idx:end_idx]

        if len(context) < 10:
            context = candles[-20:]

        closes = [c.close for c in context]
        sma = sum(closes) / len(closes)
        price = closes[-1]

        trend_bullish = price > sma
        if fvg.direction == TradeDirection.LONG and trend_bullish:
            return 1.0
        elif fvg.direction == TradeDirection.SHORT and not trend_bullish:
            return 1.0
        return 0.3
