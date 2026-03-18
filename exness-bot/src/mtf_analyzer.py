"""
Exness Bot - Multi-Timeframe Analyzer
Analyzes 15m → 5m → 1m for supply/demand + FVG confluence.

Strategy flow:
1. 15m: Identify fresh Supply/Demand zones (directional bias)
2. 5m: Confirm zone alignment + detect FVGs within zones
3. 1m: Precise entry timing — FVG/IFVG within confirmed zone
"""

import logging
from typing import Dict, List, Optional, Tuple

from .models import Candle, FVG, SupplyDemandZone, TradeDirection, ZoneType
from .config import MTFConfig
from .fvg_detector import FVGDetector
from .supply_demand import SupplyDemandDetector
from .market_structure import MarketStructure, TrendDirection
from . import fmt_price

logger = logging.getLogger(__name__)


class MTFAnalyzer:
    """
    Multi-Timeframe Analyzer.

    Workflow per symbol:
    1. HTF (15m): Detect supply/demand zones → establish directional bias
    2. MTF (5m): Confirm zone alignment, detect FVGs within zones
    3. LTF (1m): Precise entry — find FVG/IFVG inside confirmed zone

    Entry conditions:
    - Fresh supply zone on 15m + FVG inside that zone on 5m/1m → SHORT
    - Fresh demand zone on 15m + FVG inside that zone on 5m/1m → LONG
    - SL below demand zone / above supply zone
    - TP = 1:1 to 1:2 R:R (or more if confluence is strong)
    """

    def __init__(
        self,
        config: MTFConfig,
        fvg_detector: FVGDetector,
        sd_detector: SupplyDemandDetector,
    ):
        self.config = config
        self.fvg_detector = fvg_detector
        self.sd_detector = sd_detector

        # Per-symbol market structure for each timeframe
        self._structures: Dict[str, Dict[str, MarketStructure]] = {}

    def _get_structure(self, symbol: str, timeframe: str) -> MarketStructure:
        """Get or create MarketStructure for symbol/timeframe."""
        if symbol not in self._structures:
            self._structures[symbol] = {}
        if timeframe not in self._structures[symbol]:
            self._structures[symbol][timeframe] = MarketStructure(symbol, timeframe)
        return self._structures[symbol][timeframe]

    def analyze(
        self,
        symbol: str,
        candles_m15: List[Candle],
        candles_m5: List[Candle],
        candles_m1: List[Candle],
        current_price: float,
    ) -> Optional[Dict]:
        """
        Run full multi-timeframe analysis.

        Returns dict with:
        - direction: TradeDirection
        - entry_fvg: FVG (1m or 5m entry FVG)
        - zone: SupplyDemandZone (15m zone backing the trade)
        - confluence_score: float (0-1)
        - support/resistance levels
        - order_type suggestion (MARKET / BUY_STOP / SELL_STOP)

        Returns None if no valid setup found.
        """
        if not self.config.enabled:
            return self._analyze_single_timeframe(symbol, candles_m1, current_price)

        # Step 1: HTF (15m) — Supply/Demand Zones + Market Structure
        htf_ms = self._get_structure(symbol, self.config.htf_timeframe)
        if len(candles_m15) >= 5:
            htf_ms.warmup(candles_m15)

        supply_zones_15m, demand_zones_15m = self.sd_detector.detect_zones(
            candles_m15, symbol, self.config.htf_timeframe
        )

        # Also detect 15m FVGs for reference
        fvgs_15m = self.fvg_detector.detect_fvg_sliding_window(
            candles_m15, symbol, current_price, self.config.htf_timeframe
        )

        # Step 2: MTF (5m) — Confirm zone alignment + detect FVGs
        supply_zones_5m, demand_zones_5m = self.sd_detector.detect_zones(
            candles_m5, symbol, self.config.mtf_timeframe
        )
        fvgs_5m = self.fvg_detector.detect_fvg_sliding_window(
            candles_m5, symbol, current_price, self.config.mtf_timeframe
        )

        mtf_ms = self._get_structure(symbol, self.config.mtf_timeframe)
        if len(candles_m5) >= 5:
            mtf_ms.warmup(candles_m5)

        # Step 3: LTF (1m) — Precise entry FVGs
        fvgs_1m = self.fvg_detector.detect_fvg_sliding_window(
            candles_m1, symbol, current_price, self.config.ltf_timeframe
        )

        ltf_ms = self._get_structure(symbol, self.config.ltf_timeframe)
        if len(candles_m1) >= 5:
            ltf_ms.warmup(candles_m1)

        # Diagnostic: log what was found with details
        total_fvgs = len(fvgs_1m) + len(fvgs_5m) + len(fvgs_15m)
        total_zones = len(supply_zones_15m) + len(demand_zones_15m)

        # Build FVG detail string
        fvg_details = []
        for f in (fvgs_1m + fvgs_5m + fvgs_15m)[:6]:
            fvg_details.append(f"{f.direction.value[0]}({f.timeframe}){fmt_price(f.bottom)}-{fmt_price(f.top)}")
        fvg_str = ", ".join(fvg_details) if fvg_details else "none"

        # Build zone detail string
        zone_details = []
        for z in supply_zones_15m:
            zone_details.append(f"S:{fmt_price(z.bottom)}-{fmt_price(z.top)}(str={z.strength:.2f})")
        for z in demand_zones_15m:
            zone_details.append(f"D:{fmt_price(z.bottom)}-{fmt_price(z.top)}(str={z.strength:.2f})")
        zone_str = ", ".join(zone_details) if zone_details else "none"

        if total_fvgs == 0:
            logger.info(f"{symbol} @{fmt_price(current_price)}: No FVGs | zones={zone_str}")
        elif total_zones == 0:
            logger.info(f"{symbol} @{fmt_price(current_price)}: FVGs=[{fvg_str}] | NO 15m zones found")
        else:
            logger.info(
                f"{symbol} @{fmt_price(current_price)}: FVGs=[{fvg_str}] | zones=[{zone_str}]"
            )

        # Step 4: Find best confluence setup
        # Collect ALL valid setups, then pick closest FVG to price
        valid_setups = []
        overlap_checked = 0
        overlap_found = 0
        no_overlap_details = []

        # Check SELL setups: FVG inside Supply zone
        for supply_zone in supply_zones_15m:
            if supply_zone.is_broken or supply_zone.touch_count >= 3:
                continue

            # Look for SHORT FVGs (5m or 1m) inside this supply zone
            short_fvgs = [f for f in fvgs_1m + fvgs_5m if f.direction == TradeDirection.SHORT]
            for fvg in short_fvgs:
                overlap_checked += 1

                # Check if FVG overlaps with supply zone
                overlap_zone = self.sd_detector.find_fvg_in_zone(fvg, [supply_zone])
                if overlap_zone is None:
                    no_overlap_details.append(
                        f"SHORT({fvg.timeframe}){fmt_price(fvg.bottom)}-{fmt_price(fvg.top)} "
                        f"vs S:{fmt_price(supply_zone.bottom)}-{fmt_price(supply_zone.top)}"
                    )
                    continue
                overlap_found += 1

                # Calculate confluence score
                score = self._calculate_confluence_score(
                    fvg, supply_zone, htf_ms, mtf_ms, ltf_ms,
                    fvgs_15m, supply_zones_5m, current_price
                )

                if score >= self.config.min_confluence_score:
                    support, resistance = htf_ms.get_support_resistance()
                    valid_setups.append({
                        "direction": TradeDirection.SHORT,
                        "entry_fvg": fvg,
                        "zone": supply_zone,
                        "confluence_score": score,
                        "support": support,
                        "resistance": resistance,
                        "order_type": "MARKET",
                        "htf_trend": htf_ms.trend.value,
                        "_fvg_dist": abs(current_price - fvg.mid_price),
                    })

        # Check BUY setups: FVG inside Demand zone
        for demand_zone in demand_zones_15m:
            if demand_zone.is_broken or demand_zone.touch_count >= 3:
                continue

            long_fvgs = [f for f in fvgs_1m + fvgs_5m if f.direction == TradeDirection.LONG]
            for fvg in long_fvgs:
                overlap_checked += 1

                overlap_zone = self.sd_detector.find_fvg_in_zone(fvg, [demand_zone])
                if overlap_zone is None:
                    no_overlap_details.append(
                        f"LONG({fvg.timeframe}){fmt_price(fvg.bottom)}-{fmt_price(fvg.top)} "
                        f"vs D:{fmt_price(demand_zone.bottom)}-{fmt_price(demand_zone.top)}"
                    )
                    continue
                overlap_found += 1

                score = self._calculate_confluence_score(
                    fvg, demand_zone, htf_ms, mtf_ms, ltf_ms,
                    fvgs_15m, demand_zones_5m, current_price
                )

                if score >= self.config.min_confluence_score:
                    support, resistance = htf_ms.get_support_resistance()
                    valid_setups.append({
                        "direction": TradeDirection.LONG,
                        "entry_fvg": fvg,
                        "zone": demand_zone,
                        "confluence_score": score,
                        "support": support,
                        "resistance": resistance,
                        "order_type": "MARKET",
                        "htf_trend": htf_ms.trend.value,
                        "_fvg_dist": abs(current_price - fvg.mid_price),
                    })

        # Pick best: closest FVG to price first, then highest confluence
        best_setup = None
        best_score = 0.0
        if valid_setups:
            valid_setups.sort(key=lambda s: (s["_fvg_dist"], -s["confluence_score"]))
            best_setup = valid_setups[0]
            best_score = best_setup["confluence_score"]
            if len(valid_setups) > 1:
                logger.info(
                    f"{symbol}: {len(valid_setups)} valid setups, picked closest FVG "
                    f"{fmt_price(best_setup['entry_fvg'].bottom)}-{fmt_price(best_setup['entry_fvg'].top)} "
                    f"(dist={best_setup['_fvg_dist']:.1f})"
                )
            del best_setup["_fvg_dist"]

        # Diagnostic: why no setup?
        if best_setup is None and overlap_checked > 0:
            if overlap_found == 0:
                details_str = "; ".join(no_overlap_details[:3])
                logger.info(f"{symbol}: {overlap_checked} FVG-zone pairs, none overlap: {details_str}")
            else:
                logger.info(f"{symbol}: {overlap_found} overlaps but confluence < {self.config.min_confluence_score} (best={best_score:.2f})")
        elif best_setup is None and total_zones > 0 and total_fvgs > 0:
            # Have both but no matching direction pairs
            short_fvg_count = sum(1 for f in fvgs_1m + fvgs_5m if f.direction == TradeDirection.SHORT)
            long_fvg_count = sum(1 for f in fvgs_1m + fvgs_5m if f.direction == TradeDirection.LONG)
            logger.info(
                f"{symbol}: direction mismatch - {len(supply_zones_15m)} supply zones need SHORT FVGs (have {short_fvg_count}), "
                f"{len(demand_zones_15m)} demand zones need LONG FVGs (have {long_fvg_count})"
            )

        # Step 5: Check for Buy Stop / Sell Stop opportunities
        if best_setup is None:
            pending_setup = self._check_pending_order_setup(
                symbol, current_price,
                supply_zones_15m, demand_zones_15m,
                htf_ms, ltf_ms, candles_m1,
            )
            if pending_setup:
                best_setup = pending_setup

        if best_setup:
            logger.info(
                f">> MTF Setup [{symbol}]: {best_setup['direction'].value} | "
                f"confluence={best_setup['confluence_score']:.2f} | "
                f"zone={fmt_price(best_setup['zone'].bottom)}-{fmt_price(best_setup['zone'].top)} | "
                f"order={best_setup['order_type']}"
            )

        return best_setup

    def _analyze_single_timeframe(
        self, symbol: str, candles: List[Candle], current_price: float
    ) -> Optional[Dict]:
        """Fallback: single-timeframe analysis when MTF is disabled."""
        fvgs = self.fvg_detector.detect_fvg_sliding_window(candles, symbol, current_price, "M1")
        if not fvgs:
            return None

        best_fvg = fvgs[0]
        can_enter, reason = self.fvg_detector.check_entry_conditions(best_fvg, current_price)
        if not can_enter:
            return None

        return {
            "direction": best_fvg.direction,
            "entry_fvg": best_fvg,
            "zone": None,
            "confluence_score": best_fvg.strength,
            "support": None,
            "resistance": None,
            "order_type": "MARKET",
            "htf_trend": "NEUTRAL",
        }

    def _calculate_confluence_score(
        self,
        fvg: FVG,
        zone: SupplyDemandZone,
        htf_ms: MarketStructure,
        mtf_ms: MarketStructure,
        ltf_ms: MarketStructure,
        htf_fvgs: List[FVG],
        mtf_zones: List[SupplyDemandZone],
        current_price: float,
    ) -> float:
        """
        Calculate multi-timeframe confluence score (0.0 - 1.0).

        Components:
        - HTF zone freshness + strength (weight: htf_weight)
        - MTF FVG alignment + zone confirmation (weight: mtf_weight)
        - LTF FVG entry quality (weight: ltf_weight)
        - BOS alignment bonus
        """
        score = 0.0

        # HTF component: zone strength + freshness
        htf_score = zone.strength
        if zone.is_fresh:
            htf_score += self.sd_detector.config.fresh_zone_bonus
        htf_score = min(htf_score, 1.0)
        score += htf_score * self.config.htf_weight

        # MTF component: check if 5m zone confirms 15m zone direction
        mtf_score = 0.5  # Base: neutral
        for mz in mtf_zones:
            if mz.zone_type == zone.zone_type:
                # Same zone type on MTF = confirmation
                overlap_top = min(zone.top, mz.top)
                overlap_bottom = max(zone.bottom, mz.bottom)
                if overlap_top > overlap_bottom:
                    mtf_score = 0.8
                    break
        # Also check 5m HTF FVG alignment
        for hf in htf_fvgs:
            if hf.direction == fvg.direction:
                mtf_score = min(mtf_score + 0.2, 1.0)
                break
        score += mtf_score * self.config.mtf_weight

        # LTF component: FVG entry quality
        ltf_score = fvg.strength
        # Can we enter? Check fill status
        fvg.update_fill_status(current_price)
        if self.fvg_detector.config.entry_zone_min <= fvg.fill_percent <= self.fvg_detector.config.entry_zone_max:
            ltf_score = min(ltf_score + 0.2, 1.0)
        score += ltf_score * self.config.ltf_weight

        # BOS alignment bonus
        direction_str = fvg.direction.value
        if htf_ms.is_bos_aligned(direction_str) and htf_ms.is_bos_stable():
            score = min(score + 0.10, 1.0)
        if ltf_ms.is_bos_aligned(direction_str):
            score = min(score + 0.05, 1.0)

        return round(score, 4)

    def _check_pending_order_setup(
        self,
        symbol: str,
        current_price: float,
        supply_zones: List[SupplyDemandZone],
        demand_zones: List[SupplyDemandZone],
        htf_ms: MarketStructure,
        ltf_ms: MarketStructure,
        candles_m1: List[Candle],
    ) -> Optional[Dict]:
        """
        Check for Buy Stop / Sell Stop setups.

        Buy Stop: Price is consolidating below resistance (supply zone bottom).
                 If price breaks above → buy stop triggers.

        Sell Stop: Price is consolidating above support (demand zone top).
                  If price breaks below → sell stop triggers.
        """
        if len(candles_m1) < 14:
            return None

        atr = self.fvg_detector.calculate_atr(candles_m1, period=14)
        if atr <= 0:
            return None

        support, resistance = htf_ms.get_support_resistance()

        # Check for Buy Stop — price near resistance, expecting breakout
        if resistance and current_price < resistance:
            dist_to_resistance = (resistance - current_price) / current_price
            if dist_to_resistance < 0.005:  # Within 0.5% of resistance
                # Find the supply zone at this resistance
                for sz in supply_zones:
                    if sz.is_fresh and abs(sz.bottom - resistance) / resistance < 0.002:
                        return {
                            "direction": TradeDirection.LONG,
                            "entry_fvg": None,
                            "zone": sz,
                            "confluence_score": sz.strength * 0.7,
                            "support": support,
                            "resistance": resistance,
                            "order_type": "BUY_STOP",
                            "htf_trend": htf_ms.trend.value,
                            "pending_price": resistance + atr * 0.3,
                        }

        # Check for Sell Stop — price near support, expecting breakdown
        if support and current_price > support:
            dist_to_support = (current_price - support) / current_price
            if dist_to_support < 0.005:
                for dz in demand_zones:
                    if dz.is_fresh and abs(dz.top - support) / support < 0.002:
                        return {
                            "direction": TradeDirection.SHORT,
                            "entry_fvg": None,
                            "zone": dz,
                            "confluence_score": dz.strength * 0.7,
                            "support": support,
                            "resistance": resistance,
                            "order_type": "SELL_STOP",
                            "htf_trend": htf_ms.trend.value,
                            "pending_price": support - atr * 0.3,
                        }

        return None
