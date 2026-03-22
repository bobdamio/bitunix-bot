"""
GoldasT Bot v2 - FVG (Fair Value Gap) Detector
Detects bullish and bearish FVGs with strength scoring
"""

import logging
from datetime import datetime
from typing import List, Optional, Tuple

from .models import Candle, FVG, TradeDirection, FVGType
from .config import FVGConfig, LeverageConfig
from . import fmt_price
from .tpsl_calculator import TPSLCalculator


logger = logging.getLogger(__name__)


class FVGDetector:
    """
    Detects Fair Value Gaps (FVGs) and IFVGs in candle data.
    
    FVG Detection Logic:
    - Bullish FVG (LONG): c1.high < c3.low (gap up)
    - Bearish FVG (SHORT): c1.low > c3.high (gap down)
    - IFVG: Inverse FVG entry when price violates the FVG zone (configurable threshold)
    
    Entry occurs when price fills 50-80% of the gap zone (FVG) or violates the zone (IFVG).
    """
    
    def __init__(self, config: FVGConfig, leverage_config: LeverageConfig):
        self.config = config
        self.leverage_config = leverage_config
        self.ifvg_threshold_pct = config.ifvg_threshold_pct

    def _compute_min_gap(self, candles: List[Candle], ref_price: float) -> float:
        """
        Compute effective minimum gap size in absolute price terms.
        Uses ATR × min_gap_atr_mult when enough candles are available,
        falling back to min_gap_percent × price.
        Returns whichever is larger (stricter).
        """
        pct_floor = ref_price * self.config.min_gap_percent
        atr_mult = self.config.min_gap_atr_mult
        if atr_mult > 0 and len(candles) >= 15:
            atr = TPSLCalculator.calculate_atr(candles, period=14)
            if atr > 0:
                return max(pct_floor, atr * atr_mult)
        return pct_floor
    
    def detect_fvg(
        self,
        candles: List[Candle],
        symbol: str,
    ) -> Optional[FVG]:
        """
        Detect FVG or IFVG from the last 3 candles.
        """
        if len(candles) < 3:
            return None
        c1, c2, c3 = candles[-3], candles[-2], candles[-1]
        
        # Bullish FVG: c1.high < c3.low (gap up)
        bullish_gap = c3.low - c1.high
        # Bearish FVG: c1.low > c3.high (gap down)
        bearish_gap = c1.low - c3.high

        logger.debug(
            f"{symbol} gap check: bull={bullish_gap:.2f} bear={bearish_gap:.2f} "
            f"(c1 H/L={c1.high:.2f}/{c1.low:.2f}, c3 H/L={c3.high:.2f}/{c3.low:.2f})"
        )

        min_gap_abs = self._compute_min_gap(candles, c2.close)

        if bullish_gap > 0:
            gap_percent = bullish_gap / c2.close
            if bullish_gap >= min_gap_abs:
                fvg = FVG(
                    symbol=symbol,
                    direction=TradeDirection.LONG,
                    fvg_type=FVGType.BULLISH,
                    top=c3.low,       # Zone top
                    bottom=c1.high,   # Zone bottom
                    created_at=datetime.now(),
                    candle_index=len(candles) - 2,
                    gap_percent=gap_percent,
                )
                fvg.strength = self._calculate_strength(fvg, c2, candles)
                logger.info(
                    f"📈 Bullish FVG detected: {symbol} "
                    f"zone=[{fmt_price(fvg.bottom)} - {fmt_price(fvg.top)}] "
                    f"gap={gap_percent*100:.3f}% strength={fvg.strength:.2f}"
                )
                return fvg
        
        # Bearish FVG: c1.low > c3.high (gap down)
        bearish_gap = c1.low - c3.high
        if bearish_gap > 0:
            gap_percent = bearish_gap / c2.close
            if bearish_gap >= min_gap_abs:
                fvg = FVG(
                    symbol=symbol,
                    direction=TradeDirection.SHORT,
                    fvg_type=FVGType.BEARISH,
                    top=c1.low,       # Zone top
                    bottom=c3.high,   # Zone bottom
                    created_at=datetime.now(),
                    candle_index=len(candles) - 2,
                    gap_percent=gap_percent,
                )
                fvg.strength = self._calculate_strength(fvg, c2, candles)
                logger.info(
                    f"📉 Bearish FVG detected: {symbol} "
                    f"zone=[{fmt_price(fvg.bottom)} - {fmt_price(fvg.top)}] "
                    f"gap={gap_percent*100:.3f}% strength={fvg.strength:.2f}"
                )
                return fvg
        
        return None

    def detect_fvg_sliding_window(
        self,
        candles: List[Candle],
        symbol: str,
        current_price: float,
    ) -> Optional[FVG]:
        """
        Scan ALL 3-candle windows in the buffer for non-violated FVGs.
        
        Returns the best (highest strength) FVG whose zone hasn't been
        broken by subsequent price action.
        """
        if len(candles) < 3:
            return None

        candidates: List[FVG] = []

        # Compute ATR once for the whole buffer (used for dynamic min gap)
        # ref_price: use last close as representative price for % floor
        min_gap_abs = self._compute_min_gap(candles, candles[-1].close)

        # Slide through all 3-candle windows (oldest to newest)
        for i in range(len(candles) - 2):
            c1, c2, c3 = candles[i], candles[i + 1], candles[i + 2]

            # Check bullish FVG
            bull_gap = c3.low - c1.high
            if bull_gap > 0:
                gap_pct = bull_gap / c2.close
                if bull_gap >= min_gap_abs:
                    fvg = FVG(
                        symbol=symbol,
                        direction=TradeDirection.LONG,
                        fvg_type=FVGType.BULLISH,
                        top=c3.low,
                        bottom=c1.high,
                        created_at=datetime.now(),
                        candle_index=i + 1,
                        gap_percent=gap_pct,
                    )
                    fvg.strength = self._calculate_strength(fvg, c2, candles)
                    # Check if zone was violated by any subsequent candle
                    violated = False
                    for j in range(i + 3, len(candles)):
                        if candles[j].low <= fvg.bottom:
                            violated = True
                            break
                    if not violated:
                        candidates.append(fvg)

            # Check bearish FVG
            bear_gap = c1.low - c3.high
            if bear_gap > 0:
                gap_pct = bear_gap / c2.close
                if bear_gap >= min_gap_abs:
                    fvg = FVG(
                        symbol=symbol,
                        direction=TradeDirection.SHORT,
                        fvg_type=FVGType.BEARISH,
                        top=c1.low,
                        bottom=c3.high,
                        created_at=datetime.now(),
                        candle_index=i + 1,
                        gap_percent=gap_pct,
                    )
                    fvg.strength = self._calculate_strength(fvg, c2, candles)
                    # Check if zone was violated by any subsequent candle
                    violated = False
                    for j in range(i + 3, len(candles)):
                        if candles[j].high >= fvg.top:
                            violated = True
                            break
                    if not violated:
                        candidates.append(fvg)

        if not candidates:
            return None

        # Pre-filter: exclude low-volume FVGs so the best-pick isn't stuck
        # on a close-but-unenterable FVG forever (e.g. ETHUSDT vol_ratio=0.00)
        min_vol = self.config.min_volume_ratio
        if min_vol > 0:
            good_vol = [f for f in candidates if f.volume_ratio >= min_vol]
            if good_vol:
                candidates = good_vol
            # If ALL candidates have low volume, keep them all (don't filter to zero)

        # Score by proximity to current price (60%) + strength (40%)
        # Closer zones are more likely to be touched → more trades
        # Hard filter: reject zones with mid_price > max_entry_distance from current price
        max_entry_dist = self.config.max_entry_distance
        reachable = [
            f for f in candidates
            if abs(current_price - f.mid_price) / current_price <= max_entry_dist
        ]
        if reachable:
            candidates = reachable
        # If no reachable zones, keep all candidates (better than nothing)

        def combined_score(f: FVG) -> float:
            dist = abs(current_price - f.mid_price) / current_price
            # Proximity: 0% dist → 1.0, 1% dist → 0.5, 2% → 0.33, etc.
            proximity = 1.0 / (1.0 + dist * 100)
            return proximity * 0.6 + f.strength * 0.4

        best = max(candidates, key=combined_score)
        logger.info(
            f"🔍 Sliding window found {len(candidates)} valid FVG(s) on {symbol}, "
            f"best: {best.direction.value} zone={fmt_price(best.bottom)}-{fmt_price(best.top)} "
            f"strength={best.strength:.2f} dist={abs(current_price - best.mid_price)/current_price*100:.2f}%"
        )
        return best
    
    @staticmethod
    def _impulse_ratio(candle: Candle) -> float:
        """Return body/range ratio for a candle (0.0-1.0).

        High ratio = impulse candle (large body, small wicks).
        Low ratio = indecision (doji, spinning top).
        """
        candle_range = candle.high - candle.low
        if candle_range <= 0:
            return 0.0
        body = abs(candle.close - candle.open)
        return body / candle_range

    def _calculate_strength(
        self,
        fvg: FVG,
        gap_candle: Candle,
        candles: List[Candle],
    ) -> float:
        """
        Calculate FVG strength score (0.0 - 1.0).
        
        Factors:
        - Gap size (larger = stronger)
        - Volume on gap candle (higher = stronger)
        - Trend alignment
        - Impulse candle quality (body/range ratio)
        """
        score = 0.0
        weights_total = 0.0
        
        # Factor 1: Gap size (weight: 0.3)
        # Larger gaps are more significant
        gap_score = min(fvg.gap_percent * 100, 1.0)  # Cap at 1% = max score
        score += gap_score * 0.3
        weights_total += 0.3
        
        # Factor 2: Volume ratio (weight: 0.25)
        # Compare gap candle volume to NEIGHBORING candles (not latest)
        gap_idx = fvg.candle_index if hasattr(fvg, 'candle_index') else len(candles) - 2
        # Take up to 5 candles before and after gap_candle (excluding gap itself)
        neighbor_start = max(0, gap_idx - 5)
        neighbor_end = min(len(candles), gap_idx + 6)
        neighbor_volumes = [
            candles[j].volume for j in range(neighbor_start, neighbor_end)
            if j != gap_idx and candles[j].volume > 0
        ]
        if len(neighbor_volumes) >= 3:
            avg_volume = sum(neighbor_volumes) / len(neighbor_volumes)
            volume_ratio = gap_candle.volume / avg_volume if avg_volume > 0 else 1.0
            fvg.volume_ratio = volume_ratio
            volume_score = min(volume_ratio / 2, 1.0)  # 2x avg volume = max score
            score += volume_score * 0.25
            weights_total += 0.25
        else:
            fvg.volume_ratio = 1.0  # Default to neutral when insufficient data
        
        # Factor 3: Trend alignment (weight: 0.25)
        # Check if FVG aligns with recent trend
        if len(candles) >= 20:
            trend_score = self._calculate_trend_alignment(fvg, candles)
            score += trend_score * 0.25
            weights_total += 0.25

        # Factor 4: Impulse candle quality (weight: 0.20)
        # Gap candle (c2) should be an impulse candle — large body, small wicks.
        # Body/range ≥ impulse_body_ratio → full score.
        # Dojis/spinning tops create weak, unreliable FVGs.
        imp_ratio = self._impulse_ratio(gap_candle)
        imp_threshold = self.config.impulse_body_ratio
        if imp_threshold > 0:
            # Scale: 0 at ratio=0, 1.0 at ratio≥threshold
            impulse_score = min(imp_ratio / imp_threshold, 1.0)
            score += impulse_score * 0.20
            weights_total += 0.20
        
        # Normalize to 0-1 range (round to avoid float edge cases like 0.5999 < 0.60)
        final_score = score / weights_total if weights_total > 0 else 0.5
        return round(min(max(final_score, 0.0), 1.0), 4)
    
    def _calculate_trend_alignment(
        self,
        fvg: FVG,
        candles: List[Candle],
    ) -> float:
        """Calculate trend alignment score using candles around FVG formation time,
        not the latest candles (which may be hours later for old sliding-window FVGs)."""
        if len(candles) < 20:
            return 0.5
        
        # Use candles around FVG formation, not latest candles
        gap_idx = fvg.candle_index if hasattr(fvg, 'candle_index') else len(candles) - 2
        # Take up to 20 candles ending at the gap candle
        end_idx = min(gap_idx + 1, len(candles))
        start_idx = max(0, end_idx - 20)
        context_candles = candles[start_idx:end_idx]
        
        if len(context_candles) < 10:
            # Not enough context around formation — fall back to latest
            context_candles = candles[-20:]
        
        # Simple trend: compare price at formation to 20-candle SMA
        closes = [c.close for c in context_candles]
        sma_20 = sum(closes) / len(closes)
        current_price = closes[-1]  # Price at FVG formation time
        
        # Calculate trend direction
        trend_bullish = current_price > sma_20
        
        # Check alignment
        if fvg.direction == TradeDirection.LONG and trend_bullish:
            return 1.0  # Bullish FVG in uptrend
        elif fvg.direction == TradeDirection.SHORT and not trend_bullish:
            return 1.0  # Bearish FVG in downtrend
        else:
            return 0.3  # Counter-trend FVG (still valid but weaker)
    
    def check_entry_conditions(
        self,
        fvg: FVG,
        current_price: float,
    ) -> Tuple[bool, str]:
        """
        Check if entry conditions are met for an FVG.
        
        - Fresh FVGs (just formed): enter immediately (aggressive entry)
        - Older FVGs (sliding window): wait for 30-80% fill (retest entry)
        
        Returns:
            Tuple of (should_enter, reason)
        """
        # Update fill status
        fvg.update_fill_status(current_price)
        
        # Already triggered
        if fvg.entry_triggered:
            return False, "Entry already triggered"
        
        # Volume hard filter: reject low-volume FVGs
        min_vol = self.config.min_volume_ratio
        if hasattr(fvg, 'volume_ratio') and fvg.volume_ratio < min_vol:
            return False, f"Volume too low ({fvg.volume_ratio:.1f}x < {min_vol:.1f}x avg)"
        
        # Violated (100%+ fill)
        if fvg.is_violated:
            return False, "FVG violated (zone broken)"
        
        # --- Zone-edge proximity entry (anticipation) ---
        # Enter when price is VERY CLOSE to the zone edge, approaching for a retest.
        #   LONG FVG (support zone):  price just ABOVE zone.top, dropping toward zone
        #                             → anticipate retest of bullish imbalance from above
        #   SHORT FVG (resistance zone): price just BELOW zone.bottom, rising toward zone
        #                             → anticipate retest of bearish imbalance from below
        # CRITICAL: tolerance must be very tight to avoid chasing entries outside zones.
        edge_tolerance = self.config.edge_entry_tolerance
        if fvg.fill_percent == 0.0:
            if fvg.direction == TradeDirection.LONG:
                # Bullish FVG: price just above zone.top, approaching the zone from above
                # This is the correct direction — price drops into the FVG zone for a retest
                if current_price > fvg.top:
                    dist_to_edge = (current_price - fvg.top) / fvg.top
                    if 0 < dist_to_edge <= edge_tolerance:
                        return True, f"Zone-edge anticipation LONG ({dist_to_edge*100:.3f}% above zone top, approaching)"
            else:
                # Bearish FVG: price just below zone.bottom, approaching the zone from below
                # This is the correct direction — price rises into the FVG zone for a retest
                if current_price < fvg.bottom:
                    dist_to_edge = (fvg.bottom - current_price) / fvg.bottom
                    if 0 < dist_to_edge <= edge_tolerance:
                        return True, f"Zone-edge anticipation SHORT ({dist_to_edge*100:.3f}% below zone bottom, approaching)"
        
        # Standard fill-based entry
        if fvg.fill_percent < self.config.entry_zone_min:
            return False, f"Not filled enough ({fvg.fill_percent*100:.1f}% < {self.config.entry_zone_min*100:.0f}%)"
        
        if fvg.fill_percent > self.config.entry_zone_max:
            return False, f"Too deep fill ({fvg.fill_percent*100:.1f}% > {self.config.entry_zone_max*100:.0f}%)"
        
        # Entry conditions met!
        return True, f"Optimal fill zone reached ({fvg.fill_percent*100:.1f}%)"
    
    def calculate_leverage(self, fvg: FVG) -> int:
        """
        Calculate leverage: 10x if confidence (strength) >= 60%, else 5x.
        """
        if fvg.strength >= self.leverage_config.confidence_threshold:
            return self.leverage_config.high
        return self.leverage_config.low
    
    def detect_ifvg(self, fvg: FVG) -> Optional[FVG]:
        """
        Convert a violated FVG to an Inverse FVG (IFVG).
        
        When an FVG is violated (price breaks through the zone),
        it becomes an IFVG with inverse direction.
        """
        if not fvg.is_violated:
            return None
        
        # Create inverse FVG — inherit volume_ratio from parent
        ifvg = FVG(
            symbol=fvg.symbol,
            direction=(
                TradeDirection.SHORT 
                if fvg.direction == TradeDirection.LONG 
                else TradeDirection.LONG
            ),
            top=fvg.top,
            bottom=fvg.bottom,
            created_at=datetime.now(),
            candle_index=fvg.candle_index,
            fvg_type=FVGType.INVERSE,
            gap_percent=fvg.gap_percent,
            strength=fvg.strength * 0.8,  # Slightly reduce strength for IFVG
            volume_ratio=fvg.volume_ratio,  # Inherit from parent FVG
        )
        
        logger.info(
            f"🔄 IFVG created from violated FVG: {ifvg.symbol} "
            f"direction={ifvg.direction.value}"
        )
        return ifvg

    # ==================== Order Block Detection ====================

    def detect_order_blocks(
        self,
        candles: List[Candle],
        symbol: str,
        current_price: float,
        pivot_len: int = 6,
    ) -> Optional[FVG]:
        """
        Detect pivot-based Order Blocks (from Pine Script).
        
        - Bullish OB: at a pivot low, zone = [low, high] of the pivot candle.
          Entry when price sweeps below the OB and reclaims above it.
        - Bearish OB: at a pivot high, zone = [low, high] of the pivot candle.
          Entry when price sweeps above the OB and reclaims below it.
        
        Returns an FVG-compatible object (FVGType.ORDER_BLOCK) so the existing
        entry/exit pipeline works unchanged.
        """
        # Need at least 2*pivot_len + 1 candles to confirm a pivot
        min_candles = 2 * pivot_len + 1
        if len(candles) < min_candles:
            return None

        best_ob: Optional[FVG] = None
        best_distance = float('inf')

        # Scan for pivot highs and pivot lows
        # A pivot is confirmed pivot_len bars ago (we need pivot_len bars after it)
        for i in range(pivot_len, len(candles) - pivot_len):
            candle = candles[i]

            # Check pivot LOW (bullish OB)
            is_pivot_low = True
            for j in range(i - pivot_len, i + pivot_len + 1):
                if j == i:
                    continue
                if candles[j].low < candle.low:
                    is_pivot_low = False
                    break

            if is_pivot_low:
                ob_bottom = candle.low
                ob_top = candle.high
                # Check if zone wasn't already violated by subsequent candles
                violated = False
                for j in range(i + 1, len(candles)):
                    if candles[j].close < ob_bottom:
                        violated = True
                        break
                if not violated:
                    # Check entry condition: price is near/in the OB zone
                    # (sweeps below and reclaims, or is within the zone)
                    distance = abs(current_price - (ob_bottom + ob_top) / 2) / current_price
                    if distance < best_distance and current_price <= ob_top:
                        gap_pct = (ob_top - ob_bottom) / candle.close
                        if gap_pct >= self.config.min_gap_percent:
                            # Calculate volume ratio for this candle
                            vol_ratio = 1.0
                            if len(candles) >= 10:
                                start = max(0, i - 10)
                                recent_vols = [c.volume for c in candles[start:i]]
                                if recent_vols:
                                    avg_vol = sum(recent_vols) / len(recent_vols)
                                    vol_ratio = candle.volume / avg_vol if avg_vol > 0 else 1.0

                            best_ob = FVG(
                                symbol=symbol,
                                direction=TradeDirection.LONG,
                                fvg_type=FVGType.ORDER_BLOCK,
                                top=ob_top,
                                bottom=ob_bottom,
                                created_at=datetime.now(),
                                candle_index=i,
                                gap_percent=gap_pct,
                                volume_ratio=vol_ratio,
                            )
                            best_ob.strength = self._calculate_strength(best_ob, candle, candles)
                            best_distance = distance

            # Check pivot HIGH (bearish OB)
            is_pivot_high = True
            for j in range(i - pivot_len, i + pivot_len + 1):
                if j == i:
                    continue
                if candles[j].high > candle.high:
                    is_pivot_high = False
                    break

            if is_pivot_high:
                ob_bottom = candle.low
                ob_top = candle.high
                # Check if zone wasn't violated by subsequent candles
                violated = False
                for j in range(i + 1, len(candles)):
                    if candles[j].close > ob_top:
                        violated = True
                        break
                if not violated:
                    distance = abs(current_price - (ob_bottom + ob_top) / 2) / current_price
                    if distance < best_distance and current_price >= ob_bottom:
                        gap_pct = (ob_top - ob_bottom) / candle.close
                        if gap_pct >= self.config.min_gap_percent:
                            vol_ratio = 1.0
                            if len(candles) >= 10:
                                start = max(0, i - 10)
                                recent_vols = [c.volume for c in candles[start:i]]
                                if recent_vols:
                                    avg_vol = sum(recent_vols) / len(recent_vols)
                                    vol_ratio = candle.volume / avg_vol if avg_vol > 0 else 1.0

                            best_ob = FVG(
                                symbol=symbol,
                                direction=TradeDirection.SHORT,
                                fvg_type=FVGType.ORDER_BLOCK,
                                top=ob_top,
                                bottom=ob_bottom,
                                created_at=datetime.now(),
                                candle_index=i,
                                gap_percent=gap_pct,
                                volume_ratio=vol_ratio,
                            )
                            best_ob.strength = self._calculate_strength(best_ob, candle, candles)
                            best_distance = distance

        if best_ob:
            logger.info(
                f"🧱 Order Block detected: {symbol} {best_ob.direction.value} "
                f"zone={fmt_price(best_ob.bottom)}-{fmt_price(best_ob.top)} "
                f"vol_ratio={best_ob.volume_ratio:.2f}"
            )
        return best_ob
