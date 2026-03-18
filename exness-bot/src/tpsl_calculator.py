"""
Exness Bot - TP/SL Calculator
ATR-based TP/SL with zone structure anchoring.
Supports 1:1, 1:2, and dynamic R:R targets.
"""

import logging
from dataclasses import dataclass
from typing import List, Optional

from .models import Candle, FVG, SupplyDemandZone, TradeDirection, ZoneType
from .config import TPSLConfig
from . import fmt_price

logger = logging.getLogger(__name__)


@dataclass
class TPSLLevels:
    """TP/SL price levels"""
    tp_price: float
    sl_price: float
    risk_amount: float
    reward_amount: float
    atr: float
    method: str = "atr_zone"

    @property
    def risk_reward_ratio(self) -> float:
        return self.reward_amount / self.risk_amount if self.risk_amount > 0 else 0


class TPSLCalculator:
    """
    Smart TP/SL calculator using ATR + zone structure.

    SL Logic:
      LONG: SL below demand zone bottom (or FVG bottom) + ATR buffer
      SHORT: SL above supply zone top (or FVG top) + ATR buffer

    TP Logic:
      1. Default: risk × R:R ratio (1:1 to 1:3)
      2. Zone-based: opposite zone edge as target
      3. Capped by ATR constraints
    """

    def __init__(self, config: TPSLConfig):
        self.config = config

    @staticmethod
    def calculate_atr(candles: List[Candle], period: int = 14) -> float:
        """Calculate Average True Range."""
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

    def calculate(
        self,
        entry_price: float,
        direction: TradeDirection,
        candles: List[Candle],
        fvg: Optional[FVG] = None,
        zone: Optional[SupplyDemandZone] = None,
        target_rr: Optional[float] = None,
    ) -> TPSLLevels:
        """
        Calculate TP/SL levels.

        Priority for SL anchor:
        1. Supply/Demand zone edge (if available)
        2. FVG zone edge (if available)
        3. ATR-based distance from entry

        TP = SL risk × R:R ratio
        """
        # Calculate ATR
        atr = self.calculate_atr(candles, period=self.config.atr_period)
        if atr <= 0:
            atr = entry_price * self.config.atr_fallback_pct
        atr = max(atr, entry_price * self.config.atr_floor_pct)

        noise_buffer = atr * self.config.sl_buffer_atr_mult
        min_sl_dist = atr * self.config.sl_min_atr_mult
        max_sl_dist = atr * self.config.sl_max_atr_mult

        # Determine R:R target
        rr = target_rr or self.config.default_rr
        rr = max(rr, self.config.min_rr)
        rr = min(rr, self.config.max_rr)

        # --- SL Calculation ---
        if direction == TradeDirection.LONG:
            # SL anchor: zone bottom or FVG bottom
            if zone and zone.zone_type == ZoneType.DEMAND:
                sl_anchor = zone.bottom
            elif fvg:
                sl_anchor = fvg.bottom
            else:
                sl_anchor = entry_price - min_sl_dist

            structural_sl = sl_anchor - noise_buffer
            sl_distance = entry_price - structural_sl

            # Enforce min/max
            sl_distance = max(sl_distance, min_sl_dist)
            if sl_distance > max_sl_dist:
                logger.info(
                    f"SL capped: {sl_distance/entry_price*100:.3f}% -> "
                    f"{max_sl_dist/entry_price*100:.3f}%"
                )
                sl_distance = max_sl_dist

            sl_price = entry_price - sl_distance
            tp_distance = sl_distance * rr
            tp_price = entry_price + tp_distance

        else:  # SHORT
            if zone and zone.zone_type == ZoneType.SUPPLY:
                sl_anchor = zone.top
            elif fvg:
                sl_anchor = fvg.top
            else:
                sl_anchor = entry_price + min_sl_dist

            structural_sl = sl_anchor + noise_buffer
            sl_distance = structural_sl - entry_price

            sl_distance = max(sl_distance, min_sl_dist)
            if sl_distance > max_sl_dist:
                logger.info(
                    f"SL capped: {sl_distance/entry_price*100:.3f}% -> "
                    f"{max_sl_dist/entry_price*100:.3f}%"
                )
                sl_distance = max_sl_dist

            sl_price = entry_price + sl_distance
            tp_distance = sl_distance * rr
            tp_price = entry_price - tp_distance

        # TP floor: at least 1x ATR
        tp_floor = atr * self.config.tp_min_atr_mult
        if tp_distance < tp_floor:
            tp_distance = tp_floor
            if direction == TradeDirection.LONG:
                tp_price = entry_price + tp_distance
            else:
                tp_price = entry_price - tp_distance

        # Safety
        sl_price = max(sl_price, 0.0001)
        tp_price = max(tp_price, 0.0001)

        risk = sl_distance
        reward = tp_distance

        logger.info(
            f"TP/SL: entry={fmt_price(entry_price)} "
            f"SL={fmt_price(sl_price)} TP={fmt_price(tp_price)} "
            f"R:R={reward/risk:.1f} ATR={atr:.4f}"
        )

        return TPSLLevels(
            tp_price=tp_price,
            sl_price=sl_price,
            risk_amount=risk,
            reward_amount=reward,
            atr=atr,
            method="zone_atr" if zone else ("fvg_atr" if fvg else "atr_only"),
        )
